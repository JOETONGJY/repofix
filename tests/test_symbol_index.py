"""AST Symbol Index + Traceback-Aware Retrieval 测试（MVP v0.5）。

覆盖：符号提取（class/method/function/嵌套限定名）、import 解析（绝对/
from/相对）、lookup 与 imported_by、有界与健壮性（语法错误/二进制/超大/
敏感文件）、确定性；锚定信号提取（traceback/失败身份/路径提及）与仓库
匹配（精确/后缀/前缀/歧义丢弃）；Overview 锚定段落 + 候选提升 + 符号标注；
find_symbol 工具（命中/无命中/ REVIEWER 视图）。
"""
import pytest

from repofix.context.issue_signals import (
    AnchoredLocation,
    extract_anchored_locations,
    match_anchored_to_repo,
)
from repofix.context.repo_overview import build_repo_overview
from repofix.context.symbol_index import build_symbol_index
from repofix.contracts import AgentActor
from repofix.tools.registry import ToolRegistry, register_default_tools
from tests.test_repo_overview import make_repo as make_plain_repo


@pytest.fixture
def billing_repo(tmp_path):
    """关键词相关 ≠ 行为相关的经典场景：Bug 在被 import 的缓存模块。"""
    return make_plain_repo(tmp_path, {
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
    })


class TestSymbolIndex:
    def test_definitions_extracted_with_qualname_and_line(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "pkg/service.py": (
                "class OrderService:\n"
                "    def update_total(self):\n"
                "        pass\n\n"
                "    async def refresh(self):\n"
                "        pass\n\n"
                "def top_level():\n"
                "    pass\n"
            ),
        })
        index = build_symbol_index(root)
        entries = index.lookup("update_total")
        assert [(e.qualname, e.kind, e.line) for e in entries] == \
            [("OrderService.update_total", "method", 2)]
        refresh = index.lookup("refresh")
        assert refresh[0].kind == "method"
        top = index.lookup("top_level")
        assert [(e.qualname, e.kind) for e in top] == [("top_level", "function")]

    def test_lookup_supports_qualified_name_and_suffix(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "a.py": "class Foo:\n    def bar(self):\n        pass\n",
            "b.py": "def bar():\n    pass\n",
        })
        index = build_symbol_index(root)
        assert len(index.lookup("bar")) == 2  # 短名：两处定义都返回
        assert [e.qualname for e in index.lookup("Foo.bar")] == ["Foo.bar"]
        assert [e.qualname for e in index.lookup("Foo")] == ["Foo"]

    def test_import_edges_absolute_from_and_relative(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "pkg/mod.py": "VALUE = 1\n",
            "pkg/sub/deep.py": "X = 2\n",
            "pkg/inner.py": "from pkg.mod import VALUE\n",
            "pkg/uses_relative.py": (
                "from .mod import VALUE\n"
                "from .sub.deep import X\n"
                "import pkg.mod\n"
            ),
        })
        index = build_symbol_index(root)
        assert "pkg/mod.py" in index.imports["pkg/inner.py"]
        rel_imports = index.imports["pkg/uses_relative.py"]
        assert "pkg/mod.py" in rel_imports
        assert "pkg/sub/deep.py" in rel_imports
        assert "pkg/inner.py" in index.imported_by_of("pkg/mod.py")

    def test_imported_by_populated(self, billing_repo):
        index = build_symbol_index(billing_repo)
        assert index.imported_by_of("cache/order_cache.py") == ["api/orders.py"]
        assert "api/orders.py" in index.imported_by_of("services/calculator.py")

    def test_deterministic_output(self, billing_repo):
        one = build_symbol_index(billing_repo)
        two = build_symbol_index(billing_repo)
        assert one.lookup("get_cached_total") == two.lookup("get_cached_total")
        assert one.entry_files() == two.entry_files()

    def test_symbols_in_file_sorted_by_line(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "m.py": "def b():\n    pass\n\n\ndef a():\n    pass\n",
        })
        index = build_symbol_index(root)
        assert [e.name for e in index.symbols_in_file("m.py")] == ["b", "a"]

    def test_syntax_error_and_binary_skipped(self, tmp_path):
        root = make_plain_repo(tmp_path, {
            "broken.py": "def broken(:\n",
            "data.bin": "\x00\x01binary",
            "ok.py": "def fine():\n    pass\n",
        })
        index = build_symbol_index(root)
        assert index.files_skipped_syntax == 1
        assert index.lookup("fine")[0].file == "ok.py"
        assert index.lookup("broken") == []

    def test_max_files_truncation_flagged(self, tmp_path):
        files = {f"m_{i:02d}.py": f"def f{i}():\n    pass\n" for i in range(10)}
        root = make_plain_repo(tmp_path, files)
        index = build_symbol_index(root, max_files=5)
        assert index.truncated is True
        assert index.files_indexed <= 5

    def test_sensitive_files_not_indexed(self, tmp_path):
        secret = "SECRETVALUE"
        root = make_plain_repo(tmp_path, {
            "secrets.py": f"KEY = '{secret}'\n",
            "ok.py": "def run():\n    pass\n",
        })
        index = build_symbol_index(root)
        assert index.symbols_in_file("secrets.py") == []
        assert index.lookup("run")[0].file == "ok.py"

    def test_nonexistent_root_raises(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            build_symbol_index(tmp_path / "nope")


class TestIssueSignals:
    def test_traceback_extraction_with_line(self):
        issue = 'Traceback (most recent call last):\n  File "cache/order_cache.py", line 6, in get_cached_total'
        locations = extract_anchored_locations(issue)
        assert locations[0].path == "cache/order_cache.py"
        assert locations[0].line == 6
        assert locations[0].kind == "traceback"

    def test_pytest_failure_identity(self):
        issue = "FAILED tests/test_orders.py::test_update_total - assert 0 == 40"
        locations = extract_anchored_locations(issue)
        entry = next(loc for loc in locations if loc.kind == "test_failure")
        assert entry.path == "tests/test_orders.py"
        assert entry.test_name == "test_update_total"

    def test_bare_path_and_backslash_normalized(self):
        locations = extract_anchored_locations("see src\\api\\orders.py for details")
        assert locations[0].path == "src/api/orders.py"
        assert locations[0].kind == "path_mention"

    def test_dedupe_and_order(self):
        issue = ('File "a.py", line 1, in x\n'
                 'File "a.py", line 1, in x\n'
                 "also tests/test_a.py::test_x")
        locations = extract_anchored_locations(issue)
        assert len([loc for loc in locations if loc.path == "a.py"]) == 1
        assert locations[0].key() != locations[-1].key() or len(locations) >= 2

    def test_cap_at_twenty(self):
        issue = " ".join(f'File "f{i}.py", line 1, in x' for i in range(30))
        assert len(extract_anchored_locations(issue)) == 20

    def test_match_exact_suffix_and_prefix(self, billing_repo):
        from repofix.context.repo_overview import scan_repo_files

        rel_paths, _ = scan_repo_files(billing_repo, 200, 3)
        locations = [
            AnchoredLocation(path="cache/order_cache.py", line=6, kind="traceback"),
            AnchoredLocation(path="order_cache.py", line=None, kind="path_mention"),
            AnchoredLocation(path="api/orders.py", line=None, kind="path_mention"),
            AnchoredLocation(path="nonexistent/xyz.py", line=None, kind="path_mention"),
        ]
        matched = match_anchored_to_repo(locations, rel_paths)
        targets = {f for _, f in matched}
        assert "cache/order_cache.py" in targets
        assert "nonexistent/xyz.py" not in targets
        # order_cache.py 唯一后缀命中；orders.py 在 api/ 下唯一命中
        assert len([1 for _, f in matched if f.endswith("order_cache.py")]) >= 1


class TestOverviewIntegration:
    def test_anchored_file_ranks_first_despite_keywords(self, billing_repo):
        """Issue 提订单更新（关键词指向 orders/calculator），traceback 锚定缓存模块
        ——锚定必须赢过关键词（关键词相关 ≠ 行为相关）。"""
        issue = (
            "order total sometimes stale after update_order\n"
            'File "cache/order_cache.py", line 4, in get_cached_total'
        )
        text = build_repo_overview(billing_repo, issue_text=issue)
        candidates = text.split("Candidate files:")[1].split("Notes:")[0]
        first = [line.strip() for line in candidates.strip().splitlines() if line.strip()][0]
        assert first == "cache/order_cache.py"
        assert "Anchored locations (from the issue):" in text
        assert "cache/order_cache.py:4 (traceback)" in text

    def test_candidate_symbol_annotations_present(self, billing_repo):
        issue = 'File "cache/order_cache.py", line 4, in get_cached_total'
        text = build_repo_overview(billing_repo, issue_text=issue)
        assert "symbols: OrderCache (class, 1), OrderCache.get_cached_total() (2)" in text
        assert "imported by: api/orders.py" in text
        assert "anchored:4 (traceback)" in text

    def test_no_anchored_section_without_signals(self, billing_repo):
        text = build_repo_overview(billing_repo, issue_text="something is broken")
        assert "Anchored locations" not in text

    def test_symbol_index_injection_respected(self, billing_repo):

        prebuilt = build_symbol_index(billing_repo)
        text = build_repo_overview(billing_repo, symbol_index=prebuilt)
        assert "symbols:" in text  # 使用注入的索引而非重建


class TestFindSymbolTool:
    def _workspace(self, repo_root, tmp_path):
        from repofix.workspace.docker_workspace import DockerWorkspace
        from tests.conftest import FakeDockerCli

        return DockerWorkspace(repo_root, "run_sym", cli=FakeDockerCli())

    def test_lookup_returns_definitions_and_importers(self, billing_repo):
        ws = self._workspace(billing_repo, None)
        result = ws.find_symbol(name="get_cached_total")
        assert result.status.value == "succeeded"
        assert "OrderCache.get_cached_total()  cache/order_cache.py:2 (method)" in result.content
        assert "imported by: api/orders.py" in result.content

    def test_no_match_is_success_empty_result(self, billing_repo):
        ws = self._workspace(billing_repo, None)
        result = ws.find_symbol(name="does_not_exist_anywhere")
        assert result.status.value == "succeeded"
        assert "no symbol matched" in result.content

    def test_limit_bounds_output(self, tmp_path):
        files = {f"m{i}.py": "def dup():\n    pass\n" for i in range(6)}
        ws = self._workspace(make_plain_repo(tmp_path, files), None)
        result = ws.find_symbol(name="dup", limit=3)
        assert result.content.count("dup()") == 3

    def test_registry_view_and_dispatch(self, billing_repo):
        registry = ToolRegistry()
        register_default_tools(registry)
        assert registry.is_allowed(AgentActor.MAIN, "find_symbol")
        assert registry.is_allowed(AgentActor.REVIEWER, "find_symbol")
        ws = self._workspace(billing_repo, None)
        result = ws.execute("find_symbol", {"name": "update_order", "limit": 5})
        assert "update_order" in result.content

    def test_dispatcher_end_to_end(self, billing_repo):
        """完整校验链：存在性 → 权限 → Schema → 执行。"""
        from repofix.contracts import ToolCall
        from repofix.tools.dispatcher import ToolDispatcher

        registry = ToolRegistry()
        register_default_tools(registry)
        ws = self._workspace(billing_repo, None)
        dispatcher = ToolDispatcher(registry, ws)
        call = ToolCall(call_id="c1", tool_name="find_symbol",
                        arguments={"name": "calculate_total", "limit": 10},
                        raw_arguments='{"name": "calculate_total"}')
        result = dispatcher.execute(call)
        assert result.status.value == "succeeded"
        assert "calculate_total" in result.content
