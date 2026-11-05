"""M4 集成评审（复核）收尾回归：预算前置、混合拒批、修正 deadline、hash 复核、factory 降级。"""

from repofix.contracts import (
    PatchStatus,
    RunConfig,
    RunStatus,
    TaskSpec,
    TestStatus,
    VerificationOutcome,
    VerificationReport,
)
from repofix.gateway import ModelTransientError
from repofix.runtime.loop_guard import BudgetTracker
from repofix.runtime.review_runner import ReviewRunner
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


def _passed():
    return VerificationReport(
        candidate_revision="v", patch_status=PatchStatus.VALID,
        test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED)


def _runner_over_batch(registry):
    config = RunConfig(model_id="m")
    global_budget = BudgetTracker(RunConfig(model_id="m", max_tool_calls=100), FakeClock())
    calls = [make_tool_call(f"c{i}", "read_file", path=f"f{i}.py") for i in range(12)]
    runner = ReviewRunner(
        run_id="run_over", config=config,
        model_client=ScriptedModelClient([model_response("rd1", calls)]),
        registry=registry, workspace_executor=FakeToolExecutor(),
        store=SQLiteEventStore(":memory:"), clock=FakeClock(),
        global_tool_budget=global_budget,
    )
    return runner, global_budget


class TestOversizedToolBatch:
    def test_p01_stops_at_budget_with_pairing(self, registry):
        """P0-1：一次响应含 12 只读调用——恰好在 8 配额处停止，超配额 SKIPPED，配对完整。"""
        runner, global_budget = _runner_over_batch(registry)
        report = runner.review(
            __import__("tests.test_review_agent", fromlist=["make_review_request"])
            .make_review_request())
        assert report.status == "INCONCLUSIVE"  # 批次里没有 submit_review 结论
        assert global_budget.tool_calls == 8  # 恰好在 Reviewer 配额处停止（执行前占用）
        finished = [e for e in runner._store.get_events("run_over")
                    if e.type.value == "tool_finished"]
        assert len(finished) == 12  # 配对完整：全部 12 个调用都有记录
        skipped = [e for e in finished if e.payload.get("status") == "skipped"]
        assert len(skipped) == 4
        assert all(e.payload.get("skipped_reason") == "reviewer tool budget exhausted"
                   for e in skipped)


class TestMixedSubmitBatch:
    def test_p03_whole_batch_rejected(self, registry):
        """P0-3：read_file + submit_review 混合 → 整批拒绝（无执行、不采纳结论）。"""
        executed: list[str] = []

        class TrackingExecutor(FakeToolExecutor):
            def execute(self, tool_name, arguments, call_id=""):
                executed.append(tool_name)
                return super().execute(tool_name, arguments, call_id)

        runner = ReviewRunner(
            run_id="run_mix", config=RunConfig(model_id="m"),
            model_client=ScriptedModelClient([model_response("rd1", [
                make_tool_call("c_read", "read_file", path="a.py"),
                make_tool_call("c_rev", "submit_review", status="APPROVED", findings=[]),
            ])]),
            registry=registry, workspace_executor=TrackingExecutor(),
            store=SQLiteEventStore(":memory:"), clock=FakeClock(),
            global_tool_budget=BudgetTracker(RunConfig(model_id="m"), FakeClock()),
        )
        report = runner.review(
            __import__("tests.test_review_agent", fromlist=["make_review_request"])
            .make_review_request())
        assert report.status == "INCONCLUSIVE"  # 混合批次不采纳结论
        assert executed == []  # 只读调用也未执行
        events = {e.payload["call_id"]: e.payload for e in runner._store.get_events("run_mix")
                  if e.type.value == "tool_finished"}
        assert events["c_rev"]["status"] == "failed"  # submit_review INVALID_ARGUMENT
        assert events["c_read"]["status"] == "skipped"
        assert events["c_read"]["skipped_reason"] == "batch_rejected_submit_review_mixed"


class TestMainFixBudget:
    def test_p02_deadline_enforced(self, registry):
        """P0-2：FINDINGS 后修正阶段额外模型调用 ≤ review_fix_model_calls——超限 BUDGET_EXCEEDED。"""
        config = RunConfig(model_id="m", review_enabled=True, review_fix_model_calls=3,
                           max_model_calls=30)
        registry2 = ToolRegistry()
        register_default_tools(registry2)

        class ScriptedReview:
            def review(self, request):
                from repofix.contracts import ReviewFinding, ReviewReport
                return ReviewReport(status="FINDINGS", patch_hash="1" * 64, findings=[
                    ReviewFinding(finding_id="f1", file_path="a.py", severity="low",
                                  reason="r", evidence="e")], summary="s")

        # 修正轮为只读工具调用（不提前 finish）→ deadline 在第 5 次请求前触发
        read_calls = [make_tool_call(f"c{i}", "read_file", path="a.py") for i in range(1, 4)]
        responses = [
            model_response("m1", [make_tool_call("f1", "finish", summary="first")]),  # Review FINDINGS
            model_response("m2", [read_calls[0]]),
            model_response("m3", [read_calls[1]]),
            model_response("m4", [read_calls[2]]),
        ]
        runner = AgentRunner(
            run_id="run_fix", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(responses), registry=registry2,
            dispatcher=ToolDispatcher(registry2, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([_passed()]),
            clock=FakeClock(),
            patch_hash_provider=lambda: "1" * 64,
            review_runner_factory=lambda rid: ScriptedReview(),
            reviewer_executor=FakeToolExecutor(),
        )
        result = runner.run()
        # 1 初始 + 3 修正轮 → 第 5 次请求前 deadline 触发（BUDGET_EXCEEDED，RunResult 完整）
        assert result.runtime_status is RunStatus.BUDGET_EXCEEDED
        assert result.termination_reason == "budget_exhausted:review_fix_model_calls"
        assert result.model_calls == 4  # 1 初始 + 3 修正（deadline 精确生效）


class TestPatchDrift:
    def test_p1_review_invalidated_on_drift(self, registry):
        """P1-1：审查期间工作区变化 → APPROVED 降级 INCONCLUSIVE，已验证任务正常完成。"""
        config = RunConfig(model_id="m", review_enabled=True)
        registry2 = ToolRegistry()
        register_default_tools(registry2)

        class ScriptedReview:
            def review(self, request):
                from repofix.contracts import ReviewReport
                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        # 消耗顺序：verification 取 "1"（candidate_revision）→ Review 取 "2"（patch_hash）
        # → 审查后复核取 "3" ≠ "2" → 漂移被发现（VF-06 同源语义）
        hash_values = ["1" * 64, "2" * 64, "3" * 64]

        def drifting_provider():
            return hash_values.pop(0) if hash_values else "3" * 64

        runner = AgentRunner(
            run_id="run_drift", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(
                [model_response("m1", [make_tool_call("f1", "finish", summary="x")])]),
            registry=registry2, dispatcher=ToolDispatcher(registry2, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([_passed()]), clock=FakeClock(),
            patch_hash_provider=drifting_provider,
            review_runner_factory=lambda rid: ScriptedReview(),
            reviewer_executor=FakeToolExecutor(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED  # 已验证任务不受影响（RV-06）
        assert result.review_status == "INCONCLUSIVE"  # 漂移 → 审查无效
        assert result.reviewed_patch_hash == "2" * 64  # 报告绑定审查时版本


class TestFactoryFailure:
    def test_p1_construction_failure_is_inconclusive(self, registry):
        """P1-2：ReviewRunner 构造失败 → INCONCLUSIVE，已验证任务正常 COMPLETED。"""
        config = RunConfig(model_id="m", review_enabled=True)
        registry2 = ToolRegistry()
        register_default_tools(registry2)

        def broken_factory(run_id):
            raise RuntimeError("reviewer construction failed")

        runner = AgentRunner(
            run_id="run_bf", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(
                [model_response("m1", [make_tool_call("f1", "finish", summary="x")])]),
            registry=registry2, dispatcher=ToolDispatcher(registry2, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([_passed()]), clock=FakeClock(),
            patch_hash_provider=lambda: "1" * 64,
            review_runner_factory=broken_factory,
            reviewer_executor=FakeToolExecutor(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status == "INCONCLUSIVE"


class TestRetryCounting:
    def test_p13_retry_attempts_counted_into_reviewer_budget(self, registry, monkeypatch):
        """P1-3：Reviewer 独立预算按真实 API 尝试（含 Gateway 内部重试）计数。"""
        import repofix.runtime.review_runner as rr_module
        from repofix.runtime.loop_guard import BudgetTracker as _RealTracker

        config = RunConfig(model_id="m", max_reviewer_model_calls=3)
        reserve_calls: list[int] = []

        class TrackingBudget(_RealTracker):
            def reserve_model_attempt(self):
                reserve_calls.append(len(reserve_calls) + 1)
                super().reserve_model_attempt()

        monkeypatch.setattr(rr_module, "BudgetTracker", TrackingBudget)

        class RetryingAdapter:
            """模拟真实 Gateway：一次 Runtime 调用内部重试 3 次，全部失败。"""
            counts_own_attempts = True

            def __init__(self):
                self.on_attempt = None

            def complete(self, request):
                for _ in range(3):
                    self.on_attempt()  # 每次真实 API 尝试都上报（含重试）
                raise ModelTransientError("429")  # 3 次尝试全部失败后才抛出

        runner = ReviewRunner(
            run_id="run_retry", config=config,
            model_client=RetryingAdapter(),
            registry=registry, workspace_executor=FakeToolExecutor(),
            store=SQLiteEventStore(":memory:"), clock=FakeClock(),
            global_tool_budget=BudgetTracker(RunConfig(model_id="m"), FakeClock()),
        )
        report = runner.review(
            __import__("tests.test_review_agent", fromlist=["make_review_request"])
            .make_review_request())
        # 第一次 Runtime 调用的 3 次尝试（含 Gateway 内部重试）全部计入独立预算
        assert report.status == "INCONCLUSIVE"
        assert reserve_calls == [1, 2, 3]
