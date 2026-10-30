"""工作区工具测试（文件类，无需 Docker）：TL-03/04/05/06/07/10/11/13 + get_changes + 白名单。

搜索语义（TL-11/12 的 rg 解析部分）以纯函数测试覆盖；真实 rg 在 Docker 集成测试验证。
"""
import hashlib

import pytest

from repofix.contracts import ExecutionStatus, ToolErrorCode
from repofix.workspace.docker_cli import CliResult
from repofix.workspace.docker_workspace import (
    DockerWorkspace,
    check_command_allowed,
    compute_patch_hash,
    parse_rg_output,
)
from repofix.workspace.pathsafe import safe_resolve


def read_hash(ws: DockerWorkspace, rel: str) -> str:
    return hashlib.sha256((ws.task_dir / rel).read_bytes()).hexdigest()


class TestReadFile:
    def test_tl03_missing_file_is_business_feedback(self, task_workspace):
        result = task_workspace.read_file("nope.py")
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.EXECUTION_ERROR  # 业务反馈：重新定位

    def test_tl13_sha256_matches_content(self, task_workspace):
        result = task_workspace.read_file("auth.py")
        assert result.status is ExecutionStatus.SUCCEEDED
        expected = read_hash(task_workspace, "auth.py")
        assert f"[sha256={expected}]" in result.content
        assert "1\tdef validate_password" in result.content  # 带行号

    def test_read_range_and_truncation(self, task_workspace):
        full = task_workspace.read_file("auth.py")
        part = task_workspace.read_file("auth.py", start_line=1, max_lines=1)
        assert part.truncated is True
        assert "of 2]" in part.content
        assert full.truncated is False

    def test_binary_file_rejected(self, task_workspace):
        (task_workspace.task_dir / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        result = task_workspace.read_file("blob.bin")
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT
        assert "binary" in result.error_message

    def test_path_escape_rejected(self, task_workspace):
        for bad in ("../outside.py", "C:/Windows/win.ini", "/etc/passwd"):
            result = task_workspace.read_file(bad)
            assert result.status is ExecutionStatus.FAILED
            assert result.error_code is ToolErrorCode.INVALID_ARGUMENT


class TestListFiles:
    def test_lists_relative_paths_and_respects_limit(self, task_workspace):
        result = task_workspace.list_files(path=".", limit=2)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.truncated is True
        assert "auth.py" in result.content

    def test_git_dir_excluded(self, task_workspace):
        result = task_workspace.list_files(path=".", limit=500)
        assert ".git/config" not in result.content


class TestApplyPatch:
    def _patch_for(self, old: str, new: str) -> str:
        """构造 auth.py 的单文件 unified diff（首行作为上下文）。"""
        return (
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate_password(pw: str) -> bool:\n"
            f"-{old}\n"
            f"+{new}\n"
        )

    OLD_LINE = "    return len(pw) >= 0"

    def test_tl07_success_atomic_with_new_hash(self, task_workspace):
        ws = task_workspace
        current = read_hash(ws, "auth.py")
        patch = self._patch_for(
            "    return len(pw) >= 0", "    return bool(pw) and len(pw) >= 8"
        )
        result = ws.apply_patch("auth.py", patch, expected_sha256=current)
        assert result.status is ExecutionStatus.SUCCEEDED
        new_hash = read_hash(ws, "auth.py")
        assert new_hash != current
        assert f"new sha256={new_hash}" in result.content
        assert "bool(pw)" in (ws.task_dir / "auth.py").read_text(encoding="utf-8")

    def test_tl05_hash_mismatch_conflict_no_modification(self, task_workspace):
        patch = self._patch_for("    return len(pw) >= 0", "    return True")
        result = task_workspace.apply_patch("auth.py", patch, expected_sha256="0" * 64)
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.CONFLICT
        assert "return True" not in (task_workspace.task_dir / "auth.py").read_text(encoding="utf-8")

    def test_tl06_context_mismatch_execution_error(self, task_workspace):
        current = read_hash(task_workspace, "auth.py")
        patch = self._patch_for("    return WRONG_CONTEXT", "    return True")
        result = task_workspace.apply_patch("auth.py", patch, expected_sha256=current)
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.EXECUTION_ERROR
        # git apply 原子：文件未被修改
        assert read_hash(task_workspace, "auth.py") == current

    def test_tl10_new_file_patch_cannot_overwrite_existing(self, task_workspace):
        new_file_patch = (
            "--- /dev/null\n"
            "+++ b/auth.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+x = 1\n"
        )
        result = task_workspace.apply_patch("auth.py", new_file_patch, expected_sha256=None)
        assert result.error_code is ToolErrorCode.CONFLICT

    def test_new_file_created_with_null_source(self, task_workspace):
        ws = task_workspace
        patch = (
            "--- /dev/null\n"
            "+++ b/validators.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def check(pw):\n"
            "+    return len(pw) >= 8\n"
        )
        result = ws.apply_patch("validators.py", patch, expected_sha256=None)
        assert result.status is ExecutionStatus.SUCCEEDED
        assert (ws.task_dir / "validators.py").exists()
        new_hash = hashlib.sha256((ws.task_dir / "validators.py").read_bytes()).hexdigest()
        assert new_hash in result.content

    def test_patch_target_path_mismatch_rejected(self, task_workspace):
        patch = self._patch_for("    return len(pw) >= 0", "    return True")
        result = task_workspace.apply_patch("other.py", patch, expected_sha256=read_hash(task_workspace, "auth.py"))
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT
        assert "other.py" in result.error_message

    def test_modifying_existing_requires_hash(self, task_workspace):
        patch = self._patch_for("    return len(pw) >= 0", "    return True")
        result = task_workspace.apply_patch("auth.py", patch, expected_sha256=None)
        assert result.error_code is ToolErrorCode.INVALID_ARGUMENT


class TestGetChanges:
    def test_ws07_includes_new_files_and_command_changes(self, task_workspace):
        ws = task_workspace
        # 模拟 run_command 产生的未跟踪文件 + apply_patch 的修改
        (ws.task_dir / "generated.txt").write_text("by command\n", encoding="utf-8")
        patch = (
            "--- a/auth.py\n"
            "+++ b/auth.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def validate_password(pw: str) -> bool:\n"
            "-    return len(pw) >= 0\n"
            "+    return len(pw) >= 8\n"
        )
        ws.apply_patch("auth.py", patch, expected_sha256=read_hash(ws, "auth.py"))

        changes = ws.get_changes()
        assert "generated.txt" in changes["changed_files"]
        assert "auth.py" in changes["changed_files"]
        assert "+++ b/generated.txt" in changes["diff"]
        assert "return len(pw) >= 8" in changes["diff"]
        # CONS-03：patch_hash 是同一 diff 的 sha256，可复现
        assert changes["patch_hash"] == compute_patch_hash(changes["diff"])

    def test_ignored_files_excluded_from_patch(self, task_workspace):
        pycache = task_workspace.task_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "x.pyc").write_bytes(b"\x00")
        changes = task_workspace.get_changes()
        assert changes["diff"].strip() == ""  # 只有被忽略文件 → 无变化
        assert changes["changed_files"] == []


class TestCommandWhitelist:
    def test_tl09_whitelist_exact_prefix_match(self):
        assert check_command_allowed(["python", "-m", "pytest", "-q"]) is True
        assert check_command_allowed(["pytest", "tests/"]) is True
        assert check_command_allowed(["python", "-m", "unittest"]) is True
        assert check_command_allowed(["python", "-m", "compileall", "."]) is True
        assert check_command_allowed(["ruff", "check", "."]) is True
        assert check_command_allowed(["pip", "install", "requests"]) is False
        assert check_command_allowed(["bash", "-c", "rm -rf /"]) is False
        assert check_command_allowed(["python", "-c", "import os"]) is False  # 不在白名单前缀
        assert check_command_allowed(["python", "-m", "pip", "install", "x"]) is False
        # 前缀精确匹配：pytestx 不是 pytest
        assert check_command_allowed(["pytestx"]) is False

    def test_run_command_permission_denied_outside_whitelist(self, task_workspace):
        result = task_workspace.run_command(["pip", "install", "requests"], call_id="c1")
        assert result.status is ExecutionStatus.FAILED
        assert result.error_code is ToolErrorCode.PERMISSION_DENIED
        assert task_workspace.fake_cli.exec_calls == []  # 未触达容器

    def test_run_command_allowed_executes_via_container(self, task_workspace):
        ws = task_workspace
        ws.fake_cli_exec_setup = True
        # 注入一个成功的 pytest 输出
        from repofix.workspace.docker_cli import CliResult as CR

        ws._cli._exec_results = [
            CR(1, "1 failed, 1 passed in 0.01s\nFAILED tests/test_auth.py::test_empty_password_rejected", "")
        ]
        result = ws.run_command(["python", "-m", "pytest", "-q"], call_id="c9")
        assert result.status is ExecutionStatus.SUCCEEDED  # 命令执行完成 ≠ 业务成功（CONS-02）
        assert result.exit_code == 1  # 断言失败是有效业务反馈
        assert result.truncated is False
        # 全量输出可被 Scheduler 取走持久化（specs/01 stdout/stderr 分存）
        assert ws.pop_full_output("c9") is not None
        # exec 走的是容器路径
        name, argv, cwd_in_container = ws.fake_cli.exec_calls[0]
        assert cwd_in_container == "/workspace"
        assert argv == ["python", "-m", "pytest", "-q"]


class TestSearchParsing:
    def test_tl11_no_match_is_success_empty(self, task_workspace):
        ws = task_workspace
        ws._cli._exec_results = [CliResult(1, "", "")]  # rg exit 1 = 无匹配
        result = ws.search_code("def does_not_exist")
        assert result.status is ExecutionStatus.SUCCEEDED
        assert result.content == "(no matches)"

    def test_rg_output_parse_limit_truncation(self):
        lines = [f"src/m{i}.py:{i}: match {i}" for i in range(5)]
        matches, truncated = parse_rg_output("\n".join(lines), limit=3)
        assert len(matches) == 3
        assert truncated is True
        assert matches[0]["path"] == "src/m0.py"
        assert matches[0]["line"] == 0

    def test_build_rg_argv_defaults_literal_search(self):
        from repofix.workspace.docker_workspace import build_rg_argv

        argv = build_rg_argv("a.b", ".", regex=False, glob=None, limit=100)
        assert "--fixed-strings" in argv  # 默认普通字符串搜索
        assert "a.b" in argv
        assert argv[0] == "rg" and "|" not in " ".join(argv)  # 不经 Shell 拼接


class TestPathSafety:
    def test_safe_resolve_accepts_relative(self, task_workspace):
        resolved = safe_resolve(task_workspace.task_dir, "tests/test_auth.py")
        assert resolved.exists()

    def test_symlink_escape_rejected(self, task_workspace):
        link = task_workspace.task_dir / "evil"
        try:
            link.symlink_to(task_workspace.task_dir.parent, target_is_directory=True)
        except OSError:
            pytest.skip("symlink requires privilege on this host")
        with pytest.raises(Exception):
            safe_resolve(task_workspace.task_dir, "evil/secret.txt")
