"""Workspace Manager 测试：WS-01 隔离性、WS-02 副本基于 base_commit、WS-03 脏工作区拒绝。

纯 git 操作，不依赖 Docker。
"""
import pytest

from repofix.workspace.errors import DirtyWorkspaceError, WorkspaceNotFoundError
from repofix.workspace.manager import WorkspaceManager, resolve_base_commit
from tests.conftest import run_git


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(tmp_path / "workspaces")


class TestWs01Isolation:
    def test_task_execution_does_not_touch_source_repo(self, manager, sample_repo):
        """任务执行前后用户原仓库完全不变（HEAD、工作区文件、git status）。"""
        head_before = run_git(["rev-parse", "HEAD"], sample_repo).strip()
        status_before = run_git(["status", "--porcelain"], sample_repo)

        handle = manager.prepare("run_iso", sample_repo, "HEAD")
        # 模拟 Agent 在副本内大改特改
        (handle.task_dir / "auth.py").write_text("totally changed\n", encoding="utf-8")
        (handle.task_dir / "new_file.txt").write_text("new\n", encoding="utf-8")
        run_git(["checkout", "-q", "main"], handle.task_dir)  # 副本内切分支也不影响原仓库

        assert run_git(["rev-parse", "HEAD"], sample_repo).strip() == head_before
        assert run_git(["status", "--porcelain"], sample_repo) == status_before
        assert (sample_repo / "auth.py").read_text(encoding="utf-8").startswith("def validate_password")

    def test_independent_object_store_no_local_hardlink(self, manager, sample_repo, tmp_path):
        """独立 Clone：--no-local，副本 .git 与原仓库对象不共享。"""
        handle = manager.prepare("run_nolocal", sample_repo, "HEAD")
        assert (handle.task_dir / ".git").exists()


class TestWs02BaseCommit:
    def test_workspace_head_is_base_commit(self, manager, sample_repo):
        """checkout 后副本 HEAD == base_commit（完整 Hash）。"""
        commits = run_git(["log", "--oneline"], sample_repo).strip().splitlines()
        # 制造第二个 commit，用非 HEAD 的 base
        (sample_repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
        run_git(["add", "-A"], sample_repo)
        run_git(["commit", "-q", "-m", "second"], sample_repo)
        base = commits[0].split()[0]  # 第一个 commit

        handle = manager.prepare("run_base", sample_repo, base)
        assert handle.base_commit == resolve_base_commit(sample_repo, base)
        head = run_git(["rev-parse", "HEAD"], handle.task_dir).strip()
        assert head.startswith(base)
        assert (handle.task_dir / "extra.py").exists() is False  # base 快照不含后续提交

    def test_invalid_base_ref_rejected(self, manager, sample_repo):
        with pytest.raises(Exception):
            manager.prepare("run_badbase", sample_repo, "no-such-ref")


class TestWs03DirtyWorkspace:
    def test_tracked_modification_rejected(self, manager, sample_repo):
        (sample_repo / "auth.py").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(DirtyWorkspaceError) as excinfo:
            manager.prepare("run_dirty1", sample_repo, "HEAD")
        assert excinfo.value.entries  # 脏条目在错误中列出
        # 原仓库保持不变（未被清理 / commit / stash）
        assert (sample_repo / "auth.py").read_text(encoding="utf-8") == "dirty\n"

    def test_untracked_unignored_file_rejected(self, manager, sample_repo):
        (sample_repo / "stray.txt").write_text("untracked\n", encoding="utf-8")
        with pytest.raises(DirtyWorkspaceError):
            manager.prepare("run_dirty2", sample_repo, "HEAD")

    def test_ignored_files_do_not_block(self, manager, sample_repo):
        """被 Git 忽略的构建文件不影响执行。"""
        pycache = sample_repo / "__pycache__"
        pycache.mkdir()
        (pycache / "auth.cpython-312.pyc").write_bytes(b"\x00\x01")
        handle = manager.prepare("run_ignored", sample_repo, "HEAD")
        assert handle.task_dir.exists()

    def test_missing_repo_rejected(self, manager, tmp_path):
        with pytest.raises(WorkspaceNotFoundError):
            manager.prepare("run_missing", tmp_path / "nope", "HEAD")

    def test_failed_prepare_leaves_no_partial_workspace(self, manager, sample_repo):
        (sample_repo / "auth.py").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(DirtyWorkspaceError):
            manager.prepare("run_partial", sample_repo, "HEAD")
        assert not (manager._root / "run_partial").exists()
