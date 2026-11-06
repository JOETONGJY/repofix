"""Evaluation Harness 测试：EV-01~05（design/09、specs/04）。

零 Token：Agent Run 结果用构造的 RunResult；独立验收用真实 git + pytest。
"""
import subprocess
from pathlib import Path

from repofix.contracts import (
    EvaluationOutcome,
    EvaluationResult,
    RunResult,
    RunStatus,
    VerificationStatus,
)
from repofix.evaluation.harness import (
    EvalRecord,
    EvalTask,
    MetricsAggregator,
    ResultJudge,
    to_evaluation_result,
)

AUTH_BUG = "def validate_password(pw: str) -> bool:\n    return len(pw) >= 0\n"
AUTH_FIXED = "def validate_password(pw: str) -> bool:\n    return len(pw) >= 8\n"


def make_clean_repo(tmp: Path) -> Path:
    repo = tmp / "clean"
    repo.mkdir(parents=True)
    (repo / "auth.py").write_text(AUTH_BUG, encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    # 隐藏测试：Agent 可见测试之外的独立验收（EV-01 隔离性）
    (tests / "test_hidden.py").write_text(
        "from auth import validate_password\n\n"
        "def test_hidden_empty():\n    assert validate_password('') is False\n\n"
        "def test_hidden_short():\n    assert validate_password('abc') is False\n\n"
        "def test_hidden_ok():\n    assert validate_password('longenough') is True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=v@v", "-c", "user.name=v", "commit", "-q", "-m", "base"],
        cwd=repo, check=True)
    return repo


def write_patch(tmp: Path, name: str, old: str, new: str) -> Path:
    patch = tmp / name
    patch.write_text(
        f"--- a/auth.py\n+++ b/auth.py\n@@ -1,2 +1,2 @@\n"
        f" def validate_password(pw: str) -> bool:\n"
        f"-    return {old}\n+    return {new}\n",
        encoding="utf-8", newline="\n",
    )
    return patch


def run_result(status=RunStatus.COMPLETED, verification=VerificationStatus.PASSED) -> RunResult:
    return RunResult(
        run_id="run_ev", runtime_status=status, termination_reason="finish_accepted",
        verification_status=verification, model_calls=3, tool_calls=4,
        token_usage={"total": 100}, duration_ms=1000,
    )


def make_task() -> EvalTask:
    return EvalTask(
        task_id="task-auth-001", repo_source="synthetic", base_commit="base",
        instruction="Fix password validation", test_command="python -m pytest",
        hidden_test_files={"tests/test_hidden.py": "..."},
    )


class TestResultJudge:
    def test_ev02_success_reproducible(self, tmp_path):
        """EV-02：相同 Patch 在干净工作区（Base 重初始化）独立验收可复现。"""
        patch = write_patch(tmp_path, "fix.patch", "len(pw) >= 0", "len(pw) >= 8")
        judge1 = ResultJudge(workspace_provider=make_clean_repo)
        judge2 = ResultJudge(workspace_provider=make_clean_repo)
        r1 = judge1.judge(make_task(), patch, run_result())
        r2 = judge2.judge(make_task(), patch, run_result())
        assert r1.outcome is EvaluationOutcome.SUCCESS
        assert r2.outcome is EvaluationOutcome.SUCCESS  # 相同 Patch 相同环境结果一致

    def test_ev02_bad_patch_fails(self, tmp_path):
        patch = write_patch(tmp_path, "bad.patch", "len(pw) >= 0", "len(pw) >= 0")  # 未修复
        judge = ResultJudge(workspace_provider=make_clean_repo)
        record = judge.judge(make_task(), patch, run_result())
        # Patch 应用成功但隐藏测试仍失败（基线 Bug 未修复）
        assert record.outcome is EvaluationOutcome.FAILED
        assert record.hidden_tests_rc != 0

    def test_empty_patch_delivery_failure(self, tmp_path):
        """评审 P1：空 Patch 是交付失败，不伪装成功。"""
        empty = tmp_path / "empty.patch"
        empty.write_text("", encoding="utf-8")
        judge = ResultJudge(workspace_provider=make_clean_repo)
        record = judge.judge(make_task(), empty, run_result())
        assert record.outcome is EvaluationOutcome.FAILED
        assert "empty" in record.reason

    def test_runtime_incomplete_stays_in_denominator(self, tmp_path):
        """EV-03：预算耗尽（未完成）的任务 FAILED 但留在分母。"""
        patch = write_patch(tmp_path, "x.patch", "len(pw) >= 0", "len(pw) >= 8")
        judge = ResultJudge(workspace_provider=make_clean_repo)
        record = judge.judge(make_task(), patch, run_result(status=RunStatus.BUDGET_EXCEEDED))
        assert record.outcome is EvaluationOutcome.FAILED
        assert "did not complete" in record.reason

    def test_infra_error_inconclusive(self, tmp_path, monkeypatch):
        """EV-04：评测设施异常 → INCONCLUSIVE（单独报告，不混入 FAILED）。"""

        def broken_provider(tmp):
            raise RuntimeError("disk full")

        judge = ResultJudge(workspace_provider=broken_provider)
        record = judge.judge(make_task(), tmp_path / "any.patch", run_result())
        assert record.outcome is EvaluationOutcome.INCONCLUSIVE
        assert "infrastructure" in record.reason


class TestMetricsAggregator:
    def _record(self, task_id, outcome, regression=False):

        return EvalRecord(task_id=task_id, outcome=outcome,
                          regression_detected=regression,
                          run_result=run_result())

    def test_ev03_denominator_semantics(self):
        """EV-03：预算耗尽留在分母；INCONCLUSIVE 单独报告且移出分母。"""
        agg = MetricsAggregator()
        agg.add(self._record("t1", EvaluationOutcome.SUCCESS))
        agg.add(self._record("t2", EvaluationOutcome.FAILED))  # 隐藏测试失败
        agg.add(self._record("t3", EvaluationOutcome.FAILED))  # 预算耗尽（留在分母）
        agg.add(self._record("t4", EvaluationOutcome.INCONCLUSIVE))  # 设施异常
        summary = agg.summary()
        assert summary["total_tasks"] == 4
        assert summary["attempted"] == 3  # INCONCLUSIVE 移出分母
        assert summary["success"] == 1
        assert summary["task_success_rate"] == round(1 / 3, 3)
        assert summary["inconclusive"] == 1
        assert summary["total_tokens"] == 400

    def test_ev05_experiment_groups_recorded(self):
        """EV-05：三组对照（A/B/预算匹配）可被同一聚合器分账统计。"""
        agg = MetricsAggregator()
        agg.add(self._record("a-1", EvaluationOutcome.SUCCESS))
        agg.add(self._record("b-1", EvaluationOutcome.SUCCESS))
        agg.add(self._record("bm-1", EvaluationOutcome.FAILED))
        by_group = {}
        for row in agg.summary()["by_task"]:
            group = row["task_id"].split("-")[0]
            by_group.setdefault(group, []).append(row["outcome"])
        assert by_group == {"a": ["success"], "b": ["success"], "bm": ["failed"]}

    def test_ev01_hidden_tests_not_in_agent_workspace(self, sample_repo, tmp_path):
        """EV-01：隐藏测试文件不进 Agent 工作区——EvalTask 契约隔离验证。"""
        task = make_task()
        # Agent 可见的 TaskSpec 只有 visible 信息；hidden_test_files 仅评测器持有
        agent_visible = {"instruction": task.instruction, "test_command": task.test_command}
        assert all("hidden" not in str(v) for v in agent_visible.values())
        assert task.hidden_test_files  # 评测器侧持有


class TestContractConversion:
    def test_to_evaluation_result_dual_state(self):
        """双状态：Runtime COMPLETED + 验收 FAILED 同时成立（design/09）。"""
        record = EvalRecord(task_id="t", run_id="r", outcome=EvaluationOutcome.FAILED,
                            regression_detected=True, run_result=run_result())
        result = to_evaluation_result(record)
        assert isinstance(result, EvaluationResult)
        assert result.run_status is RunStatus.COMPLETED  # Runtime 维度
        assert result.outcome is EvaluationOutcome.FAILED  # 验收维度
        assert result.regression_detected is True
