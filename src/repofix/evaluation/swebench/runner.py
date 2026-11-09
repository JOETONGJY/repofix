"""SWE-bench Mini-Eval 任务编排：per-task AgentRunner 装配 + 运行 + 独立判定。

与 cli._build_stack 的差异（均为评测适配，不改 Runtime 语义）：
- 工作区 = 从官方实例镜像提取的 /testbed（含已构建产物），容器由本层启动
  （无网络 / 限资源 / 挂载 /testbed / root——偏离与理由见 workspace.py）；
- 环境验证已在 stage-0 完成，本层不再跑 PreflightRunner（避免基线重复执行）；
  Preflight 摘要从 stage-0 证据传入，Trace 可追溯；
- 可见测试计划 = plan.py 冻结规则产物（test_plan 冻结进 Verifier，VF-07）；
- 判定 = judge.run_judge（官方四步协议，干净 base 独立验收），与 Agent
  工作区完全隔离。

花钱守门：本模块不自行判断预算是否被批准——调用方（stage 脚本）必须显式
携带 --approve-tokens；每次任务结束记录真实 Usage，累计超 --total-token-cap
即拒绝继续（不精确到最后一个 Token，但跨任务硬停）。
"""
import json
import time
import uuid
from pathlib import Path

from repofix.contracts import RunConfig, TaskSpec
from repofix.evaluation.swebench.judge import extract_eval_commands, run_judge
from repofix.evaluation.swebench.workspace import (
    build_agent_image,
    extract_testbed,
    probe_python_env,
    run_install_commands,
    start_eval_container,
)
from repofix.gateway.adapter import DeepSeekAdapter
from repofix.runtime.loop_guard import BudgetTracker, SystemClock
from repofix.runtime.review_runner import ReviewRunner
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.verifier.workspace_verifier import WorkspaceVerifier
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace


class EvalBudgetExceeded(RuntimeError):
    """跨任务 Token 台账触顶：拒绝继续新的模型调用任务。"""


def build_task_stack(
    *,
    instance_id: str,
    run_id: str,
    image: str,
    base_commit: str,
    problem_statement: str,
    visible_plan_argv: list[str],
    testbed: Path,
    run_dir: Path,
    config: RunConfig,
    rg_path: Path,
    preflight_summary: dict,
    installs: list[str] | None = None,
    pkg_version: str = "",
):
    """组装单个任务的完整执行栈。返回 (runner, store, workspace, cli, container)。"""
    cli = DockerCli()
    # 派生镜像（PATH 前置 testbed conda 环境 + 内置 rg，按实例缓存）——官方镜像
    # 的裸 python 不在任务环境内（eval_script 靠显式 conda activate）
    agent_image = build_agent_image(cli, image, instance_id, rg_path, pkg_version=pkg_version)
    container = f"sweb-agent-{instance_id}-{uuid.uuid4().hex[:6]}"
    start_eval_container(cli, container, testbed, agent_image)
    env_probe = probe_python_env(cli, container, None)
    if not env_probe["rg_available"]:
        cli.remove_container(container)
        raise RuntimeError(f"rg unavailable in derived image for {instance_id}")
    if installs:
        # 官方 install 步骤（环境准备）：Agent 自行运行测试需要包可导入；
        # 禁网容器内 build isolation 失败时自动离线重试（依赖镜像已备齐）
        install_evidence = run_install_commands(
            cli, container, installs,
            env_prefix=f"SETUPTOOLS_SCM_PRETEND_VERSION={pkg_version} " if pkg_version else "")
        if not install_evidence["ok"]:
            cli.remove_container(container)
            raise RuntimeError(f"install commands failed for {instance_id}: {installs}")
    workspace = DockerWorkspace(
        testbed, run_id, base_commit=base_commit, cli=cli,
        container_name=container, command_timeout_seconds=config.command_timeout_seconds,
        workspace_mount="/testbed", command_policy=config.command_policy,
    )
    registry = ToolRegistry()
    register_default_tools(registry)
    verifier = WorkspaceVerifier(
        workspace,
        test_plan=[visible_plan_argv] if visible_plan_argv else [],
        baseline=preflight_summary.get("baseline", {}),
        timeout_seconds=config.command_timeout_seconds,
    )
    store = SQLiteEventStore(run_dir / "run.sqlite")
    api_key = _require_api_key()
    adapter = DeepSeekAdapter(
        api_key=api_key, retry_limit=config.model_retry_limit, thinking="disabled",
    )
    task_spec = TaskSpec(
        instruction=problem_statement,
        repo_path=str(testbed),
        base_commit=base_commit,
        test_command=" ".join(visible_plan_argv) if visible_plan_argv else "auto(empty-plan)",
    )

    progress_state = {"last_hash": None}

    def workspace_unchanged(call) -> bool:
        try:
            current = workspace.get_changes()["patch_hash"]
        except Exception:  # noqa: BLE001 —— 无法确认 → 保守视为未变化
            return True
        if progress_state["last_hash"] is None:
            progress_state["last_hash"] = current
            return False
        if current != progress_state["last_hash"]:
            progress_state["last_hash"] = current
            return False
        return True

    def review_runner_factory(review_run_id: str):
        reviewer_adapter = DeepSeekAdapter(
            api_key=api_key, retry_limit=config.model_retry_limit, thinking="disabled",
        )
        reviewer_clock = SystemClock()
        reviewer_budget = BudgetTracker(config, reviewer_clock)
        reviewer_adapter.on_attempt = reviewer_budget.reserve_model_attempt
        return ReviewRunner(
            run_id=review_run_id, config=config, model_client=reviewer_adapter,
            registry=registry, workspace_executor=workspace.read_only_executor(),
            store=store, clock=reviewer_clock, global_tool_budget=runner.budget,
        )

    runner = AgentRunner(
        run_id=run_id, task=task_spec, config=config,
        model_client=adapter, registry=registry,
        dispatcher=ToolDispatcher(registry, workspace, command_policy=config.command_policy),
        store=store, verifier=verifier, clock=SystemClock(),
        patch_hash_provider=lambda: workspace.get_changes()["patch_hash"],
        preflight_summary=preflight_summary,
        target_unchanged=workspace_unchanged,
        review_runner_factory=review_runner_factory if config.review_enabled else None,
    )
    adapter.on_attempt = runner.budget.reserve_model_attempt
    return runner, store, workspace, cli, container, agent_image


def run_task(
    *,
    instance_meta: dict,
    row: dict,
    visible_argv: list[str],
    config: RunConfig,
    rg_path: Path,
    data_root: Path,
    preflight_summary: dict | None = None,
    keep_testbed: bool = False,
) -> dict:
    """运行一个任务：Agent Loop → 产物导出 → 独立判定 → 记录。

    返回 per-task 记录（agent 结果 + judge 结果 + 真实 Usage + 耗时）。
    """
    instance_id = instance_meta["instance_id"]
    data_root = Path(data_root)
    run_dir = data_root / "runs" / instance_id
    run_dir.mkdir(parents=True, exist_ok=True)
    testbed = data_root / "agent-testbeds" / instance_id
    testbed.parent.mkdir(parents=True, exist_ok=True)
    from repofix.evaluation.swebench.workspace import force_rmtree

    force_rmtree(testbed)
    testbed.mkdir(parents=True)
    extract_testbed(DockerCli(), instance_meta["image"], testbed,
                    base_commit=instance_meta["base_commit"])

    preflight_summary = preflight_summary or {
        "steps": "stage0-validated",
        "test_plan": [visible_argv] if visible_argv else [],
        "test_plan_source": "issue_scoped" if visible_argv else "empty-plan",
        "baseline_kind": "stage0_validated",
    }
    # 评审 P2：实际生效的工具配置写入 Trace（PREFLIGHT_COMPLETED 事件）与逐题
    # 记录——取自 RunConfig 本身，不依赖脚本启动参数
    preflight_summary = {
        **preflight_summary,
        "command_policy": config.command_policy,
        "context_recent_full_turns": config.context_recent_full_turns,
    }
    started = time.time()
    cli = None
    container = None
    agent_image = None
    # 每次尝试独立 run_id：SQLite 库按实例持久（多次尝试共存，Trace 可追溯）
    attempt_run_id = f"run_{instance_id}_{uuid.uuid4().hex[:6]}"
    try:
        installs, _tests = extract_eval_commands(row["eval_script"])
        runner, store, workspace, cli, container, agent_image = build_task_stack(
            instance_id=instance_id, run_id=attempt_run_id, image=instance_meta["image"],
            base_commit=instance_meta["base_commit"],
            problem_statement=row["problem_statement"],
            visible_plan_argv=visible_argv, testbed=testbed, run_dir=run_dir,
            config=config, rg_path=rg_path, preflight_summary=preflight_summary,
            installs=installs, pkg_version=str(row.get("version") or ""),
        )
        result = runner.run()
        # 产物（与 CLI 交付契约一致：patch / trace / result）
        try:
            workspace.export_patch(run_dir / "changes.patch")
        except Exception:  # noqa: BLE001
            (run_dir / "changes.patch").write_text("", encoding="utf-8", newline="\n")
        # 必须用本次尝试的 run_id 导出（旧代码用固定 run_{instance_id}，在 run_id
        # 改为每次尝试唯一之后会导出空 Trace——链路校验发现）
        (run_dir / "trajectory.jsonl").write_text(
            store.export_trajectory(attempt_run_id), encoding="utf-8", newline="\n")
        (run_dir / "result.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8", newline="\n")
    finally:
        if cli is not None and container is not None:
            cli.remove_container(container)
        if not keep_testbed:
            from repofix.evaluation.swebench.workspace import force_rmtree

            force_rmtree(testbed)
    agent_seconds = time.time() - started

    # 独立判定（干净 base，官方协议）；INCONCLUSIVE 免费重试一次——
    # 判定不消耗模型 token，只花容器时间，可消除重负载下的瞬时抖动
    patch_path = run_dir / "changes.patch"

    def _judge_once(tag: str):
        # 判定容器必须用派生镜像（PATH 含 testbed conda env + rg）——官方镜像裸
        # python 没有 pytest，会造成"测试没跑"的假 INCONCLUSIVE
        return run_judge(
            DockerCli(), image=agent_image, base_commit=instance_meta["base_commit"],
            test_patch=row["test_patch"], fail_to_pass=instance_meta["fail_to_pass"],
            pass_to_pass=instance_meta["pass_to_pass"], eval_script=row["eval_script"],
            patch_path=patch_path if patch_path.exists() else None,
            work_root=data_root / "judge-runs", instance_id=f"{instance_id}{tag}",
            pkg_version=str(row.get("version") or ""),
        )

    judge_record = _judge_once("")
    if judge_record.outcome == "INCONCLUSIVE":
        judge_record = _judge_once("-retry")
    record = {
        "instance_id": instance_id,
        "repo": instance_meta["repo"],
        "agent": json.loads(result.model_dump_json()),
        "agent_seconds": round(agent_seconds, 1),
        "judge": {
            "outcome": judge_record.outcome,
            "reason": judge_record.reason,
            "patch_applied": judge_record.patch_applied,
            "f2p_failures": judge_record.f2p_failures,
            "p2p_regressions": judge_record.p2p_regressions,
            "install_attempts": judge_record.install_attempts,
            "statuses_count": judge_record.statuses_count,
            "test_output_tail": judge_record.test_output_tail[-1200:],
        },
        "resolved": judge_record.outcome == "RESOLVED",
        "artifacts_dir": str(run_dir),
    }
    (run_dir / "eval-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_ledger(data_root: Path) -> int:
    """已花 Token 台账（跨任务累计，来自各任务 eval-record.json）。"""
    total = 0
    for record_file in Path(data_root, "runs").glob("*/eval-record.json"):
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
            total += int(record.get("agent", {}).get("token_usage", {}).get("total", 0))
        except (OSError, ValueError, KeyError):
            continue
    return total


def check_ledger(data_root: Path, total_cap: int) -> None:
    used = load_ledger(data_root)
    if used >= total_cap:
        raise EvalBudgetExceeded(f"token ledger {used} >= cap {total_cap}; refusing new model tasks")


def _require_api_key() -> str:
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set; refusing to start a model-backed task")
    return api_key


def summarize(records: list[dict]) -> dict:
    """任务记录 → 汇总指标（Resolved@1 / Patch Apply Rate / 资源效率 / 失败归因）。"""
    total = len(records)
    resolved = sum(1 for r in records if r["resolved"])
    applied = sum(1 for r in records if r["judge"]["patch_applied"])
    tokens = [r["agent"].get("token_usage", {}).get("total", 0) for r in records]
    durations = [r.get("agent_seconds", 0.0) for r in records]

    def bucket(r: dict) -> str:
        status = r["agent"].get("runtime_status", "")
        if r["resolved"]:
            return "resolved"
        if not r["judge"]["patch_applied"]:
            has_spend = r["agent"].get("token_usage", {}).get("total", 0)
            if has_spend and "no patch produced" in r["judge"].get("reason", ""):
                return "no_patch_produced"
            return "patch_apply_failed"
        if r["judge"]["f2p_failures"] and r["judge"]["p2p_regressions"]:
            return "f2p_fail_and_regression"
        if r["judge"]["f2p_failures"]:
            return "f2p_not_fixed"
        if r["judge"]["p2p_regressions"]:
            return "pass_to_pass_regression"
        if status == "BUDGET_EXCEEDED":
            return "agent_budget_exceeded"
        if status != "COMPLETED":
            return f"agent_{status.lower()}"
        return "unresolved_other"

    breakdown: dict[str, int] = {}
    for r in records:
        key = bucket(r)
        breakdown[key] = breakdown.get(key, 0) + 1
    return {
        "total_tasks": total,
        "resolved": resolved,
        "resolved_at_1": round(resolved / total, 3) if total else 0.0,
        "patch_apply_rate": round(applied / total, 3) if total else 0.0,
        "avg_tokens_per_task": round(sum(tokens) / total) if total else 0,
        "total_tokens": sum(tokens),
        "avg_duration_seconds": round(sum(durations) / total, 1) if total else 0.0,
        "failure_breakdown": breakdown,
        "by_task": [
            {"instance_id": r["instance_id"], "resolved": r["resolved"],
             "runtime_status": r["agent"].get("runtime_status"),
             "verification": r["agent"].get("verification_status"),
             "model_calls": r["agent"].get("model_calls"),
             "tokens": r["agent"].get("token_usage", {}).get("total", 0),
             "duration_seconds": r.get("agent_seconds", 0),
             "judge_reason": r["judge"]["reason"][:160]}
            for r in records
        ],
    }
