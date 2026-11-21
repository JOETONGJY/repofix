"""共享 Code Retrieval Layer 测试（MVP v0.5.1）。

覆盖：分层排序键（锚定 tier 严格优先于关键词累计）、符号名命中层、
消费者扫描范围保留（Overview 200/3 vs Test Agent 500/6）、内容匹配统一、
reasons 记录、确定性、兼容性（select_issue_files 返回形状 / Overview
既有排序语义 / 信号裁剪变体）。
"""
import pytest

from repofix.context.retrieval import (
    OVERVIEW_SCOPE,
    TEST_AGENT_SCOPE,
    TIER_ANCHORED,
    TIER_KEYWORD,
    TIER_SYMBOL,
    CodeRetriever,
)
from tests.test_repo_overview import make_repo as make_plain_repo

BILLING_FILES = {
    "api/orders.py": (
        "from services.calculator import calculate_total\n"
        "from cache.order_cache import get_cached_total\n\n"
        "def update_order(order_id):\n"
        "    return calculate_total(order_id)\n"
    ),
    "services/calculator.py": (
        "class Calculator:\n"
        "    def calculate_total(self, order_id):\n"
        "        return 0\n"
    ),
    "cache/order_cache.py": (
        "class OrderCache:\n"
        "    def get_cached_total(self, order_id):\n"
        "        return 0\n"
    ),
    "tests/test_orders.py": "def test_update_order():\n    assert True\n",
}


@pytest.fixture
def billing_repo(tmp_path):
    return make_plain_repo(tmp_path, BILLING_FILES)


class TestTierOrdering:
    def test_anchor_beats_accumulated_keywords(self, tmp_path):
        """多个关键词累计得分 > +25 也不能压过锚定文件（评审约定 2）。"""
        root = make_plain_repo(tmp_path, {
            # 关键词重灾区：issue 大量词元都命中它的文件名/目录
            "order/total/price.py": "x = 1\n",
            # 只被 traceback 锚定
            "cache/store.py": "x = 1\n",
        })
        issue = (
            "order total price order total price order total calculation broken\n"
            'File "cache/store.py", line 2, in helper'
        )
        result = CodeRetriever(root, TEST_AGENT_SCOPE).rank(issue)
        assert result.locations[0].path == "cache/store.py"
        assert result.locations[0].tier == TIER_ANCHORED
        assert all(loc.tier >= TIER_KEYWORD for loc in result.locations[1:])

    def test_symbol_tier_between_anchor_and_keyword(self, billing_repo):
        """issue 提及行为名（文件名不含该词）：符号层命中排在纯关键词层之前。"""
        issue = "get_cached_total returns stale value after update_order"
        result = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank(issue)
        # 两个行为名的定义文件都进符号层（update_order → api/orders.py 同样命中）
        cache_loc = next(loc for loc in result.locations if loc.path == "cache/order_cache.py")
        orders_loc = next(loc for loc in result.locations if loc.path == "api/orders.py")
        assert cache_loc.tier == TIER_SYMBOL
        assert orders_loc.tier == TIER_SYMBOL
        assert cache_loc.symbol == "get_cached_total"
        assert orders_loc.symbol == "update_order"
        # 层间保证：任何关键词层候选都在全部符号层候选之后
        last_symbol_idx = max(i for i, loc in enumerate(result.locations)
                              if loc.tier == TIER_SYMBOL)
        first_keyword_idx = next((i for i, loc in enumerate(result.locations)
                                  if loc.tier == TIER_KEYWORD), len(result.locations))
        assert last_symbol_idx < first_keyword_idx
        assert any(loc.tier == TIER_KEYWORD for loc in result.locations)  # 内容命中的测试文件

    def test_symbol_match_requires_exact_name(self, billing_repo):
        """子串不算符号命中（order 不命中 update_order）——降噪。"""
        result = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank("order")
        assert all("symbol match" not in r for loc in result.locations for r in loc.reasons)


class TestConsumerScopes:
    def test_deep_file_reachable_by_test_agent_scope_only(self, tmp_path):
        """评审约定 1：统一算法不缩小召回面——深层文件 Overview 扫描不到、
        Test Agent 范围能到。"""
        deep_rel = "/".join(["lvl"] * 5 + ["deep_mod.py"])
        root = make_plain_repo(tmp_path, {deep_rel: "def deep_fix():\n    pass\n"})
        issue = "deep_fix is broken"
        overview = CodeRetriever(root, OVERVIEW_SCOPE).rank(issue)
        agent = CodeRetriever(root, TEST_AGENT_SCOPE).rank(issue)
        assert overview.locations == []  # 深度 3 扫不到
        assert agent.locations and agent.locations[0].path == deep_rel

    def test_scope_dataclass_defaults(self):
        assert (OVERVIEW_SCOPE.max_files, OVERVIEW_SCOPE.max_depth) == (200, 3)
        assert (TEST_AGENT_SCOPE.max_files, TEST_AGENT_SCOPE.max_depth) == (500, 6)


class TestKernelSignals:
    def test_reasons_recorded_per_candidate(self, billing_repo):
        issue = 'File "cache/order_cache.py", line 2, in get_cached_total'
        result = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank(issue)
        cache_loc = next(loc for loc in result.locations if loc.path == "cache/order_cache.py")
        assert any(r.startswith("anchored cache/order_cache.py:2") for r in cache_loc.reasons)
        assert cache_loc.line == 2
        orders_loc = next(loc for loc in result.locations if loc.path == "api/orders.py")
        assert orders_loc.reasons  # 每个候选都有入选理由（content/path keywords）

    def test_content_matching_in_kernel_for_both_consumers(self, tmp_path):
        """统一内核：内容匹配（+1）对两个消费者同规则——此前只有 Overview 有。"""
        root = make_plain_repo(tmp_path, {
            "src/unrelated_name.py": "# handles widget frobnication\n",
        })
        issue = "widget frobnication fails"
        result = CodeRetriever(root, TEST_AGENT_SCOPE).rank(issue)
        assert result.locations, "内容命中应产生候选"
        loc = result.locations[0]
        assert any("content keywords" in r for r in loc.reasons)
        assert loc.tier == TIER_KEYWORD

    def test_signal_variant_keyword_only(self, billing_repo):
        """v0.5.3 离线评估变体：仅关键词（不含锚定/符号）。"""
        issue = 'File "cache/order_cache.py", line 2, in get_cached_total'
        result = CodeRetriever(billing_repo, TEST_AGENT_SCOPE,
                               signals=("keyword",)).rank(issue)
        assert all(loc.tier == TIER_KEYWORD for loc in result.locations)
        assert result.anchored_matched == []

    def test_deterministic_ranking(self, billing_repo):
        issue = 'File "cache/order_cache.py", line 2, in get_cached_total'
        one = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank(issue)
        two = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank(issue)
        assert [(loc.path, loc.tier, loc.score) for loc in one.locations] == \
            [(loc.path, loc.tier, loc.score) for loc in two.locations]

    def test_structural_fallback_shallow_first(self, billing_repo):
        fallback = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).structural_fallback(3)
        assert fallback[0] == "api/orders.py"  # 浅层优先
        assert len(fallback) == 3

    def test_nonexistent_root_raises(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            CodeRetriever(tmp_path / "nope")


class TestConsumerCompat:
    def test_select_issue_files_shape_and_anchor_priority(self, billing_repo):
        """兼容：返回形状不变；(path, content) 的 path 首个为锚定文件。"""
        from repofix.test_agent.generator import select_issue_files

        issue = 'File "cache/order_cache.py", line 2, in get_cached_total'
        snippets = select_issue_files(billing_repo, issue, max_files=3)
        assert 1 <= len(snippets) <= 3
        assert snippets[0][0] == "cache/order_cache.py"
        assert "OrderCache" in snippets[0][1]

    def test_select_issue_files_fallback_without_signals(self, billing_repo):
        from repofix.test_agent.generator import select_issue_files

        snippets = select_issue_files(billing_repo, "", max_files=3)
        assert [path for path, _ in snippets] == ["api/orders.py", "cache/order_cache.py",
                                                  "services/calculator.py"]

    def test_overview_candidates_via_kernel(self, billing_repo):
        """Overview 委托内核后行为不变：锚定文件仍第一、标注仍在。"""
        from repofix.context.repo_overview import build_repo_overview

        issue = 'File "cache/order_cache.py", line 2, in get_cached_total'
        text = build_repo_overview(billing_repo, issue_text=issue)
        candidates = text.split("Candidate files:")[1].split("Notes:")[0]
        first = [line.strip() for line in candidates.strip().splitlines() if line.strip()][0]
        assert first == "cache/order_cache.py"
        assert "symbols: OrderCache (class, 1), OrderCache.get_cached_total() (2)" in text
        assert "Anchored locations (from the issue):" in text

    def test_overview_no_candidates_falls_back(self, tmp_path):
        from repofix.context.repo_overview import build_repo_overview

        root = make_plain_repo(tmp_path, {"a/shallow.py": "x = 1\n", "b/deep/mod.py": "x = 1\n"})
        text = build_repo_overview(root, issue_text="")
        candidates = text.split("Candidate files:")[1].split("Notes:")[0]
        first = [line.strip() for line in candidates.strip().splitlines() if line.strip()][0]
        assert first == "a/shallow.py"


# ---- v0.5.2：代码窗口 ----

from repofix.context.retrieval import (  # noqa: E402
    CodeLocation,
    CodeWindow,
    build_code_windows,
)


class TestCodeWindows:
    def _index(self, root):
        from repofix.context.symbol_index import build_symbol_index

        return build_symbol_index(root)

    def test_traceback_line_900_lands_in_window(self, tmp_path):
        """深层 traceback（第 1001 行）必须拿到对应源码，而不是文件头。"""
        body = "\n".join(f"filler_{i} = {i}" for i in range(1, 1000))
        root = make_plain_repo(tmp_path, {
            "big.py": f"{body}\n\ndef target_function():\n    return 'BUG HERE'\n",
        })
        location = CodeLocation(path="big.py", tier=0, score=25, line=1001,
                                anchor_kind="traceback",
                                reasons=["anchored big.py:1001 (traceback)"])
        window = build_code_windows(root, [location],
                                    max_window_chars=20000)[0]
        assert window.start_line <= 1001 <= window.end_line
        assert "target_function" in window.text
        assert "BUG HERE" in window.text
        assert "filler_1" not in window.text  # 文件头不再混入

    def test_symbol_window_uses_ast_range(self, tmp_path):
        """AST 命中：窗口覆盖完整函数范围（end_line）。"""
        root = make_plain_repo(tmp_path, {
            "svc.py": ("\n" * 9 + "def wide(a, b):\n" + "    x = a\n" * 10 +
                       "    return x\n"),
        })
        location = CodeLocation(path="svc.py", tier=1, score=8, symbol="wide",
                                reasons=["symbol match: wide"])
        window = build_code_windows(root, [location], index=self._index(root))[0]
        assert window.start_line == 10
        assert window.end_line == 21  # def + 10 行体 + return
        assert "def wide(a, b):" in window.text
        assert "return x" in window.text
        assert "symbol wide()" in window.reason or "symbol" in window.reason

    def test_test_failure_window_targets_test_function(self, tmp_path):
        """失败身份：窗口定位到测试函数本身（fixture 上下文的低成本近似）。"""
        root = make_plain_repo(tmp_path, {
            "tests/test_calc.py": ("def test_add_two():\n    assert add(1, 1) == 2\n"
                                   "\n\ndef other():\n    pass\n"),
        })
        location = CodeLocation(path="tests/test_calc.py", tier=0, score=25,
                                anchor_kind="test_failure", test_name="test_add_two",
                                reasons=["anchored (test_failure)"])
        window = build_code_windows(root, [location], index=self._index(root))[0]
        assert window.start_line == 1
        assert "assert add(1, 1) == 2" in window.text

    def test_overlapping_windows_merged(self, tmp_path):
        root = make_plain_repo(tmp_path, {"m.py": "\n".join(f"line_{i}" for i in range(1, 61))})
        locations = [
            CodeLocation(path="m.py", tier=0, score=25, line=10, anchor_kind="traceback"),
            CodeLocation(path="m.py", tier=0, score=25, line=15, anchor_kind="traceback"),
        ]
        windows = build_code_windows(root, locations)
        assert len(windows) == 1  # 合并为一个窗口
        assert windows[0].start_line == 1  # 10-40 与 15-55 重叠（含 -40 前向）

    def test_char_budget_clip_marks_truncated(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "long.py": "def huge():\n" + "    x = 1  # padded line content here\n" * 400,
        })
        location = CodeLocation(path="long.py", tier=1, score=8, symbol="huge",
                                reasons=["symbol match: huge"])
        window = build_code_windows(root, [location], index=self._index(root),
                                    max_window_chars=800)[0]
        assert window.truncated is True
        assert "window clipped" in window.text
        assert window.text.index("def huge():") < window.text.index("clipped")

    def test_anchor_line_beyond_eof_falls_back_to_head(self, tmp_path):
        root = make_plain_repo(tmp_path, {"small.py": "a = 1\nb = 2\n"})
        location = CodeLocation(path="small.py", tier=0, score=25, line=9999,
                                anchor_kind="traceback")
        window = build_code_windows(root, [location])[0]
        assert window.start_line == 1
        assert "beyond EOF" in window.reason
        assert "a = 1" in window.text

    def test_line_numbers_match_file_content(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "m.py": "\n".join(f"content_{i}" for i in range(1, 31)),
        })
        location = CodeLocation(path="m.py", tier=0, score=25, line=20,
                                anchor_kind="traceback")
        window = build_code_windows(root, [location], max_window_chars=5000)[0]
        assert "20\tcontent_20" in window.text
        assert window.text.splitlines()[0].startswith("# m.py, lines ")

    def test_total_budget_drops_later_windows(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "a.py": "x = 1\n" * 100, "b.py": "y = 2\n" * 100, "c.py": "z = 3\n" * 100,
        })
        locations = [CodeLocation(path=p, tier=0, score=25, line=50, anchor_kind="traceback")
                     for p in ("a.py", "b.py", "c.py")]
        windows = build_code_windows(root, locations, max_total_chars=900,
                                     max_window_chars=5000)
        assert len(windows) < 3  # 预算耗尽后丢弃后续窗口
        assert sum(len(w.text) for w in windows) <= 900 + 200  # 允许头注释开销

    def test_select_issue_files_returns_windows(self, tmp_path):
        """Test Agent 片段：traceback 行号直取窗口（v0.5.2 核心验收）。"""
        from repofix.test_agent.generator import select_issue_files

        body = "\n".join(f"filler_{i} = {i}" for i in range(1, 1000))
        root = make_plain_repo(tmp_path, {
            "big.py": f"{body}\n\ndef target_function():\n    return 'BUG HERE'\n",
        })
        issue = 'order total stale\nFile "big.py", line 1001, in target_function'
        snippets = select_issue_files(root, issue, max_files=2)
        assert snippets[0][0] == "big.py"
        assert "target_function" in snippets[0][1]
        assert "BUG HERE" in snippets[0][1]

    def test_window_render_type(self):
        window = CodeWindow(path="p.py", start_line=1, end_line=2, text="# p.py")
        assert isinstance(window, CodeWindow)


class TestSymbolNoiseGuards:
    """sklearn-10297 一题暴露的两类符号层噪声（评审后修复）。"""

    def test_generic_token_dropped_when_defined_in_many_files(self, tmp_path):
        root = make_plain_repo(tmp_path, {f"m{i}/mod.py": "def fit(self):\n    pass\n"
                                          for i in range(7)})
        result = CodeRetriever(root, TEST_AGENT_SCOPE).rank("fit is broken")
        assert "fit" not in result.symbol_hits

    def test_specific_token_still_hits(self, billing_repo):
        result = CodeRetriever(billing_repo, TEST_AGENT_SCOPE).rank("get_cached_total fails")
        assert "get_cached_total" in result.symbol_hits

    def test_dunder_tokens_never_hit(self, tmp_path):
        root = make_plain_repo(tmp_path, {f"m{i}/cls.py": "class C:\n    def __init__(self):\n        pass\n"
                                          for i in range(3)})
        result = CodeRetriever(root, TEST_AGENT_SCOPE).rank("__init__ is broken")
        assert result.symbol_hits == {}


class TestIndexSourceFirst:
    def test_budget_tight_source_files_win_over_docs(self, tmp_path):
        """预算只计 .py 且源码树优先——doc//examples/ 最后进索引。"""
        from repofix.context.symbol_index import build_symbol_index

        files = {
            "doc/guide.py": "def doc_helper():\n    pass\n",
            "examples/demo.py": "def demo_helper():\n    pass\n",
            "src/core.py": "def core_helper():\n    pass\n",
            "src/util.py": "def util_helper():\n    pass\n",
            "src/extra.py": "def extra_helper():\n    pass\n",
        }
        root = make_plain_repo(tmp_path, files)
        index = build_symbol_index(root, max_files=3)  # 预算 3 个 .py
        assert index.symbols_in_file("src/core.py")  # 源码先进
        assert index.symbols_in_file("src/util.py")
        assert index.symbols_in_file("src/extra.py")
        assert index.symbols_in_file("doc/guide.py") == []  # 预算被源码占满
        assert index.truncated is True


def test_content_score_capped(tmp_path):
    """内容匹配封顶：文本肥大的 vendor 文件不能再靠几十个撞词压过 gold 文件。"""
    root = make_plain_repo(tmp_path, {
        "vendor/pyparsing_like.py": " ".join(["parameter", "numpy", "import", "size",
                                              "alphas", "fit", "cv", "values",
                                              "store", "error", "database"]) * 3 + "\n",
        "src/real_fix_target.py": "class RidgeClassifierCV:\n    pass\n",
    })
    result = CodeRetriever(root, TEST_AGENT_SCOPE).rank(
        "store_cv_values parameter error on RidgeClassifierCV")
    target = next(loc for loc in result.locations if "real_fix_target" in loc.path)
    vendor = next(loc for loc in result.locations if "vendor" in loc.path)
    assert target.score > vendor.score  # 符号层命中应稳压封顶后的内容分
    assert target.tier == TIER_SYMBOL
