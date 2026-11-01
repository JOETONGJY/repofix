"""WorkingMemoryBuilder：从结构化事实构造任务进度视图（design/07）。

Working Memory 不是第二份事实数据库——它从 WorkingState 与文件版本记录构造，
需要时可重新生成。事实边界（规则 A）：以实际工作区与可信执行记录为准；
文件版本失效（CX-06）：修改后未重读的文件不标记"已确认最新"。
"""


def render_working_memory(
    *,
    task_goal: str,
    base_commit: str,
    modified_files: list[str],
    last_read_hashes: dict[str, str | None],
    current_hashes: dict[str, str | None],
    last_read_ops: dict[str, int],
    last_command_op: int,
    failed_tests: list[str],
    unresolved_errors: list[str],
) -> str:
    """渲染 Working Memory 文本块（每次 build 重新生成，不写入历史）。

    文件状态标注规则（CX-06 + 评审 P1-1）：
    - 从未读取 → not read yet - read before further edits
    - 读取序号晚于最后一次命令 且 读取哈希 == 当前哈希 → confirmed current
    - 其他（修改后未重读 / **命令之后未重读**）→ STALE READ：必须重读后再编辑
      （run_command 可能修改任意源文件：保守失效，不依赖命令自报改动范围）
    """
    lines = ["[Working Memory]"]
    lines.append(f"Task goal: {task_goal}")
    if base_commit:
        lines.append(f"Base commit: {base_commit}")
    if modified_files:
        lines.append(f"Modified files ({len(modified_files)}):")
        for path in modified_files:
            current = current_hashes.get(path)
            last_read = last_read_hashes.get(path)
            read_op = last_read_ops.get(path)
            if read_op is None:
                marker = "not read yet - read before further edits"
            elif read_op > last_command_op and current is not None and last_read == current:
                marker = "confirmed current"
            else:
                marker = "STALE READ - re-read before editing"
            lines.append(f"  - {path} [{marker}]")
    else:
        lines.append("Modified files: none")
    if last_command_op > 0:
        lines.append("Note: commands have run since workspace prep; re-read any file before editing.")
    if failed_tests:
        lines.append("Latest verification FAILED tests: " + ", ".join(failed_tests))
    elif unresolved_errors:
        lines.append("Unresolved errors: " + "; ".join(unresolved_errors))
    else:
        lines.append("Latest verification: no failures recorded")
    return "\n".join(lines)
