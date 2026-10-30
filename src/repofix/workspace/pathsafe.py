"""路径安全（specs/03 apply_patch 校验链第 1–2 步）。

模型生成的参数即使通过 Schema 校验也不是可信输入：
拒绝绝对路径与 `..` 逃逸；resolve 后必须仍位于副本根内（含中间目录 symlink）。
"""
from pathlib import Path


class PathEscapeError(ValueError):
    """路径逃逸：目标不在副本根内。"""


def safe_resolve(task_dir: Path, rel_path: str) -> Path:
    """把工作区相对路径解析为绝对路径，保证不逃逸副本根。

    抛出 PathEscapeError 表示路径非法（绝对路径 / .. 逃逸 / symlink 逃逸）。
    """
    if not rel_path:
        raise PathEscapeError("empty path")
    candidate = Path(rel_path)
    if candidate.is_absolute() or rel_path.startswith("~"):
        raise PathEscapeError(f"absolute paths are not allowed: {rel_path}")
    root = task_dir.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise PathEscapeError(f"path escapes workspace root: {rel_path}")
    return resolved


def display_rel(task_dir: Path, resolved: Path) -> str:
    """绝对路径 → 工作区相对显示路径（POSIX 风格，供工具输出使用）。"""
    return resolved.relative_to(task_dir.resolve()).as_posix()
