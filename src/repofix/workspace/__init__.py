"""Workspace & Policy：隔离任务环境（design/05、specs/03）。"""
from repofix.workspace.errors import (
    DirtyWorkspaceError,
    WorkspaceError,
    WorkspaceNotFoundError,
)
from repofix.workspace.manager import WorkspaceHandle, WorkspaceManager

__all__ = [
    "DirtyWorkspaceError",
    "WorkspaceError",
    "WorkspaceHandle",
    "WorkspaceManager",
    "WorkspaceNotFoundError",
]
