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
