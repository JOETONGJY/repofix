"""Context Engine 完整版测试：CX-01~06（specs/04）+ 预算数学 + Runner 集成（CX-04）。"""
import json

import pytest

from repofix.context import (
    ContextBudgetExceeded,
    ContextBuilder,
    ContextPairingError,
    estimate_tokens,
)
from repofix.context.tokens import available_input_tokens
from repofix.contracts import (
    AgentActor,
    EventType,
    ExecutionStatus,
    FailureLayer,
    ModelRequest,
    ModelResponse,
    PatchStatus,
    RunConfig,
    RunStatus,
    TaskSpec,
    TestStatus,
    ToolCall,
    ToolResult,
    VerificationOutcome,
    VerificationReport,
    WorkingState,
)
from repofix.gateway.transport import TransportResult
from tests.conftest import (
    FakeClock,
    FakeToolExecutor,
    FakeVerifier,
    ScriptedModelClient,
    make_tool_call,
    model_response,
)

TASK_ARGS = {"instruction": "Fix the login validation bug", "repo_path": "./repo", "base_commit": "abc123"}

def passed_report():
    from repofix.contracts import PatchStatus, VerificationOutcome, VerificationReport
    from repofix.contracts import TestStatus as _TS
    return VerificationReport(candidate_revision="v", patch_status=PatchStatus.VALID,
                              test_status=_TS.PASSED, outcome=VerificationOutcome.PASSED)

class FakeTransportLocal:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def post(self, path, payload, timeout):
        self.calls.append(payload)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def build_builder(**kwargs) -> ContextBuilder:
    builder = ContextBuilder("system rules", **kwargs)
    builder.start_task("Fix the login validation bug", base_commit="abc123")
    return builder


def add_turn(builder: ContextBuilder, decision: str, calls: list[ToolCall], results: list[ToolResult]) -> None:
    builder.record_response(model_response(decision, calls))
    builder.record_results(results, calls)


def read_result(call_id: str, path: str, body: str, sha: str) -> ToolResult:
    return ToolResult(call_id=call_id, status=ExecutionStatus.SUCCEEDED,
                      content=f"1\t{body}\n[sha256={sha}]")


class TestBudgetMath:
    def test_available_formula(self):
        """B_input = B_context − B_output_reserve − B_safety（10% 窗口）。"""
        assert available_input_tokens(1000, 100) == 800  # 1000-100-100
        with pytest.raises(ValueError):
            available_input_tokens(100, 95)  # 窗口太小

    def test_estimate_deterministic(self):
        messages = [{"role": "user", "content": "a" * 40}]
        assert estimate_tokens(messages) == 8 + 40 // 3  # 协议开销 + 保守 3 字符/token

    def test_no_window_means_no_trimming(self):
        """context_window_tokens=None（默认）：M1 行为不变，不裁剪。"""
        builder = build_builder()
        big = "X" * 5000
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="a.py")],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content=big)])
        messages = builder.next_messages(WorkingState(run_id="r"))
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]
        assert big in tool_msg["content"]  # 无预算约束时不截断


class TestCx01OutputTruncation:
    def test_command_output_capped_with_error_tail_kept(self):
        """CX-01：长命令输出按软预算截断且保留错误尾部（错误通常在末尾）。"""
        builder = build_builder(tool_output_soft_chars=100)
        content = "A" * 900 + " ERROR: segmentation fault in test_login"
        add_turn(builder, "d1", [make_tool_call("c1", "run_command", argv=["python", "-m", "pytest"])],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content=content, exit_code=1)])
        messages = builder.next_messages(WorkingState(run_id="r"))
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]
        assert "context budget" in tool_msg["content"]  # 明确标记，非静默
        assert "ERROR: segmentation fault" in tool_msg["content"]  # 错误尾部保留
        assert len(tool_msg["content"]) < 300  # 远小于原始 900+ 字符

    def test_read_file_gets_higher_allowance(self):
        """双层限制裁决：read_file（源码）配额更高，不做无差别截断。"""
        builder = build_builder(tool_output_soft_chars=100)
        body = "x = " + "1" * 250  # 260 字符 < read_file 配额 400
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="a.py")],
                 [read_result("c1", "a.py", body, "h1")])
        messages = builder.next_messages(WorkingState(run_id="r"))
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]
        assert "context budget" not in tool_msg["content"]  # 未被截断


class TestCx02PairingIntegrity:
    @staticmethod
    def assert_no_orphans(messages: list[dict]) -> None:
        call_ids_in_assistant: set = set()
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                call_ids_in_assistant |= {c["id"] for c in message["tool_calls"]}
            if message.get("role") == "tool":
                assert message["tool_call_id"] in call_ids_in_assistant, (
                    f"orphan tool result {message['tool_call_id']}"
                )

    def test_pairing_preserved_under_trimming(self):
        """CX-02：裁剪后任意轮次 ToolCall 与 ToolResult 一一配对，无孤儿。"""
        builder = build_builder(context_window_tokens=900, output_reserve_tokens=0)
        for i in range(1, 4):
            add_turn(
                builder, f"d{i}",
                [make_tool_call(f"c{i}a", "read_file", path=f"f{i}.py"),
                 make_tool_call(f"c{i}b", "search_code", pattern="x")],
                [ToolResult(call_id=f"c{i}a", status=ExecutionStatus.SUCCEEDED, content="R" * 800),
                 ToolResult(call_id=f"c{i}b", status=ExecutionStatus.SUCCEEDED, content="m")],
            )
        messages = builder.next_messages(WorkingState(run_id="r"))
        self.assert_no_orphans(messages)

    def test_record_results_mismatch_raises(self):
        builder = build_builder()
        builder.record_response(model_response("d1", [make_tool_call("c1", "read_file", path="a.py")]))
        with pytest.raises(ContextPairingError):
            builder.record_results(
                [ToolResult(call_id="wrong", status=ExecutionStatus.SUCCEEDED, content="x")],
                [make_tool_call("c1", "read_file", path="a.py")],
            )


class TestCx03KeyFactsSurvive:
    def test_task_and_working_memory_survive_aggressive_trimming(self):
        """CX-03：压缩后任务约束、已修改文件、未解决错误仍在上下文。"""
        builder = build_builder(context_window_tokens=800, output_reserve_tokens=0)
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="old.py")],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content="R" * 900)])
        builder.record_verification_feedback(
            VerificationReport(candidate_revision="r", patch_status=PatchStatus.VALID,
                               test_status=TestStatus.FAILED, outcome=VerificationOutcome.FAILED,
                               failed_tests=["tests/test_auth.py::test_x"])
        )
        state = WorkingState(run_id="r", modified_files=["auth.py"],
                             unresolved_errors=["empty password accepted"])
        messages = builder.next_messages(state)
        joined = json.dumps(messages, ensure_ascii=False)
        assert "Fix the login validation bug" in joined  # 任务约束
        assert "auth.py" in joined  # 已修改文件（Working Memory）
        assert "empty password accepted" in joined  # 未解决错误
        assert "test_x" in joined  # 验证失败反馈（note 不丢弃）

    def test_working_memory_regeserated_not_duplicated(self):
        """Working Memory 是视图：多次 build 不产生重复块。"""
        builder = build_builder()
        state = WorkingState(run_id="r", modified_files=["auth.py"])
        first = builder.next_messages(state)
        second = builder.next_messages(state)
        wm_blocks = [m for m in first if "[Working Memory]" in m.get("content", "")]
        wm_blocks2 = [m for m in second if "[Working Memory]" in m.get("content", "")]
        assert len(wm_blocks) == 1 and len(wm_blocks2) == 1


class TestCx04BudgetExceeded:
    def test_emergency_then_controlled_failure(self):
        """CX-04：紧急压缩一次；仍超限 → ContextBudgetExceeded 受控失败。"""
        builder = build_builder(context_window_tokens=100, output_reserve_tokens=0)
        builder.record_protocol_error("big " * 300)  # note 不丢弃 → 永远放不下
        with pytest.raises(ContextBudgetExceeded):
            builder.next_messages(WorkingState(run_id="r"))

    def test_oldest_turns_dropped_before_failing(self):
        """有旧轮次可丢时先丢轮次，不轻易失败。"""
        builder = build_builder(context_window_tokens=320, output_reserve_tokens=0)
        for i in range(1, 4):
            add_turn(builder, f"d{i}", [make_tool_call(f"c{i}", "read_file", path=f"f{i}.py")],
                     [ToolResult(call_id=f"c{i}", status=ExecutionStatus.SUCCEEDED, content="R" * 5000)])
        messages = builder.next_messages(WorkingState(run_id="r"))
        joined = json.dumps(messages)
        assert '"c1"' not in joined and '"c2"' not in joined  # 旧轮次整体移除
        assert '"c3"' in joined  # 最近轮次保留（assistant + tool 消息同在）

    def test_runner_records_event_and_fails_controlled(self, registry):
        """Runner 集成：CX-04 → CONTEXT_BUDGET_EXCEEDED 事件 + FAILED(TOOL_SYSTEM)。"""
        from repofix.gateway import ModelProtocolError  # noqa: F401 —— 确保导入路径稳定
        from repofix.runtime.runner import AgentRunner
        from repofix.state import SQLiteEventStore
        from repofix.tools.dispatcher import ToolDispatcher
        from tests.conftest import FakeToolExecutor, ScriptedModelClient

        cfg = RunConfig(model_id="m", review_enabled=False,
                        context_window_tokens=100, output_reserve_tokens=0)
        big_task = TaskSpec(instruction="task " * 2000, repo_path="./repo", base_commit="abc123")
        store = SQLiteEventStore(":memory:")
        runner = AgentRunner(
            run_id="run_cx04", task=big_task, config=cfg,
            model_client=ScriptedModelClient([]), registry=registry,
            dispatcher=ToolDispatcher(registry, FakeToolExecutor()), store=store,
            verifier=FakeVerifier([]), clock=FakeClock(),
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.FAILED
        assert result.termination_reason == "context_budget_exceeded"
        run = store.get_run("run_cx04")
        assert run["fail_layer"] == FailureLayer.TOOL_SYSTEM.value
        types = [e.type for e in store.get_events("run_cx04")]
        assert EventType.CONTEXT_BUDGET_EXCEEDED in types


class TestCx05TurnUnitSelection:
    def test_trim_by_complete_turns_no_orphans(self):
        """CX-05：裁剪以完整轮次为单位；不出现孤立 Result 消息。"""
        builder = build_builder(context_window_tokens=500, output_reserve_tokens=0)
        for i in range(1, 4):
            add_turn(
                builder, f"d{i}",
                [make_tool_call(f"c{i}a", "read_file", path=f"f{i}.py"),
                 make_tool_call(f"c{i}b", "run_command", argv=["python", "-m", "pytest"])],
                [ToolResult(call_id=f"c{i}a", status=ExecutionStatus.SUCCEEDED, content="R" * 600),
                 ToolResult(call_id=f"c{i}b", status=ExecutionStatus.SUCCEEDED, content="ok", exit_code=0)],
            )
        messages = builder.next_messages(WorkingState(run_id="r"))
        assistant_ids = set()
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                ids = {c["id"] for c in message["tool_calls"]}
                # 该轮次的 assistant 与其全部 tool 消息必须同时在场
                following_tools = [m for m in messages if m.get("role") == "tool"
                                   and m["tool_call_id"] in ids]
                assert len(following_tools) == len(ids)
                assistant_ids |= ids
            if message.get("role") == "tool":
                assert message["tool_call_id"] in assistant_ids
        # 被裁掉的轮次：assistant 与 tool 消息一同消失（由上两重断言保证）


class TestCx06FileVersionInvalidation:
    def test_modified_after_read_marked_stale(self):
        """CX-06：文件修改后未重读 → Working Memory 不标记"已确认最新"。"""
        builder = build_builder()
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="auth.py")],
                 [read_result("c1", "auth.py", "old body", "hash_old")])
        add_turn(builder, "d2", [make_tool_call("c2", "apply_patch", path="auth.py", patch="x")],
                 [ToolResult(call_id="c2", status=ExecutionStatus.SUCCEEDED,
                             content="patch applied to auth.py; new sha256=hash_new")])
        state = WorkingState(run_id="r", modified_files=["auth.py"])
        messages = builder.next_messages(state)
        wm = [m for m in messages if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "STALE READ - re-read before editing" in wm
        assert "confirmed current" not in wm

    def test_reread_after_modification_confirmed_current(self):
        builder = build_builder()
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="auth.py")],
                 [read_result("c1", "auth.py", "old body", "hash_old")])
        add_turn(builder, "d2", [make_tool_call("c2", "apply_patch", path="auth.py", patch="x")],
                 [ToolResult(call_id="c2", status=ExecutionStatus.SUCCEEDED,
                             content="patch applied to auth.py; new sha256=hash_new")])
        add_turn(builder, "d3", [make_tool_call("c3", "read_file", path="auth.py")],
                 [read_result("c3", "auth.py", "new body", "hash_new")])
        state = WorkingState(run_id="r", modified_files=["auth.py"])
        messages = builder.next_messages(state)
        wm = [m for m in messages if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "confirmed current" in wm
        assert "STALE READ" not in wm

    def test_modified_without_read_marked_not_read(self):
        builder = build_builder()
        add_turn(builder, "d1", [make_tool_call("c1", "apply_patch", path="new.py", patch="x")],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED,
                             content="patch applied to new.py; new sha256=hash_x")])
        state = WorkingState(run_id="r", modified_files=["new.py"])
        messages = builder.next_messages(state)
        wm = [m for m in messages if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "not read yet" in wm

    def test_unmodified_read_stays_confirmed(self):
        builder = build_builder()
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="stable.py")],
                 [read_result("c1", "stable.py", "body", "hash_s")])
        messages = builder.next_messages(WorkingState(run_id="r"))
        wm = [m for m in messages if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "Modified files: none" in wm  # 未修改：不产生过期警告


class TestGw05Compatibility:
    def test_reasoning_content_still_carried(self):
        """M3-1 GW-05 行为在完整版 Context 中保持。"""
        builder = build_builder()
        response = ModelResponse(
            decision_id="d1",
            message={"role": "assistant", "content": None},
            tool_calls=[make_tool_call("c1", "read_file", path="a.py")],
            provider_metadata={"reasoning_content": "thinking..."},
        )
        builder.record_response(response)
        builder.record_results(
            [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content="data")],
            [make_tool_call("c1", "read_file", path="a.py")],
        )
        messages = builder.next_messages(WorkingState(run_id="r"))
        assistant = [m for m in messages if m.get("role") == "assistant"][0]
        assert assistant["reasoning_content"] == "thinking..."


class TestM32ReviewRegressions:
    """M3-2 源码评审（基线 71f473b）修订回归。"""

    def test_p01_read_file_hash_survives_truncation(self):
        """P0-1：长 read_file 截断后仍保留 sha256 与行号元数据。"""
        import re as _re

        builder = build_builder(tool_output_soft_chars=100)
        sha = "a" * 64
        body = "L" * 900
        content = f"{body}\n[lines 1-200 of 500]\n[sha256={sha}]"
        add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="big.py")],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content=content)])
        messages = builder.next_messages(WorkingState(run_id="r"))
        tool_msg = [m for m in messages if m.get("role") == "tool"][0]["content"]
        assert f"[sha256={sha}]" in tool_msg  # 模型仍能拿到 apply_patch 所需 Hash
        assert "[lines 1-200 of 500]" in tool_msg
        assert "context budget" in tool_msg  # 正文确实被裁剪
        assert not _re.search(r"\[sha256=[0-9a-f]{64}\]", tool_msg.replace(f"[sha256={sha}]", ""))
        assert body[:50] in tool_msg  # 正文头部保留

    def test_p02_tools_schema_and_reasoning_counted(self):
        """P0-2：工具 Schema 开销计入预算——对照：带 Schema 时轮次被挤出，不带时保留。"""
        import json as _json

        big_tool = [{
            "type": "function",
            "function": {"name": "read_file", "description": "d" * 600,
                         "parameters": {"type": "object", "properties": {}}},
        }]

        def build(with_tools: bool) -> ContextBuilder:
            builder = ContextBuilder(
                "system rules", context_window_tokens=400, output_reserve_tokens=0,
                tools_payload=big_tool if with_tools else None,
            )
            builder.start_task("Fix the bug", base_commit="abc")
            add_turn(builder, "d1", [make_tool_call("c1", "read_file", path="a.py")],
                     [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED, content="R" * 600)])
            return builder

        state = WorkingState(run_id="r")
        with_tools = _json.dumps(build(True).next_messages(state))
        without_tools = _json.dumps(build(False).next_messages(state))
        assert '"c1"' not in with_tools  # Schema 挤占预算：轮次被整体移除
        assert '"c1"' in without_tools  # 无 Schema：同一轮次放得下 → 开销确实被计入

    def test_p02_reasoning_content_in_estimate(self):
        """P0-2：reasoning_content 计入 Token 估算。"""
        from repofix.context import estimate_message_tokens

        base = estimate_message_tokens({"role": "assistant"})
        with_reasoning = estimate_message_tokens({"role": "assistant", "reasoning_content": "x" * 300})
        assert with_reasoning - base >= 100

    def test_p1_command_invalidates_confirmed_read(self):
        """P1-1：run_command 之后，先前已确认的读取认知降级为 STALE。"""
        builder = build_builder()
        add_turn(builder, "d1", [make_tool_call("c1", "apply_patch", path="auth.py", patch="x")],
                 [ToolResult(call_id="c1", status=ExecutionStatus.SUCCEEDED,
                             content="patch applied to auth.py; new sha256=hash_new")])
        add_turn(builder, "d2", [make_tool_call("c2", "read_file", path="auth.py")],
                 [read_result("c2", "auth.py", "new body", "hash_new")])
        state = WorkingState(run_id="r", modified_files=["auth.py"])
        wm = [m for m in builder.next_messages(state)
              if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "confirmed current" in wm  # 命令前：读取认知有效
        add_turn(builder, "d3", [make_tool_call("c3", "run_command",
                                                argv=["python", "-m", "pytest"])],
                 [ToolResult(call_id="c3", status=ExecutionStatus.SUCCEEDED, content="ok", exit_code=0)])
        wm2 = [m for m in builder.next_messages(state)
               if "[Working Memory]" in (m.get("content") or "")][0]["content"]
        assert "STALE READ" in wm2  # 命令后：保守失效
        assert "confirmed current" not in wm2

    def test_p12_scrambled_results_use_correct_strategy(self):
        """P1-2：乱序 ToolResult 按 call_id 配对——截断策略作用于正确的工具。"""
        builder = build_builder(tool_output_soft_chars=100)
        calls = [make_tool_call("c_read", "read_file", path="a.py"),
                 make_tool_call("c_cmd", "run_command", argv=["python", "-m", "pytest"])]
        results = [  # 乱序：命令结果在前，读取结果在后
            ToolResult(call_id="c_cmd", status=ExecutionStatus.SUCCEEDED,
                       content="C" * 900, exit_code=1),
            ToolResult(call_id="c_read", status=ExecutionStatus.SUCCEEDED,
                       content="R" * 600 + f"\n[sha256={'b' * 64}]"),
        ]
        builder.record_response(model_response("d1", calls))
        builder.record_results(results, calls)
        messages = builder.next_messages(WorkingState(run_id="r"))
        by_id = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
        read_content = by_id["c_read"]
        cmd_content = by_id["c_cmd"]
        assert "R" in read_content[:10]  # 读取：保留开头（头部正文）
        assert f"[sha256={'b' * 64}]" in read_content  # 元数据保留（P0-1）
        assert cmd_content.lstrip().startswith("[...context budget")  # 命令：严格配额 + 错误尾部
        assert len(cmd_content) < 200
        assert read_content.startswith("RRR")  # 不是命令输出的尾部策略


class TestM3PolishWiring:
    """M3 集成评审收尾：预算原子占用、真实候选版本、preflight 摘要接线。"""

    def _runner(self, registry, responses, verifier, *, preflight_summary=None,
                patch_hash_provider=None):
        from repofix.runtime.runner import AgentRunner
        from repofix.state import SQLiteEventStore
        from repofix.tools.dispatcher import ToolDispatcher

        store = SQLiteEventStore(":memory:")
        runner = AgentRunner(
            run_id="run_pf", task=TaskSpec(**TASK_ARGS),
            config=RunConfig(model_id="m", review_enabled=False),
            model_client=ScriptedModelClient(responses),
            registry=registry, dispatcher=ToolDispatcher(registry, FakeToolExecutor()),
            store=store, verifier=verifier, clock=FakeClock(),
            preflight_summary=preflight_summary,
            patch_hash_provider=patch_hash_provider,
        )
        return runner, store

    def test_p02_reserve_blocks_gateway_retry_overrun(self):
        """P0-2：预算只剩 1 次，429 后 Gateway 重试被原子占用拒绝。"""
        from repofix.gateway import DeepSeekAdapter
        from repofix.runtime.loop_guard import BudgetTracker, BudgetViolation

        cfg = RunConfig(model_id="m", max_model_calls=2)
        tracker = BudgetTracker(cfg, FakeClock())
        tracker.count_model_call()  # 已用 1，剩 1

        violations: list[str] = []

        def hook():
            try:
                tracker.reserve_model_attempt()
            except BudgetViolation as exc:
                violations.append(exc.reason)
                raise

        ok_body = json.dumps({
            "id": "r", "choices": [{"message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}],
        })
        script = [
            TransportResult(429, "limited"),
            TransportResult(429, "limited"),
            TransportResult(200, ok_body),
        ]
        adapter = DeepSeekAdapter(transport=FakeTransportLocal(script), on_attempt=hook,
                                  sleep=lambda _s: None, retry_limit=2)
        request = ModelRequest(actor=AgentActor.MAIN, model_id="m",
                               messages=[{"role": "user", "content": "hi"}],
                               tools=[], request_id="r")
        # 钩子拒绝重试：BudgetViolation 直接传播（Runner 捕获后转 BUDGET_EXCEEDED）
        from repofix.runtime.loop_guard import BudgetViolation as _BV

        with pytest.raises(_BV):
            adapter.complete(request)
        assert violations == ["max_model_calls"]  # 第二次重试被预算拒绝
        assert tracker.model_calls == 2  # 原子占用：不超上限

    def test_p03_runner_uses_real_patch_hash_provider(self, registry):
        """P0-3：Runner 经 provider 提交真实候选版本，不再使用 M1 占位。"""

        seen: list[str] = []

        class RecordingVerifier(FakeVerifier):
            def verify(self, request):
                seen.append(request.candidate_revision)
                return super().verify(request)

        responses = [model_response("d1", [make_tool_call("f1", "finish", summary="x")])]
        runner, _ = self._runner(
            registry, responses, RecordingVerifier([passed_report()]),
            patch_hash_provider=lambda: "real" + "0" * 56,
        )
        result = runner.run()
        assert result.runtime_status is RunStatus.COMPLETED
        assert seen == ["real" + "0" * 56]  # 不再是 m1-no-workspace 占位

    def test_preflight_summary_recorded_when_provided(self, registry):
        """P1 遗留清理：传入真实摘要才记录 PREFLIGHT_COMPLETED；不传不发占位事件。

        P2 补充：载荷必须原样保留实际生效的档位与窗口值——这是冒烟脚本
        无法验证（不跑 Runtime 主循环）的部分，改由 Scripted Model Trace 锁定。
        """

        finish = [model_response("d1", [make_tool_call("f1", "finish", summary="x")])]
        report = passed_report()

        runner_with, store_with = self._runner(
            registry, finish, FakeVerifier([report]),
            preflight_summary={"steps": "ok", "command_policy": "coding_benchmark",
                               "context_recent_full_turns": 6},
        )
        runner_with.run()
        events_with = store_with.get_events("run_pf")
        assert EventType.PREFLIGHT_COMPLETED in [e.type for e in events_with]
        payload = next(e.payload for e in events_with
                       if e.type is EventType.PREFLIGHT_COMPLETED)
        assert payload["command_policy"] == "coding_benchmark"
        assert payload["context_recent_full_turns"] == 6

        runner_without, store_without = self._runner(registry, finish, FakeVerifier([report]))
        result = runner_without.run()
        assert result.runtime_status is RunStatus.COMPLETED
        types_without = [e.type for e in store_without.get_events("run_pf")]
        assert EventType.PREFLIGHT_COMPLETED not in types_without


class TestRecencyWindow:
    """工具效率 v1：近期完整窗口——旧轮次工具输出确定性摘要，配对完整保留。"""

    def _build_with_turns(self, recent_full_turns: int, n_turns: int = 6, body: str = "x" * 300):
        builder = build_builder(recent_full_turns=recent_full_turns)
        for i in range(n_turns):
            calls = [ToolCall(call_id=f"c{i}", tool_name="read_file",
                              arguments={"path": f"f{i}.py"})]
            results = [read_result(f"c{i}", f"f{i}.py", body, f"sha{i}")]
            add_turn(builder, f"dec{i}", calls, results)
        return builder

    def test_old_turns_elided_recent_full(self):
        builder = self._build_with_turns(recent_full_turns=2, n_turns=6)
        messages = builder.next_messages(WorkingState(run_id="r"))
        rendered = json.dumps(messages, ensure_ascii=False)
        # 最近 2 轮保留完整内容
        assert '"x' + 'x' * 299 + '"' not in rendered  # 防误报占位检查
        full_bodies = sum(1 for m in messages if "x" * 300 in str(m.get("content", "")))
        assert full_bodies == 2
        # 旧轮次占位化：状态与工具名保留
        assert "[output elided" in rendered
        assert rendered.count("[output elided") == 4
        assert "read_file" in rendered

    def test_pairing_intact_after_elide(self):
        """旧轮次占位化后 assistant tool_calls 与 tool 结果仍一一配对。"""
        builder = self._build_with_turns(recent_full_turns=1, n_turns=5)
        messages = builder.next_messages(WorkingState(run_id="r"))
        call_ids = []
        for m in messages:
            if m.get("tool_calls"):
                call_ids += [tc["id"] for tc in m["tool_calls"]]
        tool_ids = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        assert call_ids == tool_ids and len(call_ids) == 5

    def test_disabled_by_default(self):
        builder = self._build_with_turns(recent_full_turns=0, n_turns=4)
        messages = builder.next_messages(WorkingState(run_id="r"))
        assert "[output elided" not in json.dumps(messages, ensure_ascii=False)

    def test_zero_context_budget_scenario(self):
        """预算裁剪与近期窗口叠加时不抛错（emergency 路径兼容）。"""
        builder = ContextBuilder("system rules", context_window_tokens=1200,
                                 output_reserve_tokens=100, recent_full_turns=2)
        builder.start_task("Fix the login validation bug", base_commit="abc123")
        for i in range(4):
            calls = [ToolCall(call_id=f"c{i}", tool_name="read_file", arguments={"path": "f.py"})]
            add_turn(builder, f"dec{i}", calls, [read_result(f"c{i}", "f.py", "x" * 500, "sha")])
        state = WorkingState(run_id="r")
        messages = builder.next_messages(state)  # 触发压缩也不应抛 ContextPairingError
        assert messages
