"""静态 Tool Registry（design/04）。

工具在程序启动时注册，不支持模型运行中安装、发现或注册新工具；不支持 MCP。
权限按 actor 区分：Main 与 Reviewer 复用同一 Registry，按角色生成不同工具视图
（Reviewer 视图中不存在 apply_patch / run_command / finish）。
只读约束由注册表 + Workspace 权限共同保证，不能仅写在 System Prompt 中。
"""
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repofix.contracts import AgentActor
from repofix.contracts.models import ReviewFinding


class ToolSideEffect(str, Enum):
    """副作用类别决定调度行为（specs/03）：read_only 失败后同类可继续；write/process 失败即中止批次。"""

    READ_ONLY = "read_only"
    WRITE = "write"
    PROCESS = "process"
    CONTROL = "control"  # finish / submit_review：Runtime 控制动作，不进 Workspace 执行


class _StrictModel(BaseModel):
    """Pydantic 严格模式：不静默类型转换、不接受未声明额外字段（specs/03 四层校验第 2 层）。"""

    model_config = ConfigDict(extra="forbid", strict=True)


# ---- 六个工作区工具 + 两个控制动作的参数 Schema（specs/03 固定，不得增删改）----


class ListFilesArgs(_StrictModel):
    path: str = "."
    max_depth: int = Field(default=3, ge=1, le=6)
    limit: int = Field(default=100, ge=1, le=500)


class SearchCodeArgs(_StrictModel):
    pattern: str = Field(min_length=1)
    path: str = "."
    regex: bool = False  # 默认普通字符串搜索，显式 true 才启用正则
    glob: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class ReadFileArgs(_StrictModel):
    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=1000)


class ApplyPatchArgs(_StrictModel):
    path: str = Field(min_length=1)
    patch: str = Field(min_length=1)
    expected_sha256: str | None = None  # 修改已有文件时必填（语义层校验，M2 Workspace 执行）


class RunCommandArgs(_StrictModel):
    argv: list[str] = Field(min_length=1)  # 参数数组，不是 Shell 字符串
    cwd: str = "."
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class FinishArgs(_StrictModel):
    summary: str = Field(min_length=1)  # 契约唯一字段（CONS-04）


class SubmitReviewArgs(_StrictModel):
    status: str  # APPROVED / FINDINGS / INCONCLUSIVE（值校验在 validator 中）
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=5)
    summary: str = ""

    @field_validator("status")
    @classmethod
    def _status_limited(cls, value: str) -> str:
        if value not in {"APPROVED", "FINDINGS", "INCONCLUSIVE"}:
            raise ValueError("submit_review.status must be APPROVED / FINDINGS / INCONCLUSIVE")
        return value


class ToolSpec:
    """工具静态定义：名称、描述、参数 Schema、副作用类别、角色权限。"""

    def __init__(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        side_effect: ToolSideEffect,
        allowed_actors: set[AgentActor],
    ):
        self.name = name
        self.description = description
        self.args_model = args_model
        self.side_effect = side_effect
        self.allowed_actors = allowed_actors

    def json_schema(self) -> dict:
        """ModelRequest.tools 使用的 OpenAI 风格 Schema（由 Registry 生成，不能由模型决定）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def is_allowed(self, actor: AgentActor, name: str) -> bool:
        spec = self._tools.get(name)
        return spec is not None and actor in spec.allowed_actors

    def view_for(self, actor: AgentActor) -> list[dict]:
        """按 actor 生成工具视图——Reviewer 的视图里没有写工具与控制动作。"""
        return [
            spec.json_schema()
            for spec in self._tools.values()
            if actor in spec.allowed_actors
        ]


def register_default_tools(registry: ToolRegistry) -> None:
    """注册七个动作契约（specs/03）。M1 提供真实 Schema/权限/分发；M2 接入真实 Workspace 执行。"""
    read_actors = {AgentActor.MAIN, AgentActor.REVIEWER}
    registry.register(
        ToolSpec(
            "list_files",
            "List files under a path of the task workspace.",
            ListFilesArgs,
            ToolSideEffect.READ_ONLY,
            read_actors,
        )
    )
    registry.register(
        ToolSpec(
            "search_code",
            "Search code by literal string (default) or regex within the workspace.",
            SearchCodeArgs,
            ToolSideEffect.READ_ONLY,
            read_actors,
        )
    )
    registry.register(
        ToolSpec(
            "read_file",
            "Read a text file with line numbers; returns sha256 for patch version checks.",
            ReadFileArgs,
            ToolSideEffect.READ_ONLY,
            read_actors,
        )
    )
    registry.register(
        ToolSpec(
            "apply_patch",
            "Apply a single-file unified diff with expected_sha256 version check (atomic).",
            ApplyPatchArgs,
            ToolSideEffect.WRITE,
            {AgentActor.MAIN},
        )
    )
    registry.register(
        ToolSpec(
            "run_command",
            "Run an allow-listed command (argv array) inside the sandboxed workspace.",
            RunCommandArgs,
            ToolSideEffect.PROCESS,
            {AgentActor.MAIN},
        )
    )
    registry.register(
        ToolSpec(
            "finish",
            "Main Agent only. Submit completion request; must be the only call in its batch.",
            FinishArgs,
            ToolSideEffect.CONTROL,
            {AgentActor.MAIN},
        )
    )
    registry.register(
        ToolSpec(
            "submit_review",
            "Review Agent only. Submit the structured ReviewReport and end this review round.",
            SubmitReviewArgs,
            ToolSideEffect.CONTROL,
            {AgentActor.REVIEWER},
        )
    )
