"""SWE-bench Mini-Eval · 对照组：无工具 API 基线（spends tokens!）。

与 Agent 相同的冻结任务集 + 同一判定器；差异 = 无工具/无检索/无验证阶段，
静态上下文一次给足（plan.py 规则定位，Agent 可见信息），反馈回路仅含
应用错误与可见测试输出。每题 40 轮 / 600k token 上限。

用法：
  PYTHONPATH=src python scripts/run_swebench_control.py --approve-tokens \
      --total-token-cap 6000000
"""
import argparse
import json
import sys
import time
from pathlib import Path

from repofix.contracts import RunConfig
from repofix.evaluation.swebench import dataset, runner
from repofix.evaluation.swebench.control import run_control_task
from repofix.evaluation.swebench.workspace import download_rg
from repofix.workspace.docker_cli import DockerCli

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "repofix-data" / "swebench"
FREEZE_DIR = REPO_ROOT / "benchmarks" / "swebench-mini"


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench control arm (no tools, spends tokens!)")
    parser.add_argument("--approve-tokens", action="store_true", required=True)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--total-token-cap", type=int, required=True)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--max-total-tokens", type=int, default=600000)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    probe = DockerCli().run(["version", "--format", "{{.Server.Version}}"], timeout=30)
    if probe.returncode != 0:
        raise SystemExit(f"[control] docker daemon unreachable: {probe.stderr.strip()[:120]}")

    frozen = json.loads((FREEZE_DIR / "selection.json").read_text(encoding="utf-8"))
    if not frozen.get("formal"):
        raise SystemExit("[control] no frozen formal instances")
    report = json.loads((FREEZE_DIR / "stage0-report.json").read_text(encoding="utf-8"))

    config = RunConfig(model_id="deepseek-flash", max_duration_seconds=args.timeout)
    records_path = FREEZE_DIR / "records-control.json"
    records: list[dict] = []
    if records_path.exists():
        records = json.loads(records_path.read_text(encoding="utf-8"))
    done = {r["instance_id"] for r in records}

    def frozen_plan(instance_id: str) -> list[str]:
        for entry in report["instances"]:
            if entry["instance_id"] == instance_id:
                return entry.get("visible_plan", {}).get("argv", []) or []
        return []

    rg_path = download_rg(DATA_ROOT / "tools", proxy=args.proxy)
    ledger_before = sum(r["agent"]["token_usage"]["total"] for r in records)
    print(f"[control] {len(frozen['formal'])} tasks, {args.max_rounds} rounds / "
          f"{args.max_total_tokens} tokens each, ledger {ledger_before}/{args.total_token_cap}")

    for meta in frozen["formal"]:
        if meta["instance_id"] in done:
            print(f"[control] skip (done): {meta['instance_id']}")
            continue
        used = sum(r["agent"]["token_usage"]["total"] for r in records)
        if used >= args.total_token_cap:
            print(f"[control] STOP: ledger {used} >= cap {args.total_token_cap}")
            break
        row = dataset.get_instance("test", meta["instance_id"], DATA_ROOT / "dataset")
        print(f"[control] running {meta['instance_id']}")
        started = time.time()
        try:
            record = run_control_task(
                instance_meta=meta, row=row, visible_argv=frozen_plan(meta["instance_id"]),
                config=config, rg_path=rg_path, data_root=DATA_ROOT,
                max_rounds=args.max_rounds, max_tokens=args.max_total_tokens,
            )
        except Exception as exc:  # noqa: BLE001 —— 单题故障留痕不中断
            record = {
                "instance_id": meta["instance_id"], "repo": meta["repo"],
                "arm": "control_no_tools",
                "agent": {"runtime_status": "INFRA_ERROR", "termination_reason":
                          f"{type(exc).__name__}: {exc}"[:300], "model_calls": 0,
                          "tool_calls": 0, "token_usage": {"total": 0}},
                "agent_seconds": round(time.time() - started, 1),
                "judge": {"outcome": "INCONCLUSIVE", "reason": "control infra error",
                          "patch_applied": False, "f2p_failures": [],
                          "p2p_regressions": [], "statuses_count": 0},
                "resolved": False,
                "artifacts_dir": str(DATA_ROOT / "runs" / meta["instance_id"]),
            }
        records.append(record)
        records_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        a = record["agent"]
        print(f"[control] -> {a['runtime_status']} tokens={a['token_usage']['total']} "
              f"resolved={record['resolved']} judge={record['judge']['outcome']} "
              f"({time.time() - started:.0f}s)")

    resolved = sum(1 for r in records if r["resolved"])
    applied = sum(1 for r in records if r["judge"].get("patch_applied"))
    tokens = sum(r["agent"]["token_usage"]["total"] for r in records)
    print("=" * 60)
    print(f"CONTROL ARM: resolved {resolved}/{len(records)}, patch applied {applied}/{len(records)}, "
          f"total tokens {tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
