"""SWE-bench 官方评测协议的自实现判定器。

协议逐字对齐官方 eval_script（数据集字段，300/300 实例均为 eval_type=pass_and_fail）：
  1. 干净 base 状态（我们用 docker create + cp 从官方实例镜像重新提取，比官方的
     git reset 更接近"干净"定义——完全不受 Agent 工作区残留影响）；
  2. 应用 Agent Patch（git apply）；
  3. 把 test_patch 触及的测试文件 reset 回 base（丢弃 Agent 对测试文件的任何改动）；
  4. 应用 test_patch（新增官方测试）；
  5. 在官方镜像容器内执行 eval_script 中的安装命令与测试命令（pytest -rA ...）；
  6. 解析 pytest short test summary → 每测试状态表；
  7. 判定：FAIL_TO_PASS 全部 PASSED 且 PASS_TO_PASS 全部 PASSED → RESOLVED。

报告口径：判定方式如实标注"按官方评测协议自实现，在官方 per-instance 镜像中执行"。
"""
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from repofix.evaluation.swebench.workspace import (
    SWEBENCH_MOUNT,
    extract_testbed,
    run_install_commands,
    start_eval_container,
)
from repofix.workspace.docker_cli import DockerCli

# pytest -rA short summary 中出现的全部状态标记（-rA 报告所有结局）
_SUMMARY_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_SUMMARY_HEADER = "short test summary info"
START_MARKER = ">>>>> Start Test Output"
END_MARKER = ">>>>> End Test Output"


@dataclass
class JudgeRecord:
    """一次判定的完整记录（写入 stage 报告与 BENCHMARK-MINI.md）。"""

    instance_id: str
    outcome: str = "INCONCLUSIVE"  # RESOLVED / FAILED / INCONCLUSIVE（默认未知，不冒充结论）
    reason: str = ""
    patch_applied: bool = False
    patch_apply_rc: int | None = None
    install_rc: int | None = None
    install_attempts: list = field(default_factory=list)
    test_rc: int | None = None
    statuses_count: int = 0
    f2p_results: dict = field(default_factory=dict)  # test_id -> PASSED/FAILED/.../SILENT_PASS
    f2p_failures: list[str] = field(default_factory=list)
    p2p_regressions: list[str] = field(default_factory=list)
    test_output_tail: str = ""
    isolated: bool = True  # 本设计恒为 True：判定在官方镜像容器内执行


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码（sphinx 经 tox 包装强制着色，`[31mFAILED[0m` 会破坏
    startswith 匹配）。"""
    return _ANSI_RE.sub("", text)


def parse_short_summary(output: str) -> dict[str, str]:
    """解析 pytest -rA 的 short test summary 段 → {test_id: STATUS}。

    优先只在 summary 区段内解析（以 "short test summary info" 分隔线为界，
    到下一条全等号线为止）；找不到区段时退回全行匹配。行格式：
    `STATUS test_id` 或 `STATUS test_id - message`（test_id 含 ' - ' 的情形
    以首个 ' - ' 为界，与官方按行解析的精度一致）。ANSI 颜色码先剥离。
    """
    output = _strip_ansi(output)
    lines: list[str] = []
    in_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if _SUMMARY_HEADER in stripped:
            in_section = True
            continue
        if in_section:
            if set(stripped) <= {"=", "-", " "} and stripped:
                break
            if stripped:
                lines.append(stripped)
    if not lines:
        for line in output.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(f"{status} ") for status in _SUMMARY_STATUSES):
                lines.append(stripped)
    statuses: dict[str, str] = {}
    for line in lines:
        for status in _SUMMARY_STATUSES:
            if line.startswith(f"{status} "):
                rest = line[len(status) + 1:]
                # 官方 parse_log_pytest 语义对齐：test_id 取首个空白 token——
                # 数据集里的 F2P/P2P id 即由官方解析器如此截断（参数化 id 含
                # 空格时，如 `test_x[assert statement]` → `test_x[assert`）；
                # 只有两侧用同一截断规则，身份比对才与官方判定一致。
                test_id = rest.split()[0].strip() if rest.split() else ""
                if test_id:
                    statuses[test_id] = status
                break
    return statuses


def extract_eval_commands(eval_script: str) -> tuple[list[str], list[str]]:
    """从官方 eval_script 提取 (install 命令, 测试命令)。

    install = 所有 `python -m pip install ...` 行；测试命令 = Start/End Test Output
    标记之间的非空行。其余步骤（git reset/apply）由本判定器原生实现，不重放。
    """
    installs: list[str] = []
    tests: list[str] = []
    in_output = False
    for line in eval_script.splitlines():
        stripped = line.strip()
        if stripped.startswith("python -m pip install"):
            installs.append(stripped)
            continue
        if START_MARKER in stripped:
            in_output = True
            continue
        if END_MARKER in stripped:
            in_output = False
            continue
        if in_output and stripped:
            tests.append(stripped)
    return installs, tests


def run_judge(
    cli: DockerCli,
    *,
    image: str,
    base_commit: str,
    test_patch: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    eval_script: str,
    patch_path: Path | None,
    work_root: Path,
    instance_id: str = "",
    test_timeout: int = 900,
    keep_workspace: bool = False,
    pkg_version: str = "",
) -> JudgeRecord:
    """执行一次完整判定。patch_path=None 用于 stage-0 冒烟（base 状态自检）。"""
    instance_id = instance_id or f"judge-{uuid.uuid4().hex[:8]}"
    record = JudgeRecord(instance_id=instance_id, outcome="INCONCLUSIVE")
    # 绝对化：git apply 以 ws 为 cwd 执行，相对 work_root 会让 test_patch 文件找不到
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    ws = work_root / f"judge-{instance_id}"
    container = f"sweb-judge-{uuid.uuid4().hex[:10]}"
    try:
        from repofix.evaluation.swebench.workspace import force_rmtree

        force_rmtree(ws)
        ws.mkdir(parents=True)
        try:
            extract_testbed(cli, image, ws, base_commit=base_commit)
        except Exception as exc:  # noqa: BLE001 —— 评测设施异常 → INCONCLUSIVE
            record.reason = f"testbed extraction failed: {exc}"
            return record

        if patch_path is not None and Path(patch_path).exists() and Path(patch_path).read_text(encoding="utf-8").strip():
            apply = _git(["apply", str(Path(patch_path).resolve())], ws)
            record.patch_apply_rc = apply.returncode
            record.patch_applied = apply.returncode == 0
            if apply.returncode != 0:
                record.outcome = "FAILED"
                record.reason = f"patch does not apply on clean base: {(apply.stderr or apply.stdout).strip()[:200]}"
                return record
        else:
            record.patch_apply_rc = None
            record.patch_applied = False

        test_files = _test_patch_files(test_patch)
        # 官方语义对齐：eval_script 的 reset 命令没有 set -e——对 test_patch 新增
        # 文件（base 不存在）checkout 失败属预期且被官方忽略，这里同样容忍；
        # reset 的真实作用是丢弃 Agent 对**已存在**测试文件的改动。
        existing_test_files = [f for f in test_files if (ws / f).exists()]
        if existing_test_files:
            reset = _git(["checkout", base_commit, "--", *existing_test_files], ws)
            if reset.returncode != 0:
                record.reason = f"test file reset failed: {(reset.stderr or '').strip()[:200]}"
                return record
        if test_patch.strip():
            patch_file = ws.parent / f"test-patch-{instance_id}.diff"
            patch_file.write_text(test_patch, encoding="utf-8", newline="\n")
            try:
                apply_test = _git(["apply", str(patch_file)], ws)
                if apply_test.returncode != 0:
                    record.reason = f"test_patch does not apply: {(apply_test.stderr or '').strip()[:200]}"
                    return record
            finally:
                patch_file.unlink(missing_ok=True)

        installs, tests = extract_eval_commands(eval_script)
        if not tests:
            record.reason = "eval_script contains no test commands between markers"
            return record

        try:
            start_eval_container(cli, container, ws, image)
        except Exception as exc:  # noqa: BLE001
            record.reason = f"container start failed: {exc}"
            return record

        install_evidence = run_install_commands(
            cli, container, installs,
            env_prefix=f"SETUPTOOLS_SCM_PRETEND_VERSION={pkg_version} " if pkg_version else "")
        record.install_rc = 0 if install_evidence["ok"] else 1
        record.install_attempts = install_evidence["attempts"]
        # install 失败不再终止判定：派生镜像已烤入 editable install（egg-link 指向
        # /testbed），运行时 install 仅是锦上添花；import 真坏时测试会如实 FAILED
        record.reason = "" if install_evidence["ok"] else (
            f"warning: runtime install failed (baked image install should cover); {installs}")

        combined: list[str] = []
        test_rc = 0
        for command in tests:
            outcome = _exec_script(cli, container, command, timeout=test_timeout)
            if outcome is None:
                record.reason = f"test command failed to run: {command[:100]}"
                return record
            combined.append(outcome.stdout)
            combined.append(outcome.stderr)
            test_rc = test_rc or outcome.returncode
        record.test_rc = test_rc

        combined_text = chr(10).join(combined)
        # 收集中断检测（假阳性守卫，对照组实验后置发现）：官方 v1 的 silent-success
        # 语义在 patch 破坏测试收集时会把"从未运行的测试"记成通过——此时任何
        # 未显式 PASSED 的测试一律不得记为通过
        collection_interrupted = (
            "errors during collection" in combined_text
            or "Interrupted:" in combined_text
            or bool([l for l in combined_text.splitlines()
                     if l.startswith("ERROR ") and "::" not in l])
        )

        statuses = parse_short_summary(combined_text)
        record.statuses_count = len(statuses)
        # 官方对比语义（SWE-bench v1 get_eval_report，逐字复刻）：
        # - 输出中缺失的测试 = "silent success"（不带 -rA 的命令不打印通过测试）；
        # - 只有显式 FAILED / ERROR 才算失败。
        # 安全偏离（已文档化）：输出完全无状态且测试命令 rc != 0 → INCONCLUSIVE
        # （防止"测试根本没跑"被静默成功掩盖；官方无此守卫）。
        if not statuses and test_rc != 0:
            # 区分两种"无状态"：收集/导入错误（patch 产出坏代码 → FAILED，如实判定）
            # vs 执行层完全无输出（基础设施故障 → INCONCLUSIVE）
            if collection_interrupted or "ImportError while loading conftest" in combined_text \
                    or "Traceback (most recent call last)" in combined_text:
                record.outcome = "FAILED"
                record.f2p_failures = list(fail_to_pass)  # 收集失败时任何测试都未证明通过
                record.reason = (
                    "test collection/import failed with the applied patch "
                    "(broken patch); no per-test statuses (rc=%s)" % test_rc)
                return record
            record.outcome = "INCONCLUSIVE"
            record.reason = (
                "no test statuses parsed and test command failed; parser or env mismatch")
            return record
        # 收集中断时收紧 silent-success：缺席不再视为通过
        official_failed = lambda tid: (
            statuses.get(tid) in ("FAILED", "ERROR")
            or (collection_interrupted and statuses.get(tid) is None)
        )  # noqa: E731
        record.f2p_results = {tid: statuses.get(tid, "SILENT_PASS") for tid in fail_to_pass}
        record.f2p_failures = [tid for tid in fail_to_pass if official_failed(tid)]
        record.p2p_regressions = [tid for tid in pass_to_pass if official_failed(tid)]
        record.test_output_tail = "\n".join(combined)[-2000:]
        if record.f2p_failures or record.p2p_regressions:
            record.outcome = "FAILED"
            record.reason = (
                f"fail_to_pass failures: {len(record.f2p_failures)}/{len(fail_to_pass)}; "
                f"pass_to_fail regressions: {len(record.p2p_regressions)} "
                f"(official semantics: explicit FAILED/ERROR only; "
                f"absent tests are silent passes, {record.statuses_count} statuses parsed)")
            return record
        record.outcome = "RESOLVED"
        record.reason = (
            "all FAIL_TO_PASS and PASS_TO_PASS tests passed on independently applied patch "
            "(official semantics: explicit failure set + silent success)")
        return record
    finally:
        cli.remove_container(container)
        if not keep_workspace:
            from repofix.evaluation.swebench.workspace import force_rmtree

            force_rmtree(ws)


def _exec_script(cli: DockerCli, container: str, command: str, timeout: int):
    """在容器内以 bash 执行 eval_script 的一行命令（安装/测试命令均为 bash 行）。

    bash -c（非 login）：PATH 由派生镜像 ENV 提供（docker exec 总是注入镜像
    ENV）；bash -l 会加载 profile 脚本、可能重置 PATH，不可用。
    """
    result = cli.run(
        ["exec", "--workdir", SWEBENCH_MOUNT, container, "bash", "-c", command],
        timeout=timeout,
    )
    if result.returncode not in (0, 1) and not result.stdout and not result.stderr:
        return None  # 执行层故障（而非命令业务失败）
    return result


def _test_patch_files(test_patch: str) -> list[str]:
    from repofix.evaluation.swebench.dataset import test_patch_files

    return test_patch_files(test_patch)


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.fileMode=false", *args], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
