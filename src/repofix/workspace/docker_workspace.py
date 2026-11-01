"""DockerWorkspace：统一提供文件、搜索、Patch 和命令执行能力（design/05、specs/03）。

执行位置边界（specs/03 Workspace 契约）：MVP 由宿主进程对副本目录执行文件操作
（read / list / patch / get_changes）+ 命令与 ripgrep 经 CommandRunner 在容器内执行
（副本 bind-mount 进容器）。Runtime 业务代码不得绕过本类直接操作副本文件。

命令超时语义（specs/02/03）：超时必须终止实际进程树——容器整体停止；副作用状态
无法确认 → 抛 ExecutorCrashError（Scheduler 记 UNKNOWN 并终止 Run，不自动重试）。

版本标识（CONS-03）：patch_hash = sha256(get_changes 相对 base_commit 的 unified
diff)，本模块的 compute_patch_hash 是唯一生成函数，贯穿 Verifier / Reviewer / RunResult。
"""
import hashlib
import subprocess
from pathlib import Path

from repofix.contracts import (
    ExecutionStatus,
    ToolErrorCode,
    ToolResult,
)
from repofix.tools.dispatcher import ExecutorCrashError
from repofix.workspace.docker_cli import DockerCli, DockerCliError
from repofix.workspace.errors import WorkspaceError
from repofix.workspace.pathsafe import PathEscapeError, display_rel, safe_resolve

# 显式命令白名单（specs/03；模型不能自行扩展——扩展需显式策略变更）
ALLOWED_COMMANDS: list[tuple[str, ...]] = [
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "unittest"),
    ("python3", "-m", "unittest"),
    ("python", "-m", "compileall"),
    ("python3", "-m", "compileall"),
    ("ruff",),
]

# read_file 文件限制：二进制 / 超大文件明确拒绝（specs/03）
READ_MAX_BYTES = 2 * 1024 * 1024
BINARY_PROBE_BYTES = 8192

CONTAINER_WORKSPACE_MOUNT = "/workspace"


# 测试运行自身产生的缓存不进入候选版本（specs/03 WS-07：排除缓存/忽略文件）
_DIFF_EXCLUDES = [
    ":(exclude).pytest_cache",
    ":(exclude)__pycache__",
    ":(exclude)*.pyc",
    ":(exclude).mypy_cache",
    ":(exclude).ruff_cache",
    ":(exclude).coverage",
]


def check_command_allowed(argv: list[str]) -> bool:
    """argv 前缀匹配白名单（精确元组匹配，不做字符串包含判断）。"""
    for allowed in ALLOWED_COMMANDS:
        if len(argv) >= len(allowed) and tuple(argv[: len(allowed)]) == allowed:
            return True
    return False


def build_rg_argv(pattern: str, path: str, regex: bool, glob: str | None, limit: int) -> list[str]:
    """ripgrep 参数数组——底层 rg，不经 Shell 字符串拼接（specs/03）。"""
    argv = ["rg", "--no-heading", "--with-filename", "--line-number", "--color", "never", "--max-count", "1"]
    if not regex:
        argv.append("--fixed-strings")
    if glob:
        argv += ["--glob", glob]
    # "--" 结束选项解析：以 - 开头的搜索词不会被误当作 rg 选项
    argv += ["--max-filesize", "2M", "--", pattern, path]
    return argv


def parse_rg_output(stdout: str, limit: int) -> tuple[list[dict], bool]:
    """解析 rg 行输出为 [{path, line, match_text}]；返回 (matches, truncated)。"""
    matches: list[dict] = []
    truncated = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        if len(matches) >= limit:
            truncated = True
            break
        matches.append(
            {"path": parts[0], "line": int(parts[1]) if parts[1].isdigit() else None, "match_text": parts[2]}
        )
    return matches, truncated


class DockerWorkspace:
    """单任务工作区。实现 ToolExecutor 协议（dispatcher 直接注入使用）。"""

    def __init__(
        self,
        task_dir: Path,
        run_id: str,
        *,
        base_commit: str | None = None,
        cli: DockerCli | None = None,
        container_name: str | None = None,
        read_only: bool = False,
        command_timeout_seconds: int = 120,
    ):
        self.task_dir = Path(task_dir)
        self.run_id = run_id
        # 不可变 Diff 基线（CONS-03）：副本以 detached HEAD 停在该快照
        self.base_commit = base_commit
        self._cli = cli or DockerCli()
        self.container_name = container_name or f"repofix-{run_id}"
        self.read_only = read_only
        self._command_timeout = command_timeout_seconds
        self._container_up = False
        self._full_outputs: dict[str, tuple[str, str]] = {}

    # ---- 生命周期（design/05 Workspace 逻辑接口）----

    def initialize(self, image: str = "repofix-env:latest") -> None:
        """启动受限容器：非 root / 无网络 / 资源限制 / 仅挂载任务副本。"""
        if self._container_up:
            return
        self._cli.start_container(
            self.container_name,
            image,
            self.task_dir,
        )
        self._container_up = True

    def close(self) -> dict:
        """资源清理：移除容器（含超时后已停止的残留）；保留副本文件供诊断。"""
        self._cli.remove_container(self.container_name)
        self._container_up = False
        return {"container_removed": True}

    # ---- ToolExecutor 协议（M1 dispatcher 注入点）----

    def execute(self, tool_name: str, arguments: dict, call_id: str = "") -> ToolResult:
        if tool_name == "list_files":
            return self.list_files(**arguments)
        if tool_name == "search_code":
            return self.search_code(**arguments)
        if tool_name == "read_file":
            return self.read_file(**arguments)
        if tool_name == "apply_patch":
            return self.apply_patch(**arguments)
        if tool_name == "run_command":
            return self.run_command(**arguments, call_id=call_id)
        return ToolResult(
            call_id=call_id,
            status=ExecutionStatus.FAILED,
            error_code=ToolErrorCode.TOOL_NOT_FOUND,
            error_message=f"workspace does not implement tool: {tool_name}",
        )

    def pop_full_output(self, call_id: str) -> tuple[str, str] | None:
        """Scheduler 在持久化后取走全量输出（stdout, stderr）。"""
        return self._full_outputs.pop(call_id, None)

    # ---- 五个工作区工具 ----

    def list_files(self, path: str = ".", max_depth: int = 3, limit: int = 100) -> ToolResult:
        try:
            root = safe_resolve(self.task_dir, path)
        except PathEscapeError as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=str(exc))
        if not root.is_dir():
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR, error_message=f"not a directory: {path}")
        # 严格限定在目标目录内遍历（path="src" 不得返回其他目录的文件）
        task_root = self.task_dir.resolve()
        files: list[str] = []
        truncated = False
        for item in sorted(root.rglob("*")):
            rel = item.relative_to(root)
            if ".git" in rel.parts:
                continue
            if len(rel.parts) > max_depth:
                continue
            if item.is_file():
                if len(files) >= limit:
                    truncated = True
                    break
                # 输出保持相对任务副本根
                files.append(item.relative_to(task_root).as_posix())
        content = "\n".join(files) if files else "(no files)"
        content += f"\n[{len(files)} files" + (", truncated]" if truncated else "]")
        return ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED, content=content, truncated=truncated)

    def search_code(self, pattern: str, path: str = ".", regex: bool = False,
                    glob: str | None = None, limit: int = 100) -> ToolResult:
        try:
            search_root = safe_resolve(self.task_dir, path)
        except PathEscapeError as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=str(exc))
        rel = display_rel(self.task_dir, search_root)
        argv = build_rg_argv(pattern, rel, regex, glob, limit)
        try:
            outcome = self._run_in_container(argv, cwd_in_container=CONTAINER_WORKSPACE_MOUNT,
                                             timeout=self._command_timeout)
        except ExecutorCrashError:
            raise
        except (DockerCliError, WorkspaceError) as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR, error_message=str(exc))
        if outcome.returncode not in (0, 1):
            # rg 退出码 2 = 错误（如非法正则）；1 = 无匹配（正常结果）
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT,
                              error_message=f"search failed: {outcome.stderr.strip()[:300]}")
        matches, truncated = parse_rg_output(outcome.stdout, limit)
        if not matches:
            return ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED,
                              content="(no matches)", exit_code=0)
        lines = [f"{m['path']}:{m['line']}: {m['match_text']}" for m in matches]
        return ToolResult(call_id="pending", status=ExecutionStatus.SUCCEEDED,
                          content="\n".join(lines), truncated=truncated)

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> ToolResult:
        try:
            target = safe_resolve(self.task_dir, path)
        except PathEscapeError as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=str(exc))
        if not target.exists():
            # 文件不存在是业务反馈：让模型重新定位（specs/03 read_file）
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR, error_message=f"file not found: {path}")
        if not target.is_file():
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=f"not a file: {path}")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR, error_message=f"cannot read: {exc}")
        if b"\x00" in raw[:BINARY_PROBE_BYTES]:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message="binary file not supported")
        if len(raw) > READ_MAX_BYTES:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message="file too large to read")
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)
        selected = all_lines[start_line - 1: start_line - 1 + max_lines]
        numbered = "\n".join(f"{n}\t{line}" for n, line in enumerate(selected, start=start_line))
        truncated = (start_line - 1 + max_lines) < total
        suffix = f"\n[lines {start_line}-{start_line - 1 + len(selected)} of {total}]" if truncated else ""
        # sha256 供 apply_patch 版本校验（TL-13、specs/03 read_file 契约）
        sha_line = f"\n[sha256={hashlib.sha256(raw).hexdigest()}]"
        return ToolResult(
            call_id="pending",
            status=ExecutionStatus.SUCCEEDED,
            content=numbered + suffix + sha_line,
            truncated=truncated,
        )

    def apply_patch(self, path: str, patch: str, expected_sha256: str | None = None) -> ToolResult:
        if self.read_only:
            # Workspace 层真实只读防护（M4 Reviewer；不依赖 Tool Registry 单层拒绝）
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.PERMISSION_DENIED,
                              error_message="read-only workspace cannot modify files")
        try:
            target = safe_resolve(self.task_dir, path)
        except PathEscapeError as exc:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=str(exc))
        # 单文件约束（specs/03 D4-01）：整个 Diff 必须恰好修改这一个文件——
        # 解析完整 Patch，拒绝额外文件、重命名、删除与 Git 元数据变更
        patch_error = _validate_single_file_patch(patch, path)
        if patch_error:
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=patch_error)
        exists = target.exists()
        new_intent = "--- /dev/null" in patch
        if new_intent:
            # 新建语义：显式"文件不存在"前置条件；目标已存在 → CONFLICT，不能覆盖
            if exists:
                return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                                  error_code=ToolErrorCode.CONFLICT,
                                  error_message="target exists; a new-file patch cannot overwrite it")
            if expected_sha256:
                return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                                  error_code=ToolErrorCode.CONFLICT,
                                  error_message="file does not exist but expected_sha256 was provided")
        else:
            # 修改语义：必须提供匹配 Hash（specs/03 校验链第 3 步）
            if not exists:
                return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                                  error_code=ToolErrorCode.CONFLICT,
                                  error_message="file not found; cannot modify with expected_sha256")
            if not expected_sha256:
                return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                                  error_code=ToolErrorCode.INVALID_ARGUMENT,
                                  error_message="expected_sha256 is required when modifying an existing file")
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if current_hash != expected_sha256:
                return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                                  error_code=ToolErrorCode.CONFLICT,
                                  error_message="file changed since read; re-read the file")
        proc = _git_apply(self.task_dir, patch)
        if proc.returncode != 0:
            # git apply 原子：失败不修改文件（specs/03 不留下部分编辑）
            return ToolResult(call_id="pending", status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR,
                              error_message=f"patch does not apply: {(proc.stderr or proc.stdout).strip()[:300]}")
        new_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        return ToolResult(
            call_id="pending",
            status=ExecutionStatus.SUCCEEDED,
            content=f"patch applied to {path}; new sha256={new_hash}",
        )

    def run_command(self, argv: list[str], cwd: str = ".",
                    timeout_seconds: int | None = None, call_id: str = "") -> ToolResult:
        if self.read_only:
            return ToolResult(call_id=call_id, status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.PERMISSION_DENIED,
                              error_message="read-only workspace cannot run commands")
        if not check_command_allowed(argv):
            return ToolResult(call_id=call_id, status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.PERMISSION_DENIED,
                              error_message=(
                                  f"command not allowed: {argv[:2]}; allowed: pytest, python -m pytest, "
                                  "python -m unittest, python -m compileall, ruff"
                              ))
        try:
            cwd_abs = safe_resolve(self.task_dir, cwd)
        except PathEscapeError as exc:
            return ToolResult(call_id=call_id, status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.INVALID_ARGUMENT, error_message=str(exc))
        cwd_rel = display_rel(self.task_dir, cwd_abs)
        container_cwd = CONTAINER_WORKSPACE_MOUNT if cwd_rel == "." else f"{CONTAINER_WORKSPACE_MOUNT}/{cwd_rel}"
        timeout = min(timeout_seconds or self._command_timeout, 600)
        started_rc, stdout, stderr = None, "", ""
        try:
            outcome = self._run_in_container(argv, cwd_in_container=container_cwd, timeout=timeout)
            stdout, stderr = outcome.stdout, outcome.stderr
            started_rc = outcome.returncode
        except ExecutorCrashError:
            raise  # 超时等副作用未知场景：Scheduler 记 UNKNOWN 并终止 Run（WS-08）
        except (DockerCliError, WorkspaceError) as exc:
            return ToolResult(call_id=call_id, status=ExecutionStatus.FAILED,
                              error_code=ToolErrorCode.EXECUTION_ERROR, error_message=str(exc))
        self._full_outputs[call_id or "last"] = (stdout, stderr)
        merged = stdout if not stderr.strip() else stdout + "\n[stderr]\n" + stderr
        content, truncated = _fit_command_output(merged)
        return ToolResult(
            call_id=call_id,
            status=ExecutionStatus.SUCCEEDED,  # 命令执行完成；exit_code 才是业务结果（CONS-02）
            content=content,
            exit_code=started_rc,
            truncated=truncated,
        )

    # ---- Patch 导出与版本标识（CONS-03）----

    def get_changes(self) -> dict:
        """相对不可变 base_commit 的完整变化：含 run_command 产生的修改与未跟踪新文件；
        排除被 Git 忽略的内容。"""
        base = self._diff_base_commit()
        _git(["add", "--intent-to-add", "--all", "."], cwd=self.task_dir)
        try:
            diff = _git(["diff", base, "--", ".", *_DIFF_EXCLUDES], cwd=self.task_dir).stdout
            names = _git(["diff", "--name-only", base, "--", ".", *_DIFF_EXCLUDES],
                         cwd=self.task_dir).stdout
        finally:
            _git(["reset", "--quiet"], cwd=self.task_dir, check=False)
        return {
            "diff": diff,
            "changed_files": [line for line in names.splitlines() if line.strip()],
            "patch_hash": compute_patch_hash(diff),
        }

    def get_changes_patch(self) -> str:
        return self.get_changes()["diff"]

    def export_patch(self, out_path: Path) -> str:
        """导出 changes.patch（强制 LF：Windows 上 write_text 默认会转 CRLF，
        CRLF patch 无法独立 git apply——CLI-02 前置条件）。"""
        diff = self.get_changes()["diff"]
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(diff)
        return str(out_path)

    def _diff_base_commit(self) -> str:
        """Diff 基线恒为构造时固定的 base_commit；HEAD 漂移视为严重异常（副本被意外改写）。"""
        if self.base_commit:
            head = _git(["rev-parse", "HEAD"], cwd=self.task_dir).stdout.strip()
            if head != self.base_commit:
                raise WorkspaceError(
                    f"workspace HEAD drifted from base_commit: HEAD={head[:12]} base={self.base_commit[:12]}"
                )
            return self.base_commit
        return _git(["rev-parse", "HEAD"], cwd=self.task_dir).stdout.strip()

    # ---- 内部 ----

    def _run_in_container(self, argv: list[str], cwd_in_container: str, timeout: int):
        """容器内执行 argv。超时 → 终止容器（进程树）并确认停止 → 副作用仍为未知。"""
        try:
            return self._cli.exec(self.container_name, argv, timeout=timeout, cwd_in_container=cwd_in_container)
        except DockerCliError as exc:
            if "timed out" in str(exc) and self._container_up:
                # specs/03：超时必须终止实际进程树——docker stop + 状态确认 + 必要时强制移除。
                # 无论停止是否确认成功，副作用都不能据此推断为"未发生"。
                cleanup_note = "process tree terminated (container stopped, verified)"
                try:
                    self._cli.ensure_container_stopped(self.container_name)
                except DockerCliError as cleanup_exc:
                    cleanup_note = (
                        "CONTAINER CLEANUP UNVERIFIED: "
                        f"container={self.container_name} may still be running ({cleanup_exc})"
                    )
                self._container_up = False
                raise ExecutorCrashError("run_command", TimeoutError(
                    f"command timed out after {timeout}s; {cleanup_note}; "
                    "side effects remain UNKNOWN"
                )) from exc
            raise


# ---- 模块级纯函数（单测覆盖）----


def compute_patch_hash(diff_text: str) -> str:
    """candidate_revision = patch_hash = sha256(get_changes 相对 base_commit 的 unified diff)。

    唯一生成函数（design/08 一致性评审；Verifier / Reviewer / RunResult 共用）。
    """
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def _strip_diff_prefix(p: str) -> str:
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def _patch_targets(patch: str) -> list[str]:
    """提取 unified diff 的目标路径（去除 a/ b/ 前缀；忽略 /dev/null）。"""
    targets: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            p = line[4:].strip().split("\t")[0]
            if p == "/dev/null":
                continue
            targets.append(_strip_diff_prefix(p))
    return targets


def _validate_single_file_patch(patch: str, path: str) -> str | None:
    """校验整个 Unified Diff 恰好只修改 path 这一个文件（specs/03 D4-01）。

    兼容两种格式：完整 git diff（含 diff --git 头）与裸 unified diff（仅 ---/+++）。
    拒绝：多文件变更、重命名、删除、Git 元数据（mode/similarity）变更、二进制 Patch。
    返回 None 表示通过；否则返回错误描述。
    """
    lines = patch.splitlines()
    diff_git_lines = [line for line in lines if line.startswith("diff --git ")]
    if len(diff_git_lines) > 1:
        return f"patch contains {len(diff_git_lines)} file changes; apply_patch accepts exactly one file"
    if diff_git_lines:
        header = diff_git_lines[0]
        parts = header.split(" ")
        if len(parts) < 4:
            return f"malformed diff header: {header[:80]}"
        src, dst = _strip_diff_prefix(parts[2]), _strip_diff_prefix(parts[3])
        if src != dst:
            return "patch renames or moves a file, which is not supported"
        if src != path:
            return f"patch targets '{src}', arguments say '{path}'"
        body_checks = (
            ("similarity index", "file rename detected, which is not supported"),
            ("rename from", "file rename detected, which is not supported"),
            ("rename to", "file rename detected, which is not supported"),
            ("deleted file mode", "file deletion detected, which is not supported"),
            ("old mode", "git metadata (mode) changes are not supported"),
            ("GIT binary patch", "binary patches are not supported"),
        )
        for marker, reason in body_checks:
            if any(marker in line for line in lines):
                return reason
    # ---/+++ 目标解析（/dev/null 跳过）：全部目标必须恰为指定文件
    header_targets = _patch_targets(patch)
    if not header_targets:
        return "patch does not contain any file target"
    if set(header_targets) != {path}:
        return f"patch modifies {sorted(set(header_targets))}; exactly '{path}' is required"
    if "+++ /dev/null" in patch:
        return "file deletion detected, which is not supported"
    return None


def _fit_command_output(text: str, limit: int = 16 * 1024) -> tuple[str, bool]:
    """命令输出模型可见上限：合并视图，stderr 尾部优先保留（specs/01 ToolResult QA）。"""
    if len(text) <= limit:
        return text, False
    head = text[: limit * 2 // 3]
    tail = text[-(limit // 3):]
    return head + "\n...[truncated]...\n" + tail, True


def _git(args: list[str], cwd: Path, check: bool = True) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    if check and proc.returncode != 0:
        # Git 失败不能把空 stdout 当作有效结果继续（评审 P1：get_changes 退出码检查）
        raise WorkspaceError(
            f"git {' '.join(args[:2])} failed rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc


def _git_apply(task_dir: Path, patch: str) -> "subprocess.CompletedProcess[str]":
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
        fh.write(patch)
        patch_file = fh.name
    try:
        return subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_file],
            cwd=str(task_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        Path(patch_file).unlink(missing_ok=True)
