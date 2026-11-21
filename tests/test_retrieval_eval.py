"""检索离线评估纯函数测试（MVP v0.5.3）：gold 解析 / 函数标签 / 指标聚合。"""

from repofix.context.retrieval import CodeLocation, CodeWindow, RetrievalResult
from repofix.evaluation.retrieval_eval import (
    GoldLabels,
    evaluate_retrieval,
    map_ranges_to_symbols,
    parse_gold_patch,
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
    # 生产文件只含 services/calc.py；测试文件单独归类
    assert labels.files == ["services/calc.py"]
    assert labels.test_files == ["tests/test_calc.py"]
    # 精确到新增行：+new_line() 与 +extra_step() 在新文件的第 12、13 行
    assert labels.ranges["services/calc.py"] == [(11, 12)]  # 新行号从 hunk 头 10 起算


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
