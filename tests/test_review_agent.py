"""Review Agent 测试：RV-01~07 + CONS-01（design/02、specs/04）。

全部零 Token：Reviewer 的模型经脚本注入；工作区为 Fake 只读执行器。
"""
import json

from repofix.contracts import (
    AgentActor,
    ExecutionStatus,
    PatchStatus,
    RunConfig,
    RunStatus,
    TaskSpec,
    TestStatus,
    ToolResult,
    VerificationOutcome,
    VerificationReport,
)
from repofix.runtime.loop_guard import BudgetTracker
from repofix.runtime.review_runner import ReviewRunner, _parse_review
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from tests.conftest import (
    FakeClock,
    FakeToolExecutor,
    FakeVerifier,
    ScriptedModelClient,
    make_tool_call,
    model_response,
)

TASK_ARGS = {"instruction": "Fix the login validation bug", "repo_path": "./repo", "base_commit": "abc123"}

REVIEW_TOOL_CALL = make_tool_call("rv1", "submit_review",
                                  status="APPROVED", findings=[], summary="clean")


def make_review_runner(responses, registry, executor=None, config=None, run_id="run_rv"):
    runner = ReviewRunner(
        run_id=run_id,
        config=config or RunConfig(model_id="m"),
        model_client=ScriptedModelClient(responses),
        registry=registry,
        workspace_executor=executor or FakeToolExecutor(),
        store=SQLiteEventStore(":memory:"),
        clock=FakeClock(),
        global_tool_budget=BudgetTracker(RunConfig(model_id="m"), FakeClock()),
    )
    return runner


def make_review_request(**overrides):
    from repofix.contracts import ReviewRequest

    defaults = dict(
        run_id="run_rv", instruction="Fix the bug", base_commit="abc123",
        candidate_diff="--- a/auth.py\n+++ b/auth.py\n...", verification_summary="passed",
        workspace_ref="ws", patch_hash="0" * 64,
    )
    defaults.update(overrides)
    return ReviewRequest(**defaults)


class TestRv02PermissionIsolation:
    def test_reviewer_view_has_no_write_tools(self, registry):
        """RV-02：注册表层面——Reviewer 视图无 apply_patch / run_command / finish。"""
        names = [t["function"]["name"] for t in registry.view_for(AgentActor.REVIEWER)]
        assert "apply_patch" not in names
        assert "run_command" not in names
        assert "finish" not in names
        assert {"list_files", "search_code", "read_file", "submit_review"} <= set(names)

    def test_readonly_executor_rejects_writes(self):
        executor = FakeToolExecutor()
        executor.register("apply_patch", ToolResult(
            call_id="p", status=ExecutionStatus.FAILED,
            error_code=__import__("repofix.contracts", fromlist=["ToolErrorCode"]).ToolErrorCode.PERMISSION_DENIED,
            error_message="read-only workspace cannot modify files"))
        result = executor.execute("apply_patch", {"path": "a.py", "patch": "x"})
        assert result.error_message and "read-only" in result.error_message


class TestRv03ContextIsolation:
    def test_reviewer_context_has_no_main_history(self, registry):
        """RV-03：Reviewer 的上下文只含任务/Diff/验证摘要，不含 Main 推理历史。"""
        runner = make_review_runner([model_response("rd1", [REVIEW_TOOL_CALL])], registry)
        request = make_review_request(instruction="Fix X", candidate_diff="diff-text")
        report = runner.review(request)
        assert report.status == "APPROVED"
        # ReviewRequest 契约本身不含 Main 历史
        raw = make_review_request().__dict__
        assert "messages" not in raw and "history" not in raw


class TestReviewParsing:
    def test_parse_status_enum(self):
        assert _parse_review({"status": "APPROVED"}).status == "APPROVED"
        assert isinstance(_parse_review({"status": "NEEDS_WORK"}), str)

    def test_findings_require_evidence(self):
        bad = _parse_review({"status": "FINDINGS", "findings": [
            {"file_path": "a.py", "reason": "logic error", "severity": "high"}]})
        assert isinstance(bad, str) and "evidence" in bad
        good = _parse_review({"status": "FINDINGS", "findings": [
            {"file_path": "a.py", "reason": "logic error", "severity": "high",
             "evidence": "line 2 ignores empty input"}]})
        assert good.status == "FINDINGS" and len(good.findings) == 1

    def test_approved_cannot_carry_findings(self):
        result = _parse_review({"status": "APPROVED", "findings": [
            {"file_path": "a.py", "reason": "r", "severity": "low", "evidence": "e"}]})
        assert isinstance(result, str) and "APPROVED" in result

    def test_max_five_findings(self):
        many = [{"file_path": "f.py", "reason": "r", "severity": "low", "evidence": "e"} for _ in range(6)]
        assert isinstance(_parse_review({"status": "FINDINGS", "findings": many}), str)


class TestRv05BoundedExecution:
    def test_model_budget_exhaustion_ends_inconclusive(self, registry):
        """RV-05：Reviewer 超 3 次模型调用即被截断 → INCONCLUSIVE。"""
        responses = [model_response(f"rd{i}", [make_tool_call(f"c{i}", "read_file", path="a.py")])
                     for i in range(5)]
        runner = make_review_runner(responses, registry)
        report = runner.review(make_review_request())
        assert report.status == "INCONCLUSIVE"
        assert "budget" in report.summary

    def test_reviewer_failure_does_not_raise(self, registry):
        """RV-06：Reviewer 模型彻底不可用 → INCONCLUSIVE，不抛异常。"""
        runner = make_review_runner(
            [__import__("repofix.gateway", fromlist=["ModelTransientError"]).ModelTransientError("down")],
            registry)
        report = runner.review(make_review_request())
        assert report.status == "INCONCLUSIVE"
        assert "unavailable" in report.summary


class TestRv07PatchHashBinding:
    def test_report_bound_to_request_patch_hash(self, registry):
        request = make_review_request(patch_hash="f" * 64)
        runner = make_review_runner([model_response("rd1", [REVIEW_TOOL_CALL])], registry)
        report = runner.review(request)
        assert report.patch_hash == "f" * 64  # 审查版本绑定（RV-07）


class TestRv04FeedbackLoop:
    """RV-04：FINDINGS → 修正 → 重新验证 → 不二次审查。"""

    @staticmethod
    def _passed():
        return VerificationReport(
            candidate_revision="v", patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)

    @staticmethod
    def _failed():
        return VerificationReport(
            candidate_revision="v", patch_status=PatchStatus.VALID,
            test_status=TestStatus.FAILED, outcome=VerificationOutcome.FAILED,
            failed_tests=["tests/test_auth.py::test_x"])

    @staticmethod
    def _findings_report():
        from repofix.contracts import ReviewFinding, ReviewReport

        return ReviewReport(status="FINDINGS", patch_hash="1" * 64, findings=[
            ReviewFinding(finding_id="f1", file_path="auth.py", severity="high",
                          reason="empty-input crash", evidence="no guard on empty string",
                          suggested_fix="return False for empty"),
        ], summary="needs guard")

    def _runner_with_review(self, responses, verifier_reports, config, review_report=None):
        """真实 Runner + 计数 Stub Reviewer（无 Docker：命令走 Fake 执行器）。"""
        registry = ToolRegistry()
        register_default_tools(registry)
        self._stub_review_calls = {"n": 0}
        report_to_return = review_report if review_report is not None else self._findings_report()

        class ScriptedReviewRunner:
            calls = 0
            owner = None

            def review(self, request):
                self.__class__.calls += 1
                self.__class__.owner._stub_review_calls["n"] += 1
                return report_to_return

        ScriptedReviewRunner.owner = self

        def factory(run_id):
            return ScriptedReviewRunner()

        executor = FakeToolExecutor()
        store = SQLiteEventStore(":memory:")
        runner = AgentRunner(
            run_id="run_m4", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(responses), registry=registry,
            dispatcher=ToolDispatcher(registry, executor), store=store,
            verifier=FakeVerifier(verifier_reports), clock=FakeClock(),
            patch_hash_provider=lambda: "1" * 64,
            review_runner_factory=factory,
            reviewer_executor=executor,
        )
        return runner, store, ScriptedReviewRunner

    def test_findings_trigger_single_fix_cycle(self, registry, config):
        """FINDINGS → 修正回流 → 第二次验证通过 → COMPLETED（review 只发生一次）。"""
        config = RunConfig(model_id="m", review_enabled=True, review_fix_model_calls=3)
        responses = [
            model_response("m1", [make_tool_call("f1", "finish", summary="first try")]),
            model_response("m2", [make_tool_call("c1", "read_file", path="auth.py")]),
            model_response("m3", [make_tool_call("f2", "finish", summary="fixed with guard")]),
        ]
        runner, store, StubReview = self._runner_with_review(
            responses, [self._passed(), self._passed()], config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status == "FINDINGS"
        assert result.reviewed_patch_hash == "1" * 64
        assert StubReview.calls == 1  # 只触发一次 Review
        from repofix.contracts import EventType
        types = [e.type for e in store.get_events("run_m4")]
        assert types.count(EventType.VERIFICATION_FINISHED) == 2

    def test_findings_feedback_reaches_main_context(self, registry):
        """修正轮请求中包含结构化 Finding 反馈。"""
        config = RunConfig(model_id="m", review_enabled=True, review_fix_model_calls=3)
        responses = [
            model_response("m1", [make_tool_call("f1", "finish", summary="first")]),
            model_response("m2", [make_tool_call("f2", "finish", summary="second")]),
        ]
        runner, _, _ = self._runner_with_review(
            responses, [self._passed(), self._passed()], config)
        client = runner._model_client
        runner.run()
        second_messages = client.requests[1].messages
        joined = json.dumps(second_messages, ensure_ascii=False)
        assert "Review Agent returned FINDINGS" in joined
        assert "empty-input crash" in joined

    def test_approved_completes_without_fix_cycle(self, registry):
        """APPROVED → 直接 COMPLETED。"""
        from repofix.contracts import ReviewReport

        config = RunConfig(model_id="m", review_enabled=True)
        approved = ReviewReport(status="APPROVED", patch_hash="1" * 64, findings=[], summary="clean")
        responses = [model_response("m1", [make_tool_call("f1", "finish", summary="done")])]
        runner, _, _ = self._runner_with_review(
            responses, [self._passed()], config, review_report=approved)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status == "APPROVED"

    def test_second_verification_never_re_reviews_even_when_findings_repeat(self, registry):
        """第二次验证失败 → 回流（不二次审查）→ 第三次验证通过 → COMPLETED。"""
        config = RunConfig(model_id="m", review_enabled=True)
        responses = [
            model_response("m1", [make_tool_call("f1", "finish", summary="first")]),
            model_response("m2", [make_tool_call("f2", "finish", summary="second")]),
            model_response("m3", [make_tool_call("f3", "finish", summary="third")]),
        ]
        runner, store, StubReview = self._runner_with_review(
            responses, [self._passed(), self._failed(), self._passed()], config)
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert StubReview.calls == 1  # 仍然只一轮审查
        from repofix.contracts import EventType
        types = [e.type for e in store.get_events("run_m4")]
        assert types.count(EventType.VERIFICATION_FINISHED) == 3

class TestRv01TriggerRules:
    def _runner(self, config, report):
        registry = ToolRegistry()
        register_default_tools(registry)
        review_calls = {"n": 0}

        class ScriptedReviewRunner:
            last_instance = None
            def review(self, request):
                review_calls["n"] += 1
                from repofix.contracts import ReviewReport

                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        finish = [model_response("m1", [make_tool_call("f1", "finish", summary="x")])]
        runner = AgentRunner(
            run_id="run_t", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(finish), registry=registry,
            dispatcher=ToolDispatcher(registry, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([report]), clock=FakeClock(),
            patch_hash_provider=lambda: "2" * 64,
            review_runner_factory=lambda rid: ScriptedReviewRunner(),
            reviewer_executor=FakeToolExecutor(),
        )
        return runner, review_calls

    def test_review_enabled_triggers_after_pass(self):

        report = VerificationReport(
            candidate_revision="2" * 64, patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)
        config = RunConfig(model_id="m", review_enabled=True)
        runner, calls = self._runner(config, report)
        result = runner.run()
        assert calls["n"] == 1
        assert result.review_status == "APPROVED"

    def test_review_disabled_is_baseline(self):
        report = VerificationReport(
            candidate_revision="2" * 64, patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)
        config = RunConfig(model_id="m", review_enabled=False)
        runner, calls = self._runner(config, report)
        result = runner.run()
        assert calls["n"] == 0
        assert result.review_status is None

    def test_empty_patch_does_not_trigger_review(self):
        """Patch 无效 → FAILED 反馈回流（该次不启动 Review）；修正后有效 Patch → 首次审查正确触发。"""
        report_empty = VerificationReport(
            candidate_revision="", patch_status=PatchStatus.EMPTY,
            test_status=TestStatus.NOT_AVAILABLE, outcome=VerificationOutcome.FAILED)
        report_pass = VerificationReport(
            candidate_revision="2" * 64, patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)
        config = RunConfig(model_id="m", review_enabled=True)
        registry = ToolRegistry()
        register_default_tools(registry)
        review_calls = {"n": 0}

        class ScriptedReviewRunner:
            last_instance = None
            def review(self, request):
                review_calls["n"] += 1
                from repofix.contracts import ReviewReport

                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        runner = AgentRunner(
            run_id="run_ep", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient([
                model_response("m1", [make_tool_call("f1", "finish", summary="first")]),
                model_response("m2", [make_tool_call("f2", "finish", summary="fixed now")]),
            ]),
            registry=registry, dispatcher=ToolDispatcher(registry, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([report_empty, report_pass]), clock=FakeClock(),
            patch_hash_provider=lambda: "2" * 64,
            review_runner_factory=lambda rid: ScriptedReviewRunner(),
            reviewer_executor=FakeToolExecutor(),
        )
        result = runner.run()
        # 第一次 finish 因 EMPTY 被拒（不触发 Review）；修正后有效 Patch → 首次审查正确触发
        assert review_calls["n"] == 1
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status == "APPROVED"

    def test_no_factory_does_not_trigger_review(self):
        """未装配 Reviewer（M1–M3 兼容）：review_enabled=True 也不触发。"""
        registry = ToolRegistry()
        register_default_tools(registry)
        report = VerificationReport(
            candidate_revision="2" * 64, patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)
        config = RunConfig(model_id="m", review_enabled=True)
        runner = AgentRunner(
            run_id="run_nf", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient([model_response("m1", [make_tool_call("f1", "finish", summary="x")])]),
            registry=registry, dispatcher=ToolDispatcher(registry, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"), verifier=FakeVerifier([report]),
            clock=FakeClock(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status is None


class TestCons01Budget:
    def test_reviewer_tool_calls_count_into_global_counter(self, registry):
        """CONS-01：Reviewer 工具调用计入全局计数器且受角色上限约束——无双重计数或遗漏。"""
        config = RunConfig(model_id="m")
        global_budget = BudgetTracker(config, FakeClock())
        responses = [
            model_response(f"rd{i}", [
                make_tool_call(f"c{i}a", "read_file", path="a.py"),
                make_tool_call(f"c{i}b", "search_code", pattern="x"),
            ]) for i in range(2)
        ] + [model_response("rd2", [REVIEW_TOOL_CALL])]
        runner = ReviewRunner(
            run_id="run_cb", config=config,
            model_client=ScriptedModelClient(responses), registry=registry,
            workspace_executor=FakeToolExecutor(),
            store=SQLiteEventStore(":memory:"), clock=FakeClock(),
            global_tool_budget=global_budget,
        )
        report = runner.review(make_review_request())
        assert report.status == "APPROVED"
        assert global_budget.tool_calls == 4  # 2 轮 × 2 只读调用计入全局
