"""无工具 API 对照组（Control）：与 Agent 同信息量、无自主行动的裸 API 基线。

对照设计（回答"Harness 相对裸 API 的增量价值"）：
- 信息量对齐：issue + plan.py 同一规则定位的源码文件内容（全部为 Agent 可见
  信息；test_patch / F2P / P2P / gold patch 仍然禁止）；
- 无工具、无检索、无多文件编辑：模型每轮基于静态上下文 + 反馈输出一个
  single-file unified diff；反馈 = 应用错误（git apply stderr）或可见测试输出
  （事件驱动：仅 patch 应用成功时才跑测试）；
- 预算语义与 Agent 一致：40 轮 / 600k token 上限（先到先停）；
- 判定与 Agent 完全同一套 judge（官方协议，干净 base 独立验收）。

与 Agent 的差异（报告须如实列出）：无 search/read 工具（上下文一次给足）、
无 apply_patch 的 sha256 前置校验（对照用 git apply 语义）、无 finish/验证
阶段（轮次耗尽或可见测试全过即停）。
"""
import json
import subprocess
import time
import uuid
from pathlib import Path

from repofix.contracts import ModelRequest
from repofix.evaluation.swebench.judge import run_judge
from repofix.evaluation.swebench.plan import derive_visible_plan, locate_source_files, extract_identifiers, _tracked_py_files
from repofix.evaluation.swebench.workspace import (
    extract_testbed,
    force_rmtree,
    run_install_commands,
    start_eval_container,
)
from repofix.evaluation.swebench.judge import _strip_ansi
from repofix.gateway.adapter import DeepSeekAdapter
from repofix.gateway.client import ModelProtocolError
from repofix.workspace.docker_cli import DockerCli

MAX_ROUNDS = 40
CONTEXT_FILE_CHARS = 24000  # 定位文件内容的总字符上限
_CONTROL_SYSTEM_RULES = (
    "You are a code-fixing assistant WITHOUT tools. You are given an issue and the "
    "contents of the source file(s) a deterministic rule located from the issue text. "
    "Respond with EXACTLY ONE single-file unified diff (git format, with ---/+++ "
    "headers and @@ hunks) that fixes the issue. Do not modify test files. "
    "Output only the diff inside a ```diff fence, no prose."
)


class ControlExhausted(RuntimeError):
    """对照预算耗尽（轮次或 token）。"""


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.fileMode=false", *args], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def build_static_context(problem_statement: str, testbed: Path) -> tuple[str, list[str]]:
    """静态上下文：issue + 规则定位的源码文件内容（Agent 可见信息，无隐藏字段）。"""
    py_files = _tracked_py_files(testbed)
    identifiers = extract_identifiers(problem_statement)
    located = locate_source_files(identifiers, py_files, testbed)[:3]
    parts = [f"Issue\n-----\n{problem_statement}"]
    used: list[str] = []
    budget = CONTEXT_FILE_CHARS
    for rel in located:
        path = testbed / rel
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if budget <= 0:
            break
        chunk = content[:budget]
        parts.append(f"File: {rel}\n-----\n{chunk}"
                     + ("\n[...truncated...]" if len(content) > len(chunk) else ""))
        budget -= len(chunk)
        used.append(rel)
    return "\n\n".join(parts), used


def extract_diff(response_text: str) -> str:
    """从模型回复提取 unified diff（```diff 围栏优先，其次 diff --git 行）。"""
    fenced = None
    for marker in ("```diff", "```patch", "```"):
        idx = response_text.find(marker)
        if idx >= 0:
            body = response_text[idx + len(marker):]
            end = body.find("```")
            fenced = body[:end] if end >= 0 else body
            break
    text = fenced if fenced is not None else response_text
    lines = text.splitlines()
    if any(line.startswith("diff --git ") for line in lines):
        starts = [i for i, l in enumerate(lines) if l.startswith("diff --git ")]
        return "\n".join(lines[starts[0]:]).strip()
    if any(line.startswith("--- ") for line in lines) and any(line.startswith("+++ ") for line in lines):
        first = next(i for i, l in enumerate(lines) if l.startswith("--- "))
        return "\n".join(lines[first:]).strip()
    return ""


def run_control_task(
    *,
    instance_meta: dict,
    row: dict,
    visible_argv: list[str],
    config,
    rg_path: Path,
    data_root: Path,
    max_rounds: int = MAX_ROUNDS,
    max_tokens: int = 600000,
) -> dict:
    """运行单个对照任务。返回与 agent 记录同构的 record（runner 字段口径一致）。"""
    instance_id = instance_meta["instance_id"]
    run_dir = Path(data_root) / "runs" / instance_id
    run_dir.mkdir(parents=True, exist_ok=True)
    testbed = Path(data_root) / "control-testbeds" / instance_id
    testbed.parent.mkdir(parents=True, exist_ok=True)
    force_rmtree(testbed)
    testbed.mkdir(parents=True)
    extract_testbed(DockerCli(), instance_meta["image"], testbed,
                    base_commit=instance_meta["base_commit"])
    api_key = _require_api_key()
    adapter = DeepSeekAdapter(api_key=api_key, retry_limit=2, thinking="disabled")

    static_context, located_files = build_static_context(
        row["problem_statement"], testbed)
    messages: list[dict] = [
        {"role": "system", "content": _CONTROL_SYSTEM_RULES},
        {"role": "user", "content": static_context},
    ]

    total_tokens = 0
    applied_patch = False
    visible_passed = False
    last_feedback = "You have not produced a patch yet. Respond with a unified diff."
    rounds_log: list[dict] = []
    started = time.time()
    try:
        for round_no in range(1, max_rounds + 1):
            if total_tokens >= max_tokens:
                raise ControlExhausted(f"token budget {total_tokens} >= {max_tokens}")
            user_content = last_feedback if round_no > 1 else (
                static_context + "\n\nRespond with a single-file unified diff.")
            request = ModelRequest(
                actor="main", model_id=config.model_id,
                messages=messages + [{"role": "user", "content": user_content}],
                tools=[], max_output_tokens=4096,
                request_id=f"ctrl_{instance_id}_{round_no}_{uuid.uuid4().hex[:6]}",
            )
            try:
                response = adapter.complete(request)
            except ModelProtocolError as exc:
                # 输出超长（finish_reason=length）是可反馈的轮次失败，不是任务致命错误：
                # 无工具模型倾向整文件重写，应反馈"输出最小化 diff"后重试
                last_feedback = ("Your previous response was rejected: it exceeded the "
                                 "maximum output length. Produce a MINIMAL diff: the "
                                 "fewest hunks that fix the issue, no surrounding context "
                                 "beyond the required 3 lines per side.")
                rounds_log.append({"round": round_no, "tokens_after": total_tokens,
                                   "diff_chars": 0, "result": "response_truncated"})
                continue
            usage = response.usage or {}
            total_tokens += int(usage.get("total_tokens", 0) or 0)
            content = (response.message or {}).get("content") or ""
            diff = extract_diff(content)
            entry = {"round": round_no, "tokens_after": total_tokens,
                     "diff_chars": len(diff), "result": ""}
            if not diff:
                last_feedback = ("Your response contained no unified diff. Respond with "
                                 "EXACTLY ONE git-format single-file unified diff in a ```diff fence.")
                entry["result"] = "no_diff"
                rounds_log.append(entry)
                continue

            patch_file = run_dir / f"round-{round_no}.diff"
            patch_file.write_text(diff, encoding="utf-8", newline="\n")
            apply = _git(["apply", str(patch_file)], testbed)
            if apply.returncode != 0:
                last_feedback = ("Your patch failed to apply (git apply). Error:\n"
                                 f"{(apply.stderr or apply.stdout).strip()[:600]}\n"
                                 "Re-read the file content provided above and produce a "
                                 "diff whose context lines match exactly.")
                entry["result"] = "apply_failed"
                rounds_log.append(entry)
                continue

            applied_patch = True
            # patch 应用成功：跑可见测试（事件驱动反馈）
            container = f"sweb-control-{instance_id}-{uuid.uuid4().hex[:6]}"
            start_eval_container(DockerCli(), container, testbed, instance_meta["image"])
            try:
                installs, _tests = extract_eval_commands_safe(row)
                run_install_commands(DockerCli(), container, installs,
                                     env_prefix=f"SETUPTOOLS_SCM_PRETEND_VERSION={row.get('version', '')} "
                                     if row.get("version") else "")
                if visible_argv:
                    outcome = DockerCli().exec(container, visible_argv, timeout=600,
                                               cwd_in_container="/testbed")
                    text = _strip_ansi(outcome.stdout + outcome.stderr)
                    visible_passed = outcome.returncode == 0
                    last_feedback = (
                        f"Your patch applied. Visible tests ({' '.join(visible_argv[3:])}) "
                        f"exit_code={outcome.returncode}:\n{text[-1200:]}")
                    entry["result"] = f"applied_visible_rc={outcome.returncode}"
                else:
                    last_feedback = ("Your patch applied cleanly. There is no visible test "
                                     "plan for this task; respond with an improved diff or "
                                     "the same diff if you believe it is complete.")
                    entry["result"] = "applied_no_visible_tests"
            finally:
                DockerCli().remove_container(container)
            rounds_log.append(entry)
            if visible_passed:
                break
    except ControlExhausted as exc:
        termination = f"budget_exhausted:{exc}"
    except Exception as exc:  # noqa: BLE001
        termination = f"error:{type(exc).__name__}: {exc}"
    else:
        termination = ("visible_tests_passed" if visible_passed
                       else f"rounds_exhausted:{max_rounds}")

    # 导出累计 patch（相对 base）并按同一 judge 独立验收
    cumulative = _git(["diff"], testbed).stdout
    (run_dir / "changes.patch").write_text(cumulative, encoding="utf-8", newline="\n")
    (run_dir / "control-rounds.json").write_text(
        json.dumps(rounds_log, ensure_ascii=False, indent=2), encoding="utf-8")
    force_rmtree(testbed)
    agent_seconds = time.time() - started

    judge_record = run_judge(
        DockerCli(), image=_derived_image(instance_meta, row), base_commit=instance_meta["base_commit"],
        test_patch=row["test_patch"], fail_to_pass=instance_meta["fail_to_pass"],
        pass_to_pass=instance_meta["pass_to_pass"], eval_script=row["eval_script"],
        patch_path=run_dir / "changes.patch",
        work_root=Path(data_root) / "judge-runs", instance_id=f"{instance_id}-control",
        pkg_version=str(row.get("version") or ""),
    )
    record = {
        "instance_id": instance_id,
        "repo": instance_meta["repo"],
        "arm": "control_no_tools",
        "agent": {
            "runtime_status": ("COMPLETED" if visible_passed and applied_patch
                               else "BUDGET_EXCEEDED" if "budget" in termination
                               else "FAILED"),
            "termination_reason": termination,
            "model_calls": len(rounds_log),
            "tool_calls": 0,
            "token_usage": {"total": total_tokens, "prompt": total_tokens, "completion": 0},
            "modified_files": [],
        },
        "agent_seconds": round(agent_seconds, 1),
        "judge": {
            "outcome": judge_record.outcome, "reason": judge_record.reason,
            "patch_applied": judge_record.patch_applied,
            "f2p_failures": judge_record.f2p_failures,
            "p2p_regressions": judge_record.p2p_regressions,
            "statuses_count": judge_record.statuses_count,
        },
        "resolved": judge_record.outcome == "RESOLVED",
        "artifacts_dir": str(run_dir),
    }
    (run_dir / "eval-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def extract_eval_commands_safe(row: dict) -> tuple[list[str], list[str]]:
    from repofix.evaluation.swebench.judge import extract_eval_commands

    return extract_eval_commands(row["eval_script"])


def _derived_image(instance_meta: dict, row: dict) -> str:
    from repofix.evaluation.swebench.workspace import build_agent_image

    return build_agent_image(
        DockerCli(), instance_meta["image"], instance_meta["instance_id"],
        Path("repofix-data/swebench/tools/rg"), pkg_version=str(row.get("version") or ""))


def _require_api_key() -> str:
    import os

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    return key
