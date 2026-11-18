"""Issue 锚定信号（MVP v0.5，Traceback-Aware Retrieval）。

从 Issue 文本中提取"白给"的定位信息——文件与行号不需要猜：
- Traceback 行：`File "services/calculator.py", line 42`
- pytest 失败身份：`tests/test_orders.py::test_update_total`
- 裸路径提及：`cache/order_cache.py` / `src\\api\\orders.py`

并把提及路径匹配到仓库真实文件（精确 → 后缀），供 Overview 候选排序与
Test Agent 的片段选择使用。纯确定性正则提取，不执行任何代码。
"""
import re
from dataclasses import dataclass

# `File "path", line N`（traceback 标准行；路径可含任意非引号字符）
_TRACEBACK_RE = re.compile(r'File "([^"\n]+)", line (\d+)')
# pytest 节点身份：path.py::name[::param]
_PYTEST_NODE_RE = re.compile(r'([\w./\\\-]+\.py)::([\w\[\]\-<>\.]+)')
# 裸 .py 路径提及（正斜杠与反斜杠分隔符都接受；前后不能是路径字符）
_PY_PATH_RE = re.compile(r'(?<![\w/\\.\-])((?:[\w\-]+[\\/]+)*[\w\-]+\.py)(?![\w/\\.\-])')

MAX_ANCHORED_LOCATIONS = 20


@dataclass
class AnchoredLocation:
    path: str  # 文本中的原始路径（posix 归一化）
    line: int | None
    kind: str  # traceback / test_failure / path_mention
    test_name: str = ""

    def key(self) -> tuple:
        return (self.path, self.line, self.kind)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def extract_anchored_locations(issue_text: str) -> list[AnchoredLocation]:
    """提取 Issue 中的锚定位置（去重保序，有界）。"""
    if not issue_text:
        return []
    locations: list[AnchoredLocation] = []
    seen: set[tuple] = set()

    def _add(location: AnchoredLocation) -> None:
        if location.key() not in seen and len(locations) < MAX_ANCHORED_LOCATIONS:
            seen.add(location.key())
            locations.append(location)

    captured_paths: set[str] = set()
    for match in _TRACEBACK_RE.finditer(issue_text):
        path = _normalize(match.group(1))
        captured_paths.add(path)
        _add(AnchoredLocation(path=path, line=int(match.group(2)), kind="traceback"))
    for match in _PYTEST_NODE_RE.finditer(issue_text):
        path = _normalize(match.group(1))
        captured_paths.add(path)
        _add(AnchoredLocation(path=path, line=None,
                              kind="test_failure", test_name=match.group(2)))
    for match in _PY_PATH_RE.finditer(issue_text):
        path = _normalize(match.group(1))
        if path in captured_paths:
            continue  # traceback / 失败身份已捕获，不再按裸路径重复收录
        if not path.startswith("tests/") and not path.endswith("conftest.py"):
            _add(AnchoredLocation(path=path, line=None, kind="path_mention"))
    return locations


def match_anchored_to_repo(
    locations: list[AnchoredLocation], repo_files: list[str]
) -> list[tuple[AnchoredLocation, str]]:
    """把锚定位置匹配到仓库真实文件：精确 → 目录前缀 → 文件名后缀。

    返回 [(location, 仓库相对路径)]；未匹配到的条目丢弃（Issue 可能引用
    上游仓库路径，与本地结构不同——静默跳过，不猜测）。
    """
    file_set = set(repo_files)
    by_suffix: dict[str, list[str]] = {}
    for repo_file in repo_files:
        by_suffix.setdefault(repo_file.split("/")[-1], []).append(repo_file)
    matched: list[tuple[AnchoredLocation, str]] = []
    for location in locations:
        path = location.path
        if path in file_set:
            matched.append((location, path))
            continue
        suffix = path.split("/")[-1]
        candidates = by_suffix.get(suffix, [])
        if len(candidates) == 1:
            matched.append((location, candidates[0]))
            continue
        # 目录前缀匹配：提及 services/calculator.py 而仓库为 src/services/calculator.py
        prefix_candidates = [f for f in repo_files if f.endswith("/" + path)]
        if len(prefix_candidates) == 1:
            matched.append((location, prefix_candidates[0]))
    return matched
