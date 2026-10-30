"""Environment Preflight（design/05、specs/03）：Run 开始前全部通过才进入 Agent Loop。

关键规则：基线测试失败 ≠ Preflight 失败——区分「测试成功运行但断言失败」
（有价值的基线，记录后继续）与「缺依赖、测试根本无法运行」（环境阻塞，Preflight 失败）。
"""
import shlex
from dataclasses import dataclass, field

from repofix.workspace.docker_cli import DockerCli, DockerCliError
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.errors import WorkspaceError

DEFAULT_IMAGE = "repofix-env:latest"


@dataclass
class PreflightReport:
    ok: bool
    failure_step: str | None = None
    failure_reason: str = ""
    test_plan: list[str] = field(default_factory=list)  # 解析后的结构化 argv 列表（每项 = 一条命令 argv）
    baseline: dict = field(default_factory=dict)


def parse_test_command(test_command: str | None) -> list[list[str]]:
    """CLI --test-command 字符串 → 结构化 argv（CONS-05：唯一解析点，此后不再出现 Shell 解析）。

    支持以 && 分隔的多条命令；每一部分用 shlex 解析为 argv。
    """
    if not test_command or not test_command.strip():
        return []
    plan: list[list[str]] = []
    for part in test_command.split("&&"):
        argv = shlex.split(part.strip())
        if argv:
            plan.append(argv)
    return plan


class PreflightRunner:
    def __init__(self, cli: DockerCli | None = None, image: str = DEFAULT_IMAGE):
        self._cli = cli or DockerCli()
        self._image = image

    def run(self, workspace: DockerWorkspace, test_command: str | None) -> PreflightReport:
        """顺序执行各步；任一环境步骤失败 → ok=False（Run FAILED, fail_layer=WORKSPACE）。"""
        report = PreflightReport(ok=False)
        report.test_plan = parse_test_command(test_command)

        # 1. Docker 可用
        try:
            self._cli.version()
        except DockerCliError as exc:
            return self._fail(report, "docker_available", str(exc))

        # 2. 预构建镜像存在（依赖准备采用预构建环境；Agent 不联网装依赖）
        if not self._cli.image_exists(self._image):
            return self._fail(
                report, "image_available",
                f"image {self._image} not found; build it first (docker/repofix-env.Dockerfile)",
            )

        # 3. 容器启动 + 依赖检测（python / pytest / rg / git 在镜像内自检）
        try:
            workspace.initialize(self._image)
        except (DockerCliError, WorkspaceError) as exc:
            return self._fail(report, "container_start", str(exc))
        for probe in (["python", "--version"], ["python", "-c", "import pytest"],
                      ["rg", "--version"], ["git", "--version"]):
            outcome = self._cli.exec(workspace.container_name, probe, timeout=60)
            if outcome.returncode != 0:
                return self._fail(
                    report, "dependencies",
                    f"probe {' '.join(probe)} failed: {outcome.stderr.strip()[:200]}",
                )

        # 4. 测试命令已在 CLI/初始化层解析为 argv 并将做白名单校验（Verifier 消费 test_plan）
        #    （CONS-05：此处只消费结构化形式，不再做 Shell 解析）

        # 5. 基线测试（可行时）：断言失败 ≠ Preflight 失败
        if report.test_plan:
            report.baseline = self._run_baseline(workspace, report.test_plan)

        report.ok = True
        return report

    def _run_baseline(self, workspace: DockerWorkspace, test_plan: list[list[str]]) -> dict:
        """运行规定测试一次，记录基线（测试身份清单尽量解析；无法解析时保留原始输出引用）。"""
        combined_out: list[str] = []
        exit_code = 0
        try:
            for argv in test_plan:
                outcome = workspace.run_command(argv, cwd=".", timeout_seconds=300, call_id="preflight-baseline")
                combined_out.append(outcome.content)
                if outcome.exit_code not in (0, 1, 5):
                    # 非断言失败（2=用法错误等）按环境阻塞处理
                    return {
                        "kind": "environment_blocked",
                        "exit_code": outcome.exit_code,
                        "detail": outcome.content[:500],
                    }
                exit_code = outcome.exit_code
        except Exception as exc:  # noqa: BLE001 —— 基线运行故障按环境阻塞记录
            return {"kind": "environment_blocked", "exit_code": None, "detail": str(exc)[:500]}
        failed_tests = _parse_pytest_failures("\n".join(combined_out))
        if exit_code == 5 and not failed_tests:
            return {"kind": "no_tests", "exit_code": exit_code}
        return {
            "kind": "baseline_assertion_failures" if failed_tests else "baseline_passed",
            "exit_code": exit_code,
            "failed_tests": failed_tests,
        }

    @staticmethod
    def _fail(report: PreflightReport, step: str, reason: str) -> PreflightReport:
        report.ok = False
        report.failure_step = step
        report.failure_reason = reason
        return report


def _parse_pytest_failures(output: str) -> list[str]:
    """从 pytest -q --tb=no -rf 输出解析失败测试身份（短摘要行 FAILED test::id - reason）。"""
    failures: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            test_id = line[len("FAILED "):].split(" - ")[0].strip()
            failures.append(test_id)
    return failures
