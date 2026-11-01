"""WorkspaceVerifier：确定性 Final Verification（design/08、specs/02 Final Verification 规则）。

不是第三个 Agent——不调用 LLM 判断代码正确性，只依据真实工作区、Patch 与测试执行结果
生成 VerificationReport。test_plan 在任务初始化（Preflight）时确定并冻结于此（VF-07：
Main 不能在 finish 时改写）；candidate_revision = patch_hash = sha256(get_changes diff)
由 Workspace 的统一函数生成（CONS-03）。

完成规则（specs/02 七种情况）：
- 合法非空 Patch + 测试通过 → PASSED
- Patch 为空（VF-08）/ 测试断言失败 → FAILED（反馈 Main 修正）
- 没有可用测试 → INCONCLUSIVE（不得宣称验证通过，VF-05）
- 测试命令无法启动 / 用法错误 → ERROR（环境，Run FAILED）
- 测试超时（副作用未知）→ ERROR
- 验证期间候选代码变化（VF-06）/ 请求的候选版本与工作区不符 → ERROR（旧验证失效）
"""
from repofix.contracts import (
    PatchStatus,
    TestStatus,
    VerificationAttribution,
    VerificationOutcome,
    VerificationReport,
    VerificationRequest,
)
from repofix.tools.dispatcher import ExecutorCrashError
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.errors import WorkspaceError
from repofix.workspace.preflight import parse_pytest_failures

# 测试运行自身可能产生的缓存产物不进入候选版本（specs/03 WS-07：排除缓存）
_DIFF_EXCLUDES = [
    ":(exclude).pytest_cache",
    ":(exclude)__pycache__",
    ":(exclude)*.pyc",
    ":(exclude).mypy_cache",
    ":(exclude).ruff_cache",
    ":(exclude).coverage",
]


def classify_exit_code(exit_code: int | None) -> tuple[TestStatus, VerificationOutcome, VerificationAttribution | None]:
    """测试退出码 → (test_status, outcome, failure_kind)。"""
    if exit_code is None:
        return TestStatus.ERROR, VerificationOutcome.ERROR, VerificationAttribution.ENVIRONMENT_ERROR
    if exit_code == 0:
        return TestStatus.PASSED, VerificationOutcome.PASSED, None
    if exit_code == 1:
        return TestStatus.FAILED, VerificationOutcome.FAILED, VerificationAttribution.ASSERTION_FAILURE
    if exit_code == 5:
        # pytest：未收集到测试
        return TestStatus.NOT_AVAILABLE, VerificationOutcome.INCONCLUSIVE, VerificationAttribution.UNDETERMINABLE
    return TestStatus.ERROR, VerificationOutcome.ERROR, VerificationAttribution.ENVIRONMENT_ERROR


class WorkspaceVerifier:
    """实现 Verifier 协议。依赖真实 Workspace 的 get_changes / run_command。"""

    def __init__(
        self,
        workspace: DockerWorkspace,
        test_plan: list[list[str]],
        baseline: dict,
        timeout_seconds: int = 300,
    ):
        self._workspace = workspace
        self._test_plan = list(test_plan)  # 初始化时冻结（VF-07）
        self._baseline = dict(baseline)
        self._timeout = timeout_seconds

    @property
    def test_plan(self) -> list[list[str]]:
        return list(self._test_plan)

    def verify(self, request: VerificationRequest) -> VerificationReport:
        try:
            changes = self._workspace.get_changes()
        except WorkspaceError as exc:
            return self._error(request, VerificationAttribution.ENVIRONMENT_ERROR,
                               f"workspace diff failed: {exc}")
        patch_hash = changes["patch_hash"]
        diff = changes["diff"]
        patch_status = PatchStatus.VALID if diff.strip() else PatchStatus.EMPTY

        # 验证前一致性：请求携带的候选版本与工作区不符 → 旧验证失效（VF-06 前置）
        if (request.candidate_revision
                and request.candidate_revision != patch_hash
                and not request.candidate_revision.startswith(("m1-", "m2-", "m3-"))):
            return self._error(request, VerificationAttribution.UNDETERMINABLE,
                               "candidate_revision mismatch before verification; stale report")

        if patch_status is PatchStatus.EMPTY:
            # VF-08：Bug 修复必须产生有效代码变化；空 Diff → FAILED 反馈 Main
            return VerificationReport(
                candidate_revision=patch_hash, patch_status=patch_status,
                test_status=TestStatus.NOT_AVAILABLE, outcome=VerificationOutcome.FAILED,
                summary="no code changes; a bug fix must produce a non-empty diff",
            )

        if not self._test_plan:
            # VF-05：没有可用测试 → INCONCLUSIVE，不得宣称验证通过
            return VerificationReport(
                candidate_revision=patch_hash, patch_status=patch_status,
                test_status=TestStatus.NOT_AVAILABLE, outcome=VerificationOutcome.INCONCLUSIVE,
                failure_kind=VerificationAttribution.UNDETERMINABLE,
                summary="no test plan configured; cannot claim verification passed",
            )

        # 执行规定测试（&& 语义短路；test_plan 冻结于初始化，request.test_plan 被忽略 → VF-07）
        combined_output: list[str] = []
        exit_code: int | None = 0
        try:
            for i, argv in enumerate(self._test_plan):
                result = self._workspace.run_command(
                    argv, cwd=".", timeout_seconds=self._timeout, call_id=f"verify-{i}"
                )
                combined_output.append(result.content)
                exit_code = result.exit_code
                if exit_code != 0:
                    break
        except ExecutorCrashError as exc:
            return self._error(request, VerificationAttribution.EXECUTION_TIMEOUT,
                               f"test execution aborted (side effects unknown): {exc}")

        # 验证后一致性（VF-06，覆盖全部结果）：测试运行期间候选代码变化 → 旧验证失效
        drift = self._drift_check(patch_hash)
        if drift:
            return drift

        test_status, outcome, failure_kind = classify_exit_code(exit_code)
        failed_tests = parse_pytest_failures("\n".join(combined_output))
        summaries = {
            VerificationOutcome.PASSED: "visible tests passed",
            VerificationOutcome.FAILED: "visible tests failed; feedback returned to Main",
            VerificationOutcome.INCONCLUSIVE: "no tests collected by the configured commands",
            VerificationOutcome.ERROR: "test command failed to run properly (environment)",
        }
        return VerificationReport(
            candidate_revision=patch_hash, patch_status=patch_status,
            test_status=test_status, outcome=outcome, failure_kind=failure_kind,
            failed_tests=failed_tests, summary=summaries[outcome],
            report_ref=None,
        )

    def _drift_check(self, expected_hash: str) -> VerificationReport | None:
        try:
            current = self._workspace.get_changes()["patch_hash"]
        except WorkspaceError as exc:
            return self._error(None, VerificationAttribution.ENVIRONMENT_ERROR,
                               f"post-test workspace check failed: {exc}")
        if current != expected_hash:
            return self._error(None, VerificationAttribution.UNDETERMINABLE,
                               "candidate code changed during verification; stale report (VF-06)")
        return None

    def _error(self, request, kind: VerificationAttribution, summary: str) -> VerificationReport:
        revision = ""
        try:
            revision = self._workspace.get_changes()["patch_hash"]
        except WorkspaceError:
            pass
        _ = request
        return VerificationReport(
            candidate_revision=revision, patch_status=PatchStatus.INVALID,
            test_status=TestStatus.ERROR, outcome=VerificationOutcome.ERROR,
            failure_kind=kind, summary=summary,
        )
