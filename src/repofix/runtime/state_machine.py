"""RunStatus 状态机（specs/02 状态转换表，代码驱动；非法转换抛异常）。

只有 Agent Runtime 可以发起任务生命周期转换（design/00 跨模块控制权分配）。
WAITING_APPROVAL 为保留扩展状态：MVP 未启用，不在活动转换表内。
"""
from repofix.contracts import RunStatus

_ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.PREPARING},
    # Preflight 失败 → FAILED（fail_layer=WORKSPACE）
    RunStatus.PREPARING: {RunStatus.RUNNING, RunStatus.FAILED},
    # finish → VERIFYING；不可恢复错误 / 取消 / 预算触顶 → 异常终态
    RunStatus.RUNNING: {
        RunStatus.VERIFYING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BUDGET_EXCEEDED,
    },
    # 验证通过 → REVIEWING 或 COMPLETED（review_enabled=False）；断言失败 → RUNNING
    RunStatus.VERIFYING: {RunStatus.REVIEWING, RunStatus.COMPLETED, RunStatus.RUNNING},
    # APPROVED → COMPLETED；FINDINGS → RUNNING（修正后再次 VERIFYING，不二次 Review）
    RunStatus.REVIEWING: {RunStatus.COMPLETED, RunStatus.RUNNING},
}

TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BUDGET_EXCEEDED,
    }
)


class InvalidTransitionError(ValueError):
    """非法状态转换：状态机直接抛异常，由 Runtime 写事件并终止。"""

    def __init__(self, current: RunStatus, target: RunStatus):
        self.current = current
        self.target = target
        super().__init__(f"Illegal RunStatus transition: {current.value} -> {target.value}")


class StateMachine:
    """当前 Run 的状态持有者与唯一转换入口。"""

    def __init__(self, initial: RunStatus = RunStatus.CREATED):
        self._status = initial

    @property
    def status(self) -> RunStatus:
        return self._status

    def can_transition(self, target: RunStatus) -> bool:
        if self._status in TERMINAL_STATUSES:
            return False
        return target in _ALLOWED_TRANSITIONS.get(self._status, set())

    def transition(self, target: RunStatus) -> RunStatus:
        if self._status in TERMINAL_STATUSES:
            raise InvalidTransitionError(self._status, target)
        if target not in _ALLOWED_TRANSITIONS.get(self._status, set()):
            raise InvalidTransitionError(self._status, target)
        self._status = target
        return self._status
