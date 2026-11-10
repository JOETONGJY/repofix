"""SWE-bench Mini-Eval · Stage 1（预演）/ Stage 2（正式单 Agent）。

花钱守门（用户同意规则）：
- 必须 --approve-tokens 才会发起任何模型调用；缺省只做预检并退出；
- 每任务结束累计真实 Usage 到跨任务台账，达到 --total-token-cap 硬停
  （不精确到最后一个 Token，但跨任务绝不超额继续）；
- 一题失败不自动重跑、不针对单题调 Prompt / 预算（冻结协议）。

预算（GPT 复核：先用预演校准，再冻结正式上限）：
- 预演默认 20 模型调用 / 60 工具请求 / 20k token / 题；
- 正式预算在预演复盘后由用户显式传入（--max-* 参数），未传入则拒绝运行。

用法示例：
  # 预演（1 道 dev 题）
  python scripts/run_swebench_stage12.py --stage rehearsal --approve-tokens \
      --proxy http://127.0.0.1:7897 --total-token-cap 25000
  # 正式（冻结的 10 题；预算来自预演复盘）
  python scripts/run_swebench_stage12.py --stage formal --approve-tokens \
      --max-model-calls 20 --max-total-tokens 30000 --total-token-cap 320000
"""
import argparse
import json
import sys
import time
from pathlib import Path

from repofix.contracts import RunConfig
from repofix.evaluation.swebench import dataset, runner
from repofix.evaluation.swebench.runner import EvalBudgetExceeded, summarize
from repofix.evaluation.swebench.workspace import download_rg
from repofix.workspace.docker_cli import DockerCli

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "repofix-data" / "swebench"
FREEZE_DIR = REPO_ROOT / "benchmarks" / "swebench-mini"


def _load_frozen() -> dict:
    frozen_path = FREEZE_DIR / "selection.json"
    if not frozen_path.exists():
        raise SystemExit("[stage12] selection.json not found — run stage 0 first")
    return json.loads(frozen_path.read_text(encoding="utf-8"))


def _instance_payload(frozen: dict, meta: dict) -> tuple[dict, dict]:
    """冻结名单元数据 → (meta, dataset_row)。instance 必须在冻结名单内。"""
    split = "test" if any(m["instance_id"] == meta["instance_id"] for m in frozen["formal"]) else "dev"
    row = dataset.get_instance(split, meta["instance_id"], DATA_ROOT / "dataset")
    return meta, row


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench Mini-Eval stage 1/2 (spends tokens!)")
    parser.add_argument("--stage", choices=("rehearsal", "formal"), required=True)
    parser.add_argument("--approve-tokens", action="store_true",
                        help="explicit user consent to spend model tokens (required)")
    parser.add_argument("--proxy", default=None, help="HTTP proxy for rg download")
    parser.add_argument("--total-token-cap", type=int, required=True,
                        help="cross-task hard stop on cumulative real token usage")
    parser.add_argument("--max-model-calls", type=int, default=None)
    parser.add_argument("--max-tool-calls", type=int, default=None)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=1800, help="per-task wall clock seconds")
    parser.add_argument("--command-timeout", type=int, default=600)
    parser.add_argument("--only", nargs="*", default=None, help="run a subset of frozen instances")
    parser.add_argument("--keep-testbeds", action="store_true")
    parser.add_argument("--command-policy", default="strict", choices=("strict", "coding_benchmark"),
                        help="RepoFix v1 起使用 coding_benchmark（受控只读命令）")
    parser.add_argument("--context-recent-full-turns", type=int, default=0,
                        help="Context 近期完整窗口（v1 建议 6；0 = 关闭 = v0 行为）")
    parser.add_argument("--thinking", default="disabled", choices=("disabled", "enabled"),
                        help="思考模式（v2 实验轴 = enabled；v0/v1 = disabled）")
    parser.add_argument("--selection", default=None,
                        help="冻结名单路径（默认 benchmarks/swebench-mini/selection.json）")
    parser.add_argument("--arm-tag", default=None,
                        help="实验组标记（如 tierA4-1M / v1-ab）：记录文件保存为 records-formal-<tag>.json，"
                             "避免不同版本对照混用同一 records 文件（缓存复用防护）")
    args = parser.parse_args()

    if not args.approve_tokens:
        parser.error("--approve-tokens is required before any model call (spending consent)")

    frozen = (json.loads(Path(args.selection).read_text(encoding="utf-8"))
              if args.selection else _load_frozen())
    if args.stage == "rehearsal":
        if not frozen.get("rehearsal"):
            raise SystemExit("[stage12] no eligible rehearsal instance in frozen selection")
        instances = [frozen["rehearsal"]]
        # 预演预算（校准用；正式预算必须显式传入）
        config_kwargs = {
            "max_model_calls": args.max_model_calls or 20,
            "max_tool_calls": args.max_tool_calls or 60,
            "max_total_tokens": args.max_total_tokens or 20000,
        }
    else:
        if not frozen.get("formal"):
            raise SystemExit("[stage12] frozen selection has no formal instances")
        instances = list(frozen["formal"])
        if args.max_model_calls is None or args.max_total_tokens is None:
            raise SystemExit(
                "[stage12] formal budget must be set explicitly after rehearsal review "
                "(--max-model-calls / --max-total-tokens)")
        config_kwargs = {
            "max_model_calls": args.max_model_calls,
            "max_tool_calls": args.max_tool_calls or 60,
            "max_total_tokens": args.max_total_tokens,
        }
    if args.only:
        wanted = set(args.only)
        instances = [m for m in instances if m["instance_id"] in wanted]
    if not instances:
        raise SystemExit("[stage12] no instances to run")

    config = RunConfig(
        model_id="deepseek-flash",
        max_duration_seconds=args.timeout,
        command_timeout_seconds=args.command_timeout,
        review_enabled=False,  # Stage 1/2 单 Agent 基线（Stage 3 另行批准）
        context_window_tokens=131072,
        output_reserve_tokens=4096,
        command_policy=args.command_policy,
        context_recent_full_turns=args.context_recent_full_turns,
        thinking=args.thinking,
        **config_kwargs,
    )
    print(f"[stage12] stage={args.stage} instances={[m['instance_id'] for m in instances]}")
    print(f"[stage12] config: {config_kwargs}, wall-clock {args.timeout}s/task, "
          f"ledger cap {args.total_token_cap}")

    # Docker 预检：守护进程不可达时整体中止，不得把 8 题全部记成 INFRA_ERROR
    _probe = DockerCli().run(["version", "--format", "{{.Server.Version}}"], timeout=30)
    if _probe.returncode != 0:
        raise SystemExit(f"[stage12] docker daemon unreachable: {_probe.stderr.strip()[:120]}; "
                         "start Docker Desktop first (no tasks were marked)")
    rg_path = download_rg(DATA_ROOT / "tools", proxy=args.proxy)
    records_name = (f"records-{args.stage}-{args.arm_tag}.json" if args.arm_tag
                    else f"records-{args.stage}.json")
    records_path = FREEZE_DIR / records_name
    print(f"[stage12] records file: {records_path.name}")
    records: list[dict] = []
    if records_path.exists():
        records = json.loads(records_path.read_text(encoding="utf-8"))
        # 完成判定：有真实 token 消耗才算跑过——0-token INFRA_ERROR 是基础设施
        # 故障不是任务结果，允许补跑（结果覆盖同实例旧记录）
        done = {r["instance_id"] for r in records if r["agent"]["token_usage"]["total"] > 0}
        instances = [m for m in instances if m["instance_id"] not in done]
        if done:
            print(f"[stage12] resuming: {len(done)} already recorded (frozen protocol: no rerun)")

    for meta in instances:
        try:
            runner.check_ledger(DATA_ROOT, args.total_token_cap)
        except EvalBudgetExceeded as exc:
            print(f"[stage12] STOP: {exc}", file=sys.stderr)
            break
        meta, row = _instance_payload(frozen, meta)
        visible_plan = _frozen_plan(meta)
        print(f"[stage12] running {meta['instance_id']} "
              f"(visible plan: {visible_plan['plan_source']} {visible_plan.get('argv', [])})")
        started = time.time()
        try:
            record = runner.run_task(
                instance_meta=meta, row=row, visible_argv=visible_plan.get("argv", []),
                config=config, rg_path=rg_path, data_root=DATA_ROOT,
                preflight_summary={
                    "steps": "stage0-validated",
                    "test_plan": [visible_plan.get("argv", [])] if visible_plan.get("argv") else [],
                    "test_plan_source": visible_plan.get("plan_source", "unknown"),
                    "baseline_kind": "stage0_validated",
                },
                keep_testbed=args.keep_testbeds,
            )
        except Exception as exc:  # noqa: BLE001 —— 单任务故障留痕，不中断整轮
            record = {
                "instance_id": meta["instance_id"], "repo": meta["repo"],
                "resolved": False, "agent_seconds": round(time.time() - started, 1),
                "agent": {"runtime_status": "INFRA_ERROR",
                          "token_usage": {"total": 0}, "model_calls": 0},
                "judge": {"outcome": "INCONCLUSIVE", "reason": f"{type(exc).__name__}: {exc}"[:300],
                          "patch_applied": False, "f2p_failures": [], "p2p_regressions": []},
                "artifacts_dir": str(DATA_ROOT / "runs" / meta["instance_id"]),
            }
        records.append(record)
        records_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[stage12] -> runtime={record['agent'].get('runtime_status')} "
              f"resolved={record['resolved']} tokens={record['agent'].get('token_usage', {}).get('total', 0)} "
              f"({time.time() - started:.0f}s)")
        print(f"[stage12] judge: {record['judge']['outcome']} — {record['judge']['reason'][:140]}")

    print("=" * 60)
    print(json.dumps(summarize(records), ensure_ascii=False, indent=2))
    return 0


def _frozen_plan(meta: dict) -> dict:
    """冻结名单实例的可见计划来自 stage0 报告（冻结时已评审）。

    48 题合并名单的证据在两份报告里：既有 8 题在 stage0-report.json，
    扩展 40 题在 selection-40-stage0-report.json——依次查找。
    """
    for name in ("stage0-report.json", "selection-40-stage0-report.json"):
        report_path = FREEZE_DIR / name
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for entry in report["instances"]:
            if entry["instance_id"] == meta["instance_id"]:
                plan_data = entry.get("visible_plan", {})
                if plan_data.get("plan_source") == "empty":
                    return {"plan_source": "empty", "argv": []}
                return {"plan_source": plan_data.get("plan_source", "issue_scoped"),
                        "argv": plan_data.get("argv", [])}
    raise SystemExit(f"[stage12] instance not in any stage0 report: {meta['instance_id']}")


if __name__ == "__main__":
    raise SystemExit(main())
