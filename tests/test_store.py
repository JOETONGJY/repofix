"""State & Telemetry 测试：意图先行、同事务完成、崩溃对账（ST-01/02/03）、Trace 导出（ST-04）。"""
import json

from repofix.contracts import (
    AgentActor,
    EventType,
    ExecutionStatus,
    RunStatus,
    SideEffectStatus,
    ToolResult,
)
from repofix.state import SQLiteEventStore

RUN_ID = "run_1"


def make_store() -> SQLiteEventStore:
    store = SQLiteEventStore(":memory:")
    store.create_run(RUN_ID, "./repo", "abc123", model_id="scripted", prompt_version="v0", ts=1.0)
    return store


class TestIntentFirst:
    def test_start_tool_call_persists_intent_and_event_atomically(self):
        store = make_store()
        store.start_tool_call(
            RUN_ID,
            decision_id="d1",
            call_id="c1",
            step=1,
            tool_name="apply_patch",
            arguments={"path": "a.py"},
            pre_state_hash="aaa",
            expected_result="bbb",
            ts=2.0,
        )
        row = store.get_tool_call(RUN_ID, "d1", "c1")
        assert row["side_effect_status"] == SideEffectStatus.STARTED.value
        assert row["pre_state_hash"] == "aaa"
        assert row["expected_result"] == "bbb"
        events = store.get_events(RUN_ID)
        assert events[0].type is EventType.TOOL_STARTED
        assert events[0].payload["pre_state_hash"] == "aaa"

    def test_finish_tool_call_writes_result_and_event(self):
        store = make_store()
        store.start_tool_call(RUN_ID, "d1", "c1", 1, "apply_patch", {"path": "a.py"}, "aaa", "bbb", ts=2.0)
        result = ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content="ok")
        store.finish_tool_call(RUN_ID, "d1", "c1", 1, SideEffectStatus.SUCCEEDED, result, ts=3.0, stdout="out")
        row = store.get_tool_call(RUN_ID, "d1", "c1")
        assert row["side_effect_status"] == SideEffectStatus.SUCCEEDED.value
        assert json.loads(row["result_json"])["status"] == "succeeded"
        assert row["stdout"] == "out"
        events = store.get_events(RUN_ID)
        assert [e.type for e in events] == [EventType.TOOL_STARTED, EventType.TOOL_FINISHED]

    def test_skipped_call_recorded_without_execution_result(self):
        store = make_store()
        store.start_tool_call(RUN_ID, "d1", "c2", 1, "read_file", {"path": "x.py"}, ts=2.0)
        skipped = ToolResult(
            call_id="c2",
            status=ExecutionStatus.SKIPPED,
            skipped_reason="batch_aborted_after_write_failure",
        )
        store.finish_tool_call(RUN_ID, "d1", "c2", 1, SideEffectStatus.SKIPPED, skipped, ts=2.5)
        row = store.get_tool_call(RUN_ID, "d1", "c2")
        assert row["side_effect_status"] == SideEffectStatus.SKIPPED.value
        assert json.loads(row["result_json"])["skipped_reason"] == "batch_aborted_after_write_failure"

    def test_events_seq_monotonic_per_run(self):
        store = make_store()
        for i in range(5):
            store.append_event(RUN_ID, 0, AgentActor.SYSTEM, EventType.RUN_STARTED, {"i": i}, ts=float(i))
        seqs = [e.seq for e in store.get_events(RUN_ID)]
        assert seqs == [1, 2, 3, 4, 5]


class TestReconcile:
    def _crashed_run(self) -> SQLiteEventStore:
        """模拟崩溃现场：两个 STARTED 无完成记录的调用。"""
        store = make_store()
        store.start_tool_call(
            RUN_ID, "d1", "c_patched", 1, "apply_patch", {"path": "a.py"}, pre_state_hash="pre1",
            expected_result="post1", ts=2.0,
        )
        store.start_tool_call(
            RUN_ID, "d1", "c_unapplied", 1, "apply_patch", {"path": "b.py"}, pre_state_hash="pre2",
            expected_result="post2", ts=2.1,
        )
        store.start_tool_call(
            RUN_ID, "d1", "c_shell", 1, "run_command", {"argv": ["pytest"]}, ts=2.2,
        )
        return store

    def test_st01_crash_after_patch_landed_reconciles_succeeded(self):
        store = self._crashed_run()

        def state_fn(row):
            return {"c_patched": "post1"}.get(row["call_id"])

        summary = store.reconcile_run(RUN_ID, state_fn, ts=9.0)
        assert summary["succeeded"] == 1
        row = store.get_tool_call(RUN_ID, "d1", "c_patched")
        assert row["side_effect_status"] == SideEffectStatus.SUCCEEDED.value

    def test_st02_crash_before_patch_applied_marks_failed_not_retried(self):
        store = self._crashed_run()

        def state_fn(row):
            # b.py 仍是原始内容：patch 未生效
            return {"c_unapplied": "pre2"}.get(row["call_id"])

        summary = store.reconcile_run(RUN_ID, state_fn, ts=9.0)
        assert summary["failed"] == 1
        row = store.get_tool_call(RUN_ID, "d1", "c_unapplied")
        assert row["side_effect_status"] == SideEffectStatus.FAILED.value

    def test_st03_unexplainable_state_marks_unknown_and_run_interrupted(self):
        store = self._crashed_run()

        def state_fn(row):
            # Shell 副作用无法用文件 hash 核对 → 未知
            return {"c_patched": "totally-different"}.get(row["call_id"])

        summary = store.reconcile_run(RUN_ID, state_fn, ts=9.0)
        assert summary["unknown"] >= 1
        row = store.get_tool_call(RUN_ID, "d1", "c_shell")
        assert row["side_effect_status"] == SideEffectStatus.UNKNOWN.value
        run = store.get_run(RUN_ID)
        assert run["status"] == RunStatus.FAILED.value
        assert run["termination_reason"] == "PROCESS_INTERRUPTED"

    def test_reconcile_does_not_replay_tools(self):
        """对账只核对状态，不触发任何执行（事件回放 ≠ 工具重新执行）。"""
        store = self._crashed_run()
        calls_before = len(store.list_tool_calls(RUN_ID))
        store.reconcile_run(RUN_ID, lambda row: None, ts=9.0)
        assert len(store.list_tool_calls(RUN_ID)) == calls_before


class TestRunStatusAndExport:
    def test_run_status_terminal_records_reason_and_layer(self):
        store = make_store()
        store.set_run_status(
            RUN_ID, RunStatus.FAILED, ts=5.0, termination_reason="model_protocol_error",
            fail_layer="model_api",
        )
        run = store.get_run(RUN_ID)
        assert run["status"] == "failed"
        assert run["termination_reason"] == "model_protocol_error"
        assert run["fail_layer"] == "model_api"
        assert run["ended_at"] == 5.0

    def test_st04_trajectory_export_matches_events(self):
        store = make_store()
        store.append_event(RUN_ID, 0, AgentActor.MAIN, EventType.RUN_STARTED, {"task": "fix"}, ts=1.0)
        store.start_tool_call(RUN_ID, "d1", "c1", 1, "read_file", {"path": "a.py"}, ts=2.0)
        result = ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content="data")
        store.finish_tool_call(RUN_ID, "d1", "c1", 1, SideEffectStatus.SUCCEEDED, result, ts=3.0)

        trajectory = store.export_trajectory(RUN_ID)
        lines = [json.loads(line) for line in trajectory.strip().splitlines()]
        assert [item["type"] for item in lines] == ["run_started", "tool_started", "tool_finished"]
        assert lines[0]["payload"] == {"task": "fix"}
        assert lines[2]["payload"]["call_id"] == "c1"
        # 导出内容与 SQLite 内容一致：事件数一致
        assert len(lines) == len(store.get_events(RUN_ID))
