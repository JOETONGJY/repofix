"""Agent 可见测试计划推导（冻结规则 visible_plan_rule_v1）。

信息边界（GPT 评审要求 1：隐藏测试隔离）：
- 允许输入：Issue 正文（Agent 本来可见）+ base 状态的仓库文件结构（Agent 本来可见）。
- 禁止输入：test_patch、FAIL_TO_PASS、PASS_TO_PASS、gold patch——这些只供
  评测器（judge.py）与 stage-0 环境验收使用，绝不参与 Agent 可见范围的推导。

规则（机械、确定性、跑前冻结）：
  1. 从 Issue 提取候选标识符：反引号内 token + snake_case/CamelCase 词（≥4 字符）。
  2. 定位相关源码文件（git ls-files 的 .py 文件）：
     a. 文件 stem == 标识符；
     b. 标识符在文件内容中出现，且全仓库出现文件数 ≤ 20（排除高频常见词）。
  3. 对每个定位到的源码文件 S，按仓库命名约定推导测试目标（纯文件系统，
     依次取第一个命中层）：
     L1  S 同级 tests 目录中 test_<stem>.py / unittest_<stem>.py
     L2  测试目录中名字含 S 所在包目录名或 S.stem 的测试文件（≤3 个）
     L3  测试目录中内容引用 `import <pkg>.<module>` / `from <pkg>.<module>` 的文件（≤3 个）
     L4  S 最近的祖先 tests/ 目录整体（范围大，需过运行时预算）
     （测试目录 = 路径中含 tests/ 或 testing/ 段，或文件名以 test_/unittest_ 开头）
  4. 汇总目标（去重、按路径排序、上限 3 个）→ `python -m pytest <targets> …`。
  5. 无任何目标 → 空计划（Agent 无冻结可见验证，完成判定为 INCONCLUSIVE——
     specs/02 VF-05 允许该终态；Agent 仍可在循环内自行选择运行任何测试）。

运行时预算由 stage-0 实测校验：超预算的计划不冻结（降级到下一层或空计划），
决策记录在 stage0-report.json，全部发生在任何模型调用之前。
"""
import re
from pathlib import Path

from repofix.workspace.docker_workspace import ALLOWED_COMMANDS

TEST_DIR_SEGMENTS = {"tests", "testing"}
_MAX_TARGETS = 3
_MAX_CONTENT_HITS = 20
_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{3,40}\b")
_BACKTICK_RE = re.compile(r"`([^`\n]{4,80})`")

_PYTHON_KEYWORDS = {
    "python", "self", "none", "true", "false", "test", "tests", "error", "errors",
    "issue", "fix", "fixed", "bug", "code", "file", "line", "return", "import",
    "def", "class", "assert", "exception", "traceback", "value", "values",
    "should", "would", "when", "with", "this", "that", "from", "have", "uses",
    # 泛化词（stage-0 校准：作为文件 stem 时的命中与 Issue 主题无关）
    "other", "main", "base", "example", "examples", "generic", "various",
}


def _is_identifier_token(token: str) -> bool:
    """dunder（__init__ 等）与泛化词不参与源码定位——stem 匹配会引入整包噪音。"""
    return not token.startswith("__") and token.lower() not in _PYTHON_KEYWORDS


def is_test_file(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    name = parts[-1]
    return (name.startswith(("test_", "unittest_")) and name.endswith(".py")) or any(
        segment in TEST_DIR_SEGMENTS for segment in parts[:-1]
    )


def _is_runnable_test_target(rel_path: str) -> bool:
    """可作为 pytest 运行目标的文件（conftest.py 在测试目录内但不可直接运行）。"""
    name = Path(rel_path).name
    return name.startswith(("test_", "unittest_")) and name.endswith(".py")


def extract_identifiers(problem_statement: str) -> list[str]:
    """候选标识符：反引号 token 优先，其次全词 snake_case/CamelCase（去噪、保序、去重）。"""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = token.strip()
        lowered = token.lower()
        if lowered in seen or lowered in _PYTHON_KEYWORDS or len(token) < 4:
            return
        seen.add(lowered)
        tokens.append(token)

    for span in _BACKTICK_RE.findall(problem_statement):
        for piece in re.split(r"[^A-Za-z0-9_.]+", span):
            piece = piece.strip(".")
            if piece:
                add(piece)
    for token in _WORD_RE.findall(problem_statement):
        add(token)
    return [t for t in tokens if _is_identifier_token(t)]


def _tracked_py_files(testbed: Path) -> list[str]:
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=str(testbed), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def locate_source_files(identifiers: list[str], py_files: list[str], testbed: Path) -> list[str]:
    """标识符 → 相关源码文件（非测试文件）。stem 匹配优先，其后低频内容匹配。"""
    by_stem: dict[str, list[str]] = {}
    for rel in py_files:
        if is_test_file(rel):
            continue
        by_stem.setdefault(Path(rel).stem, []).append(rel)
    located: list[str] = []
    seen: set[str] = set()
    for token in identifiers:
        stem = token.split(".")[-1]
        for rel in by_stem.get(stem, []):
            if rel not in seen:
                seen.add(rel)
                located.append(rel)
    if located:
        return located
    # 低频内容匹配：rg -l 计数 ≤ 阈值才采纳（机械过滤常见词）
    import subprocess

    for token in identifiers:
        if "." in token or token.lower() in _PYTHON_KEYWORDS:
            continue
        proc = subprocess.run(
            ["rg", "--files-with-matches", "--fixed-strings", "--glob", "*.py", token],
            cwd=str(testbed), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        hits = [line for line in proc.stdout.splitlines() if line.strip()]
        sources = [rel for rel in hits if not is_test_file(rel)]
        if 0 < len(hits) <= _MAX_CONTENT_HITS and sources:
            for rel in sources[:2]:
                if rel not in seen:
                    seen.add(rel)
                    located.append(rel)
            if located:
                break
    return located


def _test_targets_for_source(source_rel: str, py_files: list[str], testbed: Path) -> tuple[str, list[str]]:
    """按 L1-L4 推导测试目标。返回 (layer, targets)。"""
    source = Path(source_rel)
    module_ref = source_rel[:-3].replace("/", ".")  # pkg/module(sub)（去掉 .py）
    pkg_dir = source.parent.name
    tests_files = [rel for rel in py_files if _is_runnable_test_target(rel)]

    # L1：同级 tests 目录中的同名测试
    sibling = source.parent / "tests"
    for pattern in (f"test_{source.stem}.py", f"unittest_{source.stem}.py"):
        candidate = (sibling / pattern).as_posix()
        if (testbed / candidate).exists():
            return "L1", [candidate]

    # L2a：测试文件名含源码文件 stem（紧匹配优先）
    stem_hits = [
        rel for rel in tests_files
        if source.stem != "__init__" and source.stem in Path(rel).stem
    ]
    # L2b：测试文件名含源码所在包目录名（松匹配，仅紧匹配为空时使用）
    pkg_hits = [
        rel for rel in tests_files
        if pkg_dir and pkg_dir not in TEST_DIR_SEGMENTS and pkg_dir in Path(rel).stem
        and rel not in stem_hits
    ]
    if stem_hits or pkg_hits:
        return "L2", (sorted(stem_hits) + sorted(pkg_hits))[:_MAX_TARGETS]

    # L3：测试文件内容引用该模块（import X.Y.Z / from X.Y.Z import）
    content_hits: list[str] = []
    for rel in tests_files:
        try:
            text = (testbed / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if f"import {module_ref}" in text or f"from {module_ref} import" in text:
            content_hits.append(rel)
        if len(content_hits) >= _MAX_TARGETS:
            break
    if content_hits:
        return "L3", sorted(content_hits)

    # L4：最近的祖先 tests/ 目录整体
    for ancestor in source.parents[:-1]:
        for segment in ("tests", "testing"):
            candidate_dir = ancestor / segment
            if candidate_dir.is_dir() and any(candidate_dir.glob("test_*.py")):
                rels = sorted(
                    fp.relative_to(testbed.resolve()).as_posix()
                    for fp in candidate_dir.rglob("test_*.py")
                )
                return "L4", rels[:_MAX_TARGETS]
    return "none", []


def derive_visible_plan(
    problem_statement: str, testbed: Path
) -> tuple[list[str], str, dict]:
    """冻结规则主入口。返回 (argv, plan_source, detail)。

    plan_source ∈ {"issue_scoped", "empty"}；argv 已按命令白名单约束构造。
    detail 记录推导证据（标识符、定位文件、命中层、目标），供 stage-0 评审与报告。
    """
    testbed = Path(testbed)
    py_files = _tracked_py_files(testbed)
    identifiers = extract_identifiers(problem_statement)
    located = locate_source_files(identifiers, py_files, testbed)
    detail: dict = {"identifiers_used": [], "located_sources": located, "targets": [], "layer": "none"}

    if not located:
        return [], "empty", detail

    targets: list[str] = []
    used_layer = "none"
    for source_rel in located[:3]:
        layer, hits = _test_targets_for_source(source_rel, py_files, testbed)
        if hits and layer != "none":
            used_layer = layer
            detail["identifiers_used"].append(source_rel)
            for hit in hits:
                if hit not in targets:
                    targets.append(hit)
        if len(targets) >= _MAX_TARGETS:
            break
    targets = sorted(targets)[:_MAX_TARGETS]
    detail["targets"] = targets
    detail["layer"] = used_layer
    if not targets:
        return [], "empty", detail
    return visible_plan_argv(targets), "issue_scoped", detail


def visible_plan_argv(test_files: list[str]) -> list[str]:
    """可见计划 → 白名单合规 argv（specs/03 命令策略；python -m pytest 前缀匹配）。"""
    allowed_prefix = next(a for a in ALLOWED_COMMANDS if a[:3] == ("python", "-m", "pytest"))
    return [*allowed_prefix, *test_files, "-q", "--tb=no", "-rf"]
