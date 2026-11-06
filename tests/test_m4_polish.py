"""M4 集成评审（复核）收尾回归：预算前置、混合拒批、修正 deadline、hash 复核、factory 降级。"""
import pytest

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
from repofix.runtime.loop_guard import BudgetTracker, BudgetViolation
from repofix.runtime.review_runner import ReviewRunner
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.workspace.errors import WorkspaceError
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


class TestRecheckBoundaries:
    """M4 修订复核的三个边界回归。"""

    def test_boundary1_stage_limit_allows_fix_after_exhaustion(self):
        """边界 1：原始 max_model_calls 耗尽后，修正阶段的 reserve 使用阶段上限。"""
        from repofix.gateway import ModelTransientError

        cfg = RunConfig(model_id="m", review_enabled=True, review_fix_model_calls=2,
                        max_model_calls=1)
        tracker = BudgetTracker(cfg, FakeClock())

        class AttemptAdapter:
            counts_own_attempts = True

            def __init__(self):
                self.on_attempt = None

            def complete(self, request):
                self.on_attempt()
                raise ModelTransientError("429")  # 每次都失败，强制走满尝试

        adapter = AttemptAdapter()
        adapter.on_attempt = tracker.reserve_model_attempt
        with pytest.raises(ModelTransientError):
            adapter.complete(None)  # 第 1 次（=原始预算耗尽）
        assert tracker.model_calls == 1

        # FINDINGS → 修正模式：阶段上限 = 1 + 2
        tracker.set_stage_model_limit(1 + 2)
        with pytest.raises(ModelTransientError):
            adapter.complete(None)  # 第 2 次：不再被原始预算拒绝
        with pytest.raises(ModelTransientError):
            adapter.complete(None)  # 第 3 次：阶段内
        assert tracker.model_calls == 3
        with pytest.raises(BudgetViolation):
            tracker.reserve_model_attempt()  # 第 4 次：突破阶段上限 → 拒绝

    def test_boundary2_tools_exhausted_still_can_submit(self, registry):
        """边界 2：8 次工具配额用尽后，submit_review 仍可单独提交结论。"""
        calls = [make_tool_call(f"c{i}", "read_file", path=f"f{i}.py") for i in range(8)]
        submit = make_tool_call("sub", "submit_review", status="APPROVED", findings=[])
        runner = ReviewRunner(
            run_id="run_b2", config=RunConfig(model_id="m"),
            model_client=ScriptedModelClient([
                model_response("rd1", calls),        # 8 次只读：配额用尽
                model_response("rd2", [submit]),      # 仅提交结论：必须被接受
            ]),
            registry=registry, workspace_executor=FakeToolExecutor(),
            store=SQLiteEventStore(":memory:"), clock=FakeClock(),
            global_tool_budget=BudgetTracker(RunConfig(model_id="m"), FakeClock()),
        )
        report = runner.review(
            __import__("tests.test_review_agent", fromlist=["make_review_request"])
            .make_review_request())
        assert report.status == "APPROVED"  # 不因轮首工具预算检查提前 INCONCLUSIVE
        assert report.patch_hash == "0" * 64

    def test_boundary3_recheck_failure_downgrades_approval(self, registry):
        """边界 3：审查后 hash 查询失败 → APPROVED 降级 INCONCLUSIVE（不默认接受）。"""
        config = RunConfig(model_id="m", review_enabled=True)
        registry2 = ToolRegistry()
        register_default_tools(registry2)
        calls = {"n": 0}

        def failing_provider():
            calls["n"] += 1
            if calls["n"] == 1:
                return "1" * 64  # 审查前：正常
            raise WorkspaceError(  # 复核：失败
                "workspace probe failed")

        class ScriptedReview:
            def review(self, request):
                from repofix.contracts import ReviewReport
                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        runner = AgentRunner(
            run_id="run_b3", task=TaskSpec(**TASK_ARGS), config=config,
            model_client=ScriptedModelClient(
                [model_response("m1", [make_tool_call("f1", "finish", summary="x")])]),
            registry=registry2, dispatcher=ToolDispatcher(registry2, FakeToolExecutor()),
            store=SQLiteEventStore(":memory:"),
            verifier=FakeVerifier([_passed()]), clock=FakeClock(),
            patch_hash_provider=failing_provider,
            review_runner_factory=lambda rid: ScriptedReview(),
            reviewer_executor=FakeToolExecutor(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert result.review_status == "INCONCLUSIVE"  # 不默认接受
        # 从事件 summary 验证降级原因（复核失败 → INCONCLUSIVE 而非默认接受）


class TestRecheckRound2:
    """M4 复核第二轮：修正阶段 API 硬上限精确对齐 + Trace 最终状态一致性。"""

    def test_boundary_a_exact_deadline_with_retries(self, registry, monkeypatch):
        """30/5/3 场景：连续 429 下第 9 次请求不会发出。"""
        import repofix.runtime.review_runner as _rr  # noqa: F401
        from repofix.runtime.loop_guard import BudgetTracker as _Real

        cfg = RunConfig(model_id="m", review_enabled=True, review_fix_model_calls=3,
                        max_model_calls=30)
        tracker = _Real(cfg, FakeClock())
        for _ in range(5):
            tracker.reserve_model_attempt()  # Main 已用 5 次
        assert tracker.model_calls == 5

        tracker.set_stage_model_limit(5 + 3)  # 修正阶段 deadline = 8

        attempts: list[int] = []

        class Adapter:
            counts_own_attempts = True

            def __init__(self):
                self.on_attempt = None

            def complete(self, request):
                for _ in range(3):  # 每次调用内部重试至多 2 次（模拟 429）
                    self.on_attempt()
                    attempts.append(len(attempts) + 1)
                raise ModelTransientError("429 after retries")  # 全部尝试失败后才抛

        adapter = Adapter()
        adapter.on_attempt = tracker.reserve_model_attempt

        # Main 已用 5 次，修正阶段配额 3 次；每次 Runtime 调用消耗 3 次尝试
        # → complete#1 用掉第 6/7/8 次（阶段配额耗尽）；
        # → complete#2 的首次尝试 = 全局第 9 次，被 reserve 硬上限拒绝——根本不会发出
        with pytest.raises(ModelTransientError):
            adapter.complete(None)
        assert attempts == [1, 2, 3]
        assert tracker.model_calls == 8  # 精确到 deadline（5+3），无越界

        with pytest.raises(BudgetViolation):
            adapter.complete(None)  # 第 9 次尝试被 reserve 硬上限拒绝
        assert len(attempts) == 3  # 第 9 次请求根本不会发出

    def test_boundary_a_stage_reset_between_phases(self):
        """阶段上限在两次 set 之间正确替换（不保留宽松值）。"""
        from repofix.runtime.loop_guard import BudgetTracker

        tracker = BudgetTracker(RunConfig(model_id="m", max_model_calls=30), FakeClock())
        tracker.set_stage_model_limit(8)
        # 再设置一个更紧的值：直接替换而非 max 保留
        tracker.set_stage_model_limit(6)
        for _ in range(6):
            tracker.reserve_model_attempt()
        with pytest.raises(BudgetViolation):
            tracker.reserve_model_attempt()  # 第 7 次被拒（上限=6）
        assert tracker.model_calls == 6

    def test_boundary_b_downgrade_recorded_in_trace(self, registry):
        """P1：APPROVED 降级 INCONCLUSIVE 后，Trace 表达最终有效状态。"""
        config = RunConfig(model_id="m", review_enabled=True)
        registry2 = ToolRegistry()
        register_default_tools(registry2)

        # 消耗顺序：verification "1" → review "2" → 复核 "3" ≠ "2"（漂移被记录）
        hash_values = ["1" * 64, "2" * 64, "3" * 64]

        def drifting_provider():
            return hash_values.pop(0) if hash_values else "3" * 64

        class ScriptedReview:
            def review(self, request):
                from repofix.contracts import ReviewReport
                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        runner = AgentRunner(
            run_id="run_tr", task=TaskSpec(**TASK_ARGS), config=config,
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
        assert result.review_status == "INCONCLUSIVE"
        # Trace 中有两条 REVIEW_FINISHED：原始 APPROVED（可追溯）+ 最终 INCONCLUSIVE
        reviews = [e.payload for e in runner._store.get_events("run_tr")
                   if e.type.value == "review_finished"]
        # Trace 最终状态：降级事件记录最终 INCONCLUSIVE 与原始 APPROVED（可追溯）。
        # （Stub Review 不经过 ReviewRunner 类，故原始 APPROVED 不单独成事件）
        assert len(reviews) == 1
        assert reviews[0]["status"] == "INCONCLUSIVE"
        assert reviews[0].get("original_status") == "APPROVED"
        assert "workspace changed" in reviews[0]["summary"]
