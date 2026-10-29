"""RT-09：状态转换表中每条合法转换可达、非法转换抛异常。"""
import pytest

from repofix.contracts import RunStatus
from repofix.runtime.state_machine import TERMINAL_STATUSES, InvalidTransitionError, StateMachine

LEGAL_TRANSITIONS = [
    (RunStatus.CREATED, RunStatus.PREPARING),
    (RunStatus.PREPARING, RunStatus.RUNNING),
    (RunStatus.PREPARING, RunStatus.FAILED),  # Preflight 失败
    (RunStatus.RUNNING, RunStatus.VERIFYING),  # 有效 finish
    (RunStatus.RUNNING, RunStatus.FAILED),  # 不可恢复错误
    (RunStatus.RUNNING, RunStatus.CANCELLED),  # 用户取消
    (RunStatus.RUNNING, RunStatus.BUDGET_EXCEEDED),  # 预算触顶
    (RunStatus.VERIFYING, RunStatus.REVIEWING),  # 验证通过且 review_enabled
    (RunStatus.VERIFYING, RunStatus.COMPLETED),  # 验证通过（Baseline）/ 无法验证
    (RunStatus.VERIFYING, RunStatus.RUNNING),  # 断言失败反馈回流
    (RunStatus.REVIEWING, RunStatus.COMPLETED),  # APPROVED / INCONCLUSIVE
    (RunStatus.REVIEWING, RunStatus.RUNNING),  # FINDINGS 反馈修正
]

ILLEGAL_TRANSITIONS = [
    (RunStatus.CREATED, RunStatus.RUNNING),  # 必须经过 PREPARING
    (RunStatus.CREATED, RunStatus.COMPLETED),
    (RunStatus.RUNNING, RunStatus.REVIEWING),  # 必须经过 VERIFYING
    (RunStatus.RUNNING, RunStatus.CREATED),
    (RunStatus.VERIFYING, RunStatus.FAILED),  # 转换表未定义该路径
    (RunStatus.VERIFYING, RunStatus.CANCELLED),
    (RunStatus.REVIEWING, RunStatus.VERIFYING),  # 第二次 VERIFYING 不再触发 Review
    (RunStatus.COMPLETED, RunStatus.RUNNING),  # 终态不可离开
    (RunStatus.FAILED, RunStatus.RUNNING),
    (RunStatus.CANCELLED, RunStatus.COMPLETED),
    (RunStatus.BUDGET_EXCEEDED, RunStatus.RUNNING),
    (RunStatus.WAITING_APPROVAL, RunStatus.RUNNING),  # MVP 未启用的保留状态
]


class TestStateMachine:
    @pytest.mark.parametrize(("initial", "target"), LEGAL_TRANSITIONS)
    def test_legal_transition_reachable(self, initial, target):
        machine = StateMachine(initial=initial)
        assert machine.transition(target) is target
        assert machine.status is target

    @pytest.mark.parametrize(("initial", "target"), ILLEGAL_TRANSITIONS)
    def test_illegal_transition_raises(self, initial, target):
        machine = StateMachine(initial=initial)
        with pytest.raises(InvalidTransitionError):
            machine.transition(target)
        assert machine.status is initial  # 失败转换不改变状态

    def test_terminal_statuses_frozen(self):
        assert TERMINAL_STATUSES == frozenset(
            {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.BUDGET_EXCEEDED,
            }
        )

    def test_waiting_approval_not_in_active_table(self):
        """WAITING_APPROVAL 是保留扩展状态，MVP 不进入活动状态集合。"""
        machine = StateMachine(initial=RunStatus.RUNNING)
        assert not machine.can_transition(RunStatus.WAITING_APPROVAL)
