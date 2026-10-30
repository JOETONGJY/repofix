"""DeepSeek ProviderAdapter：OpenAI 兼容 Chat Completions Function Calling（design/03、ADR-05）。

职责：构造请求 → 传输 → 响应分类与重试 → 标准化 ModelResponse（含 UsageRecorder 职责）。
硬约束（design/03）：
- 完整响应接纳之后才能执行工具——finish_reason=length / 结构损坏 → ModelProtocolError；
- call_id 丢失或重复 → 整个响应批次不可接纳，不得自行生成 ID；
- 参数 JSON 无效但 call_id / 工具名合法 → 保留调用（arguments 置空、raw_arguments 原样），
  由 Tool System 返回 INVALID_ARGUMENT；
- Usage 缺失 → 标记未知，不虚构 Token 与费用；
- 每次真实 API 请求尝试（含重试）经 on_attempt 钩子计入模型预算（specs/02 V0.2）；
- "OpenAI-compatible" ≠ 任意模型可替换：供应商特殊字段（如 reasoning_content）由本层
  保存到 provider_metadata，下一轮请求由 Context 原样带回。
"""
import json
import time
import uuid

from repofix.contracts import ModelRequest, ModelResponse, ToolCall
from repofix.gateway.client import (
    ModelPermanentError,
    ModelProtocolError,
    ModelTransientError,
)
from repofix.gateway.transport import HttpTransport, TransportError, UrllibHttpTransport

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_PERMANENT_STATUSES = {400, 401, 403, 404, 422}


class DeepSeekAdapter:
    """实现 ModelClient 协议。counts_own_attempts 标记：Runner 不再预计数首次调用。"""

    counts_own_attempts = True

    def __init__(
        self,
        *,
        base_url: str = "https://api.deepseek.com",
        api_key: str | None = None,
        transport: HttpTransport | None = None,
        on_attempt=None,
        sleep=time.sleep,
        retry_limit: int = 2,
    ):
        if transport is None:
            if not api_key:
                raise ModelPermanentError("DeepSeek API key is required (DEEPSEEK_API_KEY)")
            transport = UrllibHttpTransport(base_url, api_key)
        self._transport = transport
        self._on_attempt = on_attempt
        self._sleep = sleep
        self._retry_limit = retry_limit

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self._build_payload(request)
        attempt = 0
        while True:
            attempt += 1
            if self._on_attempt:
                self._on_attempt()
            try:
                result = self._transport.post("/chat/completions", payload, request.timeout_seconds)
            except TransportError as exc:
                if attempt <= self._retry_limit:
                    self._sleep(self._backoff_seconds(attempt, None))
                    continue
                raise ModelTransientError(f"transport failed after {attempt} attempt(s): {exc}") from exc
            if result.status in _RETRYABLE_STATUSES:
                if attempt <= self._retry_limit:
                    self._sleep(self._backoff_seconds(attempt, result.header("Retry-After")))
                    continue
                raise ModelTransientError(
                    f"retryable HTTP {result.status} after {attempt} attempt(s): {result.body[:200]}"
                )
            if result.status in _PERMANENT_STATUSES:
                raise ModelPermanentError(
                    f"HTTP {result.status} (permanent): {result.body[:200]}"
                )
            if result.status != 200:
                raise ModelPermanentError(f"unexpected HTTP {result.status}: {result.body[:200]}")
            return self._normalize(result.body)

    # ---- 协议构造与标准化 ----

    def _build_payload(self, request: ModelRequest) -> dict:
        if not request.messages:
            raise ModelPermanentError("ModelRequest.messages must not be empty")
        payload: dict = {
            "model": request.model_id,
            "messages": [dict(message) for message in request.messages],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    def _normalize(self, body_text: str) -> ModelResponse:
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(f"response body is not valid JSON: {exc}") from exc
        choices = body.get("choices") or []
        if not choices:
            raise ModelProtocolError("response has no choices")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # 输出被截断：完整响应接纳之后才能执行工具（GW-04）
            raise ModelProtocolError(
                "response truncated by max_tokens (finish_reason=length); partial output rejected"
            )
        message = choice.get("message") or {}
        tool_calls, seen_ids = [], set()
        for raw in message.get("tool_calls") or []:
            call_id = raw.get("id")
            if not call_id:
                raise ModelProtocolError("tool call is missing call_id; whole batch rejected")
            if call_id in seen_ids:
                raise ModelProtocolError(f"duplicate call_id in response: {call_id}; whole batch rejected")
            seen_ids.add(call_id)
            function = raw.get("function") or {}
            raw_arguments = function.get("arguments")
            raw_text = raw_arguments if isinstance(raw_arguments, str) else (
                json.dumps(raw_arguments, ensure_ascii=False) if raw_arguments is not None else ""
            )
            try:
                arguments = json.loads(raw_text) if raw_text else {}
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                # 参数 JSON 无效：保留调用与 raw_arguments，交由 Tool System 返回 INVALID_ARGUMENT（GW-02）
                arguments = {}
            tool_calls.append(
                ToolCall(call_id=call_id, tool_name=function.get("name", ""), arguments=arguments,
                         raw_arguments=raw_text or None)
            )
        usage = body.get("usage")  # 缺失 → None：标记未知，不虚构（GW-07）
        metadata: dict = {}
        reasoning = message.get("reasoning_content")
        if reasoning:
            metadata["reasoning_content"] = reasoning
        if body.get("model"):
            metadata["served_model"] = body["model"]
        return ModelResponse(
            response_id=body.get("id"),
            decision_id=f"dec_{uuid.uuid4().hex[:12]}",
            message={"role": "assistant", "content": message.get("content")},
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            provider_metadata=metadata,
        )

    def _backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return min(1.0 * (2 ** (attempt - 1)), 8.0)
