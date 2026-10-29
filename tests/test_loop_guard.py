"""LoopGuard 测试：RT-10 预算触顶（参数化）、RT-11 无进展检测。

阈值唯一权威 = specs/01 RunConfig 默认值（repeat=3 / terminate=5，可配置）；
Runtime 不得硬编码另一套数字。
"""
import pytest

from repofix.contracts import RunConfig
from repofix.runtime.loop_guard import (
    BudgetTracker,
    BudgetViolation,
    GuardSignalType,
    NoProgressDetector,
)
from tests.conftest import FakeClock, make_tool_call


class TestBudgetTracker:
    def test_rt10_model_calls_budget(self):
        config = RunConfig(model_id="m", max_model_calls=2)
        tracker = BudgetTracker(config, FakeClock())
        tracker.count_model_call()
        tracker.check_model_budget()  # 未触顶
        tracker.count_model_call()
        with pytest.raises(BudgetViolation):
            tracker.check_model_budget()

    def test_rt10_tool_calls_budget(self):
        config = RunConfig(model_id="m", max_tool_calls=1)
        tracker = BudgetTracker(config, FakeClock())
        tracker.count_tool_call()
        with pytest.raises(BudgetViolation):
            tracker.check_tool_budget()

    def test_rt10_token_budget(self):
        config = RunConfig(model_id="m", max_total_tokens=100)
        tracker = BudgetTracker(config, FakeClock())
        tracker.count_model_call({"prompt_tokens": 60, "completion_tokens": 30, "total_tokens": 90})
        tracker.check_model_budget()  # 90 < 100
        tracker.count_model_call({"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10})
        with pytest.raises(BudgetViolation):
            tracker.check_model_budget()

    def test_rt10_duration_budget_parameterized(self):
        clock = FakeClock()
        config = RunConfig(model_id="m", max_duration_seconds=100)
        tracker = BudgetTracker(config, clock)
        tracker.check_model_budget()
        clock.advance(100.0)
        with pytest.raises(BudgetViolation):
            tracker.check_model_budget()
        with pytest.raises(BudgetViolation):
            tracker.check_tool_budget()

    def test_missing_usage_not_fabricated(self):
        """Usage 缺失：标记未知，不虚构 Token。"""
        config = RunConfig(model_id="m", max_total_tokens=10)
        tracker = BudgetTracker(config, FakeClock())
        tracker.count_model_call(None)
        tracker.count_model_call({"prompt_tokens": 3})  # 无 total_tokens 字段
        assert tracker.total_tokens == 0
        tracker.check_model_budget()  # 不因缺失而触顶


class TestNoProgressDetector:
    def test_rt11_warning_at_repeat_then_terminate(self):
        detector = NoProgressDetector(repeat_threshold=3, terminate_threshold=5)
        call = make_tool_call("c", "read_file", path="same.py")
        assert detector.observe_batch([call]).type is GuardSignalType.NONE
        assert detector.observe_batch([call]).type is GuardSignalType.NONE
        signal = detector.observe_batch([call])
        assert signal.type is GuardSignalType.WARNING  # 第 3 次：注入一次警告
        assert detector.observe_batch([call]).type is GuardSignalType.NONE  # 第 4 次：无第二次警告
        signal = detector.observe_batch([call])
        assert signal.type is GuardSignalType.TERMINATE  # 第 5 次：终止

    def test_rt11_different_arguments_reset_streak(self):
        detector = NoProgressDetector(repeat_threshold=3, terminate_threshold=5)
        detector.observe_batch([make_tool_call("c1", "read_file", path="a.py")])
        detector.observe_batch([make_tool_call("c2", "read_file", path="a.py")])
        # 参数相同（call_id 不参与哈希）→ 第 3 次警告
        signal = detector.observe_batch([make_tool_call("c3", "read_file", path="a.py")])
        assert signal.type is GuardSignalType.WARNING
        # 参数变化 → 重新计数
        signal = detector.observe_batch([make_tool_call("c4", "read_file", path="b.py")])
        assert signal.type is GuardSignalType.NONE

    def test_rt11_target_changed_resets(self):
        """目标文件已变化 → 调用产生了真实效果，重置计数。"""
        detector = NoProgressDetector(
            repeat_threshold=3,
            terminate_threshold=5,
            target_unchanged=lambda call: False,
        )
        for _ in range(5):
            signal = detector.observe_batch([make_tool_call("c", "apply_patch", path="a.py", patch="x")])
        assert signal.type is GuardSignalType.NONE

    def test_thresholds_authority_is_run_config(self):
        """Runtime 从 RunConfig 读取阈值，不硬编码（repeat=2 / terminate=3 也必须生效）。"""
        config = RunConfig(model_id="m", no_progress_repeat_threshold=2, no_progress_terminate_threshold=3)
        detector = NoProgressDetector(
            repeat_threshold=config.no_progress_repeat_threshold,
            terminate_threshold=config.no_progress_terminate_threshold,
        )
        call = make_tool_call("c", "list_files")
        detector.observe_batch([call])
        assert detector.observe_batch([call]).type is GuardSignalType.WARNING
        assert detector.observe_batch([call]).type is GuardSignalType.TERMINATE

    def test_skipped_calls_not_observed_by_runner_semantics(self):
        """观察的是实际执行的调用；空批次返回 NONE。"""
        detector = NoProgressDetector(repeat_threshold=3, terminate_threshold=5)
        assert detector.observe_batch([]).type is GuardSignalType.NONE
