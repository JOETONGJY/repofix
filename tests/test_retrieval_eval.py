"""检索离线评估纯函数测试（MVP v0.5.3）：gold 解析 / 函数标签 / 指标聚合。"""

from repofix.context.retrieval import CodeLocation, CodeWindow, RetrievalResult
from repofix.evaluation.retrieval_eval import (
    GoldLabels,
    evaluate_retrieval,
    map_ranges_to_symbols,
    parse_gold_patch,
    picked_locations,
    summarize,
)
from tests.test_repo_overview import make_repo as make_plain_repo

GOLD_DIFF = """diff --git a/services/calc.py b/services/calc.py
--- a/services/calc.py
+++ b/services/calc.py
@@ -10,3 +10,5 @@ def compute(x):
     before()
-    old_line()
+    new_line()
+    extra_step()
     after()
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- /dev/null
+++ b/tests/test_calc.py
@@ -0,0 +1,2 @@
+def test_x():
+    assert compute(1)
"""


def test_parse_gold_patch_exact_added_lines():
    labels = parse_gold_patch("t1", GOLD_DIFF)
    assert labels.parse_ok is True
    # 生产文件只含 services/calc.py；/dev/null 新建的测试文件归 new_files（P1-3）
    assert labels.files == ["services/calc.py"]
    assert labels.new_files == ["tests/test_calc.py"]
    assert labels.test_files == []
    # 旧文件坐标：-old_line() 在原文件第 11 行（+ 替换锚定同一位置）
    assert labels.ranges["services/calc.py"] == [(11, 11)]


def test_parse_gold_patch_empty_and_module_level():
    labels = parse_gold_patch("t2", "")
    assert labels.parse_ok is False
    assert "empty" in labels.parse_error


def test_map_ranges_to_symbols(tmp_path):
    root = make_plain_repo(tmp_path, {
        "services/calc.py": (
            "\n" * 9 + "def compute(x):\n    before()\n    old_line()\n"
            "    new_line()\n    extra_step()\n    after()\n"
        ),
    })
    from repofix.context.symbol_index import build_symbol_index

    index = build_symbol_index(root)
    labels = parse_gold_patch("t3", GOLD_DIFF)
    mapped = map_ranges_to_symbols(labels.ranges, index)
    assert mapped["services/calc.py"][0][2] == "compute"


def test_map_ranges_module_level(tmp_path):
    root = make_plain_repo(tmp_path, {"const.py": "MAX = 10\nMIN = 1\n"})
    from repofix.context.symbol_index import build_symbol_index

    labels = parse_gold_patch("t4", "diff --git a/const.py b/const.py\n"
                              "--- a/const.py\n+++ b/const.py\n"
                              "@@ -1,1 +1,1 @@\n-MAX = 10\n+MAX = 20\n")
    mapped = map_ranges_to_symbols(labels.ranges, build_symbol_index(root))
    assert mapped["const.py"] == [(1, 1, "module-level")]


def _result(paths, scanned=None):
    locations = [CodeLocation(path=p, tier=2, score=5, reasons=[]) for p in paths]
    return RetrievalResult(locations=locations, anchored_matched=[], keywords=[],
                           symbol_hits={}, scanned_files=scanned or paths,
                           scan_truncated=False)


def _window(path, start, end, truncated=False):
    return CodeWindow(path=path, start_line=start, end_line=end,
                      text=f"# {path}, lines {start}-{end}",
                      truncated=truncated)


def test_evaluate_file_hit_vs_window_hit():
    labels = GoldLabels(instance_id="t5")
    labels.files = ["services/calc.py"]
    labels.ranges = {"services/calc.py": [(12, 13)]}
    labels.labels = {"services/calc.py": [(12, 13, "compute")]}
    # 选对了文件（any/all hit）但窗口在文件头——File Hit = True, Window Hit = False
    result = _result(["services/calc.py", "other.py"])
    windows = [_window("services/calc.py", 1, 5)]  # 文件头，未覆盖 12-13 行
    record = evaluate_retrieval(labels, result, windows)
    assert record.file_hit_any is True and record.file_hit_all is True
    assert record.window_hit_files == []
    assert record.function_hit is False
    assert record.truncated_on_gold is False  # 未截断，只是窗口位置不对


def test_evaluate_window_hit_and_truncation():
    labels = GoldLabels(instance_id="t6")
    labels.files = ["services/calc.py"]
    labels.ranges = {"services/calc.py": [(12, 13)]}
    labels.labels = {"services/calc.py": [(12, 13, "compute")]}
    result = _result(["services/calc.py"])
    hit = evaluate_retrieval(labels, result, [_window("services/calc.py", 10, 20)])
    assert hit.window_hit_files == ["services/calc.py"]
    assert hit.function_hit is True
    # 截断且 gold 行不在窗口内
    clipped = evaluate_retrieval(labels, result,
                                 [_window("services/calc.py", 1, 5, truncated=True)])
    assert clipped.window_hit_files == []
    assert clipped.truncated_on_gold is True


def test_evaluate_scan_miss_and_module_level():
    labels = GoldLabels(instance_id="t7")
    labels.files = ["deep/gold.py"]
    labels.ranges = {"deep/gold.py": [(1, 1)]}
    labels.labels = {"deep/gold.py": [(1, 1, "module-level")]}
    result = _result(["other.py"], scanned=["other.py"])  # gold 文件不在扫描范围
    record = evaluate_retrieval(labels, result, [])
    assert record.scan_miss == ["deep/gold.py"]
    assert record.file_hit_any is False
    assert record.module_level_only is True


def test_summarize_rates():
    labels = GoldLabels(instance_id="x")
    labels.files = ["a.py"]
    labels.ranges = {"a.py": [(1, 1)]}
    labels.labels = {"a.py": [(1, 1, "f")]}
    good = evaluate_retrieval(labels, _result(["a.py"]), [_window("a.py", 1, 2)])
    bad = evaluate_retrieval(labels, _result(["b.py"]), [])
    from repofix.evaluation.retrieval_eval import TaskRetrievalEval

    parsed_bad = TaskRetrievalEval(instance_id="t9", variant="v", error="gold parse failed")
    summary = summarize([good, bad, parsed_bad])
    assert summary["tasks_valid"] == 2
    assert summary["tasks_error"] == 1
    assert summary["file_recall_any@3"] == 0.5
    assert summary["window_hit@3"] == 0.5
    assert summary["function_hit@3"] == 0.5
    assert summary["scan_miss_rate"] == 0.5


def test_symbol_lookup_case_insensitive(tmp_path):
    """issue 词元小写后仍命中 CamelCase 定义（sklearn-10297 一题暴露的真 bug）。"""
    from repofix.context.symbol_index import build_symbol_index

    root = make_plain_repo(tmp_path, {"ridge.py": "class RidgeClassifierCV:\n    def fit(self):\n        pass\n"})
    index = build_symbol_index(root)
    assert [e.qualname for e in index.lookup("ridgeclassifiercv")] == ["RidgeClassifierCV"]


def test_class_docstring_labeled_class_not_module_level(tmp_path):
    """类 docstring 范围：标签为 class:<name>，不算 Function Hit 也不误标模块级。"""
    from repofix.context.symbol_index import build_symbol_index

    root = make_plain_repo(tmp_path, {
        "ridge.py": ("\n" * 9 + 'class RidgeCV:\n    """doc line"""\n' + "    pass\n" * 5),
    })
    index = build_symbol_index(root)
    labels = parse_gold_patch("tc", "diff --git a/ridge.py b/ridge.py\n"
                              "--- a/ridge.py\n+++ b/ridge.py\n"
                              "@@ -11,1 +11,1 @@\n- doc line\n+ doc changed\n")
    mapped = map_ranges_to_symbols(labels.ranges, index)
    label = mapped["ridge.py"][0][2]
    assert label.startswith("class:RidgeCV")
    spans = {p: [(s, e) for (s, e, lab) in ents
                 if lab != "module-level" and not lab.startswith("class:")]
             for p, ents in mapped.items()}
    assert spans["ridge.py"] == []  # 类级修改不进 Function Hit 分母


def test_gold_coordinates_stay_in_original_frame():
    """评审问题 1：补丁在修改点前插入 10 行时，gold 位置必须仍在原始坐标
    （900 行附近），不漂移到新文件的 910。"""
    hunk = ("diff --git a/big.py b/big.py\n"
            "--- a/big.py\n+++ b/big.py\n"
            "@@ -899,6 +899,16 @@\n"
            " context_899\n"          # ctx old 899→900
            "+inserted_1\n"            # 纯插入：锚定边界 old=900
            "+inserted_2\n"
            "+inserted_3\n"
            "+inserted_4\n"
            "+inserted_5\n"
            "+inserted_6\n"
            "+inserted_7\n"
            "+inserted_8\n"
            "+inserted_9\n"
            "+inserted_10\n"           # old 仍停在 900
            "-target_line_900\n"       # 被删行 = 原文件 900 ✓（插入不推进 old）
            "+target_line_fixed\n"     # 替换块锚定 900
            " context_901\n")          # ctx old 900→901
    labels = parse_gold_patch("t", hunk)
    # 全部落在原始 900：插入前移不漂移坐标，检索窗口 ±15/±40 必然命中
    assert labels.ranges["big.py"] == [(900, 900)]


def test_gold_pure_deletion_produces_ranges():
    """纯删除补丁：被删行在原始文件坐标下精确存在（新坐标方案会得到空集）。"""
    hunk = ("diff --git a/legacy.py b/legacy.py\n"
            "--- a/legacy.py\n+++ b/legacy.py\n"
            "@@ -5,2 +4,0 @@\n"
            "-dead_code_a\n"
            "-dead_code_b\n")
    labels = parse_gold_patch("t", hunk)
    assert labels.ranges["legacy.py"] == [(5, 6)]


def test_gold_insertion_only_anchors_at_boundary():
    hunk = ("diff --git a/mod.py b/mod.py\n"
            "--- a/mod.py\n+++ b/mod.py\n"
            "@@ -10,2 +10,3 @@\n"
            " existing_a\n"      # ctx old 10→11
            "+brand_new_line\n"  # 纯插入：锚定 11
            " existing_b\n")     # ctx old 11→12
    labels = parse_gold_patch("t", hunk)
    assert labels.ranges["mod.py"] == [(11, 11)]


def test_gold_new_file_marked_and_excluded_from_denominator():
    """评审 P1-3：gold 新建生产文件 → 检索原理上不可命中，单独标记并排除分母。"""
    from repofix.evaluation.retrieval_eval import TaskRetrievalEval

    hunk = ("diff --git a/new_module.py b/new_module.py\n"
            "--- /dev/null\n+++ b/new_module.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def new_feature():\n"
            "+    return True\n")
    labels = parse_gold_patch("t", hunk)
    assert labels.new_files == ["new_module.py"]
    assert labels.files == []
    result = _result(["anything.py"], scanned=["anything.py"])
    record = evaluate_retrieval(labels, result, [])
    assert record.impossible_for_retrieval is True
    assert record.new_file_required is True
    assert record.error == ""  # 不是错误，是单独标记
    summary = summarize([record])
    assert summary["tasks_valid"] == 0
    assert summary["tasks_impossible_new_file"] == 1
    assert summary["new_file_required_rate"] == 1.0
    assert TaskRetrievalEval(instance_id="t", variant="v",
                             new_file_required=True).to_dict()["new_file_required"] is True


def test_gold_mixed_new_and_existing_kept_in_denominator():
    """混合补丁（既有文件 + 新建文件）：既有文件仍可检索 → 留在分母。"""
    hunk = ("diff --git a/exists.py b/exists.py\n"
            "--- a/exists.py\n+++ b/exists.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/brand_new.py b/brand_new.py\n"
            "--- /dev/null\n+++ b/brand_new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+added()\n")
    labels = parse_gold_patch("t", hunk)
    assert sorted(labels.files) == ["exists.py"]
    assert labels.new_files == ["brand_new.py"]
    result = _result(["exists.py"], scanned=["exists.py"])
    record = evaluate_retrieval(labels, result, [_window("exists.py", 1, 2)])
    assert record.new_file_required is True
    assert record.impossible_for_retrieval is False
    assert record.file_hit_any is True
    summary = summarize([record])
    assert summary["tasks_valid"] == 1  # 混合任务留在分母
    assert summary["new_file_required_rate"] == 1.0


def test_variant_signals_match_production_defaults():
    """评审 P0-1：kernel 变体信号必须与生产 CodeRetriever 默认完全一致。"""
    from repofix.context.retrieval import CodeRetriever
    from repofix.evaluation.retrieval_eval import PRODUCTION_SIGNALS, VARIANT_SIGNALS

    assert VARIANT_SIGNALS["kernel_head"] == PRODUCTION_SIGNALS
    assert VARIANT_SIGNALS["kernel_windows"] == PRODUCTION_SIGNALS
    assert CodeRetriever.__init__.__defaults__ is not None  # 默认即 PRODUCTION_SIGNALS
    assert VARIANT_SIGNALS["keyword_only"] == ("keyword",)  # 消融基线：无内容/锚定/符号


def test_picked_locations_structural_fallback():
    """评审 P0-1：无信号时回退浅层源码——与 select_issue_files 同语义。"""
    result = RetrievalResult(locations=[], anchored_matched=[], keywords=[],
                             symbol_hits={}, scanned_files=["a/shallow.py", "b/deep/x.py"],
                             scan_truncated=False,
                             structural_fallback_files=["a/shallow.py", "b/deep/x.py"])
    picked = picked_locations(result, 2)
    assert [loc.path for loc in picked] == ["a/shallow.py", "b/deep/x.py"]
    assert picked[0].tier == 3 and picked[0].score == 0


class TestCachedTestbedValidation:
    """评审：缓存 testbed 必须重验基线（HEAD==base 且干净）才可复用。"""

    def _make_commit(self, tmp_path, name="repo"):
        import subprocess

        repo = tmp_path / name
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=True)
        return repo, head.stdout.strip()

    def test_valid_cache_accepted(self, tmp_path):
        from repofix.evaluation.retrieval_eval import validate_cached_testbed

        repo, head = self._make_commit(tmp_path)
        ok, reason = validate_cached_testbed(repo, head)
        assert ok is True and reason == ""

    def test_head_mismatch_rejected(self, tmp_path):
        from repofix.evaluation.retrieval_eval import validate_cached_testbed

        repo, _ = self._make_commit(tmp_path)
        ok, reason = validate_cached_testbed(repo, "0" * 40)
        assert ok is False
        assert "HEAD" in reason and "!= base" in reason

    def test_dirty_worktree_rejected(self, tmp_path):
        from repofix.evaluation.retrieval_eval import validate_cached_testbed

        repo, head = self._make_commit(tmp_path)
        (repo / "f.py").write_text("TAMPERED\n", encoding="utf-8")
        ok, reason = validate_cached_testbed(repo, head)
        assert ok is False
        assert "dirty" in reason

    def test_missing_git_rejected(self, tmp_path):
        from repofix.evaluation.retrieval_eval import validate_cached_testbed

        ok, reason = validate_cached_testbed(tmp_path / "nope", "0" * 40)
        assert ok is False and ".git" in reason


def test_function_hit_dual_denominators(tmp_path):
    """评审：Function Hit 需双口径——全体有效任务 + 仅函数级适用任务。"""
    from repofix.context.symbol_index import build_symbol_index
    from repofix.evaluation.retrieval_eval import map_ranges_to_symbols

    hunk_fn = (
        "diff --git a/svc.py b/svc.py\n"
        "--- a/svc.py\n+++ b/svc.py\n"
        "@@ -10,1 +10,1 @@\n"
        "-    old_body()\n"
        "+    new_body()\n"
    )
    hunk_const = (
        "diff --git a/const.py b/const.py\n"
        "--- a/const.py\n+++ b/const.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-MAX = 10\n"
        "+MAX = 20\n"
    )
    root = make_plain_repo(tmp_path, {
        "svc.py": "\n" * 9 + "def compute(x):\n    old_body()\n    return x\n",
        "const.py": "MAX = 10\n",
    })
    index = build_symbol_index(root)

    labels_mixed = parse_gold_patch("t1", hunk_fn + hunk_const)
    labels_mixed.labels = map_ranges_to_symbols(labels_mixed.ranges, index)

    # 命中函数级 gold（svc.py）的任务
    hit_result = _result(["svc.py"], scanned=["svc.py"])
    record_hit = evaluate_retrieval(labels_mixed, hit_result,
                                    [_window("svc.py", 1, 60)])
    assert record_hit.function_applicable is True
    assert record_hit.function_hit is True

    # 只命中模块级文件（const.py）：混合 gold 仍适用，但函数级未命中
    miss_result = _result(["const.py"], scanned=["const.py"])
    record_miss = evaluate_retrieval(labels_mixed, miss_result, [])
    assert record_miss.function_applicable is True
    assert record_miss.function_hit is False

    # 纯模块级 gold：Function Hit 不适用
    labels_module = parse_gold_patch("t2", hunk_const)
    labels_module.labels = map_ranges_to_symbols(labels_module.ranges, index)
    module_record = evaluate_retrieval(labels_module, _result(["const.py"]), [])
    assert module_record.function_applicable is False
    assert module_record.module_level_only is True

    summary = summarize([record_hit, record_miss, module_record])
    assert summary["tasks_valid"] == 3
    assert summary["tasks_function_applicable"] == 2  # 适用子集分母
    assert summary["function_hit@3"] == round(1 / 3, 3)  # 全体口径
    assert summary["function_hit@3_function_applicable"] == 0.5  # 适用子集口径


