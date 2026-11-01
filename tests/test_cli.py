"""Task Interface CLI 测试：TI-01~12（design/01）与 CLI-01/02（specs/03 退出码与产物）。

LLM 相关组件经 _build_stack seam 注入（Scripted/Fake）——不访问外部 API、不启动 Docker。
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repofix.cli import app
from repofix.contracts import (
    PatchStatus,
    TestStatus,
    VerificationOutcome,
    VerificationReport,
)
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from tests.conftest import (
    FakeClock,
    FakeDockerCli,
    FakeVerifier,
    ScriptedModelClient,
    make_tool_call,
    model_response,
)

cli_runner = CliRunner()

def base_args(repo, output_dir, extra=None):
    args = [
        "--repo", str(repo),
        "--task", "Fix the login validation bug",
        "--output-dir", str(output_dir),
    ]
    return args + (extra or [])

def build_real_fake_stack(sample_repo, tmp_path, task_spec, config, run_id,
                          responses, verifier_reports, executor=None):
    """真实 CLI 流程 + 假模型/验证器/容器：Runtime、git 副本、产物写盘全部真实。"""
    from repofix.workspace.manager import WorkspaceManager
    from repofix.workspace.preflight import PreflightRunner

    manager = WorkspaceManager(Path(config.output_dir) / "workspaces")
    handle = manager.prepare(run_id, Path(task_spec.repo_path), task_spec.base_commit)
    cli = FakeDockerCli()
    workspace = DockerWorkspace_proxy(handle, run_id, cli, config)
    preflight = PreflightRunner(cli=cli)
    report = preflight.run(workspace, task_spec.test_command)
    assert report.ok is True
    registry = ToolRegistry()
    register_default_tools(registry)
    verifier = FakeVerifier(verifier_reports)
    store = SQLiteEventStore(Path(config.output_dir) / "runs" / run_id / "run.sqlite")
    runner = AgentRunner(
        run_id=run_id, task=task_spec, config=config,
        model_client=ScriptedModelClient(responses), registry=registry,
        dispatcher=ToolDispatcher(registry, workspace),  # 真实工作区即 ToolExecutor
        store=store, verifier=verifier, clock=FakeClock(),
    )
    return runner, store, workspace, manager, handle, report

class DockerWorkspace_proxy:
    """FakeDockerCli 驱动的真实 DockerWorkspace（文件操作真实、容器假）。"""

    def __init__(self, handle, run_id, cli, config):
        from repofix.workspace.docker_workspace import DockerWorkspace

        self._ws = DockerWorkspace(
            handle.task_dir, run_id, base_commit=handle.base_commit,
            cli=cli, command_timeout_seconds=config.command_timeout_seconds,
        )

    def __getattr__(self, name):
        return getattr(self._ws, name)

    def close(self):
        return {"container_removed": True}

def passed_report():
    return VerificationReport(
        candidate_revision="v", patch_status=PatchStatus.VALID,
        test_status=TestStatus.PASSED, outcome=VerificationOutcome.PASSED,
    )

def inconclusive_report():
    return VerificationReport(
        candidate_revision="v", patch_status=PatchStatus.VALID,
        test_status=TestStatus.NOT_AVAILABLE, outcome=VerificationOutcome.INCONCLUSIVE,
    )

@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

class TestInputValidation:
    def test_ti02_task_and_task_file_mutually_exclusive(self, sample_repo, tmp_path):
        result = cli_runner.invoke(app, [
            "--repo", str(sample_repo), "--task", "x", "--task-file", "t.txt",
            "--output-dir", str(tmp_path),
        ])
        assert result.exit_code == 2

    def test_ti03_empty_task_rejected(self, sample_repo, tmp_path):
        result = cli_runner.invoke(app, base_args(sample_repo, tmp_path, ["--task", "  "]))
        assert result.exit_code == 2

    def test_ti04_invalid_repo_rejected(self, tmp_path):
        result = cli_runner.invoke(app, base_args(tmp_path / "nope", tmp_path))
        assert result.exit_code == 2

    def test_ti05_dirty_worktree_rejected_source_untouched(self, sample_repo, tmp_path):
        (sample_repo / "auth.py").write_text("dirty\n", encoding="utf-8")
        result = cli_runner.invoke(app, base_args(sample_repo, tmp_path))
        assert result.exit_code == 2
        assert (sample_repo / "auth.py").read_text(encoding="utf-8") == "dirty\n"

    def test_ti06_invalid_base_ref_rejected(self, sample_repo, tmp_path):
        result = cli_runner.invoke(app, base_args(sample_repo, tmp_path, ["--base-ref", "no-such-ref"]))
        assert result.exit_code == 2

    def test_ti07_missing_api_key_rejected_before_workspace_prep(self, sample_repo, tmp_path, no_key):
        result = cli_runner.invoke(app, base_args(sample_repo, tmp_path))
        assert result.exit_code == 2
        assert not (tmp_path / "workspaces").exists()  # 未做任何工作区准备


def invoke_with_stack(sample_repo, tmp_path, responses, verifier_reports,
                      extra=None, executor=None):
    data_dir = tmp_path / "data"

    def fake_stack(task_spec, config, run_id):
        return build_real_fake_stack(
            sample_repo, tmp_path, task_spec, config, run_id,
            responses, verifier_reports, executor=executor,
        )

    import repofix.cli as cli_module

    original = cli_module._build_stack
    cli_module._build_stack = fake_stack
    try:
        return cli_runner.invoke(app, base_args(sample_repo, data_dir, extra))
    finally:
        cli_module._build_stack = original


class TestExitCodeMatrix:
    def test_cli01_completed_and_verified_exit_0(self, sample_repo, tmp_path):
        result = invoke_with_stack(
            sample_repo, tmp_path,
            [model_response("d1", [make_tool_call("f1", "finish", summary="done")])],
            [passed_report()],
        )
        assert result.exit_code == 0
        assert "completed" in result.output

    def test_ti09_completed_without_verification_exit_1(self, sample_repo, tmp_path):
        result = invoke_with_stack(
            sample_repo, tmp_path,
            [model_response("d1", [make_tool_call("f1", "finish", summary="done")])],
            [inconclusive_report()],
        )
        assert result.exit_code == 1  # 完成但未充分验证，不声称修复成功

    def test_cli01_failed_exit_3(self, sample_repo, tmp_path):
        from repofix.gateway import ModelProtocolError

        result = invoke_with_stack(
            sample_repo, tmp_path,
            [ModelProtocolError("bad"), ModelProtocolError("worse")],
            [],
        )
        assert result.exit_code == 3

    def test_cli01_budget_exhausted_exit_4(self, sample_repo, tmp_path):
        result = invoke_with_stack(
            sample_repo, tmp_path,
            [model_response("d1", [make_tool_call("c1", "read_file", path="auth.py")])],
            [],
            extra=["--max-model-calls", "1"],
        )
        assert result.exit_code == 4

    def test_ti11_failure_reports_reason_and_artifacts(self, sample_repo, tmp_path):
        from repofix.gateway import ModelProtocolError

        result = invoke_with_stack(
            sample_repo, tmp_path,
            [ModelProtocolError("bad"), ModelProtocolError("worse")],
            [],
        )
        assert result.exit_code == 3
        assert "model_protocol_error" in result.output
        run_dirs = list((tmp_path / "data" / "runs").iterdir())
        assert len(run_dirs) == 1
        assert (run_dirs[0] / "trajectory.jsonl").exists()

    def test_ti08_user_interrupt_exit_130(self, sample_repo, tmp_path, monkeypatch):
        class InterruptingRunner:
            def run(self):
                raise KeyboardInterrupt

        store = SQLiteEventStore(":memory:")
        class _DummyManager:
            def cleanup_workspace(self, handle, keep_for_diagnosis=False):
                pass

        monkeypatch.setattr(
            "repofix.cli._build_stack",
            lambda task_spec, config, run_id: (
                InterruptingRunner(), store, _FakeWorkspace(), _DummyManager(), None,
                type("R", (), {"ok": True, "test_plan": [], "baseline": {}})(),
            ),
        )
        result = cli_runner.invoke(app, base_args(sample_repo, tmp_path / "data2"))
        assert result.exit_code == 130
        assert "cancel" in result.output.lower()

class _FakeWorkspace:
    def export_patch(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("", encoding="utf-8", newline="\n")
        return str(path)

class TestArtifacts:
    def test_cli02_artifacts_written_and_patch_real(self, sample_repo, tmp_path):
        """CLI-02：changes.patch 可 git apply 且含修改；trajectory 合法 JSONL；
        result.json 符合 RunResult 契约；均不依赖 Evaluation。"""
        import hashlib

        data_dir = tmp_path / "data"
        expected_hash = hashlib.sha256((sample_repo / "auth.py").read_bytes()).hexdigest()
        patch = (
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate_password(pw: str) -> bool:\n"
            "-    return len(pw) >= 0\n"
            "+    return len(pw) >= 8\n"
        )
        responses = [
            model_response("d1", [make_tool_call("c1", "read_file", path="auth.py")]),
            model_response("d2", [
                make_tool_call("c2", "apply_patch", path="auth.py", patch=patch,
                               expected_sha256=expected_hash),
                make_tool_call("c3", "run_command", argv=["python", "-m", "pytest", "-q"]),
            ]),
            model_response("d3", [make_tool_call("f1", "finish", summary="fixed")]),
        ]
        result = invoke_with_stack(sample_repo, tmp_path, responses, [passed_report()])
        assert result.exit_code == 0

        run_dirs = list((data_dir / "runs").iterdir())
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]
        # 四类产物齐全
        for name in ("changes.patch", "trajectory.jsonl", "result.json", "test-report.json"):
            assert (run_dir / name).exists(), name
        # patch 含真实修改
        patch_text = (run_dir / "changes.patch").read_text(encoding="utf-8")
        assert "return len(pw) >= 8" in patch_text
        # trajectory 合法 JSONL
        lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert lines and all(json.loads(line) for line in lines)
        # result.json 符合 RunResult
        result_data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert result_data["runtime_status"] == "completed"
        assert result_data["modified_files"] == ["auth.py"]
        assert result_data["model_calls"] == 3
        # test-report 含验证摘要
        assert "verification" in json.loads((run_dir / "test-report.json").read_text(encoding="utf-8"))

    def test_ti12_same_taskspec_twice_isolated(self, sample_repo, tmp_path):
        responses = [model_response("d1", [make_tool_call("f1", "finish", summary="done")])]
        for _ in range(2):
            invoke_with_stack(sample_repo, tmp_path, responses, [passed_report()])
        run_dirs = sorted((tmp_path / "data" / "runs").iterdir())
        assert len(run_dirs) == 2
        assert run_dirs[0].name != run_dirs[1].name  # 不同 run_id、相互隔离
