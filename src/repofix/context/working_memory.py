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
    failed_tests: list[str],
    unresolved_errors: list[str],
) -> str:
    """渲染 Working Memory 文本块（每次 build 重新生成，不写入历史）。

    文件状态标注规则（CX-06）：
    - 修改后哈希与最后读取一致 → confirmed current（可信任该文件认知）
    - 修改后未重读（最后读取哈希 ≠ 当前哈希）→ stale read：必须重读后再编辑
    - 修改过但从未读取 → not read yet
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
            if last_read is None:
                marker = "not read yet - read before further edits"
            elif current is not None and last_read == current:
                marker = "confirmed current"
            else:
                marker = "STALE READ - re-read before editing"
            lines.append(f"  - {path} [{marker}]")
    else:
        lines.append("Modified files: none")
    if failed_tests:
        lines.append("Latest verification FAILED tests: " + ", ".join(failed_tests))
    elif unresolved_errors:
        lines.append("Unresolved errors: " + "; ".join(unresolved_errors))
    else:
        lines.append("Latest verification: no failures recorded")
    return "\n".join(lines)
