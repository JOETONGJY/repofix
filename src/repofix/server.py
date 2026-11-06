"""RepoFix Web Server：把 Agent Run 以 HTTP + SSE 暴露给前端（Codex 风格单页）。

设计边界（对齐 design/01）：Server 只是 Task Interface 的另一种形态——
启动、事件流、结果展示；不参与模型决策、工具分发与状态转换。
复用 cli._build_stack 装配链与 _write_artifacts 产物逻辑。

运行：uvicorn repofix.server:app --port 8000（需 DEEPSEEK_API_KEY 与 Docker Desktop）。
"""
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from repofix.cli import (
    InputValidationError,
    PreflightFailure,
    _build_stack,
    _read_task,
    _validate_repo,
    _write_artifacts,
)
from repofix.contracts import RunStatus
from repofix.gateway.adapter import resolve_context_window
from repofix.state.store import StoreWriteError
from repofix.workspace.errors import DirtyWorkspaceError, WorkspaceError

# 安全边界：Server 仅绑定本机 127.0.0.1（见 uvicorn --host），无认证机制——
# 不要将端口转发到局域网或公网；可访问的仓库路径以本机文件系统为限。
app = FastAPI(title="RepoFix", docs_url=None, redoc_url=None)


@dataclass
class RunHandle:
    """一次 Web Run 的内存状态。

    生命周期（评审收尾）：Run 结束后延迟释放重资源（SQLite 连接/容器引用），
    RunHandle 本体保留轻量元数据（result/artifacts_dir）供结果查询；
    历史产物以 repofix-data/ 下文件为准，不依赖内存对象永久存活。
    """

    run_id: str
    status: str = "running"  # running / completed / failed / budget_exceeded / error
    error: str = ""
    result: object = None
    artifacts_dir: str = ""
    store: object = None
    workspace: object = None
    manager: object = None
    handle: object = None
    # 评审 P0-1：必须是 field 工厂——类属性形式的 Event 会被所有 Run 共享，
    # 第一个 Run 完成后后续 Run 的 SSE 会提前结束
    done: threading.Event = field(default_factory=threading.Event)
    subscribers: list = field(default_factory=list)


_RUNS: dict[str, RunHandle] = {}
_RUNS_LOCK = threading.Lock()


class StartRequest(BaseModel):
    repo: str = Field(min_length=1)
    task: str = Field(min_length=1)
    base_ref: str = "HEAD"
    model: str = "deepseek-flash"
    test_command: Optional[str] = Field(
        default="auto",
        description="可见验证命令；默认 auto = Preflight 自动发现 Python 测试")
    output_dir: str = "repofix-data/web"
    max_model_calls: int = Field(default=6, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    timeout: int = Field(default=600, ge=1)
    review: bool = False


_RUN_RETENTION_SECONDS = 600  # Run 结束后保留轻量元数据 10 分钟，随后释放重资源


def _release_run_resources(run: "RunHandle") -> None:
    """延迟释放：关闭 SQLite 连接、清空容器/Manager 引用（评审 P1 生命周期治理）。"""
    if run.store is not None:
        try:
            run.store.close()
        except Exception:  # noqa: BLE001
            pass
        run.store = None
    run.workspace = None
    run.manager = None
    run.handle = None


def _schedule_release(run: "RunHandle") -> None:
    timer = threading.Timer(_RUN_RETENTION_SECONDS, _release_run_resources, args=(run,))
    timer.daemon = True
    timer.start()


def _finish_run(run: RunHandle, result, artifacts_dir: str = "") -> None:
    run.result = result
    run.artifacts_dir = artifacts_dir
    # 评审 P1：区分"执行结束"与"任务成功"——Runtime FAILED/BUDGET 不再显示绿色完成
    if result is None:
        run.status = "error"
    elif result.runtime_status is RunStatus.COMPLETED:
        run.status = "completed"
    elif result.runtime_status is RunStatus.BUDGET_EXCEEDED:
        run.status = "budget_exceeded"
    else:
        run.status = "failed"
    run.done.set()
    _schedule_release(run)


def _run_worker(run: RunHandle, task_spec, config) -> None:
    """后台执行 Agent Run；异常一律落到 run.status=error（前端可见）。"""
    try:
        runner, store, workspace, manager, handle, _preflight = _build_stack(
            task_spec, config, run.run_id)
        run.store = store
        run.workspace = workspace
        run.manager = manager
        run.handle = handle
        try:
            result = runner.run()
            run_dir, delivery_error = _write_artifacts(
                Path(config.output_dir), run.run_id, store, workspace, result)
            _finish_run(run, result, str(run_dir))
            if delivery_error:
                run.error = delivery_error
        except KeyboardInterrupt:
            _finish_run(run, None)
            run.error = "interrupted"
        except Exception as exc:  # noqa: BLE001 —— 前端可见的运行失败
            run.error = f"{type(exc).__name__}: {exc}"
            _finish_run(run, None)
        finally:
            try:
                workspace.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                manager.cleanup_workspace(
                    handle, keep_for_diagnosis=(run.status != "completed"))
    except (InputValidationError, PreflightFailure, DirtyWorkspaceError,
            WorkspaceError) as exc:
        run.error = str(exc)
        run.status = "error"
        run.done.set()
    except Exception as exc:  # noqa: BLE001
        run.error = f"{type(exc).__name__}: {exc}"
        run.status = "error"
        run.done.set()


@app.post("/api/runs")
def start_run(body: StartRequest):
    """启动一次 Agent Run（后台执行；用 GET /api/runs/{id}/events 订阅事件流）。"""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    try:
        instruction = _read_task(body.task, None)
        base_commit = _validate_repo(Path(body.repo), body.base_ref)
    except (InputValidationError, DirtyWorkspaceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from repofix.contracts import RunConfig, TaskSpec

    config = RunConfig(
        model_id=body.model,
        max_model_calls=body.max_model_calls,
        max_tool_calls=body.max_tool_calls,
        max_duration_seconds=body.timeout,
        review_enabled=body.review,
        output_dir=body.output_dir,
        # 与 CLI 一致：Context Budget 生产启用（未知模型 resolve 返回 None 不虚构容量）
        context_window_tokens=resolve_context_window(body.model),
    )
    task_spec = TaskSpec(
        instruction=instruction, repo_path=body.repo, base_commit=base_commit,
        test_command=body.test_command,
    )

    run = RunHandle(run_id=run_id)
    with _RUNS_LOCK:
        _RUNS[run_id] = run
    thread = threading.Thread(
        target=_run_worker, args=(run, task_spec, config), daemon=True, name=f"agent-{run_id}")
    thread.start()
    return {"run_id": run_id, "status": run.status}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    payload = {"run_id": run_id, "status": run.status, "error": run.error}
    if run.result is not None:
        payload["result"] = run.result.model_dump()
        payload["artifacts_dir"] = run.artifacts_dir
    return payload


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str, since: int = 0):
    """SSE 事件流：增量推送 run 的事件，Run 结束后发送 done 事件。"""
    run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    def generate():
        last_seq = since
        import json
        import time

        while True:
            new_events = []
            if run.store is not None:
                try:
                    new_events = [e for e in run.store.get_events(run_id) if e.seq > last_seq]
                except StoreWriteError:
                    pass
            for event in new_events:
                last_seq = event.seq
                data = json.dumps({
                    "seq": event.seq, "type": event.type.value,
                    "actor": event.actor.value, "step": event.step,
                    "payload": event.payload, "ts": event.ts,
                }, ensure_ascii=False, default=str)
                yield f"data: {data}\n\n"
            if run.done.is_set():
                done_payload = json.dumps(
                    {"type": "done", "status": run.status, "error": run.error},
                    ensure_ascii=False)
                yield "data: " + done_payload + "\n\n"
                return
            time.sleep(0.4)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/patch")
def get_patch(run_id: str):
    run = _RUNS.get(run_id)
    if run is None or not run.artifacts_dir:
        raise HTTPException(status_code=404, detail="patch not available")
    patch_file = Path(run.artifacts_dir) / "changes.patch"
    if not patch_file.exists():
        raise HTTPException(status_code=404, detail="patch not available")
    return FileResponse(patch_file, media_type="text/plain", filename="changes.patch")


@app.get("/")
def index():
    # P2：优先包内资源（wheel 安装），回退仓库根（editable 安装）
    candidates = [
        Path(__file__).parent / "web" / "index.html",
        Path(__file__).parent.parent.parent / "web" / "index.html",
    ]
    for candidate in candidates:
        if candidate.exists():
            return FileResponse(candidate, media_type="text/html")
    raise HTTPException(status_code=404, detail="web/index.html not found")
