"""Context Engine：模型可见上下文构造（M3 完整版：预算 + 轮次裁剪 + Working Memory）。"""
from repofix.context.builder import (
    ContextBudgetExceeded,
    ContextBuilder,
    ContextPairingError,
    render_tool_result,
    render_verification_feedback,
)
from repofix.context.tokens import (
    TokenBudgetManager,
    estimate_message_tokens,
    estimate_tokens,
)
from repofix.context.working_memory import render_working_memory

__all__ = [
    "ContextBudgetExceeded",
    "ContextBuilder",
    "ContextPairingError",
    "TokenBudgetManager",
    "estimate_message_tokens",
    "estimate_tokens",
    "render_tool_result",
    "render_verification_feedback",
    "render_working_memory",
]
