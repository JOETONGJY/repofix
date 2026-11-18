"""Task Interface CLI：`repofix run`（design/01、specs/03 退出码与输出协议）。

职责边界：CLI 只做输入校验、任务启动与结果展示——不参与模型决策、工具分发、
Context 构造与状态转换（G2 控制权集中）。

退出码（specs/03）：0 完成且可见验证通过；1 完成但验证不充分；
2 输入/仓库/配置校验失败；3 Runtime/Workspace 不可恢复错误；
4 预算耗尽；130 用户中断。
"""
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer

from repofix.contracts import (
    RunConfig,
    RunStatus,
    TaskSpec,
    VerificationStatus,
)
from repofix.gateway.adapter import DeepSeekAdapter, resolve_context_window
from repofix.runtime.loop_guard import SystemClock
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.test_agent.runner import TestAgentRunner, should_trigger
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.verifier.workspace_verifier import WorkspaceVerifier
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.errors import DirtyWorkspaceError, WorkspaceError
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import DEFAULT_IMAGE, PreflightRunner

app = typer.Typer(add_completion=False, help="RepoFix: Repository-Aware Coding Agent Harness")


@app.callback()
def _root() -> None:
    """RepoFix: Repository-Aware Coding Agent Harness."""


DEFAULT_MODEL = "deepseek-flash"


@dataclass
class StackExtras:
    """_build_stack 组装期产出的新功能附加对象（MVP v0.4）。

    与 Preflight 解耦：Overview 文本与 TestAgentRunner 由 CLI/Server 在
    主循环前后使用（artifact 导出 / Fail-to-Pass 补充验证 / 清理）。
    """

    repo_overview_text: str | None = None
    repo_overview_meta: dict = field(default_factory=dict)
    test_agent: TestAgentRunner | None = None


def _generate_repo_overview(task_dir: Path,
                            instruction: str = "") -> tuple[str | None, dict]:
    """生成 Repository Overview；失败记录原因并回退（None = 不注入）。

    instruction 传入 Issue 文本：Candidate files 按 Issue 关键词做路径与
    文件名简单匹配（P2，评审：任务上下文必须真正参与候选排序）。
    """
    from repofix.context.repo_overview import build_repo_overview, overview_fingerprint

    try:
        text = build_repo_overview(Path(task_dir), issue_text=instruction or "")
    except Exception as exc:  # noqa: BLE001 —— 概览生成失败不阻塞主流程
        return None, {"enabled": True, "ok": False, "chars": 0,
                      "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return text, {
        "enabled": True,
        "ok": True,
        "chars": len(text),
        "sha256": overview_fingerprint(text),
    }


class InputValidationError(Exception):
    """输入/仓库/配置校验失败 → 退出码 2。"""


class PreflightFailure(Exception):
    """Preflight 失败 → 退出码 3（fail_layer=WORKSPACE）。"""

    def __init__(self, step: str, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"preflight failed at {step}: {reason}")


def _read_task(task: Optional[str], task_file: Optional[Path]) -> str:
    if (task is None) == (task_file is None):
        raise InputValidationError("provide exactly one of --task / --task-file")
    if task is not None:
        if not task.strip():
            raise InputValidationError("task text is empty")
        return task
    try:
        content = Path(task_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise InputValidationError(f"cannot read task file: {exc}")
    if not content.strip():
        raise InputValidationError("task file is empty")
    return content


def _validate_repo(repo: Path, base_ref: str) -> str:
    if not repo.exists():
        raise InputValidationError(f"repo path does not exist: {repo}")
    dirty = None
    from repofix.workspace.manager import check_dirty, resolve_base_commit

    try:
        dirty = check_dirty(repo)
    except WorkspaceError as exc:
        raise InputValidationError(f"not a usable git repository: {exc}")
    if dirty:
        raise DirtyWorkspaceError(dirty)
    try:
        return resolve_base_commit(repo, base_ref)
    except WorkspaceError as exc:
        raise InputValidationError(str(exc))


def _build_stack(task_spec: TaskSpec, config: RunConfig, run_id: str):
    """组装完整执行栈（测试经 monkeypatch 本函数注入 Scripted/Fake 组件）。

    MVP v0.4：Preflight 通过后依次执行 Repository Overview 生成（可选）与
    Test Agent 生成+基线复现（可选、仅在无可用测试时触发）；两者失败均
    安全降级，不阻塞主流程。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise InputValidationError("DEEPSEEK_API_KEY is not set; cannot start a model-backed run")
    manager = WorkspaceManager(Path(config.output_dir) / "workspaces")
    handle = manager.prepare(run_id, Path(task_spec.repo_path), task_spec.base_commit)
    cli = DockerCli()
    workspace = DockerWorkspace(
        handle.task_dir, run_id, base_commit=handle.base_commit, cli=cli,
        command_timeout_seconds=config.command_timeout_seconds,
        command_policy=config.command_policy,
    )
    preflight = PreflightRunner(cli=cli)
    report = preflight.run(workspace, task_spec.test_command)
    if not report.ok:
        workspace.close()
        manager.cleanup_workspace(handle)
        raise PreflightFailure(report.failure_step or "unknown", report.failure_reason)

    store = SQLiteEventStore(Path(config.output_dir) / "runs" / run_id / "run.sqlite")
    extras = StackExtras()

    # ---- MVP v0.4 功能 A：Repository Overview（可选；失败回退到无概览）----
    if config.repo_overview_enabled:
        extras.repo_overview_text, extras.repo_overview_meta = _generate_repo_overview(
            workspace.task_dir, instruction=task_spec.instruction)

    # ---- MVP v0.4 功能 B：Test Agent 阶段 1（生成 + 基线复现；独立测试工作区）----
    # v1.1 统一台账：Reviewer/Test Agent 的用量经此 sink 汇入 max_total_tokens 检查
    run_usage_sink: dict = {"reviewer_tokens": 0, "test_agent_tokens": 0}
    test_agent_note = None
    test_agent = None
    if config.test_agent_enabled:
        if should_trigger(config, report)[0]:
            test_agent = TestAgentRunner(
                run_id=run_id, task=task_spec, config=config,
                # 独立 Adapter：模型错误不重试（retry_limit=0）→ 真实请求 ≤ 上限
                model_client=DeepSeekAdapter(
                    api_key=api_key, retry_limit=0, thinking=config.thinking,
                ),
                store=store,
                workspaces_root=Path(config.output_dir) / "workspaces",
                image=DEFAULT_IMAGE,
            )
            test_agent.run_baseline(report, repo_overview=extras.repo_overview_text)
            run_usage_sink["test_agent_tokens"] = int(
                test_agent.report.token_usage.get("total") or 0)
            extras.test_agent = test_agent
            # P2（评审）：基线复现成功时，以只读非权威摘要提供给 Main
            test_agent_note = test_agent.main_context_note()

    registry = ToolRegistry()
    register_default_tools(registry)
    verifier = WorkspaceVerifier(
        workspace, report.test_plan, report.baseline,
        timeout_seconds=config.command_timeout_seconds,
    )
    adapter = DeepSeekAdapter(
        api_key=api_key, retry_limit=config.model_retry_limit,
        thinking=config.thinking,  # v0/v1 = disabled；v2 实验轴 = enabled
    )
    # LoopGuard 真实目标变化判定：工作区 patch_hash 变化 = 有进展（评审 P1 遗留接线）
    progress_state = {"last_hash": None}

    def workspace_unchanged(call) -> bool:
        try:
            current = workspace.get_changes()["patch_hash"]
        except WorkspaceError:
            return True  # 无法确认 → 保守视为未变化
        if progress_state["last_hash"] is None:
            progress_state["last_hash"] = current
            return False  # 首次观察：工作区状态本身就是进展
        if current != progress_state["last_hash"]:
            progress_state["last_hash"] = current
            return False
        return True

    def review_runner_factory(review_run_id: str):
        # 延迟引用：factory 在 REVIEWING 阶段才被调用（runner 已构造）。
        # 独立 Adapter 实例：Reviewer 的每次真实 API 尝试（含重试）计入 Reviewer 独立预算（评审 P1-3）
        from repofix.runtime.review_runner import ReviewRunner

        reviewer_adapter = DeepSeekAdapter(
            api_key=api_key, retry_limit=config.model_retry_limit, thinking=config.thinking,
        )
        reviewer_clock = SystemClock()
        # Reviewer 预算统一由 ReviewRunner 内部管理（model_calls ≤
        # max_reviewer_model_calls；Run 级 token 台账经 usage_sink 上报）——
        # 此处不再创建重复预算（此前创建的 hook 会被内部覆盖，从未生效）
        return ReviewRunner(
            run_id=review_run_id, config=config,
            model_client=reviewer_adapter, registry=registry,
            workspace_executor=workspace.read_only_executor(),
            store=store, clock=reviewer_clock,
            global_tool_budget=runner.budget,
            usage_sink=run_usage_sink,
        )

    runner = AgentRunner(
        run_id=run_id, task=task_spec, config=config,
        model_client=adapter, registry=registry,
        dispatcher=ToolDispatcher(registry, workspace, command_policy=config.command_policy),
        store=store, verifier=verifier, clock=SystemClock(),
        patch_hash_provider=lambda: workspace.get_changes()["patch_hash"],  # 评审 P0-3
        preflight_summary={
            "steps": "ok",
            "command_policy": config.command_policy,
            "context_recent_full_turns": config.context_recent_full_turns,
            "test_plan": report.test_plan,
            "test_plan_source": report.test_plan_source,
            "test_plan_evidence": report.test_plan_evidence,
            "baseline_kind": report.baseline.get("kind"),
        },
        repo_overview=extras.repo_overview_text,
        repo_overview_meta=extras.repo_overview_meta or None,
        test_agent_note=test_agent_note,
        target_unchanged=workspace_unchanged,
        review_runner_factory=review_runner_factory,
        reviewer_executor=workspace.read_only_executor(),
        upstream_token_usage=dict(test_agent.report.token_usage) if test_agent is not None else None,
    )
    # 每次真实 API 尝试（含 Gateway 内部重试）原子检查并占用预算（评审 P0-2）
    # v1.1 统一台账：max_total_tokens 按"整个 Run"口径检查（Main + Reviewer + Test Agent）
    runner.budget.external_tokens_fn = lambda: int(
        run_usage_sink.get("reviewer_tokens") or 0
    ) + int(run_usage_sink.get("test_agent_tokens") or 0)
    adapter.on_attempt = runner.budget.reserve_model_attempt
    return runner, store, workspace, manager, handle, report, extras


def _write_artifacts(output_dir: Path, run_id: str, store, workspace, result,
                     test_report_extra: dict | None = None,
                     extras: "StackExtras | None" = None) -> tuple[Path, str | None]:
    delivery_error: str | None = None
    run_dir = Path(output_dir) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        workspace.export_patch(run_dir / "changes.patch")
    except WorkspaceError:
        (run_dir / "changes.patch").write_text("", encoding="utf-8", newline="\n")
    (run_dir / "trajectory.jsonl").write_text(
        store.export_trajectory(run_id), encoding="utf-8", newline="\n"
    )
    (run_dir / "result.json").write_text(result.model_dump_json(indent=2),
                                          encoding="utf-8", newline="\n")
    verification = {}
    for event in reversed(store.get_events(run_id)):
        if event.type.value == "verification_finished":
            verification = event.payload
            break
    report_payload = {"verification": verification}
    if test_report_extra:
        # TestPlan 来源（auto/manual）与发现依据——Trace/Test Report 可追溯（评审要求 7）
        report_payload["test_plan"] = test_report_extra.get("test_plan", [])
        report_payload["test_plan_source"] = test_report_extra.get("test_plan_source", "none")
        report_payload["test_plan_evidence"] = test_report_extra.get("test_plan_evidence", "")
        report_payload["baseline_kind"] = test_report_extra.get("baseline_kind", "")
    if extras is not None and extras.repo_overview_meta:
        report_payload["repo_overview"] = {
            "enabled": True,
            "ok": bool(extras.repo_overview_meta.get("ok")),
            "chars": extras.repo_overview_meta.get("chars", 0),
        }
    if extras is not None and extras.test_agent is not None:
        report_payload["test_agent"] = {
            "triggered": bool(extras.test_agent.report.triggered),
            "status": extras.test_agent.report.status.value,
            "fail_to_pass": extras.test_agent.report.fail_to_pass,
            "authoritative_verification": extras.test_agent.report.authoritative_verification,
        }
    (run_dir / "test-report.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )
    # —— MVP v0.4 新产物：repository-overview.txt 与 test-agent/ ——
    if extras is not None and extras.repo_overview_text is not None:
        (run_dir / "repository-overview.txt").write_text(
            extras.repo_overview_text, encoding="utf-8", newline="\n")
    if extras is not None and extras.test_agent is not None:
        ta_report = extras.test_agent.report
        ta_dir = run_dir / "test-agent"
        ta_dir.mkdir(parents=True, exist_ok=True)
        (ta_dir / "report.json").write_text(
            ta_report.model_dump_json(indent=2), encoding="utf-8", newline="\n")
        candidate_source = extras.test_agent.candidate_source
        if candidate_source:
            (ta_dir / ta_report.artifact_test_file).write_text(
                candidate_source, encoding="utf-8", newline="\n")
    return run_dir, delivery_error


def _exit_code_for(result) -> int:
    if result.runtime_status is RunStatus.COMPLETED:
        return 0 if result.verification_status is VerificationStatus.PASSED else 1
    if result.runtime_status is RunStatus.BUDGET_EXCEEDED:
        return 4
    return 3  # FAILED / CANCELLED：不可恢复错误


def _post_patch_test_verification(extras: "StackExtras | None", workspace, result) -> None:
    """MVP v0.4 功能 B 阶段 2：基线复现成功时，把最终候选 Patch 应用到隔离测试
    副本并复跑生成测试（Fail-to-Pass 补充验证）。

    只读主工作区的 Patch 文本（真实候选 Patch，不复制源码）；任何失败都记录进
    Test Agent 报告，不抛出、不影响 RunResult / 退出码 / Verifier 语义。
    """
    if extras is None or extras.test_agent is None:
        return
    patch_text = ""
    if result.runtime_status is RunStatus.COMPLETED:
        try:
            patch_text = workspace.get_changes_patch()
        except WorkspaceError:
            patch_text = ""
    try:
        extras.test_agent.verify_post_patch(patch_text)
    except Exception as exc:  # noqa: BLE001 —— 补充验证失败只记录，不阻塞产物导出
        extras.test_agent.report.post_patch_status = "error"
        extras.test_agent.report.post_patch_detail = (
            f"post-patch verification crashed: {type(exc).__name__}: {str(exc)[:200]}"
        )


def run(
    repo: Path = typer.Option(..., "--repo", help="本地 Git 仓库路径"),
    task: Optional[str] = typer.Option(None, "--task", help="自然语言任务（与 --task-file 二选一）"),
    task_file: Optional[Path] = typer.Option(None, "--task-file", help="从 UTF-8 文本文件读取任务"),
    base_ref: str = typer.Option("HEAD", "--base-ref", help="修改起点的 Git Commit / Ref"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="模型 ID"),
    test_command: Optional[str] = typer.Option(
        "auto", "--test-command",
        help="可见验证命令；默认 auto = Preflight 自动发现 Python 测试（pytest）。"
             "也可显式指定（如 \"python -m pytest -q\" 或 \"auto\"）；"
             "无可用测试时最终验证将返回 INCONCLUSIVE"),
    output_dir: Path = typer.Option(Path("repofix-data"), "--output-dir", help="运行数据根目录"),
    max_model_calls: int = typer.Option(50, "--max-model-calls", min=1),
    max_tool_calls: int = typer.Option(150, "--max-tool-calls", min=1),
    timeout: int = typer.Option(1200, "--timeout", min=1, help="单任务执行时限（秒）"),
    review: bool = typer.Option(False, "--review/--no-review", help="启用 Read-only Review Agent"
                                "（默认关闭 = 单 Agent Baseline；Experiment G 用不同取值运行）"),
    command_policy: str = typer.Option("strict", "--command-policy",
                                       help="命令权限档位：strict（v0 白名单）/ coding_benchmark"
                                            "（追加受控只读命令 grep/rg/ls）"),
    context_recent_full_turns: int = typer.Option(10, "--context-recent-full-turns", min=0,
                                                  help="Context 近期完整窗口：最近 N 轮保留完整"
                                                       "工具输出，更早轮次确定性摘要；0 = 关闭（v0）"),
    repo_overview: bool = typer.Option(False, "--repo-overview/--no-repo-overview",
                                       help="启用确定性 Repository Overview 注入"
                                            "（MVP v0.4；默认关闭保证旧基线可复现）"),
    test_agent: bool = typer.Option(False, "--test-agent/--no-test-agent",
                                    help="无可用测试时启用一次性 Bug 复现测试生成"
                                         "（MVP v0.4；默认关闭保证旧基线可复现）"),
) -> None:
    """一次性任务执行：输入任务 → 自主修复 → 输出 Patch / Trace / 报告。"""
    try:
        instruction = _read_task(task, task_file)
        base_commit = _validate_repo(repo, base_ref)
    except InputValidationError as exc:
        typer.echo(f"[input error] {exc}", err=True)
        raise typer.Exit(2)
    except DirtyWorkspaceError as exc:
        typer.echo(f"[input error] dirty worktree rejected: {exc}", err=True)
        raise typer.Exit(2)

    config = RunConfig(
        model_id=model,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_duration_seconds=timeout,
        review_enabled=review,  # --review 启用双 Agent（默认 False = 单 Agent Baseline，可复现）
        output_dir=str(output_dir),
        command_policy=command_policy,
        context_recent_full_turns=context_recent_full_turns,
        repo_overview_enabled=repo_overview,  # MVP v0.4 功能 A（默认关闭）
        test_agent_enabled=test_agent,  # MVP v0.4 功能 B（默认关闭）
        # Context Budget 生产启用（评审 P0-1）：已验证模型的窗口来自 profile；未知模型不虚构容量
        context_window_tokens=resolve_context_window(model),
    )
    task_spec = TaskSpec(
        instruction=instruction, repo_path=str(repo), base_commit=base_commit,
        test_command=test_command,
    )
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    try:
        runner, store, workspace, manager, handle, preflight, extras = _build_stack(
            task_spec, config, run_id)
    except PreflightFailure as exc:
        typer.echo(f"[preflight failed] {exc}", err=True)
        raise typer.Exit(3)
    except InputValidationError as exc:
        typer.echo(f"[input error] {exc}", err=True)
        raise typer.Exit(2)
    except (WorkspaceError, DirtyWorkspaceError) as exc:
        typer.echo(f"[workspace error] {exc}", err=True)
        raise typer.Exit(3)

    typer.echo(f"RepoFix run {run_id}")
    typer.echo(f"  repo: {repo} @ {base_commit[:12]}  model: {config.model_id}")
    interrupted = False
    try:
        result = runner.run()
    except KeyboardInterrupt:
        interrupted = True
        typer.echo("\n[cancelled] user interrupt; writing partial artifacts", err=True)
        # 取消语义（design/01）：尽力完成状态记录；不承诺强杀后的完整写入
        from repofix.contracts import RunResult, VerificationStatus

        result = RunResult(
            run_id=run_id, runtime_status=RunStatus.CANCELLED,
            termination_reason="user_interrupt", verification_status=VerificationStatus.NOT_RUN,
            model_calls=0, tool_calls=0, token_usage={}, duration_ms=0,
        )

    # MVP v0.4 功能 B 阶段 2：修复后补充验证（Fail-to-Pass；失败不影响主结论）
    _post_patch_test_verification(extras, workspace, result)

    test_report_extra = {
        "test_plan": preflight.test_plan,
        "test_plan_source": preflight.test_plan_source,
        "test_plan_evidence": preflight.test_plan_evidence,
        "baseline_kind": preflight.baseline.get("kind", "") if isinstance(preflight.baseline, dict) else "",
    }
    run_dir, delivery_error = _write_artifacts(
        output_dir, run_id, store, workspace, result,
        test_report_extra=test_report_extra, extras=extras)
    # 清理顺序（评审 P1）：先停容器（进程树），再清理副本目录；Test Agent 同纪律
    try:
        workspace.close()
    except Exception:  # noqa: BLE001 —— 清理失败不改变退出码
        pass
    finally:
        manager.cleanup_workspace(handle, keep_for_diagnosis=(
            result.runtime_status is not RunStatus.COMPLETED or interrupted
        ))
        if extras.test_agent is not None:
            extras.test_agent.cleanup(keep_for_diagnosis=(
                result.runtime_status is not RunStatus.COMPLETED or interrupted))

    typer.echo(f"  status: {result.runtime_status.value}  verification: {result.verification_status.value}")
    typer.echo(f"  modified: {len(result.modified_files)} file(s)  model calls: {result.model_calls}")
    if extras.test_agent is not None and extras.test_agent.report.triggered:
        typer.echo(f"  test agent: {extras.test_agent.report.status.value}  "
                   f"post-patch: {extras.test_agent.report.post_patch_status}")
    typer.echo(f"  artifacts: {run_dir}")
    if result.termination_reason:
        typer.echo(f"  termination: {result.termination_reason}")
    if delivery_error:
        typer.echo(f"  [delivery error] {delivery_error}", err=True)

    if interrupted:
        raise typer.Exit(130)
    raise typer.Exit(_exit_code_for(result))


app.command()(run)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
