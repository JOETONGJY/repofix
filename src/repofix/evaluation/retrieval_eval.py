"""检索离线评估（MVP v0.5.3）：零 Token 度量 File Recall / Code Window Hit。

定位（评审 v0.5.3 约定 4）：在真实 Agent A/B 之前，先用冻结任务集的
gold patch 做纯离线检索评估——回答"检索模块有没有更容易找到标准修复
涉及的代码"，不回答"LLM 是否因此修得更好"（后者需要真实实验）。

四个指标（分母与判断标准按评审约定）：
- File Recall@k：top-k 候选是否覆盖 gold patch 修改的**生产文件**
  （any-hit = 至少一个；all-hit = 全部；gold 文件集合为分母）；
- Code Window Hit@k：top-k 候选产出的代码窗口是否与 gold 修改行范围
  重叠（区分 File Hit 与 Window Hit——选对文件 ≠ 读到该读的代码）；
- Scan Miss Rate：gold 文件是否根本没进有界扫描范围；
- Snippet Truncation：gold 文件的窗口被字符预算截断且 gold 行不在窗口内。

gold 行范围解析自 unified diff，采用**旧文件坐标**（评审问题 1：检索
窗口从 base_commit 原始代码提取，必须同坐标系——删除行精确、插入行记
插入点）；函数级标签由原始 AST 符号索引（含 end_line）映射，模块级
修改（常量/import）单独标记，不强行套用 Function Hit。

新建 gold 文件（--- /dev/null）在原始仓库中不存在——单独标记
new_files，检索原理上不可命中，不计入 File Recall 分母（评审 P1-3）。

变体语义（评审问题 2）：三组变体是**同一框架内的消融**（ablation），
不是对历史版本的忠实复现——keyword_only 关闭了旧版没有的内容匹配、
头窗口（60/300 行 + 字符截断）也与旧版 6000 字符头读不等价。跨变体
结论只能表述为"同一评估框架内增加信号/窗口后指标如何变化"。
"""
import re
from dataclasses import dataclass, field

from repofix.context.retrieval import CodeLocation

# gold 文件里常见的测试目录前缀（SWE-bench gold_patch 理论上不含测试，
# 防御性过滤并单独报告）
_TEST_PATH_PREFIXES = ("tests/", "test/")


@dataclass
class GoldLabels:
    """一道任务的检索评估标签（逐文件行范围 + 函数级映射）。"""

    instance_id: str
    issue_text: str = ""  # dataset problem_statement（rank 输入）
    files: list[str] = field(default_factory=list)  # 生产文件（排除测试路径与新建文件）
    test_files: list[str] = field(default_factory=list)  # 误入 gold 的测试文件
    new_files: list[str] = field(default_factory=list)  # gold 新建的生产文件
    # （base_commit 不存在——检索原理上不可能命中，单独标记不进分母，评审 P1-3）
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    labels: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    # labels[file] = [(start, end, symbol_qualname_or_"module-level")]
    parse_ok: bool = True
    parse_error: str = ""


def parse_gold_patch(instance_id: str, diff: str) -> GoldLabels:
    """unified diff → 逐文件修改范围，**旧文件坐标**（base_commit 原始行号）。

    评审问题 1：检索窗口从 base_commit 原始代码提取，gold 位置必须用同一
    坐标系——新文件坐标在补丁前部插行时会整体漂移。规则：
    - '-' 删除行：在原始文件中真实存在，按 old_line 精确记录；
    - '+' 插入行：原始文件中不存在，记录插入点 (old_line, old_line)——
      即原文件中紧随其后那一行的位置（±窗口半径足以覆盖邻域）；
    - 纯删除补丁由此产生有效范围（新坐标方案下会得到空集）；
    - 上下文行同时前进新旧计数。
    """
    labels = GoldLabels(instance_id=instance_id)
    if not diff or not diff.strip():
        labels.parse_ok = False
        labels.parse_error = "empty gold patch"
        return labels
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    old_devnull = False  # 前一个 '--- /dev/null' → 本次 '+++' 是新建文件
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    old_line = 0
    pending_anchor: int | None = None  # 当前替换块首个被删行的原始位置
    try:
        for line in diff.splitlines():
            if line.startswith("--- "):
                old_devnull = line[4:].split("\t")[0].strip() == "/dev/null"
            elif line.startswith("+++ "):
                path = line[4:].split("\t")[0].strip()
                if path == "/dev/null":
                    current = None
                    continue
                current = path[2:] if path.startswith("b/") else path
                ranges.setdefault(current, [])
                if old_devnull:
                    labels.new_files.append(current)  # gold 新建文件（评审 P1-3）
            elif line.startswith("@@ ") and current is not None:
                match = hunk_re.match(line)
                if not match:
                    continue
                old_line = int(match.group(1))
                pending_anchor = None
            elif current is not None and line.startswith(("+", "-")) \
                    and not line.startswith(("+++", "---")):
                if line.startswith("-"):
                    if pending_anchor is None:
                        pending_anchor = old_line
                    ranges[current].append((old_line, old_line))
                    old_line += 1
                else:
                    # '+' 行：替换块内锚定被删行位置；纯插入锚定边界
                    anchor = pending_anchor if pending_anchor is not None else old_line
                    ranges[current].append((anchor, anchor))
            elif current is not None and not line.startswith("\\"):
                old_line += 1  # 上下文行
                pending_anchor = None
    except ValueError as exc:
        labels.parse_ok = False
        labels.parse_error = f"{type(exc).__name__}: {exc}"
        return labels
    new_file_set = set(labels.new_files)
    for path, file_ranges in ranges.items():
        if not file_ranges or path in new_file_set:
            continue  # 新建文件：原始仓库中不存在，不进可检索集合
        merged: list[tuple[int, int]] = []
        for start, end in sorted(file_ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        if path.startswith(_TEST_PATH_PREFIXES) or "/tests/" in path:
            labels.test_files.append(path)
        else:
            labels.files.append(path)
            labels.ranges[path] = merged
    labels.test_files.sort()
    labels.files.sort()
    labels.new_files.sort()
    return labels


def map_ranges_to_symbols(ranges: dict[str, list[tuple[int, int]]],
                          index) -> dict[str, list[tuple[int, int, str]]]:
    """gold 行范围 → 函数级标签（最小包含符号；无符号 → module-level）。"""
    labels: dict[str, list[tuple[int, int, str]]] = {}
    for path, file_ranges in ranges.items():
        symbols = index.symbols_in_file(path) if index is not None else []
        file_labels: list[tuple[int, int, str]] = []
        for start, end in file_ranges:
            containing = [
                entry for entry in symbols
                if entry.line <= start and (entry.end_line or entry.line) >= end
                and entry.kind in ("function", "method", "class")
            ]
            if containing:
                best = min(containing, key=lambda e: (e.end_line or e.line) - e.line)
                # 类级包含（如类 docstring 修改）标注 kind 前缀——不冒充函数级
                label = best.qualname if best.kind in ("function", "method") \
                    else f"class:{best.qualname}"
                file_labels.append((start, end, label))
            else:
                file_labels.append((start, end, "module-level"))
        labels[path] = file_labels
    return labels


@dataclass
class TaskRetrievalEval:
    """单任务 × 单变体的检索评估结果。"""

    instance_id: str
    variant: str
    gold_files: list[str] = field(default_factory=list)
    new_file_required: bool = False  # gold 含新建生产文件（评审 P1-3）
    impossible_for_retrieval: bool = False  # gold 全为新建文件 → 检索原理上不可命中
    top_paths: list[str] = field(default_factory=list)
    file_hit_any: bool = False
    file_hit_all: bool = False
    scan_miss: list[str] = field(default_factory=list)
    window_hit_files: list[str] = field(default_factory=list)
    function_hit: bool = False
    module_level_only: bool = False  # gold 全是模块级修改 → Function Hit 不适用
    truncated_on_gold: bool = False
    window_count: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id, "variant": self.variant,
            "gold_files": self.gold_files,
            "new_file_required": self.new_file_required,
            "impossible_for_retrieval": self.impossible_for_retrieval,
            "top_paths": self.top_paths,
            "file_hit_any": self.file_hit_any, "file_hit_all": self.file_hit_all,
            "scan_miss": self.scan_miss, "window_hit_files": self.window_hit_files,
            "function_hit": self.function_hit,
            "module_level_only": self.module_level_only,
            "truncated_on_gold": self.truncated_on_gold,
            "window_count": self.window_count, "error": self.error,
        }


def evaluate_retrieval(labels: GoldLabels, result, windows, *, top_k: int = 3) -> TaskRetrievalEval:
    """单任务评估。result = CodeRetriever.rank() 的 RetrievalResult；
    windows = build_code_windows() 的输出（对应同一 result 的 top-k）。"""
    evaluation = TaskRetrievalEval(
        instance_id=labels.instance_id, variant="", gold_files=list(labels.files),
        new_file_required=bool(labels.new_files))
    if not labels.parse_ok:
        evaluation.error = f"gold parse failed: {labels.parse_error}"
        return evaluation
    if not labels.files:
        # 只有新建文件（或只有测试文件）：检索原理上不可命中——单独标记，
        # 不计入错误也不进 File Recall 分母（评审 P1-3）
        evaluation.impossible_for_retrieval = True
        evaluation.module_level_only = bool(labels.test_files)
        return evaluation

    evaluation.top_paths = [loc.path for loc in result.locations[:top_k]]
    top_set = set(evaluation.top_paths)
    gold_set = set(labels.files)
    evaluation.file_hit_any = bool(top_set & gold_set)
    evaluation.file_hit_all = gold_set <= top_set
    evaluation.scan_miss = sorted(
        f for f in labels.files if f not in set(result.scanned_files))

    # 窗口与 gold 行范围重叠
    function_labels = labels.labels or map_ranges_to_symbols(labels.ranges, None)
    gold_function_spans = {
        path: [(s, e) for (s, e, label) in entries
               if label != "module-level" and not label.startswith("class:")]
        for path, entries in function_labels.items()
    }
    module_level_files = {
        path for path, entries in function_labels.items()
        if all(label == "module-level" for _, _, label in entries)
    }
    evaluation.module_level_only = bool(module_level_files) and module_level_files == set(labels.files)
    evaluation.window_count = len(windows)
    for window in windows:
        if window.path not in labels.ranges:
            continue
        if not any(window.start_line <= e and window.end_line >= s
                   for s, e in labels.ranges[window.path]):
            # 选对了 gold 文件但窗口没盖住 gold 行——区分两种原因
            if window.truncated:
                evaluation.truncated_on_gold = True
            continue
        if window.path not in evaluation.window_hit_files:
            evaluation.window_hit_files.append(window.path)
        if any(window.start_line <= e and window.end_line >= s
               for s, e in gold_function_spans.get(window.path, [])):
            evaluation.function_hit = True
    evaluation.window_hit_files.sort()
    return evaluation


def summarize(records: list[TaskRetrievalEval]) -> dict:
    """聚合指标（评审 P0-2：由本函数统一排除错误并报告分母，调用方不得预过滤）。

    分母 = 无错误且非 impossible_for_retrieval 的任务；错误任务与
    新建文件任务分别报告数量，保证各变体分母可见、可对比。
    """
    total_assigned = len(records)
    errors = [r for r in records if r.error]
    impossible = [r for r in records if r.impossible_for_retrieval and not r.error]
    valid = [r for r in records if not r.error and not r.impossible_for_retrieval]
    total = len(valid)
    new_file_tasks = [r for r in records if r.new_file_required and not r.error]

    def rate(predicate) -> float:
        return round(sum(1 for r in valid if predicate(r)) / total, 3) if total else 0.0

    return {
        "tasks_total": total_assigned,
        "tasks_valid": total,
        "tasks_error": len(errors),
        "tasks_impossible_new_file": len(impossible),
        "new_file_required_rate": (round(len(new_file_tasks) / total_assigned, 3)
                                   if total_assigned else 0.0),
        "file_recall_any@3": rate(lambda r: r.file_hit_any),
        "file_recall_all@3": rate(lambda r: r.file_hit_all),
        "scan_miss_rate": rate(lambda r: bool(r.scan_miss)),
        "window_hit@3": rate(lambda r: bool(r.window_hit_files)),
        "function_hit@3": rate(lambda r: r.function_hit),
        "module_level_only_rate": rate(lambda r: r.module_level_only),
        "truncated_on_gold_rate": rate(lambda r: r.truncated_on_gold),
        "errors": [f"{r.instance_id}: {r.error}" for r in errors][:10],
        "impossible_new_file_ids": [r.instance_id for r in impossible][:10],
    }


# —— 变体信号定义（评审 P0-1：kernel 变体必须与生产 CodeRetriever 默认一致）——

PRODUCTION_SIGNALS = ("anchor", "symbol", "keyword", "content")

VARIANT_SIGNALS = {
    # 消融基线：仅路径/文件名评分（旧版 Test Agent 没有内容匹配）
    "keyword_only": ("keyword",),
    # 全信号（与生产默认一致）+ 文件头窗口 = v0.5.1
    "kernel_head": PRODUCTION_SIGNALS,
    # 全信号（与生产默认一致）+ 锚定/符号/内容命中行窗口 = 当前实现
    "kernel_windows": PRODUCTION_SIGNALS,
}


def picked_locations(result, top_k: int) -> list:
    """生产对齐的候选选取（评审 P0-1）：有信号候选 → 结构回退（浅层优先），
    与 select_issue_files 的回退语义一致。"""
    picked = [loc for loc in result.locations if loc.score > 0][:top_k]
    if not picked:
        fallback_files = result.structural_fallback_files[:top_k]
        picked = [
            CodeLocation(path=rel, tier=3, score=0, reasons=["structural fallback"])
            for rel in fallback_files
        ]
    return picked
