"""Test Agent：一次性测试生成（MVP v0.4 功能 B，One-shot Test Generation）。

职责边界：
- 只有一次真实模型调用（tools=[]），由调用方以 retry_limit=0 构造 Adapter 保证
  「最多 test_agent_max_model_calls 次尝试、模型错误不重试」；
- 模型只输出结构化 JSON（TestCandidate 或 cannot_generate 声明），不操作文件系统、
  不决定测试路径、不返回可执行 Shell；
- 生成输入只包含：Issue 文本、base_commit、可选 Repository Overview、按 Issue
  关键词挑出的少量原始代码（≤3 个文件、内容有界）；不发送整个仓库；
- 输入选取基于 base_commit 的原始代码（不读 Main Agent 修改后的代码——Test Agent
  的整个工作区是独立 Clone）；
- 模型调用事件以 actor=TEST_AGENT 写入 Trace，不与 Main Agent 的请求混记。
"""
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from repofix.contracts import AgentActor, EventType, ModelRequest
from repofix.gateway.client import ModelClient, ModelClientError
from repofix.state.store import SQLiteEventStore
from repofix.test_agent.schemas import TestCandidate

# 生成输入预算（任务书 §6：最多 2–3 个文件，不发送整个仓库）
MAX_SNIPPET_FILES = 3
MAX_SNIPPET_CHARS = 6000
MAX_SNIPPET_BYTES = 8 * 1024  # 单个候选文件内容读取上限（字节）
# 生成输出预算：需容纳 ≤8KiB 测试源码 + 说明，独立于 Main 的 output_reserve
GENERATION_MAX_OUTPUT_TOKENS = 8192

TEST_AGENT_SYSTEM_RULES = (
    "You are RepoFix Test Agent. Generate ONE pytest test that reproduces the reported bug "
    "against the CURRENT (buggy) code. Output ONLY a JSON object, no other text:\n"
    '{"test_source": "<complete Python test file content>", "explanation": "<one short paragraph>"}\n'
    "Hard rules for test_source:\n"
    "- It will be saved as tests/test_repofix_generated.py and run with: "
    "python -m pytest -q tests/test_repofix_generated.py\n"
    "- One single Python file, at most 8 KiB, valid syntax, with at least one function named test_*\n"
    "- Import project modules as if run from the repository root (repo root is on sys.path)\n"
    "- Assert the CORRECT expected behavior described in the issue, not the buggy behavior\n"
    "- No subprocess, no network (socket/requests/urllib), no absolute paths, no path outside the repository\n"
    "- Never modify production code; the file must only contain the test\n"
    "If you cannot determine the expected correct behavior with reasonable confidence, "
    "output instead: {\"cannot_generate\": true, \"reason\": \"<why>\"} - do not invent assertions."
)


@dataclass
class TestGenerationResult:
    """一次生成的完整结果（含失败与拒绝路径，供报告与 Trace 使用）。"""

    candidate: TestCandidate | None = None
    declined: bool = False
    decline_reason: str = ""
    # invalid_schema：JSON 合法但不符合 TestCandidate 合同 → 归入 INVALID_TEST 类
    invalid_schema_reason: str = ""
    error: str = ""  # 模型调用失败（API/超时）原因
    model_calls: int = 0
    usage: dict | None = None
    duration_ms: int = 0
    events: list = field(default_factory=list)


def select_issue_files(
    repo_root, instruction: str, max_files: int = MAX_SNIPPET_FILES
) -> list[tuple[str, str]]:
    """按共享检索内核（v0.5.1）选出候选位置，返回代码窗口（v0.5.2）。

    评分委托 CodeRetriever（锚定 > 符号名 > 关键词 分层排序）；扫描范围
    保留 Test Agent 自己的召回面（500 文件/深度 6，评审约定 1）。片段不再
    固定读文件头：锚定行号 ±40 行 / AST 函数范围 / 文件头回退，重叠窗口
    合并、总字符预算约束（此前 traceback 指向第 900 行时片段看不到目标
    代码）。确定性：同基线同输入 ⇒ 同结果。敏感文件绝不读取。
    """
    from pathlib import Path

    from repofix.context.retrieval import (
        TEST_AGENT_SCOPE,
        CodeLocation,
        CodeRetriever,
        build_code_windows,
    )

    root = Path(repo_root)
    retriever = CodeRetriever(root, TEST_AGENT_SCOPE)
    result = retriever.rank(instruction)
    locations = [loc for loc in result.locations if loc.score > 0][:max_files]
    if not locations:  # 无命中：退化取浅层源码（保持确定性，窗口为文件头）
        locations = [
            CodeLocation(path=rel, tier=3, score=0, reasons=["structural fallback"])
            for rel in retriever.structural_fallback(max_files)
        ]
    windows = build_code_windows(root, locations, index=retriever.index,
                                 keywords=result.keywords,
                                 max_total_chars=MAX_SNIPPET_CHARS * max_files,
                                 max_window_chars=MAX_SNIPPET_CHARS)
    return [(window.path, window.text) for window in windows]


def _build_generation_messages(
    instruction: str, base_commit: str, repo_overview: str | None,
    snippets: list[tuple[str, str]],
) -> list[dict]:
    sections = [
        "Reported issue:", instruction.strip() or "(no issue text provided)",
        f"\nBase commit (buggy state to reproduce): {base_commit}",
    ]
    if repo_overview:
        sections.append("\nRepository overview (for locating code only):")
        sections.append(repo_overview.strip())
    if snippets:
        sections.append("\nRelevant source files (verbatim, possibly truncated):")
        for rel, content in snippets:
            sections.append(f"--- {rel} ---\n{content}")
    else:
        sections.append("\n(no candidate source files could be selected; rely on the issue text)")
    sections.append(
        "\nGenerate the reproduction test as the JSON object described in the system rules "
        "(saved as tests/test_repofix_generated.py)."
    )
    return [
        {"role": "system", "content": TEST_AGENT_SYSTEM_RULES},
        {"role": "user", "content": "\n".join(sections)},
    ]


def _extract_json_object(text: str) -> dict | None:
    """从模型输出提取第一个 JSON 对象（容忍 ```json 围栏与前后说明文字）。"""
    if not text:
        return None
    stripped = re.sub(r"```[a-zA-Z]*", "", text)
    try:
        parsed = json.loads(stripped.strip())
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(stripped)):
        ch = stripped[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


class TestAgentGenerator:
    """一次性测试生成器：一次模型调用 + 结构化校验（无 ReAct Loop）。"""

    __test__ = False  # pytest 采集豁免：这是运行时组件，不是测试类

    def __init__(
        self,
        *,
        run_id: str,
        model_client: ModelClient,
        model_id: str,
        max_model_calls: int = 1,
        store: SQLiteEventStore | None = None,
        clock=None,
    ):
        self._run_id = run_id
        self._model_client = model_client
        self._model_id = model_id
        self._max_model_calls = max(1, int(max_model_calls))
        self._store = store
        self._clock = clock

    def generate(
        self,
        *,
        instruction: str,
        base_commit: str,
        repo_overview: str | None = None,
        snippets: list[tuple[str, str]] | None = None,
    ) -> TestGenerationResult:
        """执行一次生成。任何模型调用异常都收敛为带 error 的结果，不向调用方抛出。"""
        result = TestGenerationResult()
        if snippets is None:
            snippets = []
        messages = _build_generation_messages(instruction, base_commit, repo_overview, snippets)
        request = ModelRequest(
            actor=AgentActor.TEST_AGENT,
            model_id=self._model_id,
            messages=messages,
            tools=[],  # 一次性生成：无工具调用
            max_output_tokens=GENERATION_MAX_OUTPUT_TOKENS,
            request_id=f"testgen_1_{uuid.uuid4().hex[:8]}",
        )
        started = time.monotonic()
        self._record_model_event(EventType.MODEL_REQUESTED, {
            "request_id": request.request_id,
            "message_count": len(messages),
            "snippet_files": [rel for rel, _ in snippets],
        })
        result.model_calls = 1  # 仅此一次调用；本层不重试（Adapter 由调用方以 retry_limit=0 构造）
        try:
            response = self._model_client.complete(request)
        except ModelClientError as exc:
            result.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._record_model_event(EventType.MODEL_RETRY, {
                "reason": "test_agent_model_error", "error_type": type(exc).__name__,
                "detail": str(exc)[:300],
            })
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.usage = response.usage
        self._record_model_event(EventType.MODEL_RESPONDED, {
            "decision_id": response.decision_id,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
        })

        payload = _extract_json_object(response.message.get("content") or ""
                                       if response.message else "")
        if payload is None:
            result.invalid_schema_reason = "model output did not contain a valid JSON object"
            return result
        if payload.get("cannot_generate"):
            result.declined = True
            result.decline_reason = str(payload.get("reason", ""))[:300]
            return result
        try:
            result.candidate = TestCandidate.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 —— Schema 不符 → INVALID_TEST 类
            result.invalid_schema_reason = f"TestCandidate schema violation: {str(exc)[:300]}"
        return result

    def _record_model_event(self, event_type: EventType, payload: dict) -> None:
        if self._store is None:
            return
        self._store.append_event(
            run_id=self._run_id,
            step=0,
            actor=AgentActor.TEST_AGENT,
            event_type=event_type,
            payload=payload,
            ts=self._clock.now() if self._clock is not None else 0.0,
        )
