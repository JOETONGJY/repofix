"""SQLite 三表 DAO：runs / events / tool_calls（specs/01 权威表结构）。

关键规则（design/06）：
- 意图先行：工具执行前必须持久化执行意图（STARTED 与 TOOL_STARTED 事件同事务）；
  意图未成功持久化，不允许执行有副作用的工具。
- 结果与完成事件（TOOL_FINISHED）在同一数据库事务内提交。
- 崩溃对账：STARTED 无完成记录 → 按 pre_state_hash / expected_result 判定
  SUCCEEDED / FAILED / UNKNOWN；不自动重放工具调用。
- trajectory.jsonl 仅由导出产生，不参与状态判定。
"""
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from repofix.contracts import (
    AgentActor,
    AgentEvent,
    EventType,
    RunStatus,
    SideEffectStatus,
    ToolResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    status TEXT NOT NULL,
    eval_outcome TEXT NOT NULL DEFAULT 'not_evaluated',
    termination_reason TEXT,
    started_at REAL,
    ended_at REAL,
    model_id TEXT,
    prompt_version TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    fail_layer TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER,
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    ts REAL NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    side_effect_status TEXT NOT NULL,
    pre_state_hash TEXT,
    expected_result TEXT,
    result_json TEXT,
    stdout TEXT,
    stderr TEXT,
    started_at REAL,
    completed_at REAL,
    PRIMARY KEY (run_id, decision_id, call_id)
);
"""

_TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.BUDGET_EXCEEDED,
}


class StoreWriteError(RuntimeError):
    """事件或工具状态持久化失败。Runtime 收到后不得继续新的写入操作。"""


class SQLiteEventStore:
    """单一权威存储：运行状态与事件的权威来源（文件内容以 Workspace 为准）。"""

    def __init__(self, db_path: str | Path = ":memory:"):
        path = Path(db_path) if str(db_path) != ":memory:" else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)  # 运行目录可能尚未创建
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _begin(self) -> sqlite3.Cursor:
        cursor = self._conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        return cursor

    # ---- runs ----

    def create_run(
        self,
        run_id: str,
        repo: str,
        base_commit: str,
        model_id: str,
        prompt_version: str,
        ts: float,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO runs (run_id, repo, base_commit, status, started_at, model_id, prompt_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, repo, base_commit, RunStatus.CREATED.value, ts, model_id, prompt_version),
            )
        except sqlite3.Error as exc:
            raise StoreWriteError(str(exc)) from exc

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        ts: float,
        termination_reason: str | None = None,
        fail_layer: str | None = None,
    ) -> None:
        try:
            if status in _TERMINAL_STATUSES:
                self._conn.execute(
                    "UPDATE runs SET status = ?, termination_reason = COALESCE(?, termination_reason),"
                    " fail_layer = COALESCE(?, fail_layer), ended_at = ? WHERE run_id = ?",
                    (status.value, termination_reason, fail_layer, ts, run_id),
                )
            else:
                self._conn.execute(
                    "UPDATE runs SET status = ?, termination_reason = COALESCE(?, termination_reason),"
                    " fail_layer = COALESCE(?, fail_layer) WHERE run_id = ?",
                    (status.value, termination_reason, fail_layer, run_id),
                )
        except sqlite3.Error as exc:
            raise StoreWriteError(str(exc)) from exc

    def set_run_token_usage(self, run_id: str, total_tokens: int) -> None:
        # Token 用量由模型调用事件聚合回填，不单独建 usage 表（design/06）
        self._conn.execute("UPDATE runs SET total_tokens = ? WHERE run_id = ?", (total_tokens, run_id))

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ---- events ----

    def append_event(
        self,
        run_id: str,
        step: int,
        actor: AgentActor,
        event_type: EventType,
        payload: dict,
        ts: float,
    ) -> AgentEvent:
        cursor = self._begin()
        try:
            seq = self._next_seq(cursor, run_id)
            cursor.execute(
                "INSERT INTO events (seq, run_id, step, actor, type, ts, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, run_id, step, actor.value, event_type.value, ts, json.dumps(payload, ensure_ascii=False)),
            )
            cursor.execute("COMMIT")
        except sqlite3.Error as exc:
            cursor.execute("ROLLBACK")
            raise StoreWriteError(str(exc)) from exc
        return AgentEvent(
            seq=seq, run_id=run_id, step=step, actor=actor, type=event_type, ts=ts, payload=payload
        )

    def get_events(self, run_id: str) -> list[AgentEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [
            AgentEvent(
                seq=row["seq"],
                run_id=row["run_id"],
                step=row["step"],
                actor=AgentActor(row["actor"]),
                type=EventType(row["type"]),
                ts=row["ts"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    # ---- tool_calls：意图先行 + 同事务完成 ----

    def start_tool_call(
        self,
        run_id: str,
        decision_id: str,
        call_id: str,
        step: int,
        tool_name: str,
        arguments: dict,
        pre_state_hash: str | None = None,
        expected_result: str | None = None,
        ts: float = 0.0,
    ) -> None:
        """持久化执行意图：tool_calls 行 PENDING→STARTED 与 TOOL_STARTED 事件同事务写入。"""
        cursor = self._begin()
        try:
            cursor.execute(
                "INSERT INTO tool_calls (call_id, decision_id, run_id, step, tool_name, arguments_json,"
                " side_effect_status, pre_state_hash, expected_result, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, decision_id, call_id) DO UPDATE SET"
                " side_effect_status = excluded.side_effect_status, started_at = excluded.started_at",
                (
                    call_id,
                    decision_id,
                    run_id,
                    step,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    SideEffectStatus.STARTED.value,
                    pre_state_hash,
                    expected_result,
                    ts,
                ),
            )
            seq = self._next_seq(cursor, run_id)
            cursor.execute(
                "INSERT INTO events (seq, run_id, step, actor, type, ts, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    run_id,
                    step,
                    AgentActor.SYSTEM.value,
                    EventType.TOOL_STARTED.value,
                    ts,
                    json.dumps(
                        {
                            "call_id": call_id,
                            "decision_id": decision_id,
                            "tool_name": tool_name,
                            "pre_state_hash": pre_state_hash,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            cursor.execute("COMMIT")
        except sqlite3.Error as exc:
            cursor.execute("ROLLBACK")
            raise StoreWriteError(str(exc)) from exc

    def finish_tool_call(
        self,
        run_id: str,
        decision_id: str,
        call_id: str,
        step: int,
        side_effect_status: SideEffectStatus,
        result: ToolResult | None,
        ts: float,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """结果与完成事件在同一事务内提交。result=None 用于 SKIPPED 等无执行结果场景。"""
        cursor = self._begin()
        try:
            cursor.execute(
                "UPDATE tool_calls SET side_effect_status = ?, result_json = ?, stdout = ?, stderr = ?,"
                " completed_at = ? WHERE run_id = ? AND decision_id = ? AND call_id = ?",
                (
                    side_effect_status.value,
                    result.model_dump_json() if result else None,
                    stdout,
                    stderr,
                    ts,
                    run_id,
                    decision_id,
                    call_id,
                ),
            )
            seq = self._next_seq(cursor, run_id)
            payload = dict(result.model_dump()) if result else {}
            payload.update({"call_id": call_id, "decision_id": decision_id})
            cursor.execute(
                "INSERT INTO events (seq, run_id, step, actor, type, ts, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    run_id,
                    step,
                    AgentActor.SYSTEM.value,
                    EventType.TOOL_FINISHED.value,
                    ts,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            cursor.execute("COMMIT")
        except sqlite3.Error as exc:
            cursor.execute("ROLLBACK")
            raise StoreWriteError(str(exc)) from exc

    def record_skipped_call(
        self,
        run_id: str,
        decision_id: str,
        call_id: str,
        step: int,
        tool_name: str,
        arguments: dict,
        result: ToolResult | None,
        ts: float,
    ) -> None:
        """记录一个未实际执行的调用（批次中止 / 预算跳过 / 整批拒绝 / 校验拒绝）。

        side_effect_status=SKIPPED；ToolResult（可为 FAILED/INVALID_ARGUMENT）保存在
        result_json 供回流与统计；与 TOOL_FINISHED 事件同事务。
        """
        cursor = self._begin()
        try:
            cursor.execute(
                "INSERT INTO tool_calls (call_id, decision_id, run_id, step, tool_name, arguments_json,"
                " side_effect_status, result_json, completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, decision_id, call_id) DO UPDATE SET"
                " side_effect_status = excluded.side_effect_status,"
                " result_json = excluded.result_json, completed_at = excluded.completed_at",
                (
                    call_id,
                    decision_id,
                    run_id,
                    step,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    SideEffectStatus.SKIPPED.value,
                    result.model_dump_json() if result else None,
                    ts,
                ),
            )
            seq = self._next_seq(cursor, run_id)
            payload = dict(result.model_dump()) if result else {}
            payload.update(
                {"call_id": call_id, "decision_id": decision_id, "tool_name": tool_name}
            )
            cursor.execute(
                "INSERT INTO events (seq, run_id, step, actor, type, ts, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    run_id,
                    step,
                    AgentActor.SYSTEM.value,
                    EventType.TOOL_FINISHED.value,
                    ts,
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            cursor.execute("COMMIT")
        except sqlite3.Error as exc:
            cursor.execute("ROLLBACK")
            raise StoreWriteError(str(exc)) from exc

    def get_tool_call(self, run_id: str, decision_id: str, call_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? AND decision_id = ? AND call_id = ?",
            (run_id, decision_id, call_id),
        ).fetchone()
        return dict(row) if row else None

    def list_tool_calls(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at ASC", (run_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 崩溃对账（specs/02 恢复流程；不自动重放工具调用） ----

    def reconcile_run(
        self,
        run_id: str,
        actual_state_fn: Callable[[dict], str | None],
        ts: float,
    ) -> dict:
        """对 STARTED 且无完成记录的调用做有限状态核对。

        actual_state_fn(row) 返回该调用目标当前的实际状态标记（如文件内容 hash）：
        - 等于 expected_result → 补记 SUCCEEDED（已生效，不重复修改）
        - 等于 pre_state_hash  → 补记 FAILED（未生效，不自动重试）
        - 其他 / 无法判断      → 补记 UNKNOWN，停止自动恢复（需人工确认）
        未正常终结的 Run 标记 FAILED（termination_reason = PROCESS_INTERRUPTED）。
        """
        rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? AND side_effect_status = ?",
            (run_id, SideEffectStatus.STARTED.value),
        ).fetchall()
        summary = {"succeeded": 0, "failed": 0, "unknown": 0}
        for row in rows:
            actual = actual_state_fn(dict(row))
            if actual is not None and row["expected_result"] is not None and actual == row["expected_result"]:
                status = SideEffectStatus.SUCCEEDED
                summary["succeeded"] += 1
            elif actual is not None and row["pre_state_hash"] is not None and actual == row["pre_state_hash"]:
                status = SideEffectStatus.FAILED
                summary["failed"] += 1
            else:
                status = SideEffectStatus.UNKNOWN
                summary["unknown"] += 1
            self._conn.execute(
                "UPDATE tool_calls SET side_effect_status = ?, completed_at = ?"
                " WHERE run_id = ? AND decision_id = ? AND call_id = ?",
                (status.value, ts, run_id, row["decision_id"], row["call_id"]),
            )
        run = self.get_run(run_id)
        if run and RunStatus(run["status"]) not in _TERMINAL_STATUSES:
            self.set_run_status(
                run_id,
                RunStatus.FAILED,
                ts,
                termination_reason="PROCESS_INTERRUPTED",
            )
        return summary

    # ---- Trace 导出（ST-04：仅由导出产生，内容与 SQLite 一致） ----

    def export_trajectory(self, run_id: str) -> str:
        lines = []
        for event in self.get_events(run_id):
            record = {
                "seq": event.seq,
                "run_id": event.run_id,
                "step": event.step,
                "actor": event.actor.value,
                "type": event.type.value,
                "ts": event.ts,
                "payload": event.payload,
            }
            lines.append(json.dumps(record, ensure_ascii=False, default=str))
        return "\n".join(lines) + ("\n" if lines else "")

    # ---- 内部 ----

    def _next_seq(self, cursor: sqlite3.Cursor, run_id: str) -> int:
        row = cursor.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["next_seq"]
