# ruff: noqa
"""临时调试：judge 降级路径收集 rc=5 问题。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from repofix.contracts import RunResult, RunStatus  # noqa: E402
from repofix.evaluation.harness import ResultJudge  # noqa: E402
from tests.test_evaluation import make_clean_repo, make_task, run_result, write_patch  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="dbg-judge-"))
repo, head = make_clean_repo(tmp)
patch = write_patch(tmp, "fix.patch", "len(pw) >= 0", "len(pw) >= 8")
task = make_task(head)

captured = {}


class SpyJudge(ResultJudge):
    def _run_hidden_tests(self, repo):
        import subprocess

        from repofix.evaluation.harness import _repo_dir

        rd = _repo_dir(repo)
        print("hidden files exist:", (rd / "tests" / "test_hidden.py").exists())
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no", "--collect-only"],
            cwd=rd, capture_output=True, text=True,
            env={"PATH": __import__("os").environ.get("PATH", ""),
                 "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
                 "PYTHONPATH": str(rd)},
        )
        print("collect rc:", proc.returncode)
        print("collect out:", proc.stdout[:200])
        print("collect err:", proc.stderr[:200])
        captured["rd"] = rd
        return super()._run_hidden_tests(repo)


judge = SpyJudge(workspace_provider=lambda tmp: repo)
record = judge.judge(task, patch, run_result(), clean_provider=lambda tmp: repo)
print("outcome:", record.outcome.value, "| rc:", record.hidden_tests_rc)
import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)
