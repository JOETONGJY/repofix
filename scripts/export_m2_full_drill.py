"""M2 验收演练：Scripted ModelClient 驱动真实 Runtime + 真实工具链（Docker）全闭环。

序列：search → read → apply_patch + run_command(pytest) → finish
产出：changes.patch + trajectory.jsonl + result.json（plan.md M2 退出条件）

运行：.venv/Scripts/python scripts/export_m2_full_drill.py
前置：Docker Desktop 运行中；repofix-env:latest 镜像已构建
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import FakeClock, FakeVerifier, ScriptedModelClient, make_tool_call, model_response

from repofix.contracts import (
    PatchStatus,
    RunConfig,
    RunStatus,
    TaskSpec,
    TestStatus,
    VerificationOutcome,
    VerificationReport,
)
from repofix.runtime.runner import AgentRunner
from repofix.state import SQLiteEventStore
from repofix.tools.dispatcher import ToolDispatcher
from repofix.tools.registry import ToolRegistry, register_default_tools
from repofix.workspace.docker_cli import DockerCli
from repofix.workspace.docker_workspace import DockerWorkspace
from repofix.workspace.manager import WorkspaceManager
from repofix.workspace.preflight import PreflightRunner

RUN_ID = "run_m2_drill_001"


def make_sample_repo(base: Path) -> Path:
    repo = base / "sample-repo"
    repo.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "drill@repofix.local")
    git("config", "user.name", "drill")
    git("config", "core.autocrlf", "false")
    (repo / "auth.py").write_text(
        "def validate_password(pw: str) -> bool:\n    return len(pw) >= 0\n",
        encoding="utf-8",
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text(
        "from auth import validate_password\n\n"
        "def test_empty_password_rejected():\n    assert validate_password('') is False\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init: password validation with baseline bug")
    return repo


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="repofix-m2-"))
    repo = make_sample_repo(tmp)

    # 真实 Workspace：脏检查 → 独立 Clone → checkout → 受限容器 → Preflight
    manager = WorkspaceManager(tmp / "workspaces")
    handle = manager.prepare(RUN_ID, repo, "HEAD")
    cli = DockerCli()
    workspace = DockerWorkspace(handle.task_dir, RUN_ID, base_commit=handle.base_commit, cli=cli)
    preflight = PreflightRunner(cli=cli)
    report = preflight.run(workspace, "python -m pytest -q --tb=no -rf")
    print(f"preflight ok={report.ok} baseline={report.baseline.get('kind')}")
    if not report.ok:
        print(f"preflight failed: {report.failure_step}: {report.failure_reason}")
        return 2

    registry = ToolRegistry()
    register_default_tools(registry)

    # 副本基于干净快照：auth.py 内容与原仓库一致 → hash 可预先计算（模拟模型从 read_file 获得）
    import hashlib

    expected_hash = hashlib.sha256((repo / "auth.py").read_bytes()).hexdigest()

    # Scripted 序列：与 M1 同接口；执行全部落在真实工具上
    responses = [
        model_response("d1", [make_tool_call("c1", "search_code", pattern="validate_password")]),
        model_response("d2", [make_tool_call("c2", "read_file", path="auth.py")]),
        model_response("d3", [
            make_tool_call(
                "c3", "apply_patch",
                path="auth.py",
                patch=(
                    "--- a/auth.py\n"
                    "+++ b/auth.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    " def validate_password(pw: str) -> bool:\n"
                    "-    return len(pw) >= 0\n"
                    "+    return len(pw) >= 8\n"
                ),
                expected_sha256=expected_hash,
            ),
            make_tool_call("c4", "run_command", argv=["python", "-m", "pytest", "-q", "--tb=no"]),
        ]),
        model_response("d4", [make_tool_call("f1", "finish", summary="修复：密码最短 8 位；可见测试通过")]),
    ]

    # 修复后测试通过 → Verifier 真实判据在 M3 接入；此处按演练脚本给出 PASSED 报告
    verifier = FakeVerifier([
        VerificationReport(
            candidate_revision="m2-drill",
            patch_status=PatchStatus.VALID,
            test_status=TestStatus.PASSED,
            outcome=VerificationOutcome.PASSED,
        )
    ])

    store = SQLiteEventStore(tmp / f"{RUN_ID}.sqlite")
    runner = AgentRunner(
        run_id=RUN_ID,
        task=TaskSpec(
            instruction="Fix: empty password is accepted; require minimum length 8",
            repo_path=str(repo),
            base_commit=handle.base_commit,
            test_command="python -m pytest -q --tb=no",
        ),
        config=RunConfig(model_id="scripted-model", review_enabled=False),
        model_client=ScriptedModelClient(responses),
        registry=registry,
        dispatcher=ToolDispatcher(registry, workspace),
        store=store,
        verifier=verifier,
        clock=FakeClock(),
    )
    result = runner.run()

    # Patch 导出（get_changes 相对 base_commit，含 run_command 副作用；LF 强制）
    changes = workspace.get_changes()
    out_dir = Path("repofix-data") / "runs" / RUN_ID
    workspace.export_patch(out_dir / "changes.patch")
    (out_dir / "trajectory.jsonl").write_text(store.export_trajectory(RUN_ID), encoding="utf-8", newline="\n")
    (out_dir / "result.json").write_text(result.model_dump_json(indent=2), encoding="utf-8", newline="\n")

    workspace.close()
    manager.cleanup_workspace(handle)

    # 工具状态自检：演练闭环要求全部 SUCCEEDED（掩盖失败的假绿不可接受）
    rows = store.list_tool_calls(RUN_ID)
    for row in rows:
        print(f"  tool {row['tool_name']:<12} -> {row['side_effect_status']}")

    print(f"runtime_status:      {result.runtime_status.value}")
    print(f"termination_reason:  {result.termination_reason}")
    print(f"verification:        {result.verification_status.value}")
    print(f"modified_files:      {result.modified_files}")
    print(f"patch_hash:          {changes['patch_hash'][:16]}...")
    print(f"patch 可独立应用:    {'+++ b/auth.py' in changes['diff']}")
    print(f"output dir:          {out_dir}")
    all_ok = (
        result.runtime_status is RunStatus.COMPLETED
        and changes["diff"].strip()
        and all(r["side_effect_status"] == "succeeded" for r in rows)
    )
    print("DRILL_OK" if all_ok else "DRILL_FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
