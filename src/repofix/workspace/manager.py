"""WorkspaceManager：独立仓库副本的创建与生命周期（design/05、specs/03）。

- MVP 只处理 base_commit 快照，脏工作区直接拒绝（design/01：已跟踪修改、
  未跟踪未忽略文件都算脏；被 Git 忽略的文件不影响）。
- 独立 Clone：`git clone --no-local`——不共享用户仓库对象存储与元数据
  （hardlink 式 local clone 不构成隔离）。
- Base Commit 在副本内 checkout 为 detached HEAD，之后不跟随原仓库变化。
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repofix.workspace.errors import DirtyWorkspaceError, WorkspaceError, WorkspaceNotFoundError


class GitCliError(WorkspaceError):
    """git CLI 调用失败。"""


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise GitCliError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCliError(f"git {' '.join(args[:2])} timed out") from exc
    if proc.returncode != 0:
        raise GitCliError(f"git {' '.join(args[:2])} failed rc={proc.returncode}: {(proc.stderr or '')[:300]}")
    return proc.stdout or ""


def check_dirty(repo_path: Path) -> list[str]:
    """返回脏工作区条目（已跟踪修改 + 未跟踪未忽略文件）。空列表 = 干净。"""
    out = _git(["status", "--porcelain"], cwd=repo_path)
    return [line for line in out.splitlines() if line.strip()]


def resolve_base_commit(repo_path: Path, base_ref: str) -> str:
    """把 ref 解析为完整 Commit Hash；无效 ref 抛 GitCliError。"""
    return _git(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], cwd=repo_path).strip()


@dataclass
class WorkspaceHandle:
    """只由可信 Runtime / Manager 持有；模型不得指定容器 ID 或宿主机路径。"""

    run_id: str
    task_dir: Path
    base_commit: str
    source_repo: Path


class WorkspaceManager:
    def __init__(self, workspaces_root: Path):
        self._root = workspaces_root

    def prepare(self, run_id: str, repo_path: Path, base_commit: str) -> WorkspaceHandle:
        """校验 → 独立 Clone → checkout base_commit。任何一步失败不留半成品。"""
        repo_path = Path(repo_path)
        if not repo_path.exists():
            raise WorkspaceNotFoundError(f"repo path does not exist: {repo_path}")
        probe = _git(["rev-parse", "--is-inside-work-tree"], cwd=repo_path)
        if probe.strip() != "true":
            raise WorkspaceNotFoundError(f"not a git repository: {repo_path}")
        dirty = check_dirty(repo_path)
        if dirty:
            raise DirtyWorkspaceError(dirty)
        full_base = resolve_base_commit(repo_path, base_commit)

        task_dir = self._root / run_id
        if task_dir.exists():
            raise WorkspaceError(f"workspace already exists: {task_dir}")
        task_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            # --no-local：强制走传输协议的独立拷贝，不共享对象存储（specs/03 副本创建）
            _git(["clone", "--no-local", "--quiet", str(repo_path), str(task_dir)])
            _git(["checkout", "--quiet", "--detach", full_base], cwd=task_dir)
        except GitCliError:
            self._rmtree(task_dir)
            raise
        return WorkspaceHandle(
            run_id=run_id, task_dir=task_dir, base_commit=full_base, source_repo=repo_path
        )

    def cleanup_workspace(self, handle: WorkspaceHandle, keep_for_diagnosis: bool = False) -> None:
        """删除任务副本；keep_for_diagnosis 用于失败任务保留现场。"""
        if keep_for_diagnosis:
            return
        self._rmtree(handle.task_dir)

    @staticmethod
    def _rmtree(path: Path) -> None:
        import os
        import shutil
        import stat

        def _onerror(func, target, _exc):
            """Windows：git pack 文件带只读位，chmod 后重试。"""
            try:
                os.chmod(target, stat.S_IWRITE)
                func(target)
            except OSError:
                pass

        if path.exists():
            shutil.rmtree(path, onerror=_onerror)
