"""测试辅助：ScriptedModelClient / FakeToolExecutor / FakeVerifier / 可控时钟 / Fake DockerCli。

两条 Fake 纪律（plan.md）：与真实组件同接口契约；仅放测试辅助代码，不建生产级 Mock 框架。
Fake 不生成 Trace / RunResult——这些必须由真实 Runtime 产生。
"""
import json
import subprocess

import pytest

from repofix.contracts import (
    ExecutionStatus,
    ModelRequest,
    ModelResponse,
    RunConfig,
    ToolCall,
    ToolResult,
    VerificationReport,
    VerificationRequest,
)
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.workspace.docker_cli import CliResult


class FakeDockerCli:
    """DockerCli 同接口 Fake：容器操作只记录，exec 返回预设结果。"""

    def __init__(self, exec_results: list[CliResult] | None = None):
        self.started: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.exec_calls: list[tuple[str, list[str], str]] = []
        self._exec_results = list(exec_results or [])

    def version(self) -> str:
        return "fake"

    def image_exists(self, image: str) -> bool:
        return True

    def container_exists(self, name: str) -> bool:
        return False

    def start_container(self, name: str, image: str, task_dir, **kwargs) -> None:
        self.started.append((name, image))

    def exec(self, name: str, argv: list[str], timeout: int = 120, cwd_in_container: str = "/workspace") -> CliResult:
        self.exec_calls.append((name, argv, cwd_in_container))
        if self._exec_results:
            return self._exec_results.pop(0)
        return CliResult(0, "", "")

    def stop_container(self, name: str, timeout: int = 60) -> None:
        self.stopped.append(name)

    def remove_container(self, name: str, timeout: int = 60) -> None:
        self.removed.append(name)


def run_git(args: list[str], cwd) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return proc.stdout


@pytest.fixture
def sample_repo(tmp_path):
    """小型真实 Git 仓库：auth.py + pytest 测试 + .gitignore（WS/TL 测试共用）。"""
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    run_git(["init", "-q", "-b", "main"], repo)
    run_git(["config", "user.email", "repofix@test.local"], repo)
    run_git(["config", "user.name", "repofix-test"], repo)
    run_git(["config", "core.autocrlf", "false"], repo)
    (repo / "auth.py").write_text(
        "def validate_password(pw: str) -> bool:\n"
        "    return len(pw) >= 0\n",
        encoding="utf-8",
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_auth.py").write_text(
        "from auth import validate_password\n\n"
        "def test_empty_password_rejected():\n"
        "    assert validate_password('') is False\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    run_git(["add", "-A"], repo)
    run_git(["commit", "-q", "-m", "init"], repo)
    return repo


class FakeClock:
    """可控时钟：测试手动推进。"""

    def __init__(self, start: float = 0.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class ScriptedModelClient:
    """按脚本依次返回预设 ModelResponse；记录收到的请求供配对断言。

    脚本项也可以是 Exception 实例——complete() 时抛出（模拟 API 失败）。
    """

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("ScriptedModelClient script exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeToolExecutor:
    """与未来真实 Workspace 同接口。预设结果，不产生真实副作用。

    register_error 可让指定工具在执行时抛异常（模拟执行器崩溃——副作用是否已发生未知）。
    """

    def __init__(self, results: dict[str, ToolResult] | None = None):
        self._results = results or {}
        self._errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, dict]] = []
        self.observers: list = []  # 执行瞬间回调（用于意图先行断言）

    def register(self, tool_name: str, result: ToolResult) -> None:
        self._results[tool_name] = result

    def register_error(self, tool_name: str, exc: Exception) -> None:
        self._errors[tool_name] = exc

    def execute(self, tool_name: str, arguments: dict, call_id: str = "") -> ToolResult:
        self.calls.append((tool_name, arguments))
        for observer in self.observers:
            observer(tool_name, arguments)
        if tool_name in self._errors:
            raise self._errors[tool_name]
        template = self._results.get(tool_name)
        if template is None:
            # call_id 由 Dispatcher 统一绑定；Fake 用占位值构造
            return ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED, content=f"fake {tool_name} ok")
        return template.model_copy(update={"call_id": "pending"})

    def executed(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeVerifier:
    """按脚本返回 VerificationReport；记录收到的 VerificationRequest。"""

    def __init__(self, reports: list[VerificationReport]):
        self._reports = list(reports)
        self.requests: list[VerificationRequest] = []

    def verify(self, request: VerificationRequest) -> VerificationReport:
        self.requests.append(request)
        if not self._reports:
            raise AssertionError("FakeVerifier script exhausted")
        return self._reports.pop(0)


# ---- 构造辅助 ----


def make_tool_call(call_id: str, tool_name: str, **arguments) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, sort_keys=True),
    )


def model_response(
    decision_id: str,
    tool_calls: list[ToolCall] | None = None,
    text: str | None = None,
    usage: dict | None = None,
    finish_reason: str | None = None,
) -> ModelResponse:
    message = {"role": "assistant", "content": text} if text else None
    return ModelResponse(
        decision_id=decision_id,
        message=message,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=usage,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_default_tools(reg)
    return reg


@pytest.fixture
def config() -> RunConfig:
    return RunConfig(model_id="scripted-model", review_enabled=False)


@pytest.fixture
def task_workspace(sample_repo, tmp_path):
    """真实 WorkspaceManager 准备的干净任务副本 + 注入 Fake DockerCli 的 DockerWorkspace。"""
    from repofix.workspace.docker_workspace import DockerWorkspace
    from repofix.workspace.manager import WorkspaceManager

    handle = WorkspaceManager(tmp_path / "workspaces").prepare(
        "run_ws_test", sample_repo, "HEAD"
    )
    cli = FakeDockerCli()
    ws = DockerWorkspace(handle.task_dir, handle.run_id, base_commit=handle.base_commit, cli=cli)
    ws.handle = handle
    ws.fake_cli = cli
    return ws
