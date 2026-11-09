"""P1-2 回归：Context 效率离线分析脚本的近期窗口索引。

旧实现 `first_elided = idx - elided` / `last_elided = idx - 1 - N` 把窗口首尾都从
右侧算起：第 8 轮请求（N=6）循环为空、第 14 轮只算第 7 轮，窗口之外的 1..N 轮
历史从未被计入——离线分析系统性低估了 Context Recency 的节省比例，并直接污染
了文档里"N=6 优于 N=12"的量化依据。

这里用合成 trajectory 锁定正确口径，并与独立的闭式实现交叉验证：
    第 i 轮的成本会被其后第 i+N+1..R 次请求逐轮重发，
    saved = Σ_i cost(i) · max(0, R − i − N)。
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "analyze_context_efficiency.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("analyze_context_efficiency", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analysis = _load_script()

# 合成轮次成本：completion 100 token + 一条 300 字符 read_file 输出。
# 渲染后长度 = min(300, 8000) + 30 = 330；占位后节省 (330 − 90) // 3 = 80
# → 每轮成本 180 token。
COMPLETION_TOKENS = 100
TOOL_CONTENT = "x" * 300
ROUND_COST = COMPLETION_TOKENS + (len(TOOL_CONTENT) + 30 - 90) // 3
PROMPT_TOKENS_PER_ROUND = 1000


def write_trajectory(path: Path, rounds: int) -> Path:
    """写一份结构正确的 trajectory.jsonl（model_responded 在前，工具事件在后）。"""
    events = []
    for i in range(1, rounds + 1):
        call_id = f"c{i}"
        events.append({"type": "model_responded", "step": i,
                       "payload": {"usage": {"prompt_tokens": PROMPT_TOKENS_PER_ROUND,
                                             "completion_tokens": COMPLETION_TOKENS}}})
        events.append({"type": "tool_started", "step": i,
                       "payload": {"call_id": call_id, "tool_name": "read_file"}})
        events.append({"type": "tool_finished", "step": i,
                       "payload": {"call_id": call_id, "content": TOOL_CONTENT}})
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def expected_saved(rounds: int, recent_full_turns: int) -> int:
    """独立实现（闭式公式），与脚本的逐请求循环互为交叉验证。"""
    return sum(ROUND_COST * max(0, rounds - i - recent_full_turns)
               for i in range(1, rounds + 1))


class TestElidedRounds:
    def test_round_8_elides_only_round_1(self):
        """保留最近 6 轮：第 8 轮请求时之前有 7 轮，只压缩第 1 轮。"""
        assert analysis.elided_rounds(8, 6) == [1]

    def test_round_14_elides_rounds_1_to_7(self):
        """第 14 轮请求时之前有 13 轮，压缩第 1–7 轮（旧实现只算第 7 轮）。"""
        assert analysis.elided_rounds(14, 6) == [1, 2, 3, 4, 5, 6, 7]

    def test_window_not_yet_full(self):
        assert analysis.elided_rounds(7, 6) == []
        assert analysis.elided_rounds(6, 6) == []

    def test_disabled_window_elides_nothing(self):
        assert analysis.elided_rounds(50, 0) == []

    def test_never_elides_kept_window(self):
        for idx in range(1, 40):
            elided = analysis.elided_rounds(idx, 6)
            kept = list(range(idx - 6, idx)) if idx > 6 else list(range(1, idx))
            assert not set(elided) & set(kept)
            assert elided == (list(range(1, idx - 6)) if idx > 6 else [])


class TestSimulatedSaving:
    def test_window_fills_at_round_8(self, tmp_path):
        """旧实现在这里算出 0（循环为空）——第 1 轮必须计入。"""
        path = write_trajectory(tmp_path / "t8.jsonl", 8)
        result = analysis.analyze(path, recent_full_turns=6)
        assert result["simulated_saved_tokens"] == ROUND_COST
        assert result["simulated_saved_tokens"] > 0

    def test_round_14_elides_seven_rounds(self, tmp_path):
        path = write_trajectory(tmp_path / "t14.jsonl", 14)
        result = analysis.analyze(path, recent_full_turns=6)
        assert result["simulated_saved_tokens"] == ROUND_COST * 28  # 7+6+…+1

    @pytest.mark.parametrize(("rounds", "n"), [(21, 6), (21, 12), (46, 6), (46, 12), (8, 6), (14, 6)])
    def test_matches_independent_closed_form(self, tmp_path, rounds, n):
        path = write_trajectory(tmp_path / f"t{rounds}_{n}.jsonl", rounds)
        result = analysis.analyze(path, recent_full_turns=n)
        assert result["simulated_saved_tokens"] == expected_saved(rounds, n)
        total_prompt = rounds * PROMPT_TOKENS_PER_ROUND
        assert result["simulated_saving_pct"] == round(
            100 * expected_saved(rounds, n) / total_prompt, 1)

    def test_disabled_window_saves_nothing(self, tmp_path):
        path = write_trajectory(tmp_path / "t21.jsonl", 21)
        result = analysis.analyze(path, recent_full_turns=0)
        assert result["simulated_saved_tokens"] == 0
        assert result["simulated_saving_pct"] == 0.0

    def test_smaller_window_saves_more(self, tmp_path):
        """N 越小压缩越早、节省越多——A/B 以 N=6 为主配置的依据。"""
        path = write_trajectory(tmp_path / "t46.jsonl", 46)
        saved_6 = analysis.analyze(path, recent_full_turns=6)["simulated_saved_tokens"]
        saved_12 = analysis.analyze(path, recent_full_turns=12)["simulated_saved_tokens"]
        assert saved_6 > saved_12 > 0
