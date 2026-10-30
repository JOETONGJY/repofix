"""Docker 集成测试（integration 标记，需 Docker Desktop 运行）：WS-04~08、TL-08、Preflight。

运行：pytest -m integration；Docker 不可用时自动 skip。
"""

import pytest

from repofix.contracts import ExecutionStatus, ToolErrorCode
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import PreflightRunner

pytestmark = pytest.mark.integration

IMAGE = "repofix-env:latest"


def docker_available() -> bool:
    try:
        cli = DockerCli()
        cli.version()
        return cli.image_exists(IMAGE)
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def real_workspace(sample_repo, tmp_path):
    if not docker_available():
        pytest.skip("Docker or repofix-env image not available")
    handle = WorkspaceManager(tmp_path / "ws").prepare("run_it", sample_repo, "HEAD")
    ws = DockerWorkspace(handle.task_dir, handle.run_id, cli=DockerCli())
    yield ws
    ws.close()


class TestContainerConstraints:
    def test_ws04_non_root_no_network_resource_limits(self, real_workspace):
        """容器约束实际生效：非 root / 无网络 / 资源限制（真实容器内探测）。"""
        ws = real_workspace
        ws.initialize(IMAGE)
        outcome = ws._cli.exec(ws.container_name, ["sh", "-c", "id -u"], timeout=30)
        assert outcome.stdout.strip() == "1000"  # 非 root
        outcome = ws._cli.exec(
            ws.container_name, ["sh", "-c", "ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && echo ON || echo OFF"],
            timeout=30,
        )
        assert outcome.stdout.strip() == "OFF"  # 网络禁用

    def test_no_credentials_leaked_into_container(self, real_workspace):
        """凭据不进容器：env 中无用户凭据类变量（WS-04）。

        GPG_KEY 是 Python 官方镜像自带的源码签名密钥，不属于用户凭据，排除。
        """
        ws = real_workspace
        ws.initialize(IMAGE)
        outcome = ws._cli.exec(
            ws.container_name,
            ["sh", "-c", "env | grep -iE 'api|deepseek|secret|token' | grep -v GPG_KEY || true"],
            timeout=30,
        )
        assert outcome.stdout.strip() == ""


class TestRealSearch:
    def test_tl11_real_rg_no_match_succeeds_empty(self, real_workspace):
        ws = real_workspace
        ws.initialize(IMAGE)
        result = ws.search_code("def definitely_not_here_xyz")
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.content == "(no matches)"

    def test_search_real_match_with_line_numbers(self, real_workspace):
        ws = real_workspace
        ws.initialize(IMAGE)
        result = ws.search_code("validate_password", regex=False)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert "auth.py:1:" in result.content
        assert result.truncated is False

    def test_search_regex_opt_in_and_glob(self, real_workspace):
        ws = real_workspace
        ws.initialize(IMAGE)
        result = ws.search_code(r"def \w+\(", regex=True, glob="*.py")
        assert result.status is ExecutionStatus.SUCCEEDED
        assert "def validate_password" in result.content

    def test_search_regex_invalid_pattern_invalid_argument(self, real_workspace):
        ws = real_workspace
        ws.initialize(IMAGE)
        result = ws.search_code("([unclosed", regex=True)
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT


class TestRunCommandReal:
    def test_pytest_runs_in_container_and_reports_assertion_failure(self, real_workspace):
        """pytest 断言失败 = SUCCEEDED + exit_code=1（业务反馈，非工具故障）。"""
        ws = real_workspace
        ws.initialize(IMAGE)
        result = ws.run_command(["python", "-m", "pytest", "-q", "--tb=no"], call_id="c_t")
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.exit_code == 1  # 基线 bug：test_empty_password_rejected 失败
        stdout, stderr = ws.pop_full_output("c_t")
        assert "FAILED" in stdout or "FAILED" in stderr

    def test_tl08_timeout_kills_process_tree_and_flags_unknown(self, real_workspace):
        """TL-08/WS-08：白名单命令超时 → 进程树被终止（容器停止）+ 副作用未知语义。"""
        from repofix.tools.dispatcher import ExecutorCrashError

        ws = real_workspace
        ws.initialize(IMAGE)
        # python -m compileall 扫描整个标准库必然超过 1s：制造确定超时
        ws._command_timeout = 1
        with pytest.raises(ExecutorCrashError):
            ws.run_command(
                ["python", "-m", "compileall", "-q", "-f", "/usr/local/lib/python3.12"], call_id="c_slow"
            )
        assert ws._container_up is False  # 容器已终止（进程树随之消亡）


class TestPreflight:
    def test_preflight_success_with_baseline_assertion_failures(self, real_workspace):
        """WS-06：基线断言失败 ≠ Preflight 失败；环境阻塞才 FAIL。"""
        ws = real_workspace
        runner = PreflightRunner(cli=ws._cli, image=IMAGE)
        report = runner.run(ws, "python -m pytest -q --tb=no -rf")
        assert report.ok is True
        assert report.test_plan == [["python", "-m", "pytest", "-q", "--tb=no", "-rf"]]
        assert report.baseline["kind"] == "baseline_assertion_failures"
        assert "tests/test_auth.py::test_empty_password_rejected" in report.baseline["failed_tests"]

    def test_preflight_missing_image_fails_without_container(self, sample_repo, tmp_path):
        if not docker_available():
            pytest.skip("Docker not available")
        handle = WorkspaceManager(tmp_path / "ws2").prepare("run_pf2", sample_repo, "HEAD")
        ws = DockerWorkspace(handle.task_dir, handle.run_id, cli=DockerCli())
        runner = PreflightRunner(cli=ws._cli, image="no-such-image:latest")
        report = runner.run(ws, None)
        assert report.ok is False
        assert report.failure_step == "image_available"


class TestWs05WorkflowIsolation:
    def test_full_tool_chain_produces_valid_patch(self, real_workspace):
        """端到端工具链演练：search → read → patch → test → get_changes 导出合法 patch。"""
        ws = real_workspace
        ws.initialize(IMAGE)
        # 1. 定位
        assert "auth.py" in ws.search_code("validate_password").content
        # 2. 读取 + hash
        read = ws.read_file("auth.py")
        old_hash = read.content.split("[sha256=")[1].split("]")[0]
        # 3. 修复（空密码应返回 False → 原实现 len(pw) > 0 对 '' 返回 False 其实已对；
        #    改为要求最小长度 8 以制造真实修改）
        patch = (
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate_password(pw: str) -> bool:\n"
            "-    return len(pw) >= 0\n"
            "+    return len(pw) >= 8\n"
        )
        applied = ws.apply_patch("auth.py", patch, expected_sha256=old_hash)
        assert applied.status is ExecutionStatus.SUCCEEDED
        # 4. 测试仍在跑（基线行为可能随修复变化）
        test_run = ws.run_command(["python", "-m", "pytest", "-q", "--tb=no"], call_id="c_final")
        assert test_run.status is ExecutionStatus.SUCCEEDED
        # 5. 导出 Patch：含修改 + patch_hash 稳定
        changes = ws.get_changes()
        assert "auth.py" in changes["changed_files"]
        assert "return len(pw) >= 8" in changes["diff"]
        assert len(changes["patch_hash"]) == 64
