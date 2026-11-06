"""Auto Verification Discovery 测试（评审要求：确定性测试发现 + 来源追溯 + 语义区分）。"""

from pathlib import Path

import pytest

from repofix.workspace.preflight import (
    AUTO_TEST_COMMAND,
    PreflightRunner,
    discover_test_plan,
    parse_test_command,
)


class TestDiscoverTestPlan:
    def test_pytest_project_via_tests_dir(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_auth.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert "tests/" in evidence

    def test_pytest_ini_marker(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert evidence == "pytest.ini"

    def test_pyproject_tool_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert evidence == "pyproject.toml"

    def test_pyproject_without_pytest_section_skipped(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [] and evidence == ""  # 无 pytest 痕迹 → 无测试计划

    def test_scattered_test_file(self, tmp_path):
        (tmp_path / "test_auth.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert evidence == "test_auth.py"

    def test_no_tests_empty_plan(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [] and evidence == ""


class TestParseTestCommandAuto:
    def test_auto_returns_empty_for_discovery(self):
        assert parse_test_command("auto") == []
        assert parse_test_command("AUTO") == []

    def test_manual_still_parses(self):
        assert parse_test_command("python -m pytest -q") == [["python", "-m", "pytest", "-q"]]


class TestPreflightAutoIntegration:
    def _workspace(self, tmp_path, with_tests=True, with_pytest_cache=False):
        from repofix.workspace.manager import WorkspaceManager

        repo, head = sample_repo_with_tests(tmp_path, with_tests)
        handle = WorkspaceManager(tmp_path / "ws").prepare("run_auto", repo, head)
        from repofix.workspace.docker_cli import DockerCli
        from repofix.workspace.docker_workspace import DockerWorkspace

        ws = DockerWorkspace(handle.task_dir, handle.run_id,
                             base_commit=handle.base_commit, cli=DockerCli())
        return ws

    def test_auto_discovery_runs_pytest(self, sample_repo, tmp_path):
        pytest.importorskip("docker") if False else None
        from repofix.workspace.docker_cli import DockerCli
        from repofix.workspace.docker_workspace import DockerWorkspace
        from repofix.workspace.manager import WorkspaceManager

        handle = WorkspaceManager(tmp_path / "ws").prepare("run_auto2", sample_repo, "HEAD")
        cli = DockerCli()
        ws = DockerWorkspace(handle.task_dir, handle.run_id, base_commit=handle.base_commit, cli=cli)
        runner = PreflightRunner(cli=cli)
        report = runner.run(ws, "auto")
        assert report.ok
        assert report.test_plan_source == "auto"
        assert report.test_plan == [["python", "-m", "pytest", "-q", "--tb=no", "-rf"]]


def sample_repo_with_tests(tmp_path, with_tests=True):
    import subprocess

    repo = tmp_path / "sr"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "auth.py").write_text("def validate_password(pw: str) -> bool:\n    return len(pw) >= 0\n",
                                  encoding="utf-8")
    if with_tests:
        t = repo / "tests"
        t.mkdir()
        (t / "test_auth.py").write_text(
            "from auth import validate_password\n\ndef test_x():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=v@v", "-c", "user.name=v", "commit", "-q", "-m", "b"],
                   cwd=repo, check=True)
    return repo


class TestNestedDiscovery:
    def test_nested_test_dir_discovered(self, tmp_path):
        """P1：tests/unit/test_auth.py（嵌套）也能发现。"""
        nested = tmp_path / "tests" / "unit"
        nested.mkdir(parents=True)
        (nested / "test_auth.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert "tests/" in evidence

    def test_venv_not_scanned(self, tmp_path):
        """有界递归跳过 .venv/.git 等目录（不误报依赖里的测试文件）。"""
        (tmp_path / "tests").mkdir()
        venv_tests = tmp_path / ".venv" / "lib" / "site-packages" / "pkg" / "tests"
        venv_tests.mkdir(parents=True)
        (venv_tests / "test_vendor.py").write_text("def test_v():\n    assert True\n", encoding="utf-8")
        (tmp_path / "tests" / "test_real.py").write_text("def test_r():\n    assert True\n", encoding="utf-8")
        plan, evidence = discover_test_plan(tmp_path)
        assert plan == [AUTO_TEST_COMMAND]
        assert ".venv" not in evidence


class TestWebAutoDefaults:
    def test_web_and_server_defaults_are_auto(self):
        """GPT 复核项：三入口默认语义统一——web/index.html 预填 auto，
        FastAPI StartRequest 默认 auto，空输入由 Pydantic 拒绝而非静默禁用发现。"""
        web_html = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="testcmd" value="auto"' in web_html
        import repofix.server as srv
        fields = srv.StartRequest.model_fields
        assert fields["test_command"].default == "auto"
