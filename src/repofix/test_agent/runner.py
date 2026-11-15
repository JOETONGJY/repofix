"""Test Agent 编排器（MVP v0.4 功能 B）：候选测试的隔离验证与补充验证。

关键边界（任务书 Part C）：
- 生成测试只注入**独立 Test Workspace**（WorkspaceManager 二次 Clone），绝不写入
  Main Workspace → 不污染 changes.patch、不被算作生产代码修复；
- 基线复现与修复后复跑都在 Docker 容器内执行（非宿主机、不访问宿主 API Key）；
- 复现结果只分四类（REPRODUCED/NOT_REPRODUCED/INVALID_TEST/ENVIRONMENT_ERROR），
  不把 pytest 非零退出码一概当作复现成功；
- 生成测试只作**补充验证信号**（authoritative_verification=False），不改变
  Final Verifier / Evaluation 的权威语义；
- 任何失败都安全降级：Test Agent 故障不阻塞原本可运行的 RepoFix 主流程。
"""
import subprocess
import tempfile
from pathlib import Path

from repofix.contracts import AgentActor, EventType, RunConfig, TaskSpec
from repofix.gateway.client import ModelClient
from repofix.runtime.loop_guard import Clock, SystemClock
from repofix.state.store import SQLiteEventStore
from repofix.test_agent.generator import MAX_SNIPPET_FILES, TestAgentGenerator, select_issue_files
from repofix.test_agent.schemas import (
    GENERATED_TEST_PATH,
    MAX_TEST_SOURCE_BYTES,
    ReproStatus,
    TestAgentReport,
)
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import DEFAULT_IMAGE, parse_pytest_failures

# 静态校验禁止的 import（复现测试不需要执行外部进程/网络；非安全沙箱，仅结构检查）
_FORBIDDEN_IMPORTS = frozenset({
    "subprocess", "socket", "requests", "urllib", "http", "ftplib", "telnetlib", "smtplib",
})


def should_trigger(config: RunConfig, preflight_report) -> tuple[bool, str]:
    """触发条件（任务书 §5，四条同时成立才运行）：

    1. config.test_agent_enabled 显式启用；
    2+3. 用户未手动提供有效测试命令 且 Preflight 自动发现的 TestPlan 为空
         ——手动命令经 parse_test_command 非空、auto 发现为空计划，两者统一
         表现为 report.test_plan 为空；
    4. Docker/依赖检查通过——Preflight 失败时主流程已终止，不会走到这里。
    """
    if not config.test_agent_enabled:
        return False, "disabled by config"
    if getattr(preflight_report, "test_plan", None):
        return False, "test plan already available"
    return True, "no user test command and no discovered test plan"


def validate_candidate_source(source: str) -> list[str]:
    """候选测试内容的确定性结构校验（任务书 §7）。返回违规列表；空 = 通过。

    说明：AST 解析只用于结构检查，不是代码安全沙箱——真正的隔离由独立
    测试工作区 + 容器执行保证。
    """
    import ast

    violations: list[str] = []
    if len(source.encode("utf-8")) > MAX_TEST_SOURCE_BYTES:
        violations.append(f"test source exceeds {MAX_TEST_SOURCE_BYTES} bytes")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        violations.append(f"syntax error: {exc.msg} (line {exc.lineno})")
        return violations

    has_test_function = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in ast.walk(tree)
    )
    if not has_test_function:
        violations.append("no pytest-style test function (def test_*) found")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_module = alias.name.split(".")[0]
                if root_module in _FORBIDDEN_IMPORTS:
                    violations.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root_module = (node.module or "").split(".")[0]
            if root_module in _FORBIDDEN_IMPORTS:
                violations.append(f"forbidden import: {node.module}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_absolute_path(node.value):
                violations.append(
                    f"absolute path outside workspace rejected: {node.value[:80]}"
                )
    return violations


def _looks_like_absolute_path(value: str) -> bool:
    value = value.strip()
    if not value or value.startswith(("http://", "https://")):
        return False
    if value.startswith("/") or value.startswith("~/"):
        return True
    # Windows 盘符形态 C:\... 或 C:/...
    head = value[:3]
    return len(value) >= 3 and head[1:2] == ":" and head[2:3] in ("\\", "/")


def classify_reproduction(exit_code: int | None, output: str,
                          test_rel_path: str = GENERATED_TEST_PATH) -> tuple[ReproStatus, str]:
    """基线运行结果 → (复现状态, 说明)。纯函数，单测覆盖（任务书 §9 判定标准）。

    - 0 → NOT_REPRODUCED：测试通过 = 没有暴露 Bug；
    - 1 → 失败身份落在生成测试内 → REPRODUCED；否则 NOT_REPRODUCED
      （绝不把非零退出码一概认定为复现）；
    - 2（收集/导入错误）与 5（未收集到测试）→ INVALID_TEST；
    - 3/4/其他退出码与命令未运行 → ENVIRONMENT_ERROR。
    """
    if exit_code is None:
        return ReproStatus.ENVIRONMENT_ERROR, "baseline command did not run"
    if exit_code == 0:
        return ReproStatus.NOT_REPRODUCED, "generated test passed on the buggy baseline"
    if exit_code == 1:
        failures = parse_pytest_failures(output)
        in_generated = [
            f for f in failures if f.split("::")[0] == test_rel_path
        ]
        if in_generated:
            return ReproStatus.REPRODUCED, f"{len(in_generated)} generated test(s) failed on baseline"
        if failures:
            return (ReproStatus.NOT_REPRODUCED,
                    "failures reported outside the generated test")
        return (ReproStatus.NOT_REPRODUCED,
                "exit code 1 without parseable failure identity (not counted as reproduction)")
    if exit_code in (2, 5):
        return ReproStatus.INVALID_TEST, (
            "no tests collected / collection or import error" if exit_code == 2
            else "pytest collected no tests"
        )
    return ReproStatus.ENVIRONMENT_ERROR, f"pytest exited with code {exit_code}"


class TestAgentRunner:
    """一次 Run 的 Test Agent 编排：独立工作区 → 生成 → 校验 → 基线复现（→ 补充验证）。"""

    __test__ = False  # pytest 采集豁免：这是运行时组件，不是测试类

    def __init__(
        self,
        *,
        run_id: str,
        task: TaskSpec,
        config: RunConfig,
        model_client: ModelClient,
        store: SQLiteEventStore,
        workspaces_root: Path,
        clock: Clock | None = None,
        image: str = DEFAULT_IMAGE,
        docker_cli: DockerCli | None = None,
    ):
        self._run_id = run_id
        self._task = task
        self._config = config
        self._store = store
        self._clock = clock or SystemClock()
        self._image = image
        self._manager = WorkspaceManager(Path(workspaces_root))
        self._cli = docker_cli or DockerCli()
        self._generator = TestAgentGenerator(
            run_id=run_id,
            model_client=model_client,
            model_id=config.model_id,
            max_model_calls=config.test_agent_max_model_calls,
            store=store,
            clock=self._clock,
        )
        self._handle = None
        self._workspace: DockerWorkspace | None = None
        self._candidate_source: str | None = None
        self._last_exit_code: int | None = None
        self._context_note: str | None = None
        self.report = TestAgentReport(enabled=True)

    @property
    def candidate_source(self) -> str | None:
        """通过校验并已注入的候选测试源码（未生成成功时为 None）。"""
        return self._candidate_source

    def main_context_note(self) -> str | None:
        """给 Main Agent 的只读复现摘要（P2，评审）。

        仅在基线 REPRODUCED 时非 None：以明确的非权威声明注入 Main 初始
        Context（模型不可修改 harness 注入的 head 内容）；测试未复现时
        不注入（对定位 Bug 无信号价值）。
        """
        return self._context_note

    # ---- 阶段 1：生成 + 基线复现（Main Agent 之前）----

    def run_baseline(self, preflight_report, repo_overview: str | None = None) -> TestAgentReport:
        triggered, reason = should_trigger(self._config, preflight_report)
        self.report.trigger_reason = reason
        if not triggered:
            self.report.status = ReproStatus.NOT_TRIGGERED
            return self.report
        self.report.triggered = True
        self.report.source = "llm_generated"
        self.report.test_file = GENERATED_TEST_PATH
        self._append_event(EventType.TEST_AGENT_STARTED, {
            "enabled": True,
            "triggered": True,
            "reason": reason,
            "model_id": self._config.model_id,
            "max_model_calls": self._config.test_agent_max_model_calls,
        })
        try:
            self._run_baseline_inner(repo_overview)
        except Exception as exc:  # noqa: BLE001 —— Test Agent 任何故障不阻塞主流程
            self.report.status = ReproStatus.ENVIRONMENT_ERROR
            self.report.status_detail = (
                f"test agent infrastructure failure: {type(exc).__name__}: {str(exc)[:300]}"
            )
        self._append_event(EventType.TEST_AGENT_FINISHED, {
            "phase": "baseline_reproduction",
            "status": self.report.status.value,
            "detail": self.report.status_detail[:300],
            "model_calls": self.report.model_calls,
            "token_usage": self.report.token_usage,
            "violations": self.report.violations,
            "test_file": self.report.test_file,
            "baseline_exit_code": self.report.baseline_exit_code,
            "main_context_note_chars": len(self._context_note) if self._context_note else 0,
        })
        return self.report

    def _build_context_note(self) -> str:
        """非权威复现摘要（P2）：只读事实 + 明确的边界声明，供 Main 定位 Bug。"""
        lines = [
            "[Supplementary Signal — Test Agent reproduction (non-authoritative)]",
            "A Test-Agent-generated reproduction test failed on the original buggy code:",
        ]
        if self.report.baseline_failures:
            for failure in self.report.baseline_failures[:5]:
                lines.append(f"  - {failure[:160]}")
        else:
            lines.append(f"  - {GENERATED_TEST_PATH} (baseline exit code "
                         f"{self.report.baseline_exit_code})")
        lines.extend([
            "This file lives in a separate verification workspace: it is NOT in your task "
            "workspace, you cannot see or modify it, and it is NOT part of the final "
            "verification (the Final Verifier's outcome does not depend on it). "
            "Treat this failure as a hint for locating the bug.",
        ])
        return "\n".join(lines)

    def _run_baseline_inner(self, repo_overview: str | None) -> None:
        # 独立测试工作区：同一 base_commit 的第二次 Clone（Main Workspace 不受影响）
        handle = self._manager.prepare(f"{self._run_id}-testagent",
                                       Path(self._task.repo_path), self._task.base_commit)
        self._handle = handle
        workspace = DockerWorkspace(
            handle.task_dir, f"{self._run_id}-testagent", base_commit=handle.base_commit,
            cli=self._cli, command_timeout_seconds=self._config.command_timeout_seconds,
        )
        workspace.initialize(self._image)
        self._workspace = workspace

        snippets = select_issue_files(handle.task_dir, self._task.instruction,
                                      max_files=MAX_SNIPPET_FILES)
        result = self._generator.generate(
            instruction=self._task.instruction,
            base_commit=self._task.base_commit,
            repo_overview=repo_overview,
            snippets=snippets,
        )
        self.report.model_calls = result.model_calls
        self.report.generation_duration_ms = result.duration_ms
        if result.usage:
            # Token 用量只来自 Gateway 返回值；字段缺失保持 None（未知），不伪造为零
            self.report.token_usage = {
                "prompt": result.usage.get("prompt_tokens"),
                "completion": result.usage.get("completion_tokens"),
                "total": result.usage.get("total_tokens"),
            }
        if result.error:
            self.report.status = ReproStatus.GENERATION_FAILED
            self.report.status_detail = f"model call failed: {result.error}"
            return
        if result.declined:
            self.report.status = ReproStatus.DECLINED
            self.report.status_detail = f"model declined: {result.decline_reason}"
            return
        candidate = result.candidate
        if candidate is None:
            self.report.status = ReproStatus.INVALID_TEST
            self.report.status_detail = result.invalid_schema_reason or "no candidate produced"
            return

        violations = validate_candidate_source(candidate.test_source)
        self.report.violations = violations
        if violations:
            self.report.status = ReproStatus.INVALID_TEST
            self.report.status_detail = "candidate rejected: " + "; ".join(violations[:3])
            return

        self._candidate_source = candidate.test_source
        self._inject_candidate(handle.task_dir)
        status, detail, output = self._run_generated_test(
            call_id="testagent-baseline", timeout_seconds=300)
        self.report.status = status
        self.report.status_detail = detail
        self.report.baseline_exit_code = self._last_exit_code
        if status is ReproStatus.REPRODUCED:
            self.report.baseline_failures = [
                f for f in parse_pytest_failures(output)
                if f.split("::")[0] == GENERATED_TEST_PATH
            ]
            self._context_note = self._build_context_note()

    # ---- 阶段 2：修复后补充验证（Fail-to-Pass；Main Agent 完成后由 CLI 调用）----

    def verify_post_patch(self, patch_text: str) -> None:
        """把最终候选 Patch 应用到隔离测试副本，再次运行生成测试。

        应用前核对测试副本状态（P1，评审）：基线 pytest 运行在可写挂载的
        工作区里，生成测试可能修改受跟踪的生产文件——此时重建干净副本再
        应用 Patch，保证补充验证始终针对预期基线。结果只写入本报告，
        不影响 Final Verifier 的权威结论。
        """
        report = self.report
        if report.status is not ReproStatus.REPRODUCED or not self._workspace:
            report.post_patch_status = "not_run"
            report.post_patch_detail = "baseline did not reproduce; post-patch check skipped"
            self._append_event(EventType.TEST_AGENT_FINISHED, self._post_patch_event_payload())
            return
        if not patch_text.strip():
            report.post_patch_status = "not_run"
            report.post_patch_detail = "empty candidate patch; post-patch check skipped"
            self._append_event(EventType.TEST_AGENT_FINISHED, self._post_patch_event_payload())
            return
        drift = self._workspace_drift()
        if drift:
            rebuild_error = self._rebuild_clean_test_workspace(drift)
            if rebuild_error:
                report.post_patch_status = "error"
                report.post_patch_detail = (
                    f"workspace drift detected ({'; '.join(d[:80] for d in drift[:3])}); "
                    f"clean rebuild failed: {rebuild_error[:200]}"
                )
                self._append_event(EventType.TEST_AGENT_FINISHED, self._post_patch_event_payload())
                return
            report.test_workspace_rebuilt = True
        apply_error = self._apply_patch_to_test_workspace(patch_text)
        if apply_error:
            report.post_patch_status = "error"
            report.post_patch_detail = f"patch apply failed: {apply_error[:300]}"
            self._append_event(EventType.TEST_AGENT_FINISHED, self._post_patch_event_payload())
            return
        status, detail, _output = self._run_generated_test(
            call_id="testagent-post-patch", timeout_seconds=300)
        report.post_patch_exit_code = self._last_exit_code
        if status is ReproStatus.NOT_REPRODUCED:
            # 基线失败 → 修复后通过：干净的 Fail-to-Pass 信号
            report.post_patch_status = "passed"
            report.post_patch_detail = "generated test passed after applying the candidate patch"
            if report.test_workspace_rebuilt:
                report.post_patch_detail += " (on a rebuilt clean workspace)"
            report.fail_to_pass = True
        elif status is ReproStatus.REPRODUCED:
            report.post_patch_status = "failed"
            report.post_patch_detail = "generated test still fails after the patch"
        elif status is ReproStatus.INVALID_TEST:
            report.post_patch_status = "error"
            report.post_patch_detail = f"generated test became invalid after patch: {detail}"
        else:
            report.post_patch_status = "error"
            report.post_patch_detail = detail
        self._append_event(EventType.TEST_AGENT_FINISHED, self._post_patch_event_payload())

    def _post_patch_event_payload(self) -> dict:
        return {
            "phase": "post_patch_verification",
            "post_patch_status": self.report.post_patch_status,
            "post_patch_exit_code": self.report.post_patch_exit_code,
            "fail_to_pass": self.report.fail_to_pass,
            "test_workspace_rebuilt": self.report.test_workspace_rebuilt,
            "detail": self.report.post_patch_detail[:300],
            "authoritative_verification": self.report.authoritative_verification,
        }

    # ---- 资源清理 ----

    def cleanup(self, keep_for_diagnosis: bool = False) -> None:
        """容器与测试工作区副本的清理（与主工作区同一清理纪律）。"""
        if self._workspace is not None:
            try:
                self._workspace.close()
            except Exception:  # noqa: BLE001 —— 清理失败不改变主流程结果
                pass
            self._workspace = None
        if self._handle is not None:
            self._manager.cleanup_workspace(self._handle, keep_for_diagnosis=keep_for_diagnosis)
            self._handle = None

    # ---- 内部 ----

    def _inject_candidate(self, task_dir: Path) -> None:
        target = task_dir / GENERATED_TEST_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._candidate_source or "", encoding="utf-8", newline="\n")

    def _run_generated_test(self, call_id: str, timeout_seconds: int) -> tuple[ReproStatus, str, str]:
        """在测试工作区容器内运行生成测试（命令经现有命令策略白名单校验）。

        返回 (复现状态, 说明, 合并输出)——输出供基线失败身份解析使用。
        """
        assert self._workspace is not None
        argv = ["python", "-m", "pytest", "-q", GENERATED_TEST_PATH]
        self._last_exit_code = None
        try:
            outcome = self._workspace.run_command(
                argv, cwd=".", timeout_seconds=timeout_seconds, call_id=call_id)
        except Exception:  # noqa: BLE001 —— ExecutorCrashError/DockerCliError 都按环境问题
            return (ReproStatus.ENVIRONMENT_ERROR,
                    "generated test execution failed (infrastructure)", "")
        self._last_exit_code = outcome.exit_code
        output = outcome.content or ""
        status, detail = classify_reproduction(outcome.exit_code, output, GENERATED_TEST_PATH)
        return status, detail, output

    # ---- P1：基线副作用防护（评审）----

    # pytest 运行自身产生的未跟踪缓存不算工作区漂移（与 _DIFF_EXCLUDES/
    # LIST_FILES_PRUNE_DIRS 同口径；.hypothesis 为 pytest 插件常见产物）
    _CACHE_SEGMENTS = frozenset({
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".nox", ".hypothesis",
    })
    _CACHE_SUFFIXES = (".pyc", ".pyo", ".coverage")

    def _is_cache_artifact(self, path: str) -> bool:
        parts = path.replace("\\", "/").split("/")
        if any(segment in self._CACHE_SEGMENTS for segment in parts):
            return True
        name = parts[-1]
        return name.endswith(self._CACHE_SUFFIXES) or name == ".coverage"

    def _workspace_drift(self) -> list[str]:
        """测试工作区相对 base_commit 的非预期变化（P1）。

        预期状态：受跟踪文件全部与 base_commit 一致；未跟踪只允许注入的
        生成测试文件与 pytest 缓存产物。返回漂移条目列表（空 = 干净）。
        """
        assert self._handle is not None
        try:
            # -uall：未跟踪目录展开为文件级条目（否则全新 tests/ 会折叠成 "?? tests/"，
            # 无法与注入的生成测试路径精确匹配）
            status = self._git_out(["status", "--porcelain", "-uall"], self._handle.task_dir)
            head = self._git_out(["rev-parse", "HEAD"], self._handle.task_dir).strip()
        except Exception as exc:  # noqa: BLE001 —— 状态不可确认 → 视为漂移（保守重建）
            return [f"workspace state unreadable: {type(exc).__name__}: {str(exc)[:120]}"]
        drift: list[str] = []
        if self._handle.base_commit and head != self._handle.base_commit:
            drift.append(f"HEAD drifted: {head[:12]} != {self._handle.base_commit[:12]}")
        for line in status.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            marker, path = line[:2], line[3:].strip()
            if marker.strip() == "??":
                path = path.split(" -> ")[-1]
                if path == GENERATED_TEST_PATH or self._is_cache_artifact(path):
                    continue
            drift.append(line)
        return drift

    def _rebuild_clean_test_workspace(self, drift: list[str]) -> str:
        """基线运行污染了工作区 → 重建干净测试副本并重新注入候选测试。

        返回错误描述；空串 = 重建成功。重建后容器与副本与首次创建同规格。
        """
        try:
            self.cleanup(keep_for_diagnosis=False)
            handle = self._manager.prepare(f"{self._run_id}-testagent",
                                           Path(self._task.repo_path), self._task.base_commit)
            self._handle = handle
            workspace = DockerWorkspace(
                handle.task_dir, f"{self._run_id}-testagent", base_commit=handle.base_commit,
                cli=self._cli, command_timeout_seconds=self._config.command_timeout_seconds,
            )
            workspace.initialize(self._image)
            self._workspace = workspace
            self._inject_candidate(handle.task_dir)
            remaining = self._workspace_drift()
            if remaining:
                return f"rebuilt workspace still dirty: {'; '.join(remaining[:3])}"
            return ""
        except Exception as exc:  # noqa: BLE001 —— 重建失败如实上报，不伪装通过
            return f"{type(exc).__name__}: {str(exc)[:200]}"

    def _git_out(self, args: list[str], cwd: Path) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args[:2])} failed rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            )
        return proc.stdout or ""

    def _apply_patch_to_test_workspace(self, patch_text: str) -> str:
        """git apply 候选 Patch 到测试工作区副本。返回错误描述；空串 = 成功。

        与 Evaluation Harness 的「干净基线 + 应用 Patch」模式一致：使用真实
        Patch 文件，不从 Main Workspace 复制源码覆盖。
        """
        assert self._handle is not None
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False,
                                         encoding="utf-8", newline="\n") as fh:
            fh.write(patch_text)
            patch_file = fh.name
        try:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", patch_file],
                cwd=str(self._handle.task_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"git apply failed: {type(exc).__name__}: {exc}"
        finally:
            Path(patch_file).unlink(missing_ok=True)
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or "").strip()[:300]
        return ""

    def _append_event(self, event_type: EventType, payload: dict) -> None:
        self._store.append_event(
            run_id=self._run_id,
            step=0,
            actor=AgentActor.TEST_AGENT,
            event_type=event_type,
            payload=payload,
            ts=self._clock.now(),
        )
