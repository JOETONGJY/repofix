"""M1 交付物：生成一条 Scripted 端到端 Trace 样例（真实 Runtime 执行 → SQLite 导出）。

运行：.venv/Scripts/python scripts/export_m1_sample_trace.py
输出：repofix-data/runs/<run_id>/trajectory.jsonl + result 摘要打印
"""
import sys
from pathlib import Path

from tests.conftest import (
    FakeClock,
    FakeToolExecutor,
    FakeVerifier,
    ScriptedModelClient,
    make_tool_call,
    model_response,
)

from repofix.contracts import (
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
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools

RUN_ID = "run_m1_sample_001"


def main() -> int:
    registry = ToolRegistry()
    register_default_tools(registry)

    executor = FakeToolExecutor()
    executor.register(
        "apply_patch",
        ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED, content="patch applied; sha256=3f2a..."),
    )
    executor.register(
        "run_command",
        ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED, content="1 passed in 0.02s", exit_code=0),
    )

    verifier_report = VerificationReport(
        candidate_revision="m1-no-workspace",
        patch_status=PatchStatus.VALID,
        test_status=TestStatus.PASSED,
        outcome=VerificationOutcome.PASSED,
    )

    responses = [
        model_response("d1", [make_tool_call("c1", "search_code", pattern="validate_password")]),
        model_response("d2", [make_tool_call("c2", "read_file", path="auth.py")]),
        model_response(
            "d3",
            [
                make_tool_call("c3", "apply_patch", path="auth.py", patch="--- a/auth.py\n+++ b/auth.py"),
                make_tool_call("c4", "run_command", argv=["python", "-m", "pytest", "tests/test_auth.py", "-q"]),
            ],
        ),
        model_response("d4", [make_tool_call("f1", "finish", summary="修复登录密码验证 Bug；可见测试通过")]),
    ]

    store = SQLiteEventStore(":memory:")
    runner = AgentRunner(
        run_id=RUN_ID,
        task=TaskSpec(
            instruction="Fix the login validation bug",
            repo_path="./demo-repo",
            base_commit="9d1c0fe",
            test_command="python -m pytest tests/test_auth.py -q",
        ),
        config=RunConfig(model_id="scripted-model", review_enabled=False),
        model_client=ScriptedModelClient(responses),
        registry=registry,
        dispatcher=ToolDispatcher(registry, executor),
        store=store,
        verifier=FakeVerifier([verifier_report]),
        clock=FakeClock(),
    )
    result = runner.run()

    out_dir = Path("repofix-data") / "runs" / RUN_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectory.jsonl").write_text(store.export_trajectory(RUN_ID), encoding="utf-8")

    print(f"runtime_status:      {result.runtime_status.value}")
    print(f"termination_reason:  {result.termination_reason}")
    print(f"verification_status: {result.verification_status.value}")
    print(f"modified_files:      {result.modified_files}")
    print(f"model_calls:         {result.model_calls}")
    print(f"tool_calls:          {result.tool_calls}")
    print(f"token_usage:         {result.token_usage}")
    print(f"trace:               {out_dir / 'trajectory.jsonl'}")
    return 0 if result.runtime_status is RunStatus.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
