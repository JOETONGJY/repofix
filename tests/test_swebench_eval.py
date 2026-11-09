"""SWE-bench Mini-Eval 纯函数单测（不含 Docker / 网络依赖）。"""
import json

import pytest

from repofix.evaluation.swebench import dataset as sweb_dataset
from repofix.evaluation.swebench.dataset import (
    parse_test_list,
    patch_files,
)
from repofix.evaluation.swebench.judge import (
    JudgeRecord,
    extract_eval_commands,
    parse_short_summary,
)
from repofix.evaluation.swebench.plan import (
    derive_visible_plan,
    extract_identifiers,
    is_test_file,
    visible_plan_argv,
)
from repofix.evaluation.swebench.selection import (
    MAX_PER_REPO,
    candidate_metadata,
    funnel,
    select_candidates,
    stats,
    write_selection_json,
)


def _row(**overrides) -> dict:
    base = {
        "instance_id": "repo__pkg-1",
        "repo": "a/pkg",
        "base_commit": "abc123",
        "log_parser": "parse_log_pytest",
        "patch": "diff --git a/f.py b/f.py\n+++ b/f.py\n",
        "test_patch": "",
        "problem_statement": "x" * 500,
        "FAIL_TO_PASS": '["tests/test_a.py::test_one"]',
        "PASS_TO_PASS": '["tests/test_a.py::test_two"]',
        "image": "swebench/sweb.eval.x86_64.a_1776_pkg-1:latest",
    }
    base.update(overrides)
    return base


# ---- dataset 纯函数 ----

def test_parse_test_list_json_string():
    assert parse_test_list('["a", "b"]') == ["a", "b"]


def test_parse_test_list_malformed_returns_empty():
    assert parse_test_list("not json") == []
    assert parse_test_list(None) == []
    assert parse_test_list('{"a": 1}') == []


def test_patch_files_dedup_and_prefix_strip():
    patch = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "diff --git a/y.py b/y.py\n+++ b/y.py\n"
    )
    assert patch_files(patch) == ["x.py", "y.py"]


def test_patch_files_ignores_dev_null():
    patch = "--- /dev/null\n+++ b/new.py\n"
    assert patch_files(patch) == ["new.py"]


def test_test_patch_files_delegates():
    assert sweb_dataset.test_patch_files("--- /dev/null\n+++ b/tests/test_n.py\n") == ["tests/test_n.py"]


# ---- selection 规则 ----

def test_candidate_metadata_passes_valid_row():
    meta = candidate_metadata(_row())
    assert meta is not None
    assert meta["gold_file"] == "f.py"
    assert meta["fail_to_pass"] == ["tests/test_a.py::test_one"]


@pytest.mark.parametrize("override", [
    {"log_parser": "parse_log_django"},
    {"log_parser": "parse_log_sympy"},
    {"patch": "--- a/f.py\n+++ b/f.py\n--- a/g.py\n+++ b/g.py\n"},
    {"FAIL_TO_PASS": "[]"},
    {"FAIL_TO_PASS": json.dumps([f"t{i}" for i in range(11)])},
    {"PASS_TO_PASS": json.dumps([f"t{i}" for i in range(301)])},
    {"problem_statement": "short"},
    {"problem_statement": "x" * 9000},
])
def test_candidate_metadata_rejects_rule_violations(override):
    assert candidate_metadata(_row(**override)) is None


def test_select_candidates_is_deterministic_and_diverse():
    rows = []
    for repo in ("a/pkg", "b/pkg", "c/pkg"):
        for i in range(3):
            rows.append(_row(instance_id=f"{repo.replace('/', '_')}-{i}", repo=repo))
    first = select_candidates(rows, target=4)
    second = select_candidates(rows, target=4)
    assert first == second  # 确定性
    assert len(first) == 4
    repos = [m["repo"] for m in first]
    # 多样性：任何仓库不超过 MAX_PER_REPO 道；候选充足的仓库优先各取一道
    assert all(repos.count(r) <= MAX_PER_REPO for r in set(repos))
    assert len(set(repos)) == 3


def test_select_candidates_fills_from_larger_repos_first():
    rows = [_row(instance_id=f"big-{i}", repo="big/pkg") for i in range(5)]
    rows += [_row(instance_id="small-0", repo="small/pkg")]
    picked = select_candidates(rows, target=3)
    repos = [m["repo"] for m in picked]
    assert repos.count("big/pkg") == 2 and repos.count("small/pkg") == 1


def test_stats_and_freeze_roundtrip(tmp_path):
    rows = [_row(instance_id="repo__pkg-1")]
    picked = select_candidates(rows, target=1)
    s = stats(picked)
    assert s["rules_version"] == "selection_rules_v1"
    assert s["total_candidates"] == 1
    out = write_selection_json(tmp_path / "selection.json", picked, picked[0], [], "sha-test")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["formal"][0]["instance_id"] == "repo__pkg-1"
    assert payload["rehearsal"]["instance_id"] == "repo__pkg-1"
    assert payload["environment_blocked"] == []
    assert payload["dataset"]["commit"] == "sha-test"


# ---- judge 纯函数 ----

def test_parse_short_summary_section_scoped():
    output = (
        "some pytest header output\n"
        "PASSED should_not_be_counted_outside_section\n"  # 区段外的 PASSED 行不应误收
        "=========================== short test summary info ============================\n"
        "PASSED tests/test_a.py::test_one\n"
        "FAILED tests/test_a.py::test_two - AssertionError: boom\n"
        "SKIPPED [1] tests/test_a.py::test_three\n"
        "================= 1 failed, 2 passed, 1 skipped in 0.5s =================\n"
    )
    statuses = parse_short_summary(output)
    assert statuses["tests/test_a.py::test_one"] == "PASSED"
    assert statuses["tests/test_a.py::test_two"] == "FAILED"
    assert "should_not_be_counted_outside_section" not in statuses


def test_parse_short_summary_parametrized_ids():
    output = (
        "=========================== short test summary info ============================\n"
        "PASSED lib/matplotlib/tests/test_matplotlib.py::test_parse_to_version_info[3.5.0-version_tuple0]\n"
        "FAILED lib/x.py::test_y[a-b] - ValueError: x - y\n"
    )
    statuses = parse_short_summary(output)
    parametrized = ("lib/matplotlib/tests/test_matplotlib.py"
                    "::test_parse_to_version_info[3.5.0-version_tuple0]")
    assert statuses[parametrized] == "PASSED"
    # ' - ' 以首个为界；参数化 id 中的 '-' 不截断
    assert statuses["lib/x.py::test_y[a-b]"] == "FAILED"


def test_parse_short_summary_fallback_without_header():
    output = "PASSED tests/test_a.py::test_one\nERROR tests/test_b.py\n"
    statuses = parse_short_summary(output)
    assert statuses == {"tests/test_a.py::test_one": "PASSED", "tests/test_b.py": "ERROR"}


def test_extract_eval_commands_installs_and_tests():
    script = (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "source /opt/miniconda3/bin/activate\n"
        "conda activate testbed\n"
        "cd /testbed\n"
        "python -m pip install -e .\n"
        "python -m pip install -v --no-use-pep517 --no-build-isolation -e .\n"
        "git checkout abc123 tests/test_x.py\n"
        ": '>>>>> Start Test Output'\n"
        "pytest -rA tests/test_x.py\n"
        ": '>>>>> End Test Output'\n"
    )
    installs, tests = extract_eval_commands(script)
    assert installs == [
        "python -m pip install -e .",
        "python -m pip install -v --no-use-pep517 --no-build-isolation -e .",
    ]
    assert tests == ["pytest -rA tests/test_x.py"]


def test_extract_eval_commands_no_tests_found():
    installs, tests = extract_eval_commands("python -m pip install -e .\n")
    assert len(installs) == 1 and tests == []


def test_judge_record_defaults_are_honest():
    record = JudgeRecord(instance_id="x")
    assert record.outcome == "INCONCLUSIVE"
    assert record.isolated is True
    assert record.f2p_failures == []


# ---- workspace / plan 纯函数 ----

def test_visible_plan_argv_matches_command_whitelist():
    from repofix.workspace.docker_workspace import check_command_allowed

    argv = visible_plan_argv(["tests/test_x.py"])
    assert check_command_allowed(argv)
    assert argv[:3] == ["python", "-m", "pytest"]


def test_is_test_file():
    assert is_test_file("tests/test_a.py")
    assert is_test_file("pkg/testing/test_b.py")
    assert is_test_file("tests/unittest_c.py")
    assert is_test_file("tests/conftest.py")  # 目录级分类：属测试套件，非源码
    assert not is_test_file("pkg/core.py")


def test_extract_identifiers_prefers_backticks_and_filters_noise():
    issue = "Bug in `store_cv_values` with RidgeCV:\n```\nsome code\n```\nThe `self` and error are noise."
    tokens = extract_identifiers(issue)
    assert "store_cv_values" in tokens
    assert "RidgeCV" in tokens
    assert "self" not in tokens and "error" not in tokens


def _init_mini_repo(tmp_path):
    """构造合成仓库：pkg/core.py + 同级 tests 与根 tests。"""
    import subprocess

    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "core.py").write_text("def do_thing():\n    return 1\n")
    (tmp_path / "pkg" / "tests" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "tests" / "test_core.py").write_text("from pkg.core import do_thing\n")
    (tmp_path / "pkg" / "util.py").write_text("def helper():\n    return 2\n")
    (tmp_path / "tests" / "test_util.py").write_text("import pkg.util\n")
    (tmp_path / "tests" / "test_other.py").write_text("def test_x(): pass\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=tmp_path, check=True,
    )
    return tmp_path


def test_derive_visible_plan_l1_sibling_test(tmp_path):
    testbed = _init_mini_repo(tmp_path)
    argv, source, detail = derive_visible_plan("Fix `core` module behavior", testbed)
    assert source == "issue_scoped"
    assert "pkg/tests/test_core.py" in detail["targets"]
    assert detail["layer"] == "L1"
    assert argv[:3] == ["python", "-m", "pytest"]


def test_derive_visible_plan_l3_content_reference(tmp_path):
    testbed = _init_mini_repo(tmp_path)
    # util 无同名测试；tests/test_util.py 内容 import pkg.util → L3 命中
    argv, source, detail = derive_visible_plan("helper in `util` is wrong", testbed)
    assert source == "issue_scoped"
    assert detail["layer"] in ("L2", "L3")
    assert "tests/test_util.py" in detail["targets"]


def test_derive_visible_plan_empty_when_no_match(tmp_path):
    testbed = _init_mini_repo(tmp_path)
    argv, source, detail = derive_visible_plan("zzzqqq unrelated words only", testbed)
    assert source == "empty" and argv == []


def test_derive_visible_plan_ignores_hidden_test_info(tmp_path):
    """规则输入只有 problem_statement + 仓库结构——函数签名不接受任何隐藏字段。"""
    import inspect

    from repofix.evaluation.swebench import plan as plan_mod

    params = inspect.signature(plan_mod.derive_visible_plan).parameters
    assert list(params) == ["problem_statement", "testbed"]


def test_selection_funnel_counts_rejections():
    rows = [
        _row(instance_id="ok-1"),
        _row(instance_id="bad-parser", log_parser="parse_log_django"),
        _row(instance_id="multi-file", patch="--- a/f.py\n+++ b/f.py\n--- a/g.py\n+++ b/g.py\n"),
        _row(instance_id="too-long", problem_statement="x" * 9000),
    ]
    result = funnel(rows)
    assert result["input"] == 4 and result["kept"] == 1
    assert result["rejected_by_rule"]["R1_repo_parser"] == 1
    assert result["rejected_by_rule"]["R2_single_file"] == 1
    assert result["rejected_by_rule"]["R5_problem_length"] == 1


def test_collection_interrupt_blocks_silent_success():
    """收集中断时，缺席测试不得被 silent-success 记为通过（假阳性守卫）。"""
    output = (
        "ImportError while loading conftest '/testbed/lib/matplotlib/tests/conftest.py'.\n"
        "=========================== short test summary info ============================\n"
        "ERROR lib/matplotlib/tests/test_matplotlib.py\n"
        "!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!\n"
    )
    statuses = parse_short_summary(output)
    assert statuses == {"lib/matplotlib/tests/test_matplotlib.py": "ERROR"}
    # F2P 缺席 + 收集中断 → 必须判失败，不得静默通过
    collection_interrupted = "errors during collection" in output
    assert collection_interrupted


def test_build_task_stack_passes_policy_to_both_layers(tmp_path, monkeypatch):
    """评审 P0-1 回归防护：评测入口必须把 command_policy 同时传给 Dispatcher 与
    DockerWorkspace——Workspace 默认 strict，漏传会让 Dispatcher 放行的命令在
    执行层被打回（优化"存在于代码"却"未在链路启用"）。"""
    import repofix.evaluation.swebench.runner as eval_runner
    from repofix.contracts import RunConfig

    class FakeCli:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            class R:
                returncode, stdout, stderr = 0, "", ""
            return R()

        def remove_container(self, *a, **k):
            pass

    monkeypatch.setattr(eval_runner, "DockerCli", FakeCli)
    monkeypatch.setattr(eval_runner, "build_agent_image", lambda *a, **k: "img")
    monkeypatch.setattr(eval_runner, "start_eval_container", lambda *a, **k: None)
    monkeypatch.setattr(eval_runner, "probe_python_env",
                        lambda *a, **k: {"rg_available": True})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-no-calls")
    (tmp_path / "tb").mkdir()

    for policy in ("strict", "coding_benchmark"):
        config = RunConfig(model_id="m", command_policy=policy, review_enabled=False)
        runner, store, workspace, cli, container, _img = eval_runner.build_task_stack(
            instance_id="x", run_id=f"r-{policy}", image="img", base_commit="abc",
            problem_statement="p", visible_plan_argv=[], testbed=tmp_path / "tb",
            run_dir=tmp_path / f"run-{policy}", config=config, rg_path=tmp_path / "rg",
            preflight_summary={}, installs=None,
        )
        dispatcher = runner._scheduler._dispatcher  # noqa: SLF001
        assert workspace.command_policy == policy
        assert dispatcher.command_policy == policy
        store.close()


def test_effective_preflight_summary_uses_runconfig_not_script_args():
    """P2 回归：写入 Trace 的档位/窗口值必须取自 RunConfig 本身。

    冒烟脚本曾声明"实际生效的档位写入 PREFLIGHT_COMPLETED"，但它从不运行
    AgentRunner 主循环（该事件由 runner.run() 生成）——声明不成立，已从脚本
    移除。真正可在此验证的一步是摘要组装：Trace 记录的值来自 RunConfig，
    而不是脚本启动参数的副本。
    """
    from repofix.contracts import RunConfig
    from repofix.evaluation.swebench.runner import effective_preflight_summary

    config = RunConfig(model_id="m", command_policy="coding_benchmark",
                       context_recent_full_turns=6, review_enabled=False)
    summary = effective_preflight_summary({"steps": "stage0-validated"}, config)
    assert summary["command_policy"] == "coding_benchmark"
    assert summary["context_recent_full_turns"] == 6
    assert summary["steps"] == "stage0-validated"  # 既有字段保留

    # 默认值 = v0 行为（strict + 关闭窗口），不能被悄悄改写
    default_summary = effective_preflight_summary(
        {}, RunConfig(model_id="m", review_enabled=False))
    assert default_summary["command_policy"] == "strict"
    assert default_summary["context_recent_full_turns"] == 0
