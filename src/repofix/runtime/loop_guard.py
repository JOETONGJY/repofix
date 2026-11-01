"""LoopGuard：硬性预算 + 无进展检测（specs/02 停止条件）。

阈值唯一权威 = specs/01 RunConfig 默认值（repeat=3 / terminate=5，可配置），
Runtime 不得硬编码另一套数字。
- 预算触顶（模型 / 工具次数、Token、墙钟）→ BUDGET_EXCEEDED；
- 无进展（同工具 + 规范化参数哈希相同 + 目标文件未变化）连续 repeat_threshold 次
  → 注入一次 NO_PROGRESS_WARNING；累计 terminate_threshold 次 → FAILED（TOOL_SYSTEM, no_progress）。
"""
import hashlib
import json
from collections.abc import Callable
from enum import Enum
from typing import Protocol

from repofix.contracts import RunConfig, ToolCall


class Clock(Protocol):
    """可注入时钟：测试用可控时钟，生产用单调/系统时钟。"""

    def now(self) -> float: ...


class SystemClock:
    """生产时钟（墙钟，unix 秒）。"""

    def now(self) -> float:
        import time

        return time.time()


def normalized_call_key(call: ToolCall) -> str:
    """规范化参数哈希：sha256(tool_name + json.dumps(args, sort_keys=True))。"""
    return hashlib.sha256(
        (call.tool_name + json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()


class BudgetViolation(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class BudgetTracker:
    """预算计数（specs/02 V0.2）。

    - 模型预算：每次真实 API 请求尝试（含失败与重试）在发出前计数；Usage 只从成功响应记录。
    - 工具预算：计数对象 = 进入调度处理的工具请求数（含校验拒绝/权限拒绝/SKIPPED；
      控制动作不计入）；实际执行次数另行记录，防止非法调用绕过预算。
    - 墙钟用注入 Clock。
    """

    def __init__(self, config: RunConfig, clock: Clock):
        self._config = config
        self._clock = clock
        self._started_at = clock.now()
        self.model_calls = 0  # 模型 API 请求尝试数（含失败）
        self.tool_calls = 0  # 工具请求数（预算口径）
        self.tool_executions = 0  # 实际执行次数（效率统计）
        self.total_tokens = 0

    def tool_calls_remaining(self) -> int:
        return max(0, self._config.max_tool_calls - self.tool_calls)

    def count_tool_call(self) -> None:
        """计入一个工具请求（无论后续是否被拒绝或跳过）。"""
        self.tool_calls += 1

    def count_tool_execution(self) -> None:
        """计入一次实际执行（仅真正 dispatched 的调用）。"""
        self.tool_executions += 1

    def count_model_call(self, usage: dict | None = None) -> None:
        """计入一次模型 API 请求尝试。usage 仅在成功响应时传入。"""
        self.model_calls += 1
        if usage:
            self._accumulate_tokens(usage)

    def record_usage(self, usage: dict | None = None) -> None:
        """成功响应后记录 Token 用量（与尝试计数分离；缺失时标记未知，不虚构）。"""
        if usage:
            self._accumulate_tokens(usage)

    def check_model_budget(self) -> None:
        """发起模型调用前检查；触顶抛 BudgetViolation（Runner 转 BUDGET_EXCEEDED）。"""
        if self.model_calls >= self._config.max_model_calls:
            raise BudgetViolation("max_model_calls")
        if self._config.max_total_tokens is not None and self.total_tokens >= self._config.max_total_tokens:
            raise BudgetViolation("max_total_tokens")
        if self._wall_clock_exceeded():
            raise BudgetViolation("max_duration_seconds")

    def check_tool_budget(self) -> None:
        if self.tool_calls >= self._config.max_tool_calls:
            raise BudgetViolation("max_tool_calls")
        if self._wall_clock_exceeded():
            raise BudgetViolation("max_duration_seconds")

    def elapsed_seconds(self) -> float:
        return self._clock.now() - self._started_at

    def _wall_clock_exceeded(self) -> bool:
        return self.elapsed_seconds() >= self._config.max_duration_seconds

    def _accumulate_tokens(self, usage: dict) -> None:
        if isinstance(usage.get("total_tokens"), int):
            self.total_tokens += usage["total_tokens"]


class GuardSignalType(str, Enum):
    NONE = "none"
    WARNING = "warning"
    TERMINATE = "terminate"


class GuardSignal:
    def __init__(self, signal_type: GuardSignalType, key: str | None = None, repeats: int = 0):
        self.type = signal_type
        self.key = key
        self.repeats = repeats


class NoProgressDetector:
    """无进展检测：观察本批次实际执行的调用（SKIPPED 不计）。"""

    def __init__(
        self,
        repeat_threshold: int,
        terminate_threshold: int,
        target_unchanged: Callable[[ToolCall], bool] | None = None,
    ):
        self._repeat_threshold = repeat_threshold
        self._terminate_threshold = terminate_threshold
        # M1 默认"目标文件未变化"恒为 True（plan.md：M1 默认视为未变化，M2 接真实文件状态）
        self._target_unchanged = target_unchanged or (lambda call: True)
        self._streaks: dict[str, int] = {}
        self._warned: set[str] = set()

    def observe_batch(self, executed_calls: list[ToolCall]) -> GuardSignal:
        terminate: GuardSignal | None = None
        warning: GuardSignal | None = None
        for call in executed_calls:
            key = normalized_call_key(call)
            if self._target_unchanged(call):
                self._streaks[key] = self._streaks.get(key, 0) + 1
            else:
                # 目标文件已变化：该调用产生了真实效果，重置计数
                self._streaks[key] = 0
                self._warned.discard(key)
            repeats = self._streaks[key]
            if repeats >= self._terminate_threshold:
                terminate = GuardSignal(GuardSignalType.TERMINATE, key, repeats)
            elif repeats >= self._repeat_threshold and key not in self._warned:
                self._warned.add(key)
                warning = GuardSignal(GuardSignalType.WARNING, key, repeats)
        return terminate or warning or GuardSignal(GuardSignalType.NONE)
