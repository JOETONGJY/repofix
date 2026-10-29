"""Tool System：静态注册表 + 严格 Schema + 单 Dispatcher + 结构化 ToolResult。"""
from repofix.tools.dispatcher import (
    DEFAULT_CONTENT_LIMIT_BYTES,
    ExecutorCrashError,
    ToolDispatcher,
    ToolExecutor,
    truncate_content,
)
from repofix.tools.registry import (
    ToolRegistry,
    ToolSideEffect,
    ToolSpec,
    register_default_tools,
)

__all__ = [
    "DEFAULT_CONTENT_LIMIT_BYTES",
    "ExecutorCrashError",
    "ToolDispatcher",
    "ToolExecutor",
    "ToolRegistry",
    "ToolSideEffect",
    "ToolSpec",
    "register_default_tools",
    "truncate_content",
]
