"""Workspace 异常类型（design/05：环境阻塞与业务失败严格区分）。"""


class WorkspaceError(RuntimeError):
    """Workspace 层基础设施错误（fail_layer=WORKSPACE）。"""


class DirtyWorkspaceError(WorkspaceError):
    """用户仓库存在未提交修改 / 未跟踪未忽略文件：直接拒绝执行（design/01）。"""

    def __init__(self, entries: list[str]):
        self.entries = entries
        super().__init__(
            "repository has uncommitted changes; RepoFix only runs on clean "
            f"base_commit snapshots ({len(entries)} dirty entries)"
        )


class WorkspaceNotFoundError(WorkspaceError):
    """repo 路径不存在或不是 Git 仓库。"""
