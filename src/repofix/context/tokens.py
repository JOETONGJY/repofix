"""TokenBudgetManager：模型上下文预算计算与确定性估算（design/07）。

预算公式：B_input = B_context − B_output_reserve − B_safety（B_safety 取窗口 10%）。
估算为确定性启发式——评审 P0-2 修订：
- 常数取保守值（3 字符/token）：中文等 CJK 内容实际约 1~1.5 字符/token，
  4 字符/token 会明显低估；宁可高估触发提前裁剪，不可低估导致请求失败。
- 每消息开销计入协议结构（role / name / id 等）。
- reasoning_content（供应商续接字段）计入估算（M3-1 已在视图中保留）。
- 静态开销（工具 Schema 等 ModelRequest 级载荷）由构造方传入并从预算扣除。
估算不准时按 specs/02 执行一次紧急压缩后受控失败，不无限尝试缩小上下文；
真实 API Usage 数据可在后续用于校准常数。
"""

MESSAGE_OVERHEAD_TOKENS = 8
CHARS_PER_TOKEN = 3  # 保守值：对中文内容仍可能低估，由 10% safety 与保守常数共同吸收
SAFETY_FRACTION = 10  # B_safety = 窗口的 10%（design/07 建议，整数百分比）


def estimate_message_tokens(message: dict) -> int:
    """单条消息的确定性 Token 估算（含 reasoning_content 续接字段与协议开销）。"""
    total = MESSAGE_OVERHEAD_TOKENS
    content = message.get("content")
    if isinstance(content, str):
        total += len(content) // CHARS_PER_TOKEN
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        total += len(reasoning) // CHARS_PER_TOKEN
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("name", "")) // CHARS_PER_TOKEN
        total += len(function.get("arguments", "")) // CHARS_PER_TOKEN
    tool_call_id = message.get("tool_call_id")
    if tool_call_id:
        total += len(str(tool_call_id)) // CHARS_PER_TOKEN
    return total


def estimate_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_static_overhead(items: list[dict]) -> int:
    """ModelRequest 级静态载荷的工具 Schema 开销（tools 列表）。"""
    import json

    return sum(len(json.dumps(item, ensure_ascii=False)) // CHARS_PER_TOKEN for item in items)


def available_input_tokens(context_window_tokens: int, output_reserve_tokens: int) -> int:
    """B_input = B_context − B_output_reserve − B_safety（B_safety = 10% 窗口）。"""
    safety = max(1, context_window_tokens * SAFETY_FRACTION // 100)
    available = context_window_tokens - output_reserve_tokens - safety
    if available <= 0:
        raise ValueError(
            f"context budget too small: window={context_window_tokens} "
            f"reserve={output_reserve_tokens} safety={safety}"
        )
    return available


class TokenBudgetManager:
    def __init__(
        self,
        context_window_tokens: int,
        output_reserve_tokens: int,
        static_overhead_tokens: int = 0,
    ):
        self.available = available_input_tokens(context_window_tokens, output_reserve_tokens)
        # 静态开销（工具 Schema）挤占消息预算；钳制为 0——此时任何消息都无法放入，
        # 由 Builder 统一走 ContextBudgetExceeded 受控失败（而非构造期崩溃）
        self.available = max(0, self.available - static_overhead_tokens)

    def fits(self, messages: list[dict]) -> bool:
        return estimate_tokens(messages) <= self.available
