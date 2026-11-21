"""AST Symbol Index（MVP v0.5）：确定性的 Python 符号与 import 索引。

定位（评审讨论 Level 2）：修复"关键词相关 ≠ 程序行为相关"的定位盲区——
Issue 提到订单更新，真正的 Bug 在被 import 的缓存模块里。本模块只做：
- `ast.parse` 提取函数/类/方法定义（名称、限定名、行号、类别）；
- 提取 import 语句并把模块路径尽量解析为仓库内文件（绝对/相对 import）；
- 反向索引：谁定义了某符号（find_symbol）、谁 import 了某文件（imported_by）。

硬约束（与 repo_overview 同一纪律）：
- 纯确定性：同基线同配置 ⇒ 同索引；全部列表排序后输出；
- 不执行仓库代码（只 ast.parse，不 import）；语法错误/二进制文件静默跳过并计数；
- 有界：文件数、单文件大小、符号总数、import 边数都有上限；
- 复用 iter_workspace_files 的目录剪枝与符号链接越界防护；敏感文件（.env/
  密钥类）不索引——与 Overview 的内容纪律一致。
"""
import ast
from dataclasses import dataclass, field
from pathlib import Path

from repofix.workspace.docker_workspace import iter_workspace_files

DEFAULT_INDEX_MAX_FILES = 300
MAX_FILE_BYTES = 256 * 1024  # 超大源文件跳过（索引用途不需要全文）
MAX_SYMBOLS_PER_FILE = 200
MAX_TOTAL_SYMBOLS = 5000
MAX_IMPORT_EDGES = 2000


@dataclass
class SymbolEntry:
    name: str  # 短名：update_total
    qualname: str  # 限定名：OrderService.update_total
    kind: str  # class / method / function
    file: str  # 仓库相对 posix 路径
    line: int
    end_line: int | None = None  # 定义块结束行（v0.5.2：代码窗口用；老索引兼容 None）

    def render(self) -> str:
        display = f"{self.qualname}()" if self.kind in ("function", "method") else self.qualname
        return f"{display}  {self.file}:{self.line} ({self.kind})"


@dataclass
class SymbolIndex:
    symbols: dict[str, list[SymbolEntry]] = field(default_factory=dict)  # 短名 → 定义
    imports: dict[str, list[str]] = field(default_factory=dict)  # file → 引用的仓库内文件
    imported_by: dict[str, list[str]] = field(default_factory=dict)  # file → 引用方
    files_indexed: int = 0
    files_skipped_syntax: int = 0
    files_skipped_size: int = 0
    truncated: bool = False

    def lookup(self, name: str) -> list[SymbolEntry]:
        """按短名或限定名（含后缀）查定义；命中按 (file, line) 排序保证确定性。"""
        entries: list[SymbolEntry] = []
        for key_entries in self.symbols.values():
            for entry in key_entries:
                if (entry.name == name or entry.qualname == name
                        or entry.qualname.endswith("." + name)):
                    entries.append(entry)
        entries.sort(key=lambda e: (e.file, e.line, e.qualname))
        return entries

    def imported_by_of(self, file: str) -> list[str]:
        return sorted(self.imported_by.get(file, []))

    def symbols_in_file(self, file: str) -> list[SymbolEntry]:
        """某文件内定义的全部符号（按行号排序）。"""
        entries = [entry for key_entries in self.symbols.values()
                   for entry in key_entries if entry.file == file]
        entries.sort(key=lambda e: (e.line, e.qualname))
        return entries

    def entry_files(self) -> list[str]:
        """索引覆盖的文件（确定性顺序）。"""
        files = set(self.imports)
        for entries in self.symbols.values():
            files.update(e.file for e in entries)
        return sorted(files)


def _module_to_file(repo_root: Path, module: str) -> str | None:
    """点分模块路径 → 仓库内文件（<mod>.py 或 <mod>/__init__.py）。"""
    if not module:
        return None
    rel = module.replace(".", "/")
    for candidate in (f"{rel}.py", f"{rel}/__init__.py"):
        if (repo_root / candidate).is_file():
            return candidate
    return None


def _resolve_relative(repo_root: Path, source_file: str, level: int, module: str | None) -> str | None:
    """相对 import（from . import x / from ..pkg import y）→ 仓库内文件。

    level=1 = 当前包（源文件所在目录），每升一级去掉一层目录。
    """
    parts = source_file.split("/")[:-1]
    if level > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)] if level > 1 else parts
    dotted = ".".join(base) + ("." + module if module else "")
    return _module_to_file(repo_root, dotted.strip("."))


class _IndexBuilder(ast.NodeVisitor):
    """单文件符号收集：def/class 的嵌套限定名 + 行号（栈携带容器 kind）。"""

    def __init__(self, file: str, budget: int):
        self.file = file
        self.budget = budget
        self.entries: list[SymbolEntry] = []
        self.truncated = False
        self._stack: list[tuple[str, str]] = []  # (name, kind)

    def _visit_container(self, node, kind: str) -> None:
        if self.budget <= 0:
            self.truncated = True
            return
        self.budget -= 1
        qualname = ".".join(name for name, _ in self._stack + [(node.name, kind)])
        # end_lineno：Python 3.8+ 定义块结束行（代码窗口截取用）；缺失时保持 None 兼容
        self.entries.append(SymbolEntry(
            name=node.name, qualname=qualname, kind=kind, file=self.file, line=node.lineno,
            end_line=getattr(node, "end_lineno", None)))
        self._stack.append((node.name, kind))
        self.generic_visit(node)
        self._stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_container(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_container(node, "method" if self._in_class() else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_container(node, "method" if self._in_class() else "function")

    def _in_class(self) -> bool:
        return any(kind == "class" for _, kind in self._stack)


def _parse_imports(tree: ast.AST, repo_root: Path, source_file: str) -> list[str]:
    """单文件的 import → 已解析的仓库内目标文件（尽力而为，解析不了就丢弃）。"""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _module_to_file(repo_root, alias.name)
                if resolved and resolved != source_file:
                    targets.add(resolved)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and node.level > 0:
                resolved = _resolve_relative(repo_root, source_file, node.level, module)
            else:
                resolved = _module_to_file(repo_root, module)
            if resolved and resolved != source_file:
                targets.add(resolved)
                # from x.y import z：z 可能是子模块
                for alias in node.names:
                    sub = _module_to_file(repo_root, f"{module}.{alias.name}")
                    if sub:
                        targets.add(sub)
    return sorted(targets)[:50]  # 单文件 import 边上限


def build_symbol_index(
    repo_root: Path,
    max_files: int = DEFAULT_INDEX_MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> SymbolIndex:
    """构建有界符号索引。任何单文件失败都不影响整体（跳过并计数）。"""
    repo_root = Path(repo_root).resolve()  # 相对路径会让 relative_to 全部失败
    if not repo_root.is_dir():
        raise NotADirectoryError(f"repository root does not exist: {repo_root}")
    # 函数级导入：repo_overview 顶层导入本模块，模块级反向导入会形成循环
    from repofix.context.repo_overview import is_sensitive_path

    index = SymbolIndex()
    found, truncated = iter_workspace_files(repo_root, 8, max_files)
    index.truncated = truncated
    total_symbols = MAX_TOTAL_SYMBOLS
    total_edges = MAX_IMPORT_EDGES
    for path in found:
        if total_symbols <= 0 and total_edges <= 0:
            index.truncated = True
            break
        try:
            rel = path.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            continue
        if path.suffix != ".py" or is_sensitive_path(rel):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                index.files_skipped_size += 1
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue  # 二进制
        try:
            tree = ast.parse(raw.decode("utf-8", errors="replace"))
        except SyntaxError:
            index.files_skipped_syntax += 1
            continue
        builder = _IndexBuilder(rel, min(total_symbols, MAX_SYMBOLS_PER_FILE))
        builder.visit(tree)
        for entry in builder.entries:
            index.symbols.setdefault(entry.name, []).append(entry)
        total_symbols -= len(builder.entries)
        if total_edges > 0:
            targets = _parse_imports(tree, repo_root, rel)[:total_edges]
            if targets:
                index.imports[rel] = targets
                total_edges -= len(targets)
                for target in targets:
                    index.imported_by.setdefault(target, []).append(rel)
        index.files_indexed += 1
    # 确定性：所有列表排序
    for entries in index.symbols.values():
        entries.sort(key=lambda e: (e.file, e.line, e.qualname))
    for key in index.imported_by:
        index.imported_by[key] = sorted(set(index.imported_by[key]))
    for key in index.imports:
        index.imports[key] = sorted(set(index.imports[key]))
    return index
