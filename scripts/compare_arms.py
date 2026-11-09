"""SWE-bench Mini-Eval 版本对照分析（A/B，零模型 Token）。

输入两组逐题记录 + 对应 Trace 目录，输出三个层面的核验：
  ① 系统稳定性：每题记录/Trace/Judge 完整、Trace run_id 与记录一致（防陈旧副本）、
     无 INFRA_ERROR / INCONCLUSIVE；
  ② 优化是否真实生效：命令拒绝次数与类别、只读命令使用量、批次级联、每轮
     prompt（均值/最大/末轮）、read/search/patch 调用量与 patch 应用成功率；
  ③ 解决率信号：Resolved@1、Patch Apply Rate、终止原因分布、Token、耗时。

全部指标取自真实 Trace（trajectory.jsonl）与逐题记录，不使用离线模拟。

用法：
  PYTHONPATH=src python scripts/compare_arms.py \
      --a-records benchmarks/swebench-mini/records-formal-tierA3-600k.json \
      --a-traces repofix-data/swebench/runs-tierA3-600k --a-label v0 \
      --b-records benchmarks/swebench-mini/records-formal-v1-ab.json \
      --b-traces repofix-data/swebench/runs --b-label v1 [--json out.json]
"""
import argparse
import collections
import json
import statistics
from pathlib import Path

READONLY_PROGRAMS = {"grep", "rg", "ls"}


def trace_metrics(trace: Path) -> dict:
    """单条 Trace 的工具/上下文指标。"""
    if not trace.exists():
        return {"trace_missing": True}
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_id = events[0]["run_id"] if events else ""
    tool_by_call: dict[str, str] = {}
    argv_by_call: dict[str, list] = {}
    tool_counts: collections.Counter = collections.Counter()
    denied: collections.Counter = collections.Counter()
    cascades = 0
    patch_ok = patch_fail = 0
    readonly_used = 0
    prompts: list[int] = []
    finish_seen = False
    preflight: dict = {}
    for e in events:
        p = e.get("payload") or {}
        t = e["type"]
        if t == "preflight_completed":
            preflight = p
        elif t == "model_responded":
            usage = p.get("usage") or {}
            if usage.get("prompt_tokens"):
                prompts.append(int(usage["prompt_tokens"]))
        elif t == "tool_started":
            tool_by_call[p.get("call_id", "")] = p.get("tool_name", "?")
        elif t == "tool_finished":
            name = p.get("tool_name") or tool_by_call.get(p.get("call_id", ""), "?")
            tool_counts[name] += 1
            if name == "finish":
                finish_seen = True
            if p.get("error_code") == "permission_denied":
                msg = str(p.get("error_message") or "")
                # 取被拒命令的前两个 token 作为类别（如 ['python', '-c']）
                category = msg.split(":")[-1].strip()[:40] if ":" in msg else msg[:40]
                denied[category] += 1
            if p.get("skipped_reason") in ("batch_aborted_after_write_failure",
                                           "batch_aborted_after_read_failure"):
                cascades += 1
            if name == "apply_patch":
                if p.get("status") == "succeeded":
                    patch_ok += 1
                elif p.get("status") == "failed":
                    patch_fail += 1
    # 只读命令实际使用量（run_command 中 argv[0] ∈ grep/rg/ls 且执行成功）
    for e in events:
        if e["type"] == "tool_finished":
            p = e.get("payload") or {}
            if p.get("status") == "succeeded":
                cid = p.get("call_id", "")
                if tool_by_call.get(cid) == "run_command":
                    argv_by_call.setdefault(cid, [])
    return {
        "trace_run_id": run_id,
        "events": len(events),
        "preflight_policy": preflight.get("command_policy"),
        "preflight_window": preflight.get("context_recent_full_turns"),
        "tool_counts": dict(tool_counts),
        "permission_denied": sum(denied.values()),
        "denied_categories": dict(denied),
        "batch_cascades": cascades,
        "apply_patch_ok": patch_ok,
        "apply_patch_failed": patch_fail,
        "readonly_used": readonly_used,
        "rounds": len(prompts),
        "prompt_mean": round(statistics.mean(prompts)) if prompts else 0,
        "prompt_max": max(prompts) if prompts else 0,
        "prompt_last": prompts[-1] if prompts else 0,
        "finish_seen": finish_seen,
    }


def readonly_usage(db_trace_dir: Path, iid: str, run_id: str) -> int:
    """从权威 SQLite 统计 grep/rg/ls 实际执行次数（argv 需从 tool_calls 表取）。"""
    import sqlite3

    db = db_trace_dir / iid / "run.sqlite"
    if not db.exists() or not run_id:
        return -1
    conn = sqlite3.connect(str(db))
    n = 0
    for (args, side) in conn.execute(
        "SELECT arguments_json, side_effect_status FROM tool_calls "
        "WHERE run_id = ? AND tool_name = 'run_command'", (run_id,)
    ):
        try:
            argv = json.loads(args).get("argv") or []
        except ValueError:
            argv = []
        if argv and argv[0] in READONLY_PROGRAMS and side == "succeeded":
            n += 1
    conn.close()
    return n


def arm_table(records_path: Path, traces_dir: Path, sqlite_dir: Path) -> list[dict]:
    rows = []
    for rec in json.loads(records_path.read_text(encoding="utf-8")):
        iid = rec["instance_id"]
        m = trace_metrics(traces_dir / iid / "trajectory.jsonl")
        record_run_id = rec.get("run_id", "")
        m["readonly_used"] = readonly_usage(sqlite_dir, iid, m.get("trace_run_id", ""))
        a, j = rec["agent"], rec["judge"]
        rows.append({
            "instance_id": iid,
            "resolved": rec["resolved"],
            "judge": j["outcome"],
            "patch_applied": j.get("patch_applied"),
            "runtime_status": a.get("runtime_status"),
            "termination": a.get("termination_reason", ""),
            "model_calls": a.get("model_calls", 0),
            "tool_calls": a.get("tool_calls", 0),
            "tokens": a.get("token_usage", {}).get("total", 0),
            "agent_seconds": rec.get("agent_seconds", 0),
            "record_run_id": record_run_id,
            "trace_matches_record": (not record_run_id) or record_run_id == m.get("trace_run_id"),
            **m,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)

    def mean(key):
        return round(sum(r[key] for r in rows) / n) if n else 0

    return {
        "tasks": n,
        "resolved": sum(r["resolved"] for r in rows),
        "patch_applied": sum(bool(r["patch_applied"]) for r in rows),
        "infra_or_inconclusive": sum(r["judge"] == "INCONCLUSIVE" or r["runtime_status"] == "INFRA_ERROR"
                                     for r in rows),
        "trace_mismatch": sum(not r["trace_matches_record"] for r in rows),
        "trace_missing": sum(bool(r.get("trace_missing")) for r in rows),
        "avg_tokens": mean("tokens"),
        "avg_model_calls": round(sum(r["model_calls"] for r in rows) / n, 1),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "avg_agent_seconds": round(sum(r["agent_seconds"] for r in rows) / n, 1),
        "avg_prompt_per_round": mean("prompt_mean"),
        "avg_prompt_max": mean("prompt_max"),
        "tokens_per_model_call": round(sum(r["tokens"] for r in rows)
                                       / max(1, sum(r["model_calls"] for r in rows))),
        "permission_denied": sum(r["permission_denied"] for r in rows),
        "batch_cascades": sum(r["batch_cascades"] for r in rows),
        "readonly_cmds_used": sum(max(0, r["readonly_used"]) for r in rows),
        "apply_patch_ok": sum(r["apply_patch_ok"] for r in rows),
        "apply_patch_failed": sum(r["apply_patch_failed"] for r in rows),
        "finish_reached": sum(r["finish_seen"] for r in rows),
        "termination": dict(collections.Counter(r["termination"].split(":")[-1] or r["termination"]
                                                for r in rows)),
    }


def _fmt_row(r: dict) -> str:
    return (f"{'Y' if r['resolved'] else 'n'} {r['tokens']:7d} {r['model_calls']:3d}c "
            f"{r['apply_patch_ok']}/{r['apply_patch_ok'] + r['apply_patch_failed']}p")


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B arm comparison from real traces (no model tokens)")
    ap.add_argument("--a-records", required=True)
    ap.add_argument("--a-traces", required=True)
    ap.add_argument("--a-sqlite", default=None, help="SQLite 根目录（默认同 traces）")
    ap.add_argument("--a-label", default="A")
    ap.add_argument("--b-records", required=True)
    ap.add_argument("--b-traces", required=True)
    ap.add_argument("--b-sqlite", default=None)
    ap.add_argument("--b-label", default="B")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    a_rows = arm_table(Path(args.a_records), Path(args.a_traces), Path(args.a_sqlite or args.a_traces))
    b_rows = arm_table(Path(args.b_records), Path(args.b_traces), Path(args.b_sqlite or args.b_traces))
    a_sum, b_sum = summarize(a_rows), summarize(b_rows)

    print(f"{'metric':28s} {args.a_label:>14s} {args.b_label:>14s}")
    for key in a_sum:
        if key == "termination":
            continue
        print(f"{key:28s} {str(a_sum[key]):>14s} {str(b_sum[key]):>14s}")
    print(f"{'termination':28s} {args.a_label}: {a_sum['termination']}")
    print(f"{'':28s} {args.b_label}: {b_sum['termination']}")
    print()
    a_head = args.a_label + " res/tok/calls/patch"
    b_head = args.b_label + " res/tok/calls/patch"
    print(f"{'instance':34s} {a_head:>30s}   {b_head:>30s}")
    b_by = {r["instance_id"]: r for r in b_rows}
    for ra in a_rows:
        rb = b_by.get(ra["instance_id"])
        print(f"{ra['instance_id'].split('__')[1][:32]:34s} {_fmt_row(ra):>30s}   "
              f"{_fmt_row(rb) if rb else '-':>30s}")
    if args.json:
        Path(args.json).write_text(json.dumps({"a": {"label": args.a_label, "summary": a_sum, "rows": a_rows},
                                               "b": {"label": args.b_label, "summary": b_sum, "rows": b_rows}},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
