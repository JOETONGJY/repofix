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
from pathlib import Path
from typing import Optional

import typer

from repofix.contracts import (
    RunConfig,
    RunStatus,
    TaskSpec,
    VerificationStatus,
)
from repofix.gateway.adapter import DeepSeekAdapter
from repofix.runtime.loop_guard import SystemClock
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.verifier.workspace_verifier import WorkspaceVerifier
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.errors import DirtyWorkspaceError, WorkspaceError
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import PreflightRunner

app = typer.Typer(add_completion=False, help="RepoFix: Repository-Aware Coding Agent Harness")

DEFAULT_MODEL = "deepseek-flash"


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
    """组装完整执行栈（测试经 monkeypatch 本函数注入 Scripted/Fake 组件）。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise InputValidationError("DEEPSEEK_API_KEY is not set; cannot start a model-backed run")
    manager = WorkspaceManager(Path(config.output_dir) / "workspaces")
    handle = manager.prepare(run_id, Path(task_spec.repo_path), task_spec.base_commit)
    cli = DockerCli()
    workspace = DockerWorkspace(
        handle.task_dir, run_id, base_commit=handle.base_commit, cli=cli,
        command_timeout_seconds=config.command_timeout_seconds,
    )
    preflight = PreflightRunner(cli=cli)
    report = preflight.run(workspace, task_spec.test_command)
    if not report.ok:
        workspace.close()
        manager.cleanup_workspace(handle)
        raise PreflightFailure(report.failure_step or "unknown", report.failure_reason)

    registry = ToolRegistry()
    register_default_tools(registry)
    verifier = WorkspaceVerifier(
        workspace, report.test_plan, report.baseline,
        timeout_seconds=config.command_timeout_seconds,
    )
    store = SQLiteEventStore(Path(config.output_dir) / "runs" / run_id / "run.sqlite")
    adapter = DeepSeekAdapter(api_key=api_key, retry_limit=config.model_retry_limit)
    runner = AgentRunner(
        run_id=run_id, task=task_spec, config=config,
        model_client=adapter, registry=registry,
        dispatcher=ToolDispatcher(registry, workspace),
        store=store, verifier=verifier, clock=SystemClock(),
    )
    # 每次真实 API 尝试（含失败与重试）计入 Runner 预算（specs/02 V0.2）
    adapter.on_attempt = runner.budget.count_model_call
    return runner, store, workspace, manager, handle, report


def _write_artifacts(output_dir: Path, run_id: str, store, workspace, result) -> Path:
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
    (run_dir / "test-report.json").write_text(
        json.dumps({"verification": verification}, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )
    return run_dir


def _exit_code_for(result) -> int:
    if result.runtime_status is RunStatus.COMPLETED:
        return 0 if result.verification_status is VerificationStatus.PASSED else 1
    if result.runtime_status is RunStatus.BUDGET_EXCEEDED:
        return 4
    return 3  # FAILED / CANCELLED：不可恢复错误


@app.command()
def run(
    repo: Path = typer.Option(..., "--repo", help="本地 Git 仓库路径"),
    task: Optional[str] = typer.Option(None, "--task", help="自然语言任务（与 --task-file 二选一）"),
    task_file: Optional[Path] = typer.Option(None, "--task-file", help="从 UTF-8 文本文件读取任务"),
    base_ref: str = typer.Option("HEAD", "--base-ref", help="修改起点的 Git Commit / Ref"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="模型 ID"),
    test_command: Optional[str] = typer.Option(None, "--test-command", help="可见验证命令（在受控 Workspace 内执行）"),
    output_dir: Path = typer.Option(Path("repofix-data"), "--output-dir", help="运行数据根目录"),
    max_model_calls: int = typer.Option(30, "--max-model-calls", min=1),
    max_tool_calls: int = typer.Option(100, "--max-tool-calls", min=1),
    timeout: int = typer.Option(1200, "--timeout", min=1, help="单任务执行时限（秒）"),
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
        review_enabled=False,  # M3：单 Agent Baseline（review_enabled 不暴露为 CLI 参数）
        output_dir=str(output_dir),
    )
    task_spec = TaskSpec(
        instruction=instruction, repo_path=str(repo), base_commit=base_commit,
        test_command=test_command,
    )
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    try:
        runner, store, workspace, manager, handle, preflight = _build_stack(task_spec, config, run_id)
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

    run_dir = _write_artifacts(output_dir, run_id, store, workspace, result)
    manager.cleanup_workspace(handle, keep_for_diagnosis=(
        result.runtime_status is not RunStatus.COMPLETED or interrupted
    ))
    try:
        workspace.close()
    except Exception:  # noqa: BLE001 —— 清理失败不改变退出码
        pass

    typer.echo(f"  status: {result.runtime_status.value}  verification: {result.verification_status.value}")
    typer.echo(f"  modified: {len(result.modified_files)} file(s)  model calls: {result.model_calls}")
    typer.echo(f"  artifacts: {run_dir}")
    if result.termination_reason:
        typer.echo(f"  termination: {result.termination_reason}")

    if interrupted:
        raise typer.Exit(130)
    raise typer.Exit(_exit_code_for(result))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
