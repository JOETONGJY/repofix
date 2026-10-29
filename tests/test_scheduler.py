"""ToolBatchScheduler 协议测试：RT-03/04/05/06 + 意图先行 + 配对完整。"""

from repofix.contracts import (
    AgentActor,
    ExecutionStatus,
    RunConfig,
    SideEffectStatus,
    ToolErrorCode,
    ToolResult,
)
from repofix.runtime.loop_guard import BudgetTracker
from repofix.runtime.scheduler import ToolBatchScheduler
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from tests.conftest import FakeClock, FakeToolExecutor, make_tool_call

RUN_ID = "run_sched"


def make_scheduler(registry, executor, store, config=None):
    budget = BudgetTracker(config or RunConfig(model_id="m"), FakeClock())
    dispatcher = ToolDispatcher(registry, executor)
    scheduler = ToolBatchScheduler(
        run_id=RUN_ID, dispatcher=dispatcher, registry=registry, store=store, budget=budget
    )
    return scheduler, budget


class TestNormalBatch:
    def test_rt03_n_calls_execute_in_order_with_pairing(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, budget = make_scheduler(registry, executor, store)
        calls = [
            make_tool_call("c1", "search_code", pattern="def fix"),
            make_tool_call("c2", "read_file", path="auth.py"),
            make_tool_call("c3", "list_files", path="."),
        ]
        outcome = scheduler.execute_batch(calls, AgentActor.MAIN, step=1, decision_id="d1", ts=1.0)
        assert [r.call_id for r in outcome.results] == ["c1", "c2", "c3"]
        assert all(r.status is ExecutionStatus.SUCCEEDED for r in outcome.results)
        assert executor.executed() == ["search_code", "read_file", "list_files"]
        assert budget.tool_calls == 3
        rows = store.list_tool_calls(RUN_ID)
        assert [row["call_id"] for row in rows] == ["c1", "c2", "c3"]
        assert all(row["side_effect_status"] == SideEffectStatus.SUCCEEDED.value for row in rows)

    def test_intent_persisted_before_execution(self, registry):
        """意图先行：执行器被调用的瞬间，tool_calls 行已处于 STARTED。"""
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()

        def observer(tool_name, arguments):
            rows = [r for r in store.list_tool_calls(RUN_ID) if r["tool_name"] == tool_name]
            assert rows and rows[-1]["side_effect_status"] == SideEffectStatus.STARTED.value

        executor.observers.append(observer)
        scheduler, _ = make_scheduler(registry, executor, store)
        outcome = scheduler.execute_batch(
            [make_tool_call("c1", "apply_patch", path="a.py", patch="---")],
            AgentActor.MAIN,
            step=1,
            decision_id="d1",
            ts=1.0,
        )
        assert outcome.results[0].status is ExecutionStatus.SUCCEEDED


class TestBatchAbort:
    def test_rt04_write_failure_aborts_rest_of_batch(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor(
            results={
                "apply_patch": ToolResult(
                    call_id="pending", status=ExecutionStatus.FAILED, error_code=ToolErrorCode.CONFLICT
                )
            }
        )
        scheduler, _ = make_scheduler(registry, executor, store)
        calls = [
            make_tool_call("c1", "read_file", path="a.py"),
            make_tool_call("c2", "apply_patch", path="a.py", patch="x"),
            make_tool_call("c3", "read_file", path="b.py"),
        ]
        outcome = scheduler.execute_batch(calls, AgentActor.MAIN, step=1, decision_id="d1", ts=1.0)
        assert outcome.results[0].status is ExecutionStatus.SUCCEEDED
        assert outcome.results[1].status is ExecutionStatus.FAILED
        assert outcome.results[2].status is ExecutionStatus.SKIPPED
        assert outcome.results[2].skipped_reason == "batch_aborted_after_write_failure"
        assert executor.executed() == ["read_file", "apply_patch"]  # 第三个未执行

    def test_rt05_read_failure_skips_subsequent_write(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor(
            results={
                "search_code": ToolResult(
                    call_id="pending",
                    status=ExecutionStatus.FAILED,
                    error_code=ToolErrorCode.EXECUTION_ERROR,
                )
            }
        )
        scheduler, _ = make_scheduler(registry, executor, store)
        calls = [
            make_tool_call("c1", "search_code", pattern="x"),
            make_tool_call("c2", "read_file", path="ok.py"),  # 只读失败后只读可继续
            make_tool_call("c3", "apply_patch", path="a.py", patch="x"),
        ]
        outcome = scheduler.execute_batch(calls, AgentActor.MAIN, step=1, decision_id="d1", ts=1.0)
        assert outcome.results[0].status is ExecutionStatus.FAILED
        assert outcome.results[1].status is ExecutionStatus.SUCCEEDED  # 只读继续
        assert outcome.results[2].status is ExecutionStatus.SKIPPED
        assert outcome.results[2].skipped_reason == "batch_aborted_after_read_failure"
        assert executor.executed() == ["search_code", "read_file"]

    def test_validation_failure_flows_back_without_execution(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, store)
        outcome = scheduler.execute_batch(
            [make_tool_call("c1", "read_file")],  # 缺必填 path
            AgentActor.MAIN,
            step=1,
            decision_id="d1",
            ts=1.0,
        )
        assert outcome.results[0].status is ExecutionStatus.FAILED
        assert outcome.results[0].error_code is ToolErrorCode.INVALID_ARGUMENT
        assert executor.executed() == []

    def test_unknown_tool_returns_tool_not_found_with_candidates(self, registry):
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, SQLiteEventStore(":memory:"))
        outcome = scheduler.execute_batch(
            [make_tool_call("c1", "modify_database")],
            AgentActor.MAIN,
            step=1,
            decision_id="d1",
            ts=1.0,
        )
        result = outcome.results[0]
        assert result.error_code is ToolErrorCode.TOOL_NOT_FOUND
        assert "search_code" in result.error_message  # 附可用工具列表


class TestFinishRules:
    def test_rt06_mixed_finish_rejects_whole_batch(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, store)
        calls = [
            make_tool_call("c1", "read_file", path="a.py"),  # 只读也无任何副作用
            make_tool_call("c2", "apply_patch", path="a.py", patch="x"),
            make_tool_call("c3", "finish", summary="done"),
        ]
        outcome = scheduler.execute_batch(calls, AgentActor.MAIN, step=1, decision_id="d1", ts=1.0)
        # 无任何执行
        assert executor.executed() == []
        # finish 得 INVALID_ARGUMENT（提示单独提交）
        finish_result = outcome.results[2]
        assert finish_result.error_code is ToolErrorCode.INVALID_ARGUMENT
        assert "alone" in finish_result.error_message
        # 其余 SKIPPED（batch_rejected_finish_mixed），配对完整
        assert outcome.results[0].status is ExecutionStatus.SKIPPED
        assert outcome.results[0].skipped_reason == "batch_rejected_finish_mixed"
        assert outcome.results[1].status is ExecutionStatus.SKIPPED
        assert outcome.results[1].skipped_reason == "batch_rejected_finish_mixed"
        assert [r.call_id for r in outcome.results] == ["c1", "c2", "c3"]
        # 持久化：全部 SKIPPED，无 STARTED
        for row in store.list_tool_calls(RUN_ID):
            assert row["side_effect_status"] == SideEffectStatus.SKIPPED.value

    def test_finish_alone_returns_control_to_runtime(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, store)
        outcome = scheduler.execute_batch(
            [make_tool_call("f1", "finish", summary="done")],
            AgentActor.MAIN,
            step=1,
            decision_id="d2",
            ts=2.0,
        )
        assert outcome.control_tool == "finish"
        assert outcome.control_call is not None and outcome.control_call.call_id == "f1"
        assert outcome.results == []  # 不进 ToolExecutor
        assert executor.executed() == []

    def test_next_turn_can_resubmit_finish_alone(self, registry):
        """RT-06 尾段：整批拒绝后，下一轮单独 finish 可被接受。"""
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, store)
        scheduler.execute_batch(
            [make_tool_call("c1", "finish", summary="done"), make_tool_call("c2", "read_file", path="a.py")],
            AgentActor.MAIN,
            step=1,
            decision_id="d1",
            ts=1.0,
        )
        second = scheduler.execute_batch(
            [make_tool_call("f2", "finish", summary="done")],
            AgentActor.MAIN,
            step=2,
            decision_id="d2",
            ts=2.0,
        )
        assert second.control_tool == "finish"


class TestPermissionAndBudget:
    def test_rt12_reviewer_permission_denied(self, registry):
        """越权直接 PERMISSION_DENIED 回流，无任何审批交互。"""
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        scheduler, _ = make_scheduler(registry, executor, store)
        outcome = scheduler.execute_batch(
            [make_tool_call("c1", "apply_patch", path="a.py", patch="x")],
            AgentActor.REVIEWER,
            step=1,
            decision_id="d1",
            ts=1.0,
        )
        result = outcome.results[0]
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.PERMISSION_DENIED
        assert executor.executed() == []  # 工作区不受影响

    def test_budget_exhausted_mid_batch_skips_remaining(self, registry):
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        config = RunConfig(model_id="m", max_tool_calls=2)
        scheduler, _ = make_scheduler(registry, executor, store, config)
        calls = [
            make_tool_call("c1", "read_file", path="a.py"),
            make_tool_call("c2", "read_file", path="b.py"),
            make_tool_call("c3", "read_file", path="c.py"),
        ]
        outcome = scheduler.execute_batch(calls, AgentActor.MAIN, step=1, decision_id="d1", ts=1.0)
        assert outcome.budget_exhausted is True
        assert outcome.results[0].status is ExecutionStatus.SUCCEEDED
        assert outcome.results[1].status is ExecutionStatus.SUCCEEDED
        assert outcome.results[2].status is ExecutionStatus.SKIPPED
        assert outcome.results[2].skipped_reason == "budget_exhausted"
