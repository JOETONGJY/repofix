"""契约 schema 校验测试：specs/01 每个核心模型的合法 / 非法用例。"""
import pytest
from pydantic import ValidationError

from repofix.contracts import (
    AgentActor,
    AgentEvent,
    EventType,
    ExecutionStatus,
    ModelRequest,
    ModelResponse,
    ReviewFinding,
    ReviewReport,
    RunConfig,
    RunResult,
    RunStatus,
    TaskSpec,
    ToolCall,
    ToolResult,
    VerificationReport,
    VerificationStatus,
    WorkingState,
)


class TestRunConfig:
    def test_defaults_match_specs(self):
        config = RunConfig(model_id="m")
        assert config.max_model_calls == 30
        assert config.max_tool_calls == 100
        assert config.max_duration_seconds == 1200
        assert config.command_timeout_seconds == 120
        assert config.model_retry_limit == 2
        assert config.no_progress_repeat_threshold == 3
        assert config.no_progress_terminate_threshold == 5
        assert config.review_enabled is True
        assert config.max_reviewer_model_calls == 3
        assert config.max_reviewer_tool_calls == 8
        assert config.review_fix_model_calls == 3
        assert config.workspace_network == "disabled"

    @pytest.mark.parametrize(
        "field", ["max_model_calls", "max_tool_calls", "max_duration_seconds"]
    )
    def test_budget_must_be_positive(self, field):
        with pytest.raises(ValidationError):
            RunConfig(model_id="m", **{field: 0})

    def test_no_progress_thresholds_configurable(self):
        config = RunConfig(model_id="m", no_progress_repeat_threshold=4, no_progress_terminate_threshold=6)
        assert config.no_progress_repeat_threshold == 4
        assert config.no_progress_terminate_threshold == 6


class TestToolCallAndResult:
    def test_tool_call_keeps_raw_arguments(self):
        call = ToolCall(
            call_id="c1", tool_name="read_file", arguments={"path": "a.py"}, raw_arguments='{"path":"a.py"}'
        )
        assert call.raw_arguments == '{"path":"a.py"}'

    def test_tool_call_requires_id_and_name(self):
        with pytest.raises(ValidationError):
            ToolCall(arguments={})
        with pytest.raises(ValidationError):
            ToolCall(call_id="c1", arguments={})

    def test_tool_result_status_limited_to_three_values(self):
        for status in ExecutionStatus:
            ToolResult(call_id="c1", status=status)
        # UNKNOWN 属于持久化状态，不是 ToolResult.status 的合法值（CONS-02）
        with pytest.raises(ValidationError):
            ToolResult(call_id="c1", status="unknown")

    def test_skipped_result_semantics(self):
        result = ToolResult(call_id="c1", status="skipped", skipped_reason="batch_aborted_after_write_failure")
        assert result.skipped_reason == "batch_aborted_after_write_failure"
        assert result.retryable is False  # 业务失败永远 retryable=False


class TestTaskSpecAndRunResult:
    def test_task_spec_minimal(self):
        spec = TaskSpec(instruction="fix bug", repo_path="./repo", base_commit="abc123")
        assert spec.test_command is None

    def test_run_result_roundtrip(self):
        result = RunResult(
            run_id="run_1",
            runtime_status=RunStatus.COMPLETED,
            termination_reason="finished",
            verification_status=VerificationStatus.PASSED,
            model_calls=3,
            tool_calls=5,
            token_usage={"prompt": 100, "completion": 50},
            duration_ms=1234,
        )
        assert result.review_status is None
        assert result.reviewed_patch_hash is None


class TestModelContracts:
    def test_model_request_actor_typed(self):
        request = ModelRequest(
            actor=AgentActor.MAIN,
            model_id="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            request_id="req_1",
        )
        assert request.actor is AgentActor.MAIN
        with pytest.raises(ValidationError):
            ModelRequest(
                actor="nobody", model_id="m", messages=[], tools=[], request_id="r"
            )

    def test_model_response_defaults(self):
        response = ModelResponse(decision_id="d1")
        assert response.tool_calls == []
        assert response.provider_metadata == {}
        assert response.usage is None  # Usage 缺失时标记未知，不虚构

    def test_agent_event_actor_enum(self):
        event = AgentEvent(
            seq=1, run_id="r", step=0, actor=AgentActor.SYSTEM, type=EventType.RUN_STARTED, ts=1.0, payload={}
        )
        assert event.type is EventType.RUN_STARTED


class TestReviewContracts:
    def test_review_finding_severity_limited(self):
        ReviewFinding(finding_id="f1", file_path="a.py", severity="high", reason="r", evidence="e")
        with pytest.raises(ValidationError):
            ReviewFinding(finding_id="f1", file_path="a.py", severity="critical", reason="r", evidence="e")

    def test_review_report_max_five_findings(self):
        findings = [
            ReviewFinding(finding_id=f"f{i}", file_path="a.py", severity="low", reason="r", evidence="e")
            for i in range(6)
        ]
        with pytest.raises(ValidationError):
            ReviewReport(status="FINDINGS", patch_hash="h", findings=findings)

    def test_review_report_status_limited(self):
        with pytest.raises(ValidationError):
            ReviewReport(status="NEEDS_WORK", patch_hash="h")


class TestWorkingState:
    def test_working_state_defaults(self):
        state = WorkingState(run_id="r1")
        assert state.modified_files == []
        assert state.failed_tests == []
        assert state.unresolved_errors == []
        assert state.updated_at_step == 0


class TestVerificationReport:
    def test_verification_report_fields(self):
        report = VerificationReport(
            candidate_revision="rev1",
            patch_status="valid",
            test_status="passed",
            outcome="passed",
        )
        assert report.failure_kind is None
        assert report.failed_tests == []
