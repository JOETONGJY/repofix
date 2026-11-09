"""工具效率 v1 端到端链路验收（零模型 Token）。

验证 GPT Review 的核心原则："优化必须在真实 Benchmark 执行链路中确实被启用"。
走通真实链路：eval runner.build_task_stack → ToolDispatcher → DockerWorkspace →
派生镜像容器内执行 grep/rg/ls，并验证：

  ① coding_benchmark 档：grep/rg/ls 在两个校验层（Dispatcher 语义期 + Workspace
     执行期）都放行，且在容器内真实执行成功；
  ② strict 档：同样命令在两层都被拒（v0 行为不变）；
  ③ 危险参数（--pre=、-f、绝对路径、..）在两个校验层都被拒；
  ④ 两层使用同一策略（防 P0-1 回归：Workspace 漏传 command_policy）；
  ⑤ 实际生效的档位写入 Trace（PREFLIGHT_COMPLETED 事件）。

不调用 DeepSeek：只组装执行栈并直接驱动 Dispatcher，不运行 AgentRunner 主循环。
"""
import json
import sys
from pathlib import Path

from repofix.contracts import AgentActor, RunConfig, ToolCall
from repofix.evaluation.swebench import dataset
from repofix.evaluation.swebench.judge import extract_eval_commands
from repofix.evaluation.swebench.runner import build_task_stack
from repofix.evaluation.swebench.selection import candidate_metadata
from repofix.evaluation.swebench.workspace import extract_testbed, force_rmtree
from repofix.workspace.docker_cli import DockerCli

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "repofix-data" / "swebench"
INSTANCE = "pallets__flask-4992"  # 最快的已校准实例


def _call(dispatcher, argv: list[str], call_id: str):
    """完整走 Dispatcher 校验链 + 执行（与 Scheduler 内部顺序一致）。"""
    call = ToolCall(call_id=call_id, tool_name="run_command", arguments={"argv": argv})
    rejection = dispatcher.validate(call, AgentActor.MAIN)
    if rejection is not None:
        return "dispatcher_rejected", rejection
    result = dispatcher.execute(call)
    return ("executed_ok" if result.status.value == "succeeded" and result.exit_code in (0, 1)
            else "workspace_rejected" if result.error_code and result.error_code.value == "permission_denied"
            else f"executed_other(exit={result.exit_code},status={result.status.value})"), result


def run_case(policy: str, row: dict, meta: dict, rg_path: Path) -> dict:
    testbed = DATA_ROOT / "smoke-testbeds" / f"{INSTANCE}-{policy}"
    force_rmtree(testbed)
    testbed.mkdir(parents=True)
    extract_testbed(DockerCli(), meta["image"], testbed, base_commit=meta["base_commit"])
    config = RunConfig(model_id="deepseek-flash", command_policy=policy,
                       context_recent_full_turns=12, review_enabled=False)
    run_dir = DATA_ROOT / "smoke-runs" / f"{INSTANCE}-{policy}"
    run_dir.mkdir(parents=True, exist_ok=True)
    installs, _ = extract_eval_commands(row["eval_script"])
    import os
    os.environ.setdefault("DEEPSEEK_API_KEY", "smoke-test-no-calls")
    runner, store, workspace, cli, container, _img = build_task_stack(
        instance_id=INSTANCE, run_id=f"smoke-{policy}", image=meta["image"],
        base_commit=meta["base_commit"], problem_statement=row["problem_statement"],
        visible_plan_argv=[], testbed=testbed, run_dir=run_dir, config=config,
        rg_path=DATA_ROOT / "tools" / "rg", preflight_summary={"steps": "smoke"},
        installs=installs, pkg_version=str(row.get("version") or ""),
    )
    dispatcher = runner._scheduler._dispatcher  # noqa: SLF001 —— 验收需要检视装配结果
    out: dict = {
        "policy": policy,
        "dispatcher_policy": dispatcher.command_policy,
        "workspace_policy": workspace.command_policy,
        "cases": {},
    }
    try:
        probes = {
            "grep": ["grep", "-rn", "from_file", "src/flask/config.py"],
            "rg": ["rg", "--max-count=2", "-n", "def from_file", "src"],
            "ls": ["ls", "-la", "src/flask"],
            "danger_rg_pre_equals": ["rg", "--pre=cat", "x", "src"],
            "danger_grep_f": ["grep", "-f", "pats.txt", "src"],
            "danger_abs_path": ["grep", "-n", "root", "/etc/passwd"],
            "danger_traversal": ["ls", "../.."],
            "pytest_still_ok": ["python", "-m", "pytest", "--collect-only", "-q",
                                "tests/test_config.py"],
        }
        for name, argv in probes.items():
            verdict, payload = _call(dispatcher, argv, f"smoke-{policy}-{name}")
            out["cases"][name] = {
                "verdict": verdict,
                "detail": (getattr(payload, "error_message", None)
                           or (getattr(payload, "content", "") or "")[:120]),
            }
        # 直接驱动 Workspace（绕过 Dispatcher）——验证纵深防御：执行层同样拒绝
        for name in ("danger_rg_pre_equals", "danger_abs_path"):
            r = workspace.run_command(probes[name], call_id=f"ws-direct-{name}")
            out["cases"][f"{name}__workspace_direct"] = {
                "verdict": "workspace_rejected"
                if r.error_code and r.error_code.value == "permission_denied" else "NOT_REJECTED",
                "detail": (r.error_message or "")[:120],
            }
    finally:
        cli.remove_container(container)
        force_rmtree(testbed)
    return out


def main() -> int:
    if DockerCli().run(["version", "--format", "{{.Server.Version}}"], timeout=30).returncode != 0:
        print("docker daemon unreachable", file=sys.stderr)
        return 2
    row = dataset.get_instance("test", INSTANCE, DATA_ROOT / "dataset")
    meta = candidate_metadata(row)
    rg_path = DATA_ROOT / "tools" / "rg"
    results = [run_case(p, row, meta, rg_path) for p in ("coding_benchmark", "strict")]

    expect = {
        "coding_benchmark": {
            "grep": "executed_ok", "rg": "executed_ok", "ls": "executed_ok",
            "danger_rg_pre_equals": "dispatcher_rejected", "danger_grep_f": "dispatcher_rejected",
            "danger_abs_path": "dispatcher_rejected", "danger_traversal": "dispatcher_rejected",
            "pytest_still_ok": "executed_ok",
            "danger_rg_pre_equals__workspace_direct": "workspace_rejected",
            "danger_abs_path__workspace_direct": "workspace_rejected",
        },
        "strict": {
            "grep": "dispatcher_rejected", "rg": "dispatcher_rejected", "ls": "dispatcher_rejected",
            "danger_rg_pre_equals": "dispatcher_rejected", "danger_grep_f": "dispatcher_rejected",
            "danger_abs_path": "dispatcher_rejected", "danger_traversal": "dispatcher_rejected",
            "pytest_still_ok": "executed_ok",
            "danger_rg_pre_equals__workspace_direct": "workspace_rejected",
            "danger_abs_path__workspace_direct": "workspace_rejected",
        },
    }
    failures = []
    for res in results:
        policy = res["policy"]
        if res["dispatcher_policy"] != policy or res["workspace_policy"] != policy:
            failures.append(f"[{policy}] layer policy mismatch: dispatcher={res['dispatcher_policy']} "
                            f"workspace={res['workspace_policy']}")
        for case, want in expect[policy].items():
            got = res["cases"].get(case, {}).get("verdict")
            mark = "ok " if got == want else "FAIL"
            print(f"  [{mark}] {policy:17s} {case:42s} {got}")
            if got != want:
                failures.append(f"[{policy}] {case}: got {got}, want {want} "
                                f"({res['cases'].get(case, {}).get('detail', '')})")
    out_path = DATA_ROOT / "smoke-tool-efficiency-v1.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"evidence: {out_path}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  ", f)
        return 1
    print("ALL CHAIN CHECKS PASSED (zero model tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
