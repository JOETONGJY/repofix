"""WorkspaceVerifier 测试：VF-01/04~08（specs/02 Final Verification 规则，design/08）。

无 Docker：文件操作走真实 git 副本，命令经 FakeDockerCli 脚本化。
"""

from repofix.contracts import (
    PatchStatus,
    TestStatus,
    VerificationAttribution,
    VerificationOutcome,
    VerificationRequest,
)
from repofix.verifier.workspace_verifier import WorkspaceVerifier
from repofix.workspace.docker_cli import CliResult, DockerCliError
from repofix.workspace.docker_workspace import compute_patch_hash


def make_request(**overrides) -> VerificationRequest:
    defaults = dict(
        run_id="run_v", workspace_ref="ws", base_commit="abc123",
        candidate_revision="", test_plan=[], baseline_result={}, timeout_seconds=60,
    )
    defaults.update(overrides)
    return VerificationRequest(**defaults)


def apply_real_change(ws) -> str:
    """在副本内产生真实修改，返回 patch_hash。"""
    target = ws.task_dir / "auth.py"
    old = target.read_bytes()
    target.write_text(
        "def validate_password(pw: str) -> bool:\n    return len(pw) >= 8\n",
        encoding="utf-8",
    )
    _ = old
    return ws.get_changes()["patch_hash"]


def build_verifier(ws, plan=None, baseline=None, exec_results=None, exec_hook=None):
    if exec_results is not None or exec_hook is not None:
        from tests.conftest import FakeDockerCli

        ws._cli = FakeDockerCli(exec_results=exec_results, exec_hook=exec_hook)
    plan = plan if plan is not None else [["python", "-m", "pytest", "-q", "--tb=no", "-rf"]]
    return WorkspaceVerifier(ws, plan, baseline or {}), ws._cli


class TestVf08EmptyPatch:
    def test_no_diff_rejected_as_failed(self, task_workspace):
        """VF-08：无 Diff 的纯解释 finish → PatchValidator 判 EMPTY → FAILED 反馈。"""
        verifier, _ = build_verifier(task_workspace, plan=[])
        report = verifier.verify(make_request())
        assert report.patch_status is PatchStatus.EMPTY
        assert report.outcome is VerificationOutcome.FAILED
        assert "no code changes" in report.summary


class TestVf05NoTests:
    def test_valid_patch_without_tests_is_inconclusive_never_passed(self, task_workspace):
        apply_real_change(task_workspace)
        verifier, _ = build_verifier(task_workspace, plan=[])
        report = verifier.verify(make_request())
        assert report.patch_status is PatchStatus.VALID
        assert report.test_status is TestStatus.NOT_AVAILABLE
        assert report.outcome is VerificationOutcome.INCONCLUSIVE  # 不能错误产生 PASSED
        assert report.failure_kind is VerificationAttribution.UNDETERMINABLE


class TestVf01Attribution:
    def make_changed_ws(self, task_workspace):
        apply_real_change(task_workspace)
        return task_workspace

    def test_assertion_failure_attribution(self, task_workspace):
        ws = self.make_changed_ws(task_workspace)
        verifier, _ = build_verifier(ws, exec_results=[
            CliResult(1, "1 failed\nFAILED tests/test_auth.py::test_empty_password_rejected", ""),
        ])
        report = verifier.verify(make_request())
        assert report.outcome is VerificationOutcome.FAILED
        assert report.failure_kind is VerificationAttribution.ASSERTION_FAILURE
        assert report.failed_tests == ["tests/test_auth.py::test_empty_password_rejected"]

    def test_environment_error_attribution(self, task_workspace):
        ws = self.make_changed_ws(task_workspace)
        verifier, _ = build_verifier(ws, exec_results=[
            CliResult(4, "ERROR: usage", ""),  # pytest usage error
        ])
        report = verifier.verify(make_request())
        assert report.outcome is VerificationOutcome.ERROR
        assert report.failure_kind is VerificationAttribution.ENVIRONMENT_ERROR
        assert report.test_status is TestStatus.ERROR

    def test_timeout_attribution(self, task_workspace):
        ws = self.make_changed_ws(task_workspace)
        ws._container_up = True  # 模拟运行中的容器：超时触发进程树终止路径
        verifier, _ = build_verifier(ws, exec_results=[
            DockerCliError("docker exec timed out after 300s"),
        ])
        report = verifier.verify(make_request())
        assert report.outcome is VerificationOutcome.ERROR
        assert report.failure_kind is VerificationAttribution.EXECUTION_TIMEOUT


class TestVf06CandidateBinding:
    def test_report_bound_to_patch_hash(self, task_workspace):
        patch_hash = apply_real_change(task_workspace)
        verifier, _ = build_verifier(task_workspace, exec_results=[
            CliResult(0, "1 passed", ""),
        ])
        report = verifier.verify(make_request())
        assert report.candidate_revision == patch_hash  # CONS-03：绑定 Workspace 统一函数生成的版本
        assert report.candidate_revision == compute_patch_hash(task_workspace.get_changes()["diff"])

    def test_code_changed_during_verification_is_error(self, task_workspace):
        """VF-06：测试运行修改了待交付源码 → 该验证不能用于接受 Finish。"""
        patch_hash = apply_real_change(task_workspace)

        def hook(argv):
            # 模拟"测试命令运行期间修改源码"
            target = task_workspace.task_dir / "auth.py"
            target.write_text(
                "def validate_password(pw: str) -> bool:\n    return len(pw) >= 8\n    # mutated\n",
                encoding="utf-8",
            )

        verifier, _ = build_verifier(task_workspace, exec_results=[
            CliResult(0, "1 passed", ""),
        ], exec_hook=hook)
        report = verifier.verify(make_request(candidate_revision=""))
        assert report.outcome is VerificationOutcome.ERROR
        assert "changed during verification" in report.summary
        assert report.candidate_revision != patch_hash  # 报告绑定的是变化后的版本

    def test_stale_candidate_revision_rejected(self, task_workspace):
        apply_real_change(task_workspace)
        verifier, _ = build_verifier(task_workspace, exec_results=[
            CliResult(0, "1 passed", ""),
        ])
        report = verifier.verify(make_request(candidate_revision="0" * 64))  # 旧版本
        assert report.outcome is VerificationOutcome.ERROR
        assert "mismatch" in report.summary


class TestVf07TestPlanFrozen:
    def test_request_cannot_rewrite_plan(self, task_workspace):
        """VF-07：Main 在 finish 时无法改写规定测试计划——verifier 只用初始化时冻结的计划。"""
        apply_real_change(task_workspace)
        verifier, cli = build_verifier(task_workspace, plan=[["python", "-m", "pytest", "-q"]])
        request = make_request(test_plan=["python", "-m", "compileall", "."])
        verifier.verify(request)
        executed = [argv for _, argv, _ in cli.exec_calls]
        assert ["python", "-m", "pytest", "-q"] in executed  # 冻结的计划被执行
        assert ["python", "-m", "compileall", "."] not in executed  # 请求中的改写被忽略


class TestCacheExclusion:
    def test_pytest_cache_not_in_candidate(self, task_workspace):
        """测试运行自身的缓存不进入候选版本（否则 VF-06 误判）。"""
        apply_real_change(task_workspace)
        cache = task_workspace.task_dir / ".pytest_cache"
        cache.mkdir()
        (cache / "v" / "cache").mkdir(parents=True)
        (cache / "v" / "cache" / "lastfailed").write_text("x", encoding="utf-8")
        changes = task_workspace.get_changes()
        assert ".pytest_cache" not in changes["diff"]
        assert all(".pytest_cache" not in name for name in changes["changed_files"])


