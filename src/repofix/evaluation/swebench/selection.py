"""冻结选题规则（GPT 方案第四节：选题在模型运行前固定，不允许看到结果后换题）。

规则版本 selection_rules_v1 —— 在任何 Agent 运行之前冻结并写入 selection.json：

R1 仓库门：log_parser 不是 django / sympy（官方判定协议为 pytest node-id，
   RepoFix 的失败解析与官方 harness 的 per-repo 解析器对齐）。
R2 单文件门：gold patch 恰好修改 1 个文件（与 apply_patch 单文件契约匹配；
   属选题期元数据使用，报告披露，patch 内容不给 Agent）。
R3 规模门：len(PASS_TO_PASS) ≤ 300（约束判定器运行时长）。
R4 可解门：len(FAIL_TO_PASS) ≤ 10。
R5 输入门：300 ≤ len(problem_statement) ≤ 8000（过短歧义、过长挤占上下文）。
R6 环境门（stage-0 验证，冻结前执行）：官方实例镜像可拉取 + 工作区可提取 +
   Preflight 基线可见测试可运行 + 判定器冒烟（base 状态下 F2P 失败、P2P 通过）。

排序与多样性：通过 R1-R5 的实例按 (repo 候选数降序, repo 名升序) 分组轮取，
每组最多 max_per_repo 道，直至目标数；同组内按 instance_id 升序。
该排序在 stage-0 之前生效——环境验证只做淘汰，不做重排、不做替补式换题
（被 R6 淘汰的实例记入 environment_blocked，不做静默删除）。
"""
from pathlib import Path

from repofix.evaluation.swebench.dataset import parse_test_list, patch_files

SELECTION_RULES_VERSION = "selection_rules_v1"
MAX_PER_REPO = 2
EXCLUDED_PARSERS = {"parse_log_django", "parse_log_sympy"}
MAX_PASS_TO_PASS = 300
MAX_FAIL_TO_PASS = 10
MIN_PROBLEM_CHARS = 300
MAX_PROBLEM_CHARS = 8000


def candidate_metadata(row: dict) -> dict | None:
    """R1-R5 静态规则判定。返回 None 表示淘汰；否则返回冻结所需的元数据。"""
    if row["log_parser"] in EXCLUDED_PARSERS:
        return None
    gold_files = patch_files(row["patch"])
    if len(gold_files) != 1:
        return None
    fail_to_pass = parse_test_list(row["FAIL_TO_PASS"])
    pass_to_pass = parse_test_list(row["PASS_TO_PASS"])
    if not fail_to_pass or len(fail_to_pass) > MAX_FAIL_TO_PASS:
        return None
    if len(pass_to_pass) > MAX_PASS_TO_PASS:
        return None
    statement = row["problem_statement"]
    if not (MIN_PROBLEM_CHARS <= len(statement) <= MAX_PROBLEM_CHARS):
        return None
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "image": row["image"],
        "gold_file": gold_files[0],
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "problem_chars": len(statement),
    }


def select_candidates(rows: list[dict], target: int) -> list[dict]:
    """R1-R5 + 多样性排序。确定性：同输入永远同输出。"""
    candidates = [meta for row in rows if (meta := candidate_metadata(row)) is not None]
    by_repo: dict[str, list[dict]] = {}
    for meta in candidates:
        by_repo.setdefault(meta["repo"], []).append(meta)
    for metas in by_repo.values():
        metas.sort(key=lambda m: m["instance_id"])
    repo_order = sorted(
        by_repo,
        key=lambda repo: (-len(by_repo[repo]), repo),
    )
    picked: list[dict] = []
    while len(picked) < target:
        grew = False
        for repo in repo_order:
            if len(picked) >= target:
                break
            bucket = by_repo[repo]
            taken = sum(1 for m in picked if m["repo"] == repo)
            if taken < MAX_PER_REPO and len(bucket) > taken:
                picked.append(bucket[taken])
                grew = True
        if not grew:
            break
    return picked


def stats(candidates: list[dict]) -> dict:
    """筛选漏斗统计（写入 selection.json 供报告引用）。"""
    by_repo: dict[str, int] = {}
    for meta in candidates:
        by_repo[meta["repo"]] = by_repo.get(meta["repo"], 0) + 1
    return {
        "rules_version": SELECTION_RULES_VERSION,
        "total_candidates": len(candidates),
        "by_repo": dict(sorted(by_repo.items(), key=lambda kv: -kv[1])),
        "max_fail_to_pass": max((len(m["fail_to_pass"]) for m in candidates), default=0),
        "max_pass_to_pass": max((len(m["pass_to_pass"]) for m in candidates), default=0),
    }


def funnel(rows: list[dict]) -> dict:
    """逐规则淘汰漏斗（评审要求 4：被筛除任务与原因全部留痕）。"""
    counts = {"R1_repo_parser": 0, "R2_single_file": 0, "R4_fail_to_pass": 0,
              "R3_pass_to_pass": 0, "R5_problem_length": 0}
    kept = 0
    for row in rows:
        if row["log_parser"] in EXCLUDED_PARSERS:
            counts["R1_repo_parser"] += 1
            continue
        from repofix.evaluation.swebench.dataset import patch_files, parse_test_list

        if len(patch_files(row["patch"])) != 1:
            counts["R2_single_file"] += 1
            continue
        f2p = parse_test_list(row["FAIL_TO_PASS"])
        p2p = parse_test_list(row["PASS_TO_PASS"])
        if not f2p or len(f2p) > MAX_FAIL_TO_PASS:
            counts["R4_fail_to_pass"] += 1
            continue
        if len(p2p) > MAX_PASS_TO_PASS:
            counts["R3_pass_to_pass"] += 1
            continue
        if not (MIN_PROBLEM_CHARS <= len(row["problem_statement"]) <= MAX_PROBLEM_CHARS):
            counts["R5_problem_length"] += 1
            continue
        kept += 1
    return {"input": len(rows), "kept": kept, "rejected_by_rule": counts}


def write_selection_json(path: Path, formal: list[dict], dev: dict | None,
                         env_blocked: list[dict], dataset_version: str) -> Path:
    """冻结名单：正式组 + dev 预演题 + stage-0 淘汰记录。冻结后不允许换题。"""
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "rules_version": SELECTION_RULES_VERSION,
        "dataset": {"id": "SWE-bench/SWE-bench_Lite", "commit": dataset_version,
                    "formal_split": "test", "rehearsal_split": "dev"},
        "formal": formal,
        "rehearsal": dev,
        "environment_blocked": env_blocked,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
