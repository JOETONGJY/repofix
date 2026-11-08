"""Context 效率离线分析（P1-3，零模型 Token）。

从真实 trajectory.jsonl 统计每轮成本结构，并模拟近期完整窗口
（context_recent_full_turns=N）下的确定性节省：

- 工具输出：渲染后长度（软上限截断口径）；
- assistant 历史体量：每轮 completion_tokens（模型自身输出会在后续每轮
  被重发计费——600k 档 Trace 证明这是 prompt 的大头）；
- 近期窗口模拟：轮次 <= r-1-N 的整轮内容（assistant 消息体 + 工具输出）
  被占位（assistant ~60 token、工具输出 ~30 token/条）。

用法：
  PYTHONPATH=src python scripts/analyze_context_efficiency.py \
      --trajectory <path> [--trajectory ...] [--recent-full-turns 12] [--json out.json]
"""
import argparse
import json
from pathlib import Path

PLACEHOLDER_TOOL_CHARS = 90
CHARS_PER_TOKEN = 3  # RepoFix 保守估算口径

# 渲染后长度估算：Context 层按工具类型的软上限截断（specs/03 V0.3 系数）
_RENDER_CAPS = {"read_file": 8000, "search_code": 4000, "list_files": 4000}


def rendered_len(tool: str, raw_chars: int) -> int:
    return min(raw_chars, _RENDER_CAPS.get(tool, 2000)) + 30


def analyze(trajectory: Path, recent_full_turns: int) -> dict:
    events = [json.loads(line) for line in trajectory.read_text(encoding="utf-8").splitlines() if line.strip()]
    tool_by_call: dict[str, str] = {}
    rounds: list[dict] = []
    for e in events:
        if e["type"] == "model_responded":
            usage = (e.get("payload") or {}).get("usage") or {}
            rounds.append({
                "step": e["step"],
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "outputs": [],
            })
        elif e["type"] == "tool_started":
            p = e.get("payload") or {}
            tool_by_call[p.get("call_id", "")] = p.get("tool_name", "?")
        elif e["type"] == "tool_finished" and rounds:
            p = e.get("payload") or {}
            tool = tool_by_call.get(p.get("call_id", ""), "?")
            raw = len(str(p.get("content") or "")) + len(str(p.get("error_message") or ""))
            rounds[-1]["outputs"].append({
                "round": len(rounds), "tool": tool, "chars": rendered_len(tool, raw),
            })

    total_prompt = sum(r["prompt_tokens"] for r in rounds)
    by_tool: dict[str, int] = {}
    for r in rounds:
        for o in r["outputs"]:
            by_tool[o["tool"]] = by_tool.get(o["tool"], 0) + o["chars"]
    total_completion = sum(r["completion_tokens"] for r in rounds)

    # 近期窗口模拟：第 r 轮，轮次 <= r-1-N 的整轮被占位
    saved_tokens = 0
    for idx, r in enumerate(rounds, start=1):
        if recent_full_turns <= 0:
            break
        elided = max(0, (idx - 1) - recent_full_turns)
        if elided == 0:
            continue
        first_elided = idx - elided
        last_elided = idx - 1 - recent_full_turns
        for rnd in range(first_elided, last_elided + 1):
            if rnd - 1 < len(rounds):
                # 被占位轮次的 assistant 体量 = 该轮真实 completion（其输出不再重发）
                saved_tokens += rounds[rnd - 1]["completion_tokens"]
                for o in rounds[rnd - 1]["outputs"]:
                    saved_tokens += max(0, o["chars"] - PLACEHOLDER_TOOL_CHARS) // CHARS_PER_TOKEN

    return {
        "trajectory": str(trajectory),
        "rounds": len(rounds),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "output_chars_by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
        "simulated_recent_full_turns": recent_full_turns,
        "simulated_saved_tokens": saved_tokens,
        "simulated_saving_pct": round(100 * saved_tokens / total_prompt, 1) if total_prompt else 0.0,
        "per_round": [{"step": r["step"], "prompt_tokens": r["prompt_tokens"],
                       "completion_tokens": r["completion_tokens"],
                       "injected_output_chars": sum(o["chars"] for o in r["outputs"])}
                      for r in rounds],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Context efficiency offline analysis (no model tokens)")
    parser.add_argument("--trajectory", required=True, action="append")
    parser.add_argument("--recent-full-turns", type=int, default=12)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    results = [analyze(Path(t), args.recent_full_turns) for t in args.trajectory]
    print(f"{'trajectory':72s} {'rounds':>6s} {'prompt_tok':>10s} {'saving%':>8s}")
    for r in results:
        print(f"{Path(r['trajectory']).parent.name[:70]:72s} {r['rounds']:6d} "
              f"{r['total_prompt_tokens']:10d} {r['simulated_saving_pct']:7.1f}%")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
