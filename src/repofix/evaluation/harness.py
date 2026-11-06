"""Evaluation Harness：独立验收 + 指标统计（design/09、specs/04 EV 系列）。

三个组件（逻辑职责，可组合）：
- EvalRunner：对每个任务启动独立 Run（经 CLI 装配或注入栈），收集候选 Patch 与 RunResult。
- ResultJudge：干净工作区独立验收——Base Commit 重初始化 → 应用候选 Patch → 运行隐藏测试；
  SUCCESS / FAILED / INCONCLUSIVE；**不用 Agent 工作区自验**（检验 Patch 可独立复现）。
- MetricsAggregator：三层指标（业务效果 / 执行效率 / 运行可靠性），
  统计口径成功率 = 独立验收成功数 / 有效且已尝试的可修复任务数（预算耗尽留在分母）。
"""
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from repofix.contracts import (
    EvaluationOutcome,
    EvaluationResult,
    RunResult,
    RunStatus,
)


@dataclass
class EvalTask:
    """评测任务定义：visible/hidden 配置严格分离（design/09）。"""

    task_id: str
    repo_source: str  # 合成仓库生成器或固定 Commit 的路径
    base_commit: str
    instruction: str
    test_command: str
    hidden_test_files: dict[str, str]  # 仅评测器持有；不进 Agent 工作区（EV-01）
    expected_behavior: str = ""


@dataclass
class EvalRecord:
    """一次评测运行的完整记录（EV-03：运行失败/隐藏失败/设施异常分账）。"""

    task_id: str
    run_id: str = ""
    run_result: RunResult | None = None
    outcome: EvaluationOutcome = EvaluationOutcome.NOT_EVALUATED
    reason: str = ""
    regression_detected: bool = False
    independent_apply_rc: int | None = None
    hidden_tests_rc: int | None = None
    patch_hash: str = ""


class ResultJudge:
    """干净工作区独立验收（design/09 第三节）。"""

    def __init__(self, workspace_provider, python_exe: str | None = None):
        # workspace_provider: 生成干净 Base 仓库的可调用（合成仓库时重建）
        self._provider = workspace_provider
        self._python = python_exe

    def judge(self, task: EvalTask, patch_path, run_result: RunResult | None) -> EvalRecord:
        """验收流程：干净 Base → git apply → 隐藏测试 → 判定。"""
        record = EvalRecord(task_id=task.task_id, run_id=run_result.run_id if run_result else "",
                            run_result=run_result)
        import subprocess
        # Runtime 必须正常完成（必要条件，非充分）
        if run_result is None or run_result.runtime_status not in (RunStatus.COMPLETED,):
            record.outcome = EvaluationOutcome.FAILED
            record.reason = f"runtime did not complete ({run_result.runtime_status.value if run_result else 'no run'})"
            return record

        tmp = Path(tempfile.mkdtemp(prefix=f"eval-{task.task_id}-"))
        try:
            clean = self._provider(tmp)  # 干净 Base 仓库（重建）
            patch_text = ""
            if patch_path and Path(patch_path).exists():
                patch_text = Path(patch_path).read_text(encoding="utf-8")
            if not patch_text.strip():
                record.outcome = EvaluationOutcome.FAILED
                record.reason = "empty or missing patch (delivery failure)"
                return record

            patch_abs = str(Path(patch_path).resolve())
            apply = subprocess.run(["git", "apply", patch_abs], cwd=repo_dir(clean),
                                   capture_output=True, text=True, timeout=120)
            record.independent_apply_rc = apply.returncode
            if apply.returncode != 0:
                record.outcome = EvaluationOutcome.FAILED
                record.reason = f"patch does not apply on clean base: {apply.stderr.strip()[:200]}"
                return record

            env = base_env(clean)
            test = subprocess.run(
                [self._python or sys_executable(), "-m", "pytest", "-q", "--tb=no"],
                cwd=repo_dir(clean), capture_output=True, text=True, timeout=300, env=env,
            )
            record.hidden_tests_rc = test.returncode
            if test.returncode == 0:
                record.outcome = EvaluationOutcome.SUCCESS
                record.reason = "hidden tests passed on independently applied patch"
            else:
                record.outcome = EvaluationOutcome.FAILED
                record.reason = f"hidden tests failed rc={test.returncode}"
                record.regression_detected = True
            return record
        except Exception as exc:  # noqa: BLE001 —— 评测设施异常 → INCONCLUSIVE（EV-04）
            record.outcome = EvaluationOutcome.INCONCLUSIVE
            record.reason = f"evaluation infrastructure error: {type(exc).__name__}: {exc}"
            return record
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class MetricsAggregator:
    """三层指标汇总（design/09 第五节）。"""

    def __init__(self):
        self.records: list[EvalRecord] = []

    def add(self, record: EvalRecord) -> None:
        self.records.append(record)

    def summary(self) -> dict:
        """统计口径（design/09）：成功率分母 = 有效且已尝试的可修复任务——
        Runtime 失败、预算耗尽都不移出分母；INCONCLUSIVE 单独报告（EV-03/04）。"""
        total = len(self.records)
        outcomes = {}
        for r in self.records:
            outcomes[r.outcome.value] = outcomes.get(r.outcome.value, 0) + 1
        success = outcomes.get(EvaluationOutcome.SUCCESS.value, 0)
        attempted = sum(1 for r in self.records
                        if r.outcome != EvaluationOutcome.INCONCLUSIVE)
        tokens = sum(r.run_result.token_usage.get("total", 0)
                     for r in self.records if r.run_result)
        return {
            "total_tasks": total,
            "attempted": attempted,
            "success": success,
            "task_success_rate": round(success / attempted, 3) if attempted else 0.0,
            "outcomes": outcomes,
            "inconclusive": outcomes.get(EvaluationOutcome.INCONCLUSIVE.value, 0),
            "regressions": sum(1 for r in self.records if r.regression_detected),
            "total_tokens": tokens,
            "by_task": [
                {"task_id": r.task_id, "run_id": r.run_id, "outcome": r.outcome.value,
                 "reason": r.reason[:200], "regression": r.regression_detected}
                for r in self.records
            ],
        }


def to_evaluation_result(record: EvalRecord) -> EvaluationResult:
    """EvalRecord → 契约 EvaluationResult（specs/01 双状态维度）。"""
    return EvaluationResult(
        run_id=record.run_id, outcome=record.outcome,
        run_status=record.run_result.runtime_status if record.run_result else RunStatus.FAILED,
        reason=record.reason, regression_detected=record.regression_detected,
    )


def repo_dir(clean_root: Path) -> Path:
    """合成仓库 provider 生成的是根目录本身（含 .git）。"""
    if (clean_root / ".git").exists():
        return clean_root
    subdirs = [d for d in clean_root.iterdir() if d.is_dir()]
    return subdirs[0] if subdirs else clean_root


def base_env(repo: Path) -> dict:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo)
    return env


def sys_executable() -> str:
    return sys.executable

