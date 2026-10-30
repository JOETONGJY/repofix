"""PermissionPolicy：按 Agent 身份与操作类型判断执行权限（design/05）。

M2 只实现 Main Agent 路径；Reviewer 只读视图占位（M4 启用）。
权限的真实来源是三层共同保证：ToolRegistry 角色视图（实现见 tools/registry.py）、
本策略的写/执行判定、以及 Workspace 的文件系统与容器约束——不能仅写在 System Prompt。
"""
from repofix.contracts import AgentActor
from repofix.workspace.docker_workspace import DockerWorkspace


class PermissionPolicy:
    def __init__(self, workspace: DockerWorkspace):
        self._workspace = workspace

    def workspace_for(self, actor: AgentActor) -> DockerWorkspace:
        """Reviewer 获得只读视图（同一候选副本，写权限被 Workspace 层拒绝）——M4 启用。"""
        if actor is AgentActor.REVIEWER:
            reviewer_view = DockerWorkspace(
                task_dir=self._workspace.task_dir,
                run_id=self._workspace.run_id,
                cli=None,
                container_name=self._workspace.container_name,
                read_only=True,
            )
            # 复用已运行容器；标记只读
            reviewer_view._container_up = self._workspace._container_up  # noqa: SL001 — 同进程内的受控共享
            return reviewer_view
        return self._workspace
