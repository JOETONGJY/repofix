"""共享 Code Retrieval Layer（MVP v0.5.1 / v0.5.2）。

统一 Overview 与 Test Agent 此前两处平行的候选评分实现（GPT 评审 v0.5.1）：
- 统一评分内核：锚定 > 符号名 > 文件名/路径 > 内容，每个候选记录入选理由；
- **分层排序键 (tier, -score, path)**：明确 traceback 的文件一定排在任何
  仅关键词命中的文件之前（多个关键词累计可超过 +25，单靠分数无法保证
  严格优先——评审约定 2）；层内按分数竞争；
- **扫描范围按消费者配置**（评审约定 1）：Overview 200 文件/深度 3，
  Test Agent 500 文件/深度 6；内核只统一算法，不缩小任何一方的召回面；
- 评分信号可裁剪（v0.5.3 离线评估的 A/B 变体复用同一内核）；
- 代码窗口（v0.5.2）：按 traceback 行号 / AST 函数范围截取，合并重叠、
  尊重字符预算，无可靠位置时回退文件头并显式记录回退原因。

边界：纯确定性、不执行仓库代码、敏感文件不读取、不跟随越界符号链接；
索引按消费者重建（亚秒级）——find_symbol 的"每次重建保证新鲜"语义不变。
"""
from dataclasses import dataclass, field
from pathlib import Path

from repofix.context.issue_signals import AnchoredLocation, extract_anchored_locations, match_anchored_to_repo
from repofix.context.repo_overview import (
    _TEXT_CONTENT_SUFFIXES,
    _read_bounded,
    basename_of,
    is_sensitive_path,
    scan_repo_files,
    tokenize_issue,
)
from repofix.context.symbol_index import SymbolIndex, build_symbol_index

# —— 证据分层（排序键第一维；层间严格隔离，层内分数竞争）——
TIER_ANCHORED = 0  # Issue 白给定位：traceback / 失败身份 / 路径提及
TIER_SYMBOL = 1  # Issue 词元命中 AST 符号定义名（行为名相关但文件名不相关）
TIER_KEYWORD = 2  # 文件名/目录/内容关键词命中
TIER_STRUCTURAL = 3  # 无信号回退（浅层优先，仅消费者显式请求时生成）

SYMBOL_TOKEN_MAX_FILES = 5  # 定义散布超过此文件数 = 泛化符号（fit/model 类），不作为信号
KEYWORD_CONTENT_MAX = 3  # 内容匹配是最弱信号：贡献封顶（sklearn-10297 实证：无上限时
# vendored 文件靠几十个英文词撞分压过真正的 gold 文件）

ANCHOR_SCORE = 25
SYMBOL_SCORE = 8
KEYWORD_STEM_SCORE = 5
KEYWORD_DIR_SCORE = 2
KEYWORD_CONTENT_SCORE = 1

# —— 消费者扫描范围（评审约定 1：统一算法、保留各自的召回面）——


@dataclass
class RetrievalScope:
    max_files: int = 200
    max_depth: int = 3


OVERVIEW_SCOPE = RetrievalScope(200, 3)
TEST_AGENT_SCOPE = RetrievalScope(500, 6)

# —— 代码窗口（v0.5.2）——
WINDOW_CONTEXT_LINES = 40  # 锚定行号上下的窗口半径
HEAD_FALLBACK_LINES = 60  # 无可靠位置时的文件头回退行数
MERGE_GAP_LINES = 5  # 同文件窗口间隙 ≤ 此值时合并
DEFAULT_MAX_WINDOW_CHARS = 6000
DEFAULT_MAX_TOTAL_CHARS = 18000


@dataclass
class CodeLocation:
    """一个候选位置及其证据（reasons 供诊断与离线评估）。"""

    path: str
    tier: int
    score: int
    reasons: list[str] = field(default_factory=list)
    line: int | None = None  # 锚定行号（traceback 提供；其余为 None）
    anchor_kind: str = ""  # traceback / test_failure / path_mention
    test_name: str = ""  # 失败身份的测试名（test_failure 时非空）
    symbol: str | None = None  # 命中的符号名（窗口提取用）


@dataclass
class RetrievalResult:
    locations: list[CodeLocation]
    anchored_matched: list[tuple[AnchoredLocation, str]]
    keywords: list[str]
    symbol_hits: dict[str, list[str]]  # token → 定义所在文件
    scanned_files: list[str]
    scan_truncated: bool


class CodeRetriever:
    """按仓库基线状态构建的多信号候选检索器（确定性；无 LLM）。"""

    def __init__(self, repo_root, scope: RetrievalScope | None = None, *,
                 index: SymbolIndex | None = None, signals=("anchor", "symbol", "keyword")):
        root = Path(repo_root).resolve()  # 相对路径会让 scan/index 的 rel 计算全部失败
        if not root.is_dir():
            raise NotADirectoryError(f"repository root does not exist: {root}")
        self._root = root
        self._scope = scope or OVERVIEW_SCOPE
        self._signals = frozenset(signals)
        self._rel_paths, self._scan_truncated = scan_repo_files(
            root, self._scope.max_files, self._scope.max_depth)
        self._index = index if index is not None else build_symbol_index(root)
        self._py_candidates = [
            rel for rel in self._rel_paths
            if rel.endswith(".py") and not is_sensitive_path(rel)
            and basename_of(rel) not in ("conftest.py", "setup.py")
        ]
        self._content_cache: dict[str, str] = {}

    # ---- 公开属性 ----

    @property
    def index(self) -> SymbolIndex:
        return self._index

    @property
    def scanned_files(self) -> list[str]:
        return list(self._rel_paths)

    @property
    def scan_truncated(self) -> bool:
        return self._scan_truncated

    def structural_fallback(self, top_k: int) -> list[str]:
        """无信号时的确定性回退：浅层源码优先（两个消费者的既有语义）。"""
        return sorted(self._py_candidates, key=lambda rel: (rel.count("/"), rel))[:top_k]

    # ---- 评分与排序 ----

    def rank(self, issue_text: str) -> RetrievalResult:
        """多信号评分 + 分层排序。locations 只含有效信号（score>0）的候选。"""
        keywords = tokenize_issue(issue_text or "")
        anchored_matched: list[tuple[AnchoredLocation, str]] = []
        if self._signals & {"anchor"} and (issue_text or "").strip():
            anchored_matched = match_anchored_to_repo(
                extract_anchored_locations(issue_text), self._rel_paths)
        anchored_by_file: dict[str, list[AnchoredLocation]] = {}
        for anchor, rel in anchored_matched:
            anchored_by_file.setdefault(rel, []).append(anchor)

        symbol_hits: dict[str, list[str]] = {}
        if self._signals & {"symbol"}:
            for token in keywords:
                if token.startswith("__"):
                    continue  # dunder 噪声（__init__ 之类人人都定义）
                files = sorted({entry.file for entry in self._index.lookup(token)})
                if not files:
                    continue
                if len(files) > SYMBOL_TOKEN_MAX_FILES:
                    continue  # 泛化符号：定义散布太广，无定位价值（sklearn-10297 实证）
                symbol_hits[token] = files

        locations: list[CodeLocation] = []
        for rel in self._py_candidates:
            location = self._score_candidate(rel, keywords, anchored_by_file, symbol_hits)
            if location is not None:
                locations.append(location)
        locations.sort(key=lambda loc: (loc.tier, -loc.score, loc.path))
        return RetrievalResult(
            locations=locations,
            anchored_matched=anchored_matched,
            keywords=keywords,
            symbol_hits=symbol_hits,
            scanned_files=list(self._rel_paths),
            scan_truncated=self._scan_truncated,
        )

    def _score_candidate(
        self, rel: str, keywords: list[str],
        anchored_by_file: dict[str, list[AnchoredLocation]],
        symbol_hits: dict[str, list[str]],
    ) -> CodeLocation | None:
        reasons: list[str] = []
        line: int | None = None
        anchor_kind = ""
        test_name = ""
        symbol: str | None = None
        tier = TIER_KEYWORD
        score = 0

        anchors = anchored_by_file.get(rel, ())
        if anchors:
            tier = TIER_ANCHORED
        for anchor in anchors:
            score += ANCHOR_SCORE
            if anchor.line is not None and line is None:
                line = anchor.line  # 取第一个带行号的锚定（窗口提取用）
            anchor_kind = anchor_kind or anchor.kind
            test_name = test_name or anchor.test_name
            suffix = f":{anchor.line}" if anchor.line is not None else ""
            reasons.append(f"anchored {anchor.path}{suffix} ({anchor.kind})")

        matched_symbols = sorted(t for t, files in symbol_hits.items() if rel in files)
        if matched_symbols:
            if tier > TIER_SYMBOL:
                tier = TIER_SYMBOL
            score += SYMBOL_SCORE * len(matched_symbols)
            symbol = matched_symbols[0]
            reasons.append(f"symbol match: {', '.join(matched_symbols)}")

        stem = basename_of(rel).rsplit(".", 1)[0].lower()
        parts = [seg.lower() for seg in rel.split("/")[:-1]]
        path_score = 0
        for kw in keywords:
            if kw in stem:
                path_score += KEYWORD_STEM_SCORE
            if kw in parts:
                path_score += KEYWORD_DIR_SCORE
        if path_score:
            score += path_score
            reasons.append(f"path keywords (+{path_score})")
        elif rel.endswith(tuple(_TEXT_CONTENT_SUFFIXES)):
            content_hits = self._content_keywords(rel, keywords)
            if content_hits:
                credited = min(len(content_hits), KEYWORD_CONTENT_MAX)
                score += KEYWORD_CONTENT_SCORE * credited
                reasons.append(f"content keywords (+{credited}): "
                               + ", ".join(content_hits[:4]))

        if tier is not TIER_KEYWORD or score > 0:
            return CodeLocation(
                path=rel, tier=tier, score=score, reasons=reasons,
                line=line, anchor_kind=anchor_kind, test_name=test_name, symbol=symbol,
            )
        return None

    def _content_keywords(self, rel: str, keywords: list[str]) -> list[str]:
        """路径关键词未命中时的有界内容匹配（统一内核：两个消费者同规则）。"""
        if not keywords or is_sensitive_path(rel):
            return []
        if rel not in self._content_cache:
            self._content_cache[rel] = _read_bounded(self._root / rel).lower()
        content = self._content_cache[rel]
        return [kw for kw in keywords if kw in content]


# ---- 代码窗口（v0.5.2：文件级候选 → 行级证据）----


@dataclass
class CodeWindow:
    path: str
    start_line: int
    end_line: int
    text: str  # 头注释 + 行号内容（渲染后的最终片段）
    truncated: bool = False
    reason: str = ""  # 窗口来源 / 回退原因（"缺少上下文"记录点）


def build_code_windows(
    repo_root,
    locations: list[CodeLocation],
    *,
    index: SymbolIndex | None = None,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    max_window_chars: int = DEFAULT_MAX_WINDOW_CHARS,
) -> list[CodeWindow]:
    """从候选位置提取代码窗口：锚定行号 ±40 / AST 函数范围 / 文件头回退。

    同文件多命中先合并重叠（含 ≤ MERGE_GAP_LINES 间隙），再按总字符预算
    顺序渲染；预算耗尽后剩余位置丢弃——窗口数 < 位置数即发生了丢弃，
    调用方可据此记录诊断。回退情况（无位置信号 / 行号越界）写入 reason。
    """
    root = Path(repo_root)
    spans_by_file: dict[str, list[tuple[int, int, str]]] = {}
    file_order: list[str] = []  # 按位置排名顺序渲染——窗口顺序 = 检索优先级
    for location in locations:
        span = _window_span(location, index)
        if span is None:
            continue
        if location.path not in spans_by_file:
            file_order.append(location.path)
            spans_by_file[location.path] = []
        spans_by_file[location.path].append(span)

    windows: list[CodeWindow] = []
    budget = max_total_chars
    for path in file_order:
        for start, end, reason in _merge_spans(spans_by_file[path]):
            if budget <= 0:
                break
            window = _render_window(root, path, start, end, reason,
                                    min(max_window_chars, budget))
            budget -= len(window.text)
            windows.append(window)
        if budget <= 0:
            break
    return windows


def _window_span(location: CodeLocation,
                 index: SymbolIndex | None) -> tuple[int, int, str] | None:
    """位置 → (start, end, reason)。行号为 1-based 闭区间。"""
    if location.line is not None:
        start = max(1, location.line - WINDOW_CONTEXT_LINES)
        return start, location.line + WINDOW_CONTEXT_LINES, \
            f"anchor line {location.line} ({location.anchor_kind})"
    # 失败身份：测试函数本身也是 Test Agent 需要的上下文
    if location.test_name and index is not None:
        for entry in index.lookup(location.test_name):
            if entry.file == location.path and entry.end_line is not None:
                return entry.line, entry.end_line, f"test function {entry.qualname}()"
    if location.symbol and index is not None:
        for entry in index.lookup(location.symbol):
            if entry.file == location.path and entry.end_line is not None:
                return entry.line, entry.end_line, f"symbol {entry.qualname}()"
    return 1, HEAD_FALLBACK_LINES, "no position signal; file head fallback"


def _merge_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """同文件窗口按起点排序并合并重叠 / 相邻（间隙 ≤ MERGE_GAP_LINES）。"""
    ordered = sorted(spans, key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, str]] = []
    for start, end, reason in ordered:
        if merged and start <= merged[-1][1] + MERGE_GAP_LINES:
            prev_start, prev_end, prev_reason = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), f"{prev_reason}; {reason}")
        else:
            merged.append((start, end, reason))
    return merged


def _render_window(root: Path, path: str, start: int, end: int,
                   reason: str, char_cap: int) -> CodeWindow:
    """渲染单个窗口：头注释 + 行号内容；超预算截断并显式标记。"""
    try:
        raw = (root / path).read_bytes()
    except OSError as exc:
        return CodeWindow(path=path, start_line=0, end_line=0,
                          text=f"# {path} (unreadable: {type(exc).__name__})",
                          truncated=True, reason=reason)
    if b"\x00" in raw[:8192]:
        return CodeWindow(path=path, start_line=0, end_line=0,
                          text=f"# {path} (binary file skipped)",
                          truncated=True, reason=reason)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    if start > total:  # 锚定行号越界（错误行号）：回退文件头并记录
        start, end = 1, min(HEAD_FALLBACK_LINES, total)
        reason = f"{reason}; anchor line beyond EOF, fell back to head"
    end = min(end, total)
    start = max(1, start)

    body: list[str] = []
    truncated = False
    used = 0
    for number in range(start, end + 1):
        rendered = f"{number}\t{lines[number - 1]}"
        if used + len(rendered) > char_cap:
            truncated = True
            break
        body.append(rendered)
        used += len(rendered)
    if truncated:
        body.append(f"[...window clipped at {char_cap} chars; "
                    f"requested lines {start}-{end}]")
    shown_end = start + len(body) - 1 if body else start - 1
    header = f"# {path}, lines {start}-{shown_end}"
    if reason:
        header += f"  ({reason})"
    return CodeWindow(path=path, start_line=start, end_line=shown_end,
                      text=header + "\n" + "\n".join(body),
                      truncated=truncated, reason=reason)
