"""Evaluation Harness：独立验收 + 指标统计（design/09、specs/04 EV 系列）。

三个组件（逻辑职责，可组合）：
- EvalRunner：对每个任务启动独立 Run（经 CLI 装配或注入栈），收集候选 Patch 与 RunResult。
- ResultJudge：干净工作区独立验收——Base Commit 重初始化 → 装载 EvalTask.hidden_test_files
  （仅评测器持有，不进 Agent 工作区，EV-01）→ 应用候选 Patch → **在受限容器内**运行隐藏测试
  （评审 P0-1：不复用宿主 Python/环境变量——宿主密钥不进测试进程）→ 判定
  SUCCESS / FAILED / INCONCLUSIVE。
- MetricsAggregator：三层指标；统计口径对齐 design/09（评审 P0-2）：**有效且已尝试的任务
  一律留在成功率分母**（INCONCLUSIVE 也不移出，单独报告数量），另报可判定样本上的
  条件成功率作参考。
"""
import shutil
import subprocess
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

DIFF_EXCLUDES = [":(exclude).pytest_cache", ":(exclude)__pycache__"]


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
    isolated_execution: bool = False  # P0-1：隐藏测试是否在容器内执行


class ResultJudge:
    """干净工作区独立验收（design/09 第三节）。

    执行隔离（评审 P0-1）：hidden tests 默认在受限容器（repofix-env 镜像：
    非 root / 无网络 / 最小环境变量）内运行，评测工作区与 Agent 工作区分离，
    宿主 API Key 不进入测试进程。
    """

    def __init__(
        self,
        workspace_provider,
        docker_cli=None,
        image: str = "repofix-env:latest",
        container_timeout: int = 300,
        allow_host_fallback: bool = False,
    ):
        self._provider = workspace_provider
        self._docker_cli = docker_cli
        self._image = image
        self._container_timeout = container_timeout
        # 评审最终收尾：正常评测强制 Docker 隔离——docker_cli 缺失且未显式
        # 开启宿主回退时，隐藏测试判定为评测设施异常（INCONCLUSIVE），不降级宿主
        self._allow_host_fallback = allow_host_fallback

    def judge(self, task: EvalTask, patch_path, run_result: RunResult | None,
              clean_provider=None) -> EvalRecord:
        """验收流程：干净 Base → 装载隐藏测试 → git apply → 容器内隐藏测试 → 判定。

        clean_provider：可覆盖构造期的 workspace_provider（按 task.base_commit 重建）。
        """
        record = EvalRecord(task_id=task.task_id, run_id=run_result.run_id if run_result else "",
                            run_result=run_result)
        provider = clean_provider or self._provider

        # Runtime 正常完成是必要条件（非充分）
        if run_result is None or run_result.runtime_status is not RunStatus.COMPLETED:
            record.outcome = EvaluationOutcome.FAILED
            record.reason = f"runtime did not complete ({run_result.runtime_status.value if run_result else 'no run'})"
            return record

        tmp = Path(tempfile.mkdtemp(prefix=f"eval-{task.task_id}-"))
        try:
            clean = provider(tmp)
            repo = _repo_dir(clean)

            # P0-3：Base Commit 核对——provider 重建的仓库必须停在任务指定版本
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  capture_output=True, text=True, timeout=60)
            if head.returncode != 0 or head.stdout.strip() != task.base_commit:
                record.outcome = EvaluationOutcome.INCONCLUSIVE
                record.reason = (f"clean workspace HEAD mismatch: "
                                 f"{head.stdout.strip()[:12] or 'error'} != {task.base_commit[:12]}")
                return record

            patch_text = ""
            if patch_path and Path(patch_path).exists():
                patch_text = Path(patch_path).read_text(encoding="utf-8")
            if not patch_text.strip():
                record.outcome = EvaluationOutcome.FAILED
                record.reason = "empty or missing patch (delivery failure)"
                return record

            patch_abs = str(Path(patch_path).resolve())
            apply = subprocess.run(["git", "apply", patch_abs], cwd=repo,
                                   capture_output=True, text=True, timeout=120)
            record.independent_apply_rc = apply.returncode
            if apply.returncode != 0:
                record.outcome = EvaluationOutcome.FAILED
                record.reason = f"patch does not apply on clean base: {apply.stderr.strip()[:200]}"
                return record

            # P0-3：装载 EvalTask 持有的隐藏测试（写入评测工作区——不进 Agent 工作区）
            for rel_path, content in task.hidden_test_files.items():
                target = repo / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            # P0-1（最终收尾）：正常评测强制容器隔离——docker_cli 缺失且未显式
            # 允许宿主回退 → INCONCLUSIVE（评测设施异常），绝不静默降级宿主执行
            if self._docker_cli is None and not self._allow_host_fallback:
                record.outcome = EvaluationOutcome.INCONCLUSIVE
                record.reason = ("evaluation isolation required: no Docker CLI configured; "
                                 "refusing to run hidden tests on the host")
                return record
            record.isolated_execution = self._docker_cli is not None
            test_rc, test_out = self._run_hidden_tests(repo)
            record.hidden_tests_rc = test_rc
            if test_rc == 0:
                record.outcome = EvaluationOutcome.SUCCESS
                record.reason = "hidden tests passed on independently applied patch"
            else:
                # 口径修正（评审收尾）：隐藏测试失败 = 修复未达标（FAILED）；
                # regression 需与基线对比判定——本判定无基线数据，不标 regression
                record.outcome = EvaluationOutcome.FAILED
                record.reason = f"hidden tests failed rc={test_rc}"
                _ = test_out
            return record
        except Exception as exc:  # noqa: BLE001 —— 评测设施异常 → INCONCLUSIVE（EV-04）
            record.outcome = EvaluationOutcome.INCONCLUSIVE
            record.reason = f"evaluation infrastructure error: {type(exc).__name__}: {exc}"
            return record
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run_hidden_tests(self, repo: Path) -> tuple[int, str]:
        """在受限容器内运行隐藏测试；容器不可用时降级宿主执行（仅供测试路径），
        并在 record.isolated_execution 上如实标记。"""
        if self._docker_cli is not None:
            from repofix.workspace.docker_cli import DockerCliError

            name = f"eval-{tempfile.mkdtemp(prefix='n').split('n')[-1][:10]}"
            try:
                self._docker_cli.start_container(name, self._image, repo)
                outcome = self._docker_cli.exec(
                    name, ["python", "-m", "pytest", "-q", "--tb=no"],
                    timeout=self._container_timeout, cwd_in_container="/workspace",
                )
                return outcome.returncode, outcome.stdout + outcome.stderr
            except DockerCliError as exc:
                raise RuntimeError(f"container execution failed: {exc}") from exc
            finally:
                self._docker_cli.remove_container(name, timeout=60)
        # 显式宿主回退路径（allow_host_fallback=True，仅供测试）：环境变量最小化——
        # 不复制 os.environ，只传 PATH/SYSTEMROOT；宿主 API Key 等密钥不进入测试进程
        import os

        repo = _repo_dir(repo)
        env = {"PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "PYTHONPATH": str(repo)}
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no"],
            cwd=repo, capture_output=True, text=True, timeout=300, env=env,
        )
        return proc.returncode, proc.stdout + proc.stderr


class MetricsAggregator:
    """三层指标汇总（design/09 第五节；评审 P0-2 对齐分母口径）。"""

    def __init__(self):
        self.records: list[EvalRecord] = []

    def add(self, record: EvalRecord) -> None:
        self.records.append(record)

    def summary(self) -> dict:
        """统计口径（design/09 + 评审 P0-2）：有效且已尝试的任务一律留在主要成功率
        分母（INCONCLUSIVE 也不移出，单独报告数量与原因）；另报可判定样本上的
        条件成功率作参考（不替代主口径）。"""
        total = len(self.records)
        outcomes = {}
        for r in self.records:
            outcomes[r.outcome.value] = outcomes.get(r.outcome.value, 0) + 1
        success = outcomes.get(EvaluationOutcome.SUCCESS.value, 0)
        inconclusive = outcomes.get(EvaluationOutcome.INCONCLUSIVE.value, 0)
        # 主口径：全部有效已尝试任务（含 INCONCLUSIVE）
        main_rate = round(success / total, 3) if total else 0.0
        # 参考口径：仅可判定样本
        decidable = total - inconclusive
        conditional_rate = round(success / decidable, 3) if decidable else 0.0
        return {
            "total_tasks": total,
            "attempted": total,
            "success": success,
            "task_success_rate": main_rate,
            "inconclusive": inconclusive,
            "conditional_success_rate_on_decidable": conditional_rate,
            "outcomes": outcomes,
            "regressions": sum(1 for r in self.records if r.regression_detected),
            "total_tokens": sum(r.run_result.token_usage.get("total", 0)
                                for r in self.records if r.run_result),
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


def _repo_dir(clean_root: Path) -> Path:
    """合成仓库 provider 生成的是根目录本身（含 .git）。"""
    if (clean_root / ".git").exists():
        return clean_root
    subdirs = [d for d in clean_root.iterdir() if d.is_dir()]
    return subdirs[0] if subdirs else clean_root
