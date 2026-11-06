"""Environment Preflight（design/05、specs/03）：Run 开始前全部通过才进入 Agent Loop。

关键规则（评审 P0-2 修订）：基线结果分类必须准确——
- 基线全部通过 / 断言失败（可解析失败身份）/ 断言失败但无法解析身份 / 无测试：Preflight 通过，
  但后三者不得标记为 baseline_passed；
- 测试命令不在白名单 / 缺依赖 / 命令无法启动 / 执行环境异常：Preflight 失败（ok=False）。
环境失败不能被误判为成功。
"""
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from repofix.workspace.docker_cli import DockerCli, DockerCliError
from repofix.workspace.docker_workspace import (
    DockerWorkspace,
    check_command_allowed,
)
from repofix.workspace.errors import WorkspaceError

DEFAULT_IMAGE = "repofix-env:latest"


@dataclass
class PreflightReport:
    ok: bool
    failure_step: str | None = None
    failure_reason: str = ""
    test_plan: list[str] = field(default_factory=list)  # 解析后的结构化 argv 列表（每项 = 一条命令 argv）
    test_plan_source: str = "none"  # auto（发现）/ manual（用户指定）/ none（未配置）
    test_plan_evidence: str = ""  # auto 发现依据（如 "pytest.ini" / "tests/ 目录"）
    baseline: dict = field(default_factory=dict)


AUTO_TEST_COMMAND = ["python", "-m", "pytest", "-q", "--tb=no", "-rf"]


def parse_test_command(test_command: str | None) -> list[list[str]]:
    """CLI --test-command 字符串 → 结构化 argv（CONS-05：唯一解析点，此后不再出现 Shell 解析）。

    支持以 && 分隔的多条命令；每一部分用 shlex 解析为 argv。
    特殊值 "auto" → []（由 discover_test_plan 在工作区上发现）。
    """
    if not test_command or not test_command.strip():
        return []
    if test_command.strip().lower() == "auto":
        return []
    plan: list[list[str]] = []
    for part in test_command.split("&&"):
        argv = shlex.split(part.strip())
        if argv:
            plan.append(argv)
    return plan


def discover_test_plan(workspace_root: Path) -> tuple[list[list[str]], str]:
    """确定性 Python 测试发现（评审 Auto Verification Discovery）。

    纯文件系统启发，不执行仓库代码。返回 (test_plan, evidence)：
    - pytest 项目（pyproject/pytest.ini/setup.cfg/tox.ini 配置，或 tests|test 目录，
      或 test_*.py 文件）→ [["python", "-m", "pytest", "-q", "--tb=no", "-rf"]]
    - 无任何测试痕迹 → []（空计划 → Preflight 标记 no_tests）
    发现依据记录在 evidence（如 "pytest.ini" / "tests/test_auth.py"）。
    """
    root = Path(workspace_root)
    pytest_cfg_markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
    for marker in pytest_cfg_markers:
        if (root / marker).exists():
            content = (root / marker).read_text(encoding="utf-8", errors="replace")
            if marker == "pyproject.toml" and "[tool.pytest" not in content:
                continue
            if marker in ("setup.cfg", "tox.ini") and "[tool:pytest]" not in content:
                continue
            return [list(AUTO_TEST_COMMAND)], marker
    test_dirs = [d for d in ("tests", "test") if (root / d).is_dir()]
    if test_dirs:
        has_tests = any(
            p.name.startswith(("test_",)) and p.name.endswith(".py")
            for d in test_dirs
            for p in (root / d).glob("*.py")
        )
        if has_tests:
            return [list(AUTO_TEST_COMMAND)], f"{test_dirs[0]}/ 目录（含 test_*.py）"
    scattered = sorted(root.glob("test_*.py"))
    if scattered:
        return [list(AUTO_TEST_COMMAND)], scattered[0].name
    return [], ""


def classify_baseline(exit_code: int | None, output: str) -> dict:
    """基线结果分类（纯函数，单测覆盖）。

    - exit_code=None 或非 0/1/5 → environment_blocked（环境阻塞，Preflight 失败）
    - 0 → baseline_passed
    - 1 + 可解析失败身份 → baseline_assertion_failures（有效基线）
    - 1 + 无可解析身份 → assertion_failures_unparsed（保留失败，不得标 passed——
      unittest / 非 pytest 输出常见）
    - 5 → no_tests（明确标记，不得称为通过）
    """
    if exit_code is None:
        return {"kind": "environment_blocked", "exit_code": None}
    if exit_code not in (0, 1, 5):
        return {"kind": "environment_blocked", "exit_code": exit_code}
    failed_tests = parse_pytest_failures(output)
    if exit_code == 0:
        return {"kind": "baseline_passed", "exit_code": 0, "failed_tests": []}
    if exit_code == 5:
        return {"kind": "no_tests", "exit_code": 5, "failed_tests": []}
    if failed_tests:
        return {"kind": "baseline_assertion_failures", "exit_code": 1, "failed_tests": failed_tests}
    return {"kind": "assertion_failures_unparsed", "exit_code": 1, "failed_tests": []}


def parse_pytest_failures(output: str) -> list[str]:
    """从 pytest -q --tb=no -rf 输出解析失败测试身份（短摘要行 FAILED test::id - reason）。

    排除 unittest 风格总结行（如 "FAILED (failures=2)"）——那是汇总计数，不是测试身份。
    """
    failures: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            test_id = line[len("FAILED "):].split(" - ")[0].strip()
            if "(" in test_id or " " in test_id or not test_id:
                continue  # 汇总行 / 非法身份：不算已解析的失败测试
            failures.append(test_id)
    return failures


class PreflightRunner:
    def __init__(self, cli: DockerCli | None = None, image: str = DEFAULT_IMAGE):
        self._cli = cli or DockerCli()
        self._image = image

    def run(self, workspace: DockerWorkspace, test_command: str | None,
            discover: bool = True) -> PreflightReport:
        """顺序执行各步；任一环境步骤失败 → ok=False（Run FAILED, fail_layer=WORKSPACE）。

        test_command="auto"（CLI/Web 默认）→ 在工作区上确定性发现测试计划；
        手动命令 → 白名单校验后使用；来源（auto/manual）记录进报告供 Trace 追溯。
        """
        report = PreflightReport(ok=False)
        if test_command and test_command.strip().lower() == "auto":
            plan, evidence = discover_test_plan(workspace.task_dir)
            report.test_plan = plan
            report.test_plan_source = "auto"
            report.test_plan_evidence = evidence
        else:
            report.test_plan = parse_test_command(test_command)
            report.test_plan_source = "manual" if report.test_plan else "none"

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

        # 2b. 测试命令白名单校验（specs/03：auto 发现与手动命令同一策略，不能绕过命令策略）
        for argv in report.test_plan:
            if not check_command_allowed(argv):
                return self._fail(
                    report, "test_command_policy",
                    f"test command not allowed: {argv[:2]}; allowed: pytest, python -m pytest, "
                    "python -m unittest, python -m compileall, ruff",
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

        # 4. 基线测试（可行时）：断言失败 ≠ Preflight 失败；环境阻塞 = Preflight 失败
        if report.test_plan:
            report.baseline = self._run_baseline(workspace, report.test_plan)
            if report.baseline.get("kind") == "environment_blocked":
                return self._fail(
                    report, "baseline_environment",
                    f"baseline could not run (exit_code={report.baseline.get('exit_code')}): "
                    f"{str(report.baseline.get('detail', ''))[:300]}",
                )

        report.ok = True
        return report

    def _run_baseline(self, workspace: DockerWorkspace, test_plan: list[list[str]]) -> dict:
        """运行规定测试，记录基线分类（classify_baseline 纯函数判定）。

        多条命令按 `&&` 语义短路：前一条非零即停止后续执行——
        失败退出码不能被后续成功命令覆盖（评审复核修订）。
        """
        combined_out: list[str] = []
        exit_code: int | None = 0
        try:
            for argv in test_plan:
                outcome = workspace.run_command(argv, cwd=".", timeout_seconds=300, call_id="preflight-baseline")
                combined_out.append(outcome.content)
                exit_code = outcome.exit_code
                if exit_code != 0:
                    break  # && 短路：失败结果保持，不被后续命令覆盖
        except Exception as exc:  # noqa: BLE001 —— 基线运行故障按环境阻塞记录
            return {"kind": "environment_blocked", "exit_code": None, "detail": str(exc)[:500]}
        result = classify_baseline(exit_code, "\n".join(combined_out))
        if result["kind"] in ("environment_blocked", "assertion_failures_unparsed"):
            result["detail"] = "\n".join(combined_out)[:500]
        return result

    @staticmethod
    def _fail(report: PreflightReport, step: str, reason: str) -> PreflightReport:
        report.ok = False
        report.failure_step = step
        report.failure_reason = reason
        return report
