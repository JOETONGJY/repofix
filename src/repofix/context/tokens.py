"""TokenBudgetManager：模型上下文预算计算与确定性估算（design/07）。

预算公式：B_input = B_context − B_output_reserve − B_safety（B_safety 取窗口 10%）。
估算为确定性启发式（约 4 字符 / token + 每消息 4 token 开销）——
估算不准时按 specs/02 执行一次紧急压缩后受控失败，不无限尝试缩小上下文。
"""

MESSAGE_OVERHEAD_TOKENS = 4
CHARS_PER_TOKEN = 4
SAFETY_FRACTION = 10  # B_safety = 窗口的 10%（design/07 建议，整数百分比）


def estimate_message_tokens(message: dict) -> int:
    """单条消息的确定性 Token 估算。"""
    total = MESSAGE_OVERHEAD_TOKENS
    content = message.get("content")
    if isinstance(content, str):
        total += len(content) // CHARS_PER_TOKEN
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
    def __init__(self, context_window_tokens: int, output_reserve_tokens: int):
        self.available = available_input_tokens(context_window_tokens, output_reserve_tokens)

    def fits(self, messages: list[dict]) -> bool:
        return estimate_tokens(messages) <= self.available
