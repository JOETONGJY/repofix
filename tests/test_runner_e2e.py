"""AgentRunner 端到端测试：RT-01/02/07/08/12/13 + 配对可见性 + 确定性闭环 + Trace 导出。

真实 Runtime 执行全流程；Fake 仅提供外部能力（模型脚本 / 工具结果 / 验证报告），
不生成 Trace / RunResult。全部测试不访问外部模型 API、不启动 Docker。
"""
import json

from repofix.contracts import (
    AgentActor,
    EventType,
    FailureLayer,
    PatchStatus,
    RunConfig,
    RunStatus,
    TaskSpec,
    VerificationOutcome,
    VerificationReport,
    VerificationStatus,
)
from repofix.contracts import TestStatus as VisibleTestStatus
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from tests.conftest import (
    FakeClock,
    FakeToolExecutor,
    FakeVerifier,
    ScriptedModelClient,
    make_tool_call,
    model_response,
)

RUN_ID = "run_e2e"
TASK_ARGS = {"instruction": "Fix the login validation bug", "repo_path": "./repo", "base_commit": "abc123"}


def passed_report() -> VerificationReport:
    return VerificationReport(
        candidate_revision="m1-no-workspace",
        patch_status=PatchStatus.VALID,
        test_status=VisibleTestStatus.PASSED,
        outcome=VerificationOutcome.PASSED,
    )


def failed_report() -> VerificationReport:
    return VerificationReport(
        candidate_revision="m1-no-workspace",
        patch_status=PatchStatus.VALID,
        test_status=VisibleTestStatus.FAILED,
        outcome=VerificationOutcome.FAILED,
        failure_kind="assertion_failure",
        failed_tests=["tests/test_auth.py::test_empty_password"],
    )


def build_runner(responses, verifier_reports, registry, config, executor=None):
    """组装真实 Runtime + 三 Fake（模型脚本 / 工具结果 / 验证报告）。"""
    store = SQLiteEventStore(":memory:")
    executor = executor or FakeToolExecutor()
    runner = AgentRunner(
        run_id=RUN_ID,
        task=TaskSpec(**TASK_ARGS),
        config=config,
        model_client=ScriptedModelClient(responses),
        registry=registry,
        dispatcher=ToolDispatcher(registry, executor),
        store=store,
        verifier=FakeVerifier(verifier_reports),
        clock=FakeClock(),
    )
    return runner, store


class TestVerticalSlice:
    def test_rt02_full_happy_loop_search_read_patch_test_finish(self, registry, config):
        """确定性闭环：read → apply_patch + run_command → finish（验证通过）→ COMPLETED。"""
        responses = [
            model_response(
                "d1",
                [make_tool_call("c1", "read_file", path="auth.py")],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            ),
            model_response(
                "d2",
                [
                    make_tool_call("c2", "apply_patch", path="auth.py", patch="--- a\n+++ b"),
                    make_tool_call("c3", "run_command", argv=["python", "-m", "pytest", "-q"]),
                ],
                usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            ),
            model_response("d3", [make_tool_call("f1", "finish", summary="修复完成")]),
        ]
        runner, store = build_runner(responses, [passed_report()], registry, config)
        result = runner.run()

        # RunResult 由真实 Runtime 产生
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.termination_reason == "finish_accepted"
        assert result.verification_status is VerificationStatus.PASSED
        assert result.modified_files == ["auth.py"]
        assert result.model_calls == 3
        assert result.tool_calls == 3  # finish 是控制动作，不计工具调用
        assert result.token_usage["total"] == 45
        assert result.duration_ms >= 0

        # SQLite 可查询：三表都有内容，apply_patch 生命周期完整
        run = store.get_run(RUN_ID)
        assert run["status"] == "completed"
        assert run["total_tokens"] == 45
        rows = {row["call_id"]: row for row in store.list_tool_calls(RUN_ID)}
        assert rows["c2"]["tool_name"] == "apply_patch"
        assert rows["c2"]["side_effect_status"] == "succeeded"
        assert rows["f1"]["tool_name"] == "finish"  # 控制动作也有 Trace

        # 事件序列完整（specs/02 单 Turn 标准序列）
        event_types = [event.type for event in store.get_events(RUN_ID)]
        assert event_types[0] is EventType.RUN_STARTED
        assert EventType.PREFLIGHT_COMPLETED in event_types
        assert event_types.count(EventType.MODEL_REQUESTED) == 3
        assert event_types.count(EventType.TOOL_FINISHED) == 4  # 3 个工具 + 1 个控制动作
        assert event_types.count(EventType.VERIFICATION_FINISHED) == 1
        assert event_types[-1] is EventType.RUN_FINISHED

        # trajectory.jsonl 导出可解析且与 SQLite 一致
        trajectory = store.export_trajectory(RUN_ID)
        lines = [json.loads(line) for line in trajectory.strip().splitlines()]
        assert len(lines) == len(store.get_events(RUN_ID))

    def test_pairing_visibility_in_next_request(self, registry, config):
        """下一轮模型请求包含正确的 Tool Call/Result 配对（闭环验证的另一半）。"""
        responses = [
            model_response(
                "d1",
                [
                    make_tool_call("c1", "read_file", path="auth.py"),
                    make_tool_call("c2", "list_files", path="."),
                ],
            ),
            model_response("d2", [make_tool_call("f1", "finish", summary="done")]),
        ]
        runner, _ = build_runner(responses, [passed_report()], registry, config)
        runner.run()

        # 这里通过 runner 的 context 不可直接访问；改为校验 ScriptedModelClient 收到的第二轮请求
        # build_runner 内部持有 client——重新构造以拿到引用
        store = SQLiteEventStore(":memory:")
        executor = FakeToolExecutor()
        client = ScriptedModelClient(
            [
                model_response(
                    "d1",
                    [
                        make_tool_call("c1", "read_file", path="auth.py"),
                        make_tool_call("c2", "list_files", path="."),
                    ],
                ),
                model_response("d2", [make_tool_call("f1", "finish", summary="done")]),
            ]
        )
        runner2 = AgentRunner(
            run_id=RUN_ID,
            task=TaskSpec(**TASK_ARGS),
            config=config,
            model_client=client,
            registry=registry,
            dispatcher=ToolDispatcher(registry, executor),
            store=store,
            verifier=FakeVerifier([passed_report()]),
            clock=FakeClock(),
        )
        runner2.run()
        assert len(client.requests) == 2
        second_messages = client.requests[1].messages
        assistant_turns = [m for m in second_messages if m.get("role") == "assistant" and m.get("tool_calls")]
        tool_results = [m for m in second_messages if m.get("role") == "tool"]
        assert len(assistant_turns) == 1
        emitted_ids = [c["id"] for c in assistant_turns[0]["tool_calls"]]
        result_ids = [m["tool_call_id"] for m in tool_results]
        assert emitted_ids == ["c1", "c2"]
        assert result_ids == ["c1", "c2"]  # 一一配对，无孤儿
        # Reviewer 不可见的写工具不出现在 Main 工具视图（视图由 Registry 生成）
        tool_names = [t["function"]["name"] for t in client.requests[0].tools]
        assert "submit_review" not in tool_names


class TestProtocolErrors:
    def test_rt01_empty_response_retry_once_then_fail(self, registry, config):
        responses = [
            model_response("d1", []),
            model_response("d2", []),
        ]
        runner, store = build_runner(responses, [], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.FAILED
        assert result.termination_reason == "model_protocol_error:empty_response"
        events = store.get_events(RUN_ID)
        assert sum(1 for e in events if e.type is EventType.MODEL_RETRY) == 1  # 只重发一次
        run = store.get_run(RUN_ID)
        assert run["fail_layer"] == FailureLayer.MODEL_API.value

    def test_rt01_empty_then_valid_response_continues(self, registry, config):
        responses = [
            model_response("d1", []),  # 空响应 → 协议纠正
            model_response("d2", [make_tool_call("f1", "finish", summary="done")]),
        ]
        runner, _ = build_runner(responses, [passed_report()], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED

    def test_rt13_text_only_response_does_not_end_task(self, registry, config):
        """模型仅输出文本：不直接结束，按协议错误处理。"""
        responses = [
            model_response("d1", [], text="I think the task is done."),  # 文本不是完成信号
            model_response("d2", []),
        ]
        runner, store = build_runner(responses, [], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.FAILED  # 第二次仍无效 → FAILED
        types = [e.type for e in store.get_events(RUN_ID)]
        assert EventType.MODEL_RETRY in types
        # 全程未进入 VERIFYING / COMPLETED
        run = store.get_run(RUN_ID)
        assert run["status"] == "failed"

    def test_rt13_text_then_finish_is_accepted(self, registry, config):
        responses = [
            model_response("d1", [], text="Done!"),
            model_response("d2", [make_tool_call("f1", "finish", summary="真的完成了")]),
        ]
        runner, _ = build_runner(responses, [passed_report()], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED


class TestVerificationLoop:
    def test_rt07_failed_verification_feeds_back_and_continues(self, registry, config):
        responses = [
            model_response("d1", [make_tool_call("f1", "finish", summary="第一次提交")]),
            model_response("d2", [make_tool_call("f2", "finish", summary="修复断言后再次提交")]),
        ]
        runner, store = build_runner(responses, [failed_report(), passed_report()], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.verification_status is VerificationStatus.PASSED
        events = store.get_events(RUN_ID)
        assert sum(1 for e in events if e.type is EventType.VERIFICATION_FINISHED) == 2
        run = store.get_run(RUN_ID)
        assert run["status"] == "completed"

    def test_rt07_working_state_records_failed_tests(self, registry, config):
        """验证失败反馈进入上下文（下一轮请求包含失败反馈）。"""
        responses = [
            model_response("d1", [make_tool_call("f1", "finish", summary="x")]),
            model_response("d2", [make_tool_call("f2", "finish", summary="y")]),
        ]
        store = SQLiteEventStore(":memory:")
        client = ScriptedModelClient(responses)
        runner = AgentRunner(
            run_id=RUN_ID,
            task=TaskSpec(**TASK_ARGS),
            config=config,
            model_client=client,
            registry=registry,
            dispatcher=ToolDispatcher(registry, FakeToolExecutor()),
            store=store,
            verifier=FakeVerifier([failed_report(), passed_report()]),
            clock=FakeClock(),
        )
        runner.run()
        second_messages = client.requests[1].messages
        feedback = [
            m
            for m in second_messages
            if m.get("role") == "user" and "Final Verification FAILED" in m.get("content", "")
        ]
        assert feedback, "验证失败反馈必须回流到下一轮请求"
        assert "test_empty_password" in feedback[0]["content"]

    def test_rt08_environment_error_completes_without_review_or_reverify(self, registry, config):
        error_report = VerificationReport(
            candidate_revision="m1-no-workspace",
            patch_status=PatchStatus.VALID,
            test_status=VisibleTestStatus.ERROR,
            outcome=VerificationOutcome.ERROR,
            failure_kind="environment_error",
            summary="test command failed to start",
        )
        responses = [model_response("d1", [make_tool_call("f1", "finish", summary="done")])]
        runner, store = build_runner(responses, [error_report], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.verification_status is VerificationStatus.INCONCLUSIVE  # 无法验证 ≠ 通过
        events = store.get_events(RUN_ID)
        assert sum(1 for e in events if e.type is EventType.VERIFICATION_FINISHED) == 1  # 不重验

    def test_no_test_does_not_claim_passed(self, registry, config):
        """无可用测试：INCONCLUSIVE，不能错误产生 PASSED（VF-05 的 Runtime 侧语义）。"""
        inconclusive = VerificationReport(
            candidate_revision="m1-no-workspace",
            patch_status=PatchStatus.VALID,
            test_status=VisibleTestStatus.NOT_AVAILABLE,
            outcome=VerificationOutcome.INCONCLUSIVE,
        )
        responses = [model_response("d1", [make_tool_call("f1", "finish", summary="done")])]
        runner, _ = build_runner(responses, [inconclusive], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.verification_status is VerificationStatus.INCONCLUSIVE


class TestBudgetAndGuard:
    def test_rt10_model_budget_exhaustion_terminates(self, registry):
        config = RunConfig(model_id="scripted-model", review_enabled=False, max_model_calls=1)
        responses = [
            model_response("d1", [make_tool_call("c1", "read_file", path="a.py")]),
            model_response("d2", [make_tool_call("f1", "finish", summary="done")]),  # 不应到达
        ]
        runner, store = build_runner(responses, [passed_report()], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.BUDGET_EXCEEDED
        assert result.termination_reason == "budget_exhausted:max_model_calls"
        events = store.get_events(RUN_ID)
        assert events[-1].type is EventType.RUN_FINISHED
        assert events[-1].payload["status"] == "budget_exceeded"

    def test_rt11_no_progress_terminates_run(self, registry, config):
        """同一调用连续 5 次（阈值读 RunConfig）→ FAILED(no_progress)。"""
        responses = [
            model_response(f"d{i}", [make_tool_call(f"c{i}", "read_file", path="same.py")])
            for i in range(1, 6)
        ]
        runner, store = build_runner(responses, [], registry, config)
        result = runner.run()
        assert result.runtime_status is RunStatus.FAILED
        assert result.termination_reason == "no_progress"
        run = store.get_run(RUN_ID)
        assert run["fail_layer"] == FailureLayer.TOOL_SYSTEM.value
        types = [e.type for e in store.get_events(RUN_ID)]
        assert types.count(EventType.NO_PROGRESS_WARNING) == 1  # 第 3 次注入一次

    def test_reviewer_view_excludes_write_tools(self, registry):
        """RV-02 前置：Reviewer 工具视图在注册表层不含写工具 / finish。"""
        reviewer_tools = [t["function"]["name"] for t in registry.view_for(AgentActor.REVIEWER)]
        assert "apply_patch" not in reviewer_tools
        assert "run_command" not in reviewer_tools
        assert "finish" not in reviewer_tools
        assert {"list_files", "search_code", "read_file", "submit_review"} <= set(reviewer_tools)
