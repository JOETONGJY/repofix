"""Web Server 测试：端点契约与事件流（假栈注入，零 Token）。"""
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import repofix.server as server_module
from repofix.server import app
from tests.conftest import make_tool_call, model_response


def _fake_stack_factory(captured: dict | None = None):
    """构造假栈：Scripted 模型 + Fake 容器；Runtime / git 副本 / 事件存储真实。"""
    from repofix.contracts import RunConfig, TaskSpec

    def fake_stack(task_spec: TaskSpec, config: RunConfig, run_id: str):
        from repofix.runtime.runner import AgentRunner
        from repofix.state import SQLiteEventStore
        from repofix.tools.dispatcher import ToolDispatcher
        from repofix.tools.registry import ToolRegistry, register_default_tools
        from repofix.workspace.docker_cli import DockerCli
        from repofix.workspace.docker_workspace import DockerWorkspace
        from repofix.workspace.manager import WorkspaceManager
        from tests.conftest import (
            FakeClock,
            FakeToolExecutor,
            ScriptedModelClient,
        )

        if captured is not None:
            captured["review_enabled"] = config.review_enabled
            captured["output_dir"] = config.output_dir

        manager = WorkspaceManager(Path(config.output_dir) / "workspaces")
        handle = manager.prepare(run_id, Path(task_spec.repo_path), task_spec.base_commit)
        docker = DockerCli()
        workspace = DockerWorkspace(
            handle.task_dir, run_id, base_commit=handle.base_commit,
            cli=docker, command_timeout_seconds=config.command_timeout_seconds)
        preflight_report = type("R", (), {"ok": True, "test_plan": [], "baseline": {}})()
        registry = ToolRegistry()
        register_default_tools(registry)
        # 预置真实代码修改：让 finish 后 changes.patch 非空（模拟 Agent 已完成的修复）
        auth = handle.task_dir / "auth.py"
        auth.write_text(auth.read_text(encoding="utf-8").replace(
            "len(pw) >= 0", "len(pw) >= 8"), encoding="utf-8")
        # Stub Verifier：Web 端点测试的目标是 HTTP/SSE/产物链路，
        # Verifier 自身的 P0-3 严格校验已由 test_verifier.py 真实覆盖
        from repofix.contracts import (
            PatchStatus as _PS,
        )
        from repofix.contracts import (
            TestStatus as _TS,
        )
        from repofix.contracts import (
            VerificationOutcome as _VO,
        )
        from repofix.contracts import (
            VerificationReport as _VR,
        )

        class StubVerifier:
            def verify(self, request):
                return _VR(candidate_revision=request.candidate_revision,
                           patch_status=_PS.VALID, test_status=_TS.PASSED,
                           outcome=_VO.PASSED)

        verifier = StubVerifier()
        store = SQLiteEventStore(Path(config.output_dir) / "runs" / run_id / "run.sqlite")
        finish = model_response("m1", [make_tool_call("f1", "finish", summary="done")])

        class ScriptedReviewRunner:
            calls = 0

            def review(self, request):
                self.__class__.calls += 1
                from repofix.contracts import ReviewReport

                return ReviewReport(status="APPROVED", patch_hash=request.patch_hash)

        runner = AgentRunner(
            run_id=run_id, task=task_spec, config=config,
            model_client=ScriptedModelClient([finish]), registry=registry,
            dispatcher=ToolDispatcher(registry, workspace),
            store=store, verifier=verifier, clock=FakeClock(),
            patch_hash_provider=lambda: workspace.get_changes()["patch_hash"],  # 动态：与 Verifier 重算一致
            review_runner_factory=lambda rid: ScriptedReviewRunner(),
            reviewer_executor=FakeToolExecutor(),
        )
        return runner, store, workspace, manager, handle, preflight_report

    return fake_stack


def wait_completed(client, run_id, tries=60):
    state = {"status": "running"}
    for _ in range(tries):
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] in ("completed", "error"):
            return state
        time.sleep(0.1)
    return state


class TestEndpoints:
    def test_start_validation_error_400(self):
        client = TestClient(app)
        # 空字段被 Pydantic 拒绝（422）；语义等价于 CLI 的校验失败（400/422 均为输入错误）
        resp = client.post("/api/runs", json={"repo": "", "task": ""})
        assert resp.status_code in (400, 422)

    def test_unknown_run_404(self):
        client = TestClient(app)
        assert client.get("/api/runs/nope").status_code == 404
        assert client.get("/api/runs/nope/events").status_code == 404
        assert client.get("/api/runs/nope/patch").status_code == 404

    def test_index_missing_404(self):
        client = TestClient(app)
        # web/index.html 存在于仓库根的相对路径——TestClient cwd 不同则 404 可接受
        assert client.get("/").status_code in (200, 404)


class TestRunWithFakeStack:
    def _install(self, monkeypatch, captured=None):
        import repofix.cli as cli_module

        fake_stack = _fake_stack_factory(captured)
        monkeypatch.setattr(cli_module, "_build_stack", fake_stack)
        monkeypatch.setattr(server_module, "_build_stack", fake_stack)
        return TestClient(app)

    def test_full_flow_events_result_patch(self, sample_repo, tmp_path, monkeypatch):
        """启动 → SSE 事件流（单调 seq + done）→ 结果与 patch 端点。"""
        client = self._install(monkeypatch)
        body = {
            "repo": str(sample_repo), "task": "Fix the bug",
            "test_command": "python -m pytest -q", "output_dir": str(tmp_path / "data"),
        }
        start = client.post("/api/runs", json=body)
        assert start.status_code == 200
        run_id = start.json()["run_id"]

        state = wait_completed(client, run_id)
        assert state["status"] == "completed"
        assert state["result"]["verification_status"] == "passed"

        # SSE：完成后订阅仍能收到全部历史事件 + done（单调 seq）
        stream = client.get(f"/api/runs/{run_id}/events")
        assert stream.status_code == 200
        sse_events = [json.loads(line[6:]) for line in stream.text.splitlines()
                      if line.startswith("data: ")]
        assert sse_events[-1]["type"] == "done"
        assert any(e["type"] == "run_finished" for e in sse_events)
        seqs = [e["seq"] for e in sse_events if "seq" in e]
        assert seqs == sorted(seqs)

        payload = client.get(f"/api/runs/{run_id}").json()
        assert payload["result"]["runtime_status"] == "completed"
        assert client.get(f"/api/runs/{run_id}/patch").status_code == 200

    def test_review_flag_passes_through(self, sample_repo, tmp_path, monkeypatch):
        """--review 开关从请求体传入 RunConfig（P0-4 的 Web 形态）。"""
        captured: dict = {}
        client = self._install(monkeypatch, captured)
        resp = client.post("/api/runs", json={
            "repo": str(sample_repo), "task": "Fix", "review": True,
            "output_dir": str(tmp_path / "d"),
        })
        assert resp.status_code == 200
        for _ in range(50):
            if captured.get("review_enabled") is not None:
                break
            time.sleep(0.1)
        assert captured.get("review_enabled") is True

    def test_error_run_surfaces(self, sample_repo, tmp_path, monkeypatch):
        """栈内异常 → run.status=error，前端可见（不静默）。"""
        import repofix.cli as cli_module
        import repofix.server as srv

        def bad_stack(task_spec, config, run_id):
            raise PreflightFailureSim("container_start: boom")

        class PreflightFailureSim(Exception):
            pass

        # PreflightFailure 在 server 中按名字捕获——构造同名异常类
        from repofix.cli import PreflightFailure

        def failing_stack(task_spec, config, run_id):
            raise PreflightFailure("container_start", "boom")

        monkeypatch.setattr(cli_module, "_build_stack", failing_stack)
        monkeypatch.setattr(srv, "_build_stack", failing_stack)
        client = TestClient(app)
        resp = client.post("/api/runs", json={
            "repo": str(sample_repo), "task": "Fix", "output_dir": str(tmp_path / "d2"),
        })
        assert resp.status_code == 200
        for _ in range(30):
            state = client.get(f"/api/runs/{resp.json()['run_id']}").json()
            if state["status"] == "error":
                break
            time.sleep(0.1)
        assert state["status"] == "error"
        assert "preflight" in state["error"]
