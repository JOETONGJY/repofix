"""P1-3 回归：search_code 的新参数必须完整穿过 Schema → Dispatcher → Executor。

接口遗漏的表现是"默认值生效、模型不可控"：DockerWorkspace.search_code 早已支持
max_per_file / context，但若 SearchCodeArgs 没声明它们，extra="forbid" 会让模型
显式传入时直接 INVALID_ARGUMENT，而 System Prompt 又告诉模型支持上下文行。
这组测试锁定三层：Schema 暴露、边界校验、参数原样到达执行器。
"""
import pytest

from repofix.contracts import AgentActor, ExecutionStatus, ToolErrorCode
from repofix.tools.dispatcher import ToolDispatcher
from tests.conftest import FakeToolExecutor, make_tool_call


def search_code_function(registry) -> dict:
    view = {item["function"]["name"]: item["function"]
            for item in registry.view_for(AgentActor.MAIN)}
    assert "search_code" in view
    return view["search_code"]


class TestSearchCodeSchema:
    def test_schema_exposes_max_per_file_and_context(self, registry):
        params = search_code_function(registry)["parameters"]["properties"]
        assert {"pattern", "path", "regex", "glob", "limit", "max_per_file", "context"} <= set(params)

    def test_bounds_are_limited(self, registry):
        params = search_code_function(registry)["parameters"]["properties"]
        assert params["max_per_file"]["minimum"] == 1
        assert params["max_per_file"]["maximum"] == 20
        assert params["context"]["minimum"] == 0
        assert params["context"]["maximum"] == 5

    def test_defaults_match_workspace_signature(self, registry):
        params = search_code_function(registry)["parameters"]["properties"]
        assert params["max_per_file"]["default"] == 5
        assert params["context"]["default"] == 0

    def test_description_documents_both_params(self, registry):
        description = search_code_function(registry)["description"]
        assert "max_per_file" in description
        assert "context" in description


class TestSearchCodeDispatcher:
    def test_valid_new_args_pass_validation(self, registry):
        dispatcher = ToolDispatcher(registry, FakeToolExecutor())
        call = make_tool_call("c1", "search_code", pattern="needle",
                              max_per_file=10, context=3)
        assert dispatcher.validate(call, AgentActor.MAIN) is None

    @pytest.mark.parametrize("arguments", [
        {"max_per_file": 0},
        {"max_per_file": 21},
        {"context": -1},
        {"context": 6},
    ])
    def test_out_of_range_rejected(self, registry, arguments):
        dispatcher = ToolDispatcher(registry, FakeToolExecutor())
        call = make_tool_call("c1", "search_code", pattern="needle", **arguments)
        result = dispatcher.validate(call, AgentActor.MAIN)
        assert result is not None
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT

    def test_unknown_field_still_rejected(self, registry):
        """extra="forbid" 的边界不能因新增字段而放松。"""
        dispatcher = ToolDispatcher(registry, FakeToolExecutor())
        call = make_tool_call("c1", "search_code", pattern="needle", not_a_field=1)
        result = dispatcher.validate(call, AgentActor.MAIN)
        assert result is not None
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT

    def test_arguments_reach_executor_intact(self, registry):
        executor = FakeToolExecutor()
        dispatcher = ToolDispatcher(registry, executor)
        call = make_tool_call("c1", "search_code", pattern="needle", path="src",
                              max_per_file=9, context=2)
        assert dispatcher.validate(call, AgentActor.MAIN) is None
        result = dispatcher.execute(call)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert executor.calls == [("search_code", {
            "pattern": "needle", "path": "src", "max_per_file": 9, "context": 2,
        })]
