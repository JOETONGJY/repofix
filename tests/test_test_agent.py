"""Test Agent 测试（MVP v0.4 功能 B）。

覆盖：触发矩阵、复现分类（含"非零退出码 ≠ 复现成功"）、候选内容结构校验、
一次性生成（成功/拒绝/Schema 违规/模型失败/Usage 缺失）、隔离基线复现端到端
（真实 Git 副本 + Fake Docker）、修复后 Fail-to-Pass 补充验证、清理纪律、
Verifier 权威语义不变、CLI 级产物与 patch 洁净性。
"""
import json
from pathlib import Path

import pytest

from repofix.cli import StackExtras
from repofix.contracts import (
    AgentActor,
    EventType,
    RunConfig,
    TaskSpec,
    VerificationOutcome,
    VerificationRequest,
)
from repofix.gateway.client import ModelPermanentError
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.test_agent.generator import TestAgentGenerator
from repofix.test_agent.runner import (
    GENERATED_TEST_PATH,
    TestAgentRunner,
    classify_reproduction,
    should_trigger,
    validate_candidate_source,
)
from repofix.test_agent.schemas import ReproStatus, TestAgentReport
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.verifier.workspace_verifier import WorkspaceVerifier
from repofix.workspace.docker_cli import CliResult, DockerCliError
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import PreflightReport, PreflightRunner
from tests.conftest import (
    FakeClock,
    FakeDockerCli,
    ScriptedModelClient,
    make_tool_call,
    model_response,
    run_git,
)

RUN_ID = "run_ta"
VALID_TEST_SOURCE = (
    "from calc import add\n\n\n"
    "def test_add_two_numbers():\n"
    "    assert add(1, 1) == 2\n"
)
BASELINE_FAILED_OUTPUT = (
    "F                                                                        [100%]\n"
    "================================== FAILURES ===================================\n"
    "________________________________ test_add_two_numbers ______________________________\n"
    "    assert add(1, 1) == 2\nE           +  where 0 = add(1, 1)\n"
    "FAILED tests/test_repofix_generated.py::test_add_two_numbers - assert 0 == 2\n"
    "1 failed in 0.01s\n"
)
FIX_PATCH = (
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


@pytest.fixture
def bug_repo(tmp_path):
    """无测试目录的 Git 仓库：calc.py 的 add 实现为减法（Bug）。"""
    repo = tmp_path / "bug-repo"
    repo.mkdir()
    run_git(["init", "-q", "-b", "main"], repo)
    run_git(["config", "user.email", "repofix@test.local"], repo)
    run_git(["config", "user.name", "repofix-test"], repo)
    run_git(["config", "core.autocrlf", "false"], repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "README.md").write_text("# bug repo\n", encoding="utf-8")
    run_git(["add", "-A"], repo)
    run_git(["commit", "-q", "-m", "init"], repo)
    return repo


class PytestScriptedCli(FakeDockerCli):
    """探针命令返回成功；pytest 命令按脚本返回（隔离 Preflight 探针与测试执行）。"""

    def __init__(self, pytest_results):
        super().__init__()
        self.pytest_results = list(pytest_results)

    def exec(self, name, argv, timeout=120, cwd_in_container="/workspace") -> CliResult:
        if argv[:3] == ["python", "-m", "pytest"]:
            self.exec_calls.append((name, argv, cwd_in_container))
            item = self.pytest_results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return super().exec(name, argv, timeout=timeout, cwd_in_container=cwd_in_container)


DEFAULT_USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


def generation_response(source=VALID_TEST_SOURCE, usage=DEFAULT_USAGE):
    payload = {"test_source": source, "explanation": "add() subtracts instead of adding"}
    return model_response("tg1", text=json.dumps(payload), usage=usage)


def empty_preflight() -> PreflightReport:
    return PreflightReport(ok=True, test_plan=[], test_plan_source="auto", test_plan_evidence="")


def make_runner(bug_repo, tmp_path, responses, exec_results, run_id=RUN_ID):
    store = SQLiteEventStore(":memory:")
    config = RunConfig(model_id="scripted-model", review_enabled=False,
                       test_agent_enabled=True, output_dir=str(tmp_path))
    runner = TestAgentRunner(
        run_id=run_id,
        task=TaskSpec(instruction="Fix add(): add(1, 1) returns 0 but should return 2",
                      repo_path=str(bug_repo), base_commit="HEAD"),
        config=config,
        model_client=ScriptedModelClient(responses),
        store=store,
        workspaces_root=tmp_path / "workspaces",
        clock=FakeClock(),
        docker_cli=exec_results,
    )
    return runner, store


class TestShouldTrigger:
    def _config(self, enabled):
        return RunConfig(model_id="m", test_agent_enabled=enabled)

    def test_disabled_never_triggers(self):
        assert should_trigger(self._config(False), empty_preflight()) == (False, "disabled by config")

    def test_existing_plan_blocks_trigger(self):
        report = PreflightReport(ok=True, test_plan=[["python", "-m", "pytest", "-q"]],
                                 test_plan_source="manual")
        assert should_trigger(self._config(True), report)[0] is False

    def test_empty_plan_triggers(self):
        assert should_trigger(self._config(True), empty_preflight())[0] is True


class TestClassifyReproduction:
    @pytest.mark.parametrize("exit_code,output,expected", [
        (0, "", ReproStatus.NOT_REPRODUCED),  # 测试通过 = 未复现
        (1, BASELINE_FAILED_OUTPUT, ReproStatus.REPRODUCED),
        # 失败身份不在生成测试内：不算复现（非零退出码 ≠ 复现成功）
        (1, "FAILED tests/test_other.py::test_x - boom\n", ReproStatus.NOT_REPRODUCED),
        (1, "no parseable identity\n", ReproStatus.NOT_REPRODUCED),
        (2, "ERROR collecting tests/test_repofix_generated.py\n", ReproStatus.INVALID_TEST),
        (5, "no tests ran\n", ReproStatus.INVALID_TEST),
        (3, "internal error\n", ReproStatus.ENVIRONMENT_ERROR),
        (4, "usage error\n", ReproStatus.ENVIRONMENT_ERROR),
        (None, "", ReproStatus.ENVIRONMENT_ERROR),
    ])
    def test_matrix(self, exit_code, output, expected):
        status, _ = classify_reproduction(exit_code, output, GENERATED_TEST_PATH)
        assert status is expected


class TestValidateCandidateSource:
    def test_valid_source_passes(self):
        assert validate_candidate_source(VALID_TEST_SOURCE) == []

    def test_syntax_error_rejected(self):
        violations = validate_candidate_source("def broken(:\n    pass\n")
        assert any("syntax error" in v for v in violations)

    def test_no_test_function_rejected(self):
        violations = validate_candidate_source("from calc import add\n\nprint(add(1, 1))\n")
        assert any("no pytest-style test function" in v for v in violations)

    def test_oversize_rejected(self):
        source = VALID_TEST_SOURCE + "#" + "x" * (8 * 1024)
        assert any("exceeds" in v for v in validate_candidate_source(source))

    @pytest.mark.parametrize("source,marker", [
        ("import subprocess\nsubprocess.run(['ls'])\n", "forbidden import"),
        ("import socket\n", "forbidden import"),
        ("from urllib.request import urlopen\n", "forbidden import"),
        ("open('/etc/passwd')\n\ndef test_x():\n    pass\n", "absolute path"),
        ("open('C:\\\\Users\\\\bob\\\\secret')\n\ndef test_x():\n    pass\n", "absolute path"),
    ])
    def test_dangerous_constructs_rejected(self, source, marker):
        violations = validate_candidate_source(source)
        assert any(marker in v for v in violations)

    def test_no_absolute_path_false_positive_on_relative(self):
        assert validate_candidate_source("open('calc.py')\n\ndef test_x():\n    pass\n") == []


class TestGenerator:
    def _generate(self, responses, store=None):
        client = ScriptedModelClient(responses)
        gen = TestAgentGenerator(run_id="run_gen", model_client=client, model_id="m",
                                 store=store, clock=FakeClock())
        return gen.generate(instruction="Fix add", base_commit="abc",
                            snippets=[("calc.py", "def add(a, b):\n    return a - b\n")])

    def test_success_parses_candidate_and_usage(self):
        store = SQLiteEventStore(":memory:")
        result = self._generate([generation_response()], store=store)
        assert result.candidate is not None
        assert result.candidate.test_source == VALID_TEST_SOURCE
        assert result.model_calls == 1
        assert result.usage["total_tokens"] == 150
        assert result.duration_ms >= 0
        events = store.get_events("run_gen")
        assert any(e.type is EventType.MODEL_REQUESTED and e.actor is AgentActor.TEST_AGENT
                   for e in events)
        assert any(e.type is EventType.MODEL_RESPONDED and e.actor is AgentActor.TEST_AGENT
                   for e in events)

    def test_non_json_output_is_schema_violation(self):
        result = self._generate([model_response("t", text="I wrote a test, hope it helps!")])
        assert result.candidate is None
        assert "JSON" in result.invalid_schema_reason

    def test_declined_when_model_cannot_determine(self):
        response = model_response("t", text=json.dumps(
            {"cannot_generate": True, "reason": "expected behavior is ambiguous"}))
        result = self._generate([response])
        assert result.declined is True
        assert "ambiguous" in result.decline_reason
        assert result.candidate is None

    def test_model_error_recorded_no_retry(self):
        result = self._generate([ModelPermanentError("HTTP 401")])
        assert result.candidate is None
        assert "ModelPermanentError" in result.error
        assert result.model_calls == 1  # 一次尝试，不重试

    def test_missing_usage_stays_unknown(self):
        result = self._generate([generation_response(usage=None)])  # Gateway 未返回 Usage
        assert result.usage is None
        assert result.candidate is not None  # 生成本身成功，只是用量未知


class TestBaselineReproduction:
    def test_reproduced_end_to_end(self, bug_repo, tmp_path):
        runner, store = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT)]))
        report = runner.run_baseline(empty_preflight(), repo_overview=None)
        assert report.status is ReproStatus.REPRODUCED
        assert report.triggered is True
        assert report.baseline_exit_code == 1
        assert report.token_usage == {"prompt": 100, "completion": 50, "total": 150}
        assert report.authoritative_verification is False
        # 生成测试只注入测试工作区，原仓库不被触碰
        injected = tmp_path / "workspaces" / f"{RUN_ID}-testagent" / GENERATED_TEST_PATH
        assert injected.exists()
        assert (bug_repo / GENERATED_TEST_PATH).exists() is False
        events = store.get_events(RUN_ID)
        assert any(e.type is EventType.TEST_AGENT_STARTED and e.actor is AgentActor.TEST_AGENT
                   for e in events)
        assert any(e.type is EventType.TEST_AGENT_FINISHED for e in events)
        runner.cleanup()
        assert not injected.parent.parent.exists()  # 测试工作区已清理

    def test_not_reproduced_when_test_passes_on_baseline(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(0, "1 passed in 0.01s", "")]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.NOT_REPRODUCED
        runner.cleanup()

    def test_invalid_test_syntax_error(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response(source="def broken(:\n    pass\n")],
            PytestScriptedCli([]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.INVALID_TEST
        assert any("syntax error" in v for v in report.violations)
        injected = tmp_path / "workspaces" / f"{RUN_ID}-testagent" / GENERATED_TEST_PATH
        assert not injected.exists()  # 校验失败不注入
        runner.cleanup()

    def test_schema_violation_is_invalid_test(self, bug_repo, tmp_path):
        response = model_response("t", text=json.dumps({"test_source": "", "explanation": ""}))
        runner, _ = make_runner(bug_repo, tmp_path, [response], PytestScriptedCli([]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.INVALID_TEST
        assert "schema" in report.status_detail.lower()
        runner.cleanup()

    def test_generation_failed_degrades_safely(self, bug_repo, tmp_path):
        runner, _ = make_runner(bug_repo, tmp_path,
                                [ModelPermanentError("HTTP 401")], PytestScriptedCli([]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.GENERATION_FAILED
        assert "401" in report.status_detail
        runner.cleanup()  # 生成失败也必须清理工作区与容器

    def test_declined_by_model(self, bug_repo, tmp_path):
        response = model_response("t", text=json.dumps(
            {"cannot_generate": True, "reason": "behavior ambiguous"}))
        runner, _ = make_runner(bug_repo, tmp_path, [response], PytestScriptedCli([]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.DECLINED
        runner.cleanup()

    def test_environment_error_on_docker_failure(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([DockerCliError("docker daemon down")]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.ENVIRONMENT_ERROR
        assert report.baseline_exit_code is None
        runner.cleanup()

    def test_missing_usage_recorded_unknown(self, bug_repo, tmp_path):
        response = generation_response(usage=None)
        runner, _ = make_runner(
            bug_repo, tmp_path, [response],
            PytestScriptedCli([CliResult(0, "", "")]))
        report = runner.run_baseline(empty_preflight())
        assert report.status is ReproStatus.NOT_REPRODUCED
        # Gateway 未返回 Usage → 明确记录为未知，不得伪造为零
        assert report.token_usage == {"prompt": None, "completion": None, "total": None}
        runner.cleanup()


class TestPostPatchVerification:
    def test_fail_to_pass_recorded(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT), CliResult(0, "", "")]))
        assert runner.run_baseline(empty_preflight()).status is ReproStatus.REPRODUCED
        runner.verify_post_patch(FIX_PATCH)
        assert runner.report.post_patch_status == "passed"
        assert runner.report.fail_to_pass is True
        # 真实 Patch 应用到测试工作区（不是复制源码）
        ws_calc = tmp_path / "workspaces" / f"{RUN_ID}-testagent" / "calc.py"
        assert "a + b" in ws_calc.read_text(encoding="utf-8")
        runner.cleanup()

    def test_post_patch_failed_when_still_reproduced(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT),
                               CliResult(1, "", BASELINE_FAILED_OUTPUT)]))
        runner.run_baseline(empty_preflight())
        runner.verify_post_patch(FIX_PATCH)
        assert runner.report.post_patch_status == "failed"
        assert runner.report.fail_to_pass is False
        runner.cleanup()

    def test_skipped_when_not_reproduced(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(0, "", "")]))
        runner.run_baseline(empty_preflight())
        runner.verify_post_patch(FIX_PATCH)
        assert runner.report.post_patch_status == "not_run"
        assert runner.report.fail_to_pass is False
        runner.cleanup()

    def test_apply_failure_is_error_not_crash(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT)]))
        runner.run_baseline(empty_preflight())
        runner.verify_post_patch("this is not a valid patch")
        assert runner.report.post_patch_status == "error"
        assert runner.report.fail_to_pass is False
        runner.cleanup()

    def test_empty_patch_skipped(self, bug_repo, tmp_path):
        runner, _ = make_runner(
            bug_repo, tmp_path,
            [generation_response()],
            PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT)]))
        runner.run_baseline(empty_preflight())
        runner.verify_post_patch("")
        assert runner.report.post_patch_status == "not_run"
        runner.cleanup()


class TestAuthoritativeSemanticsUnchanged:
    def test_generated_test_cannot_flip_verifier(self, tmp_path):
        """生成测试 Fail-to-Pass 也不改变 Verifier：原 TestPlan 为空 ⇒ INCONCLUSIVE。"""
        class _FakeWs:
            def get_changes(self):
                return {"diff": "--- a/calc.py\n+++ b/calc.py\n@@", "patch_hash": "h1"}

            def run_command(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("empty test plan must not run commands")

        verifier = WorkspaceVerifier(_FakeWs(), [], {})
        report = verifier.verify(VerificationRequest(
            run_id="r", workspace_ref="r", base_commit="abc", candidate_revision="h1",
            test_plan=[], baseline_result={},
        ))
        assert report.outcome is VerificationOutcome.INCONCLUSIVE
        assert report.test_status.value == "not_available"
        # 报告永远声明：生成测试非权威验证
        assert TestAgentReport(enabled=True, triggered=True,
                               status=ReproStatus.REPRODUCED).authoritative_verification is False


class TestCliIntegration:
    def _install_fake_stack(self, monkeypatch, bug_repo, tmp_path, exec_results):
        """真实 CLI 流程 + 假模型/容器 + 真实 Verifier：验证 run() 对新功能的编排与产物。"""
        import repofix.cli as cli_module

        def fake_stack(task_spec, config, run_id):
            manager = WorkspaceManager(Path(config.output_dir) / "workspaces")
            handle = manager.prepare(run_id, Path(task_spec.repo_path), task_spec.base_commit)
            cli = exec_results
            workspace = DockerWorkspace(handle.task_dir, run_id,
                                        base_commit=handle.base_commit, cli=cli)
            preflight = PreflightRunner(cli=cli)
            report = preflight.run(workspace, task_spec.test_command)
            assert report.ok
            registry = ToolRegistry()
            register_default_tools(registry)
            store = SQLiteEventStore(Path(config.output_dir) / "runs" / run_id / "run.sqlite")
            extras = StackExtras()
            # 模拟 Main Agent 已完成修复（脚本只回 finish）：非空 patch 是 VF-08 前提
            calc = handle.task_dir / "calc.py"
            calc.write_text(calc.read_text(encoding="utf-8").replace("a - b", "a + b"),
                            encoding="utf-8")
            if config.test_agent_enabled:
                ta = TestAgentRunner(
                    run_id=run_id, task=task_spec, config=config,
                    model_client=ScriptedModelClient([generation_response()]),
                    store=store, workspaces_root=Path(config.output_dir) / "workspaces",
                    docker_cli=cli)
                ta.run_baseline(report)
                extras.test_agent = ta
            if config.repo_overview_enabled:
                from repofix.cli import _generate_repo_overview

                extras.repo_overview_text, extras.repo_overview_meta = \
                    _generate_repo_overview(handle.task_dir)
            verifier = WorkspaceVerifier(
                workspace, report.test_plan, report.baseline,
                timeout_seconds=config.command_timeout_seconds,
            )
            runner = AgentRunner(
                run_id=run_id, task=task_spec, config=config,
                model_client=ScriptedModelClient(
                    [model_response("m1", [make_tool_call("f1", "finish", summary="done")])]),
                registry=registry,
                dispatcher=ToolDispatcher(registry, workspace),
                store=store, verifier=verifier, clock=FakeClock(),
                patch_hash_provider=lambda: workspace.get_changes()["patch_hash"],
                repo_overview=extras.repo_overview_text,
                repo_overview_meta=extras.repo_overview_meta or None,
            )
            return runner, store, workspace, manager, handle, report, extras

        monkeypatch.setattr(cli_module, "_build_stack", fake_stack)

    def test_test_agent_flag_full_flow(self, bug_repo, tmp_path, monkeypatch):
        self._install_fake_stack(monkeypatch, bug_repo, tmp_path,
                                 PytestScriptedCli([CliResult(1, "", BASELINE_FAILED_OUTPUT),
                                                    CliResult(0, "", "")]))
        from typer.testing import CliRunner

        from repofix.cli import app

        result = CliRunner().invoke(app, [
            "run", "--repo", str(bug_repo), "--task", "Fix add", "--test-command", "auto",
            "--output-dir", str(tmp_path / "data"), "--test-agent", "--no-review",
        ])
        # 原仓库无测试 → Verifier INCONCLUSIVE：即使生成测试 Fail-to-Pass，
        # 退出码仍是 1（完成但可见验证不充分），不宣称权威验证通过
        assert result.exit_code == 1
        run_dirs = list((tmp_path / "data" / "runs").iterdir())
        run_dir = run_dirs[0]
        # 产物：test-agent/report.json + candidate_test.py；patch 不含生成测试
        report_payload = json.loads((run_dir / "test-agent" / "report.json").read_text(encoding="utf-8"))
        assert report_payload["status"] == "reproduced"
        assert report_payload["post_patch_status"] == "passed"
        assert report_payload["fail_to_pass"] is True
        assert report_payload["authoritative_verification"] is False
        assert (run_dir / "test-agent" / "candidate_test.py").exists()
        patch_text = (run_dir / "changes.patch").read_text(encoding="utf-8")
        assert "calc.py" in patch_text
        assert "test_repofix_generated" not in patch_text
        # test-report.json 汇总区段 + Trace 事件
        test_report = json.loads((run_dir / "test-report.json").read_text(encoding="utf-8"))
        assert test_report["test_agent"]["fail_to_pass"] is True
        trajectory = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8")
        assert "test_agent_started" in trajectory
        assert "test_agent_finished" in trajectory
        # 测试工作区副本随主流程清理（无 -testagent 残留）
        ws_dirs = [p.name for p in (tmp_path / "data" / "workspaces").iterdir()]
        assert not any(name.endswith("-testagent") for name in ws_dirs)
        # 不因生成测试通过而宣称权威验证通过
        result_payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert result_payload["verification_status"] == "inconclusive"

    def test_repo_overview_flag_writes_artifact_and_injects(self, bug_repo, tmp_path, monkeypatch):
        self._install_fake_stack(monkeypatch, bug_repo, tmp_path, FakeDockerCli())
        from typer.testing import CliRunner

        from repofix.cli import app

        result = CliRunner().invoke(app, [
            "run", "--repo", str(bug_repo), "--task", "Fix add", "--test-command", "auto",
            "--output-dir", str(tmp_path / "data"), "--repo-overview", "--no-review",
        ])
        assert result.exit_code == 1  # 完成；仓库无测试 → 可见验证 INCONCLUSIVE
        run_dir = list((tmp_path / "data" / "runs").iterdir())[0]
        overview = (run_dir / "repository-overview.txt").read_text(encoding="utf-8")
        assert "Repository Overview" in overview
        assert "calc.py" in overview
        assert "repo_overview_generated" in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8")
        # 默认关闭的 Test Agent 不产生任何产物
        assert not (run_dir / "test-agent").exists()

    def test_flags_off_preserve_old_behavior(self, bug_repo, tmp_path, monkeypatch):
        self._install_fake_stack(monkeypatch, bug_repo, tmp_path, FakeDockerCli())
        from typer.testing import CliRunner

        from repofix.cli import app

        result = CliRunner().invoke(app, [
            "run", "--repo", str(bug_repo), "--task", "Fix add", "--test-command", "auto",
            "--output-dir", str(tmp_path / "data"), "--no-review",
        ])
        assert result.exit_code == 1  # 完成；INCONCLUSIVE（与旧语义一致）
        run_dir = list((tmp_path / "data" / "runs").iterdir())[0]
        assert not (run_dir / "repository-overview.txt").exists()
        assert not (run_dir / "test-agent").exists()
        trajectory = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8")
        assert "repo_overview_generated" not in trajectory
        assert "test_agent_started" not in trajectory
        test_report = json.loads((run_dir / "test-report.json").read_text(encoding="utf-8"))
        assert "repo_overview" not in test_report
        assert "test_agent" not in test_report
