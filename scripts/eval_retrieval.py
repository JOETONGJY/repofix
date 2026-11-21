"""冻结任务集的检索离线评估（MVP v0.5.3，零 Token）。

从官方镜像提取 testbed（复用 swebench 提取机制，结果缓存复用）→
构建 CodeRetriever → 用 dataset 的 problem_statement 检索 → 与 gold patch
的生产文件/行范围对比 → 输出逐题结果与聚合指标。

变体（评审 A/B/C 设计，同一数据集同一预算）——**消融实验，不是历史版本
复现**（评审问题 2：keyword_only 关闭旧版本没有的内容匹配、头窗口与旧版
6000 字符头读不等价；跨变体结论只能表述为"同一评估框架内增加信号/窗口
后检索指标如何变化"，不得宣称"比部署过的旧版提升 X%"）：
  keyword_only   消融：仅路径/文件名评分 + 文件头窗口（无锚定/符号/内容）
  kernel_head    统一内核全信号 + 文件头窗口                = v0.5.1
  kernel_windows 统一内核 + 锚定/符号/内容命中行窗口        = v0.5.2+

用法：python scripts/eval_retrieval.py --out benchmarks/swebench-mini/retrieval-eval.json
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from repofix.context.retrieval import (  # noqa: E402
    OVERVIEW_SCOPE,
    TEST_AGENT_SCOPE,
    CodeLocation,
    CodeRetriever,
    RetrievalResult,
    RetrievalScope,
    build_code_windows,
)
from repofix.context.symbol_index import build_symbol_index  # noqa: E402
from repofix.evaluation.retrieval_eval import (  # noqa: E402
    VARIANT_SIGNALS,
    GoldLabels,
    TaskRetrievalEval,
    evaluate_retrieval,
    map_ranges_to_symbols,
    parse_gold_patch,
    picked_locations,
    summarize,
)
from repofix.evaluation.swebench.workspace import extract_testbed  # noqa: E402
from repofix.workspace.docker_cli import DockerCli  # noqa: E402

_SCOPE = TEST_AGENT_SCOPE
WIDE_SCOPE = RetrievalScope(3000, 8)  # 宽扫描：测评分质量本身（scan 饥饿另报）


def load_dataset(dataset_dir: Path) -> dict[str, dict]:
    """test.parquet → instance_id → 行字典（problem_statement/gold_patch/image/base_commit）。"""
    import pyarrow.parquet as pq

    table = pq.read_table(dataset_dir / "test.parquet",
                          columns=["instance_id", "problem_statement", "patch",
                                   "image", "base_commit"])
    rows = {}
    for batch in table.to_batches():
        columns = batch.to_pydict()
        for i, instance_id in enumerate(columns["instance_id"]):
            rows[instance_id] = {
                "problem_statement": columns["problem_statement"][i] or "",
                "gold_patch": columns["patch"][i] or "",
                "image": columns["image"][i] or "",
                "base_commit": columns["base_commit"][i] or "",
            }
    return rows


def ensure_testbed(cli: DockerCli, image: str, instance_id: str, base_commit: str,
                   extract_root: Path) -> tuple[Path | None, str]:
    """提取（含 base_commit checkout）或复用已有 testbed。返回 (路径, 错误)。"""
    dest = extract_root / instance_id
    if (dest / ".git").exists():
        return dest, ""
    dest.mkdir(parents=True, exist_ok=True)  # extract_testbed 要求目标目录已存在
    try:
        extract_testbed(cli, image, dest, base_commit=base_commit)
        return dest, ""
    except Exception as exc:  # noqa: BLE001 —— 单任务提取失败不阻塞其余任务
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


def evaluate_variant(instance_id: str, variant: str, testbed_dir: Path,
                     labels: GoldLabels, top_k: int, window_budget: int) -> dict:
    scope = _SCOPE  # 模块级：由 --scope 决定（wide 用于把"评分质量"与"扫描预算"解耦）
    retriever = CodeRetriever(testbed_dir, scope, signals=VARIANT_SIGNALS[variant])
    result = retriever.rank(labels.issue_text or "")
    # 生产对齐（评审 P0-1）：候选选取含结构回退，与 select_issue_files 同语义
    picked = picked_locations(result, top_k)
    effective = RetrievalResult(
        locations=picked, anchored_matched=result.anchored_matched,
        keywords=result.keywords, symbol_hits=result.symbol_hits,
        scanned_files=result.scanned_files, scan_truncated=result.scan_truncated)
    if variant == "kernel_windows":
        windows = build_code_windows(testbed_dir, picked,
                                     index=retriever.index,
                                     keywords=result.keywords,
                                     max_total_chars=window_budget)
    else:
        # 文件头窗口（head fallback 语义与 v0.5.2 的无信号回退一致）
        windows = build_code_windows(
            testbed_dir,
            [CodeLocation(path=loc.path, tier=loc.tier, score=loc.score)
             for loc in picked],
            index=retriever.index, max_total_chars=window_budget)
    record = evaluate_retrieval(labels, effective, windows, top_k=top_k)
    record.variant = variant
    return record.to_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline retrieval evaluation (zero tokens)")
    parser.add_argument("--records", default="benchmarks/swebench-mini/records-formal-v1-48.json")
    parser.add_argument("--dataset-dir", default="repofix-data/swebench/dataset")
    parser.add_argument("--extract-root", default="repofix-data/swebench/agent-testbeds")
    parser.add_argument("--out", default="benchmarks/swebench-mini/retrieval-eval.json")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--window-budget", type=int, default=18000)
    parser.add_argument("--max-tasks", type=int, default=0, help="0 = 全部")
    parser.add_argument("--variants", default="keyword_only,kernel_head,kernel_windows")
    parser.add_argument("--scope", default="test_agent", choices=["overview", "test_agent", "wide"])
    args = parser.parse_args()

    records = json.loads(Path(args.records).read_text(encoding="utf-8"))
    instance_ids = list(dict.fromkeys(r["instance_id"] for r in records))
    if args.max_tasks:
        instance_ids = instance_ids[:args.max_tasks]
    dataset = load_dataset(Path(args.dataset_dir))
    cli = DockerCli()
    extract_root = Path(args.extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    global _SCOPE
    _SCOPE = {"overview": OVERVIEW_SCOPE, "test_agent": TEST_AGENT_SCOPE,
              "wide": WIDE_SCOPE}[args.scope]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    all_records: list[dict] = []
    skips: Counter = Counter()
    for index, instance_id in enumerate(instance_ids, start=1):
        row = dataset.get(instance_id)
        if row is None:
            skips["no_dataset_row"] += 1
            continue
        labels = parse_gold_patch(instance_id, row["gold_patch"])
        labels.issue_text = row["problem_statement"]
        testbed, error = ensure_testbed(cli, row["image"], instance_id,
                                        row["base_commit"], extract_root)
        if testbed is None:
            skips["extract_failed"] += 1
            all_records.append({"instance_id": instance_id, "variant": "",
                                "error": f"extract failed: {error}"})
            continue
        # 函数级标签需要 testbed 的 AST 索引（end_line）
        label_index = build_symbol_index(testbed)
        labels.labels = map_ranges_to_symbols(labels.ranges, label_index)
        for variant in variants:
            try:
                record = evaluate_variant(instance_id, variant, testbed, labels,
                                          args.top_k, args.window_budget)
            except Exception as exc:  # noqa: BLE001
                record = {"instance_id": instance_id, "variant": variant,
                          "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            all_records.append(record)
        print(f"[{index}/{len(instance_ids)}] {instance_id}: "
              f"gold={len(labels.files)} files, variants done", flush=True)

    # P0-2（评审）：全部记录按变体归属传给 summarize——由它统一排除错误/
    # 新建文件任务并报告各组分母；另计算"共同有效任务集"汇总，供跨变体
    # 对比时避免分母不一致。
    def to_eval(r: dict) -> TaskRetrievalEval:
        return TaskRetrievalEval(**{k: v for k, v in r.items()
                                    if k in TaskRetrievalEval.__dataclass_fields__})

    summary = {}
    for variant in variants:
        variant_records = [to_eval(r) for r in all_records if r.get("variant") == variant]
        summary[variant] = summarize(variant_records)

    valid_ids_by_variant = []
    for variant in variants:
        ids = {r["instance_id"] for r in all_records
               if r.get("variant") == variant and not r.get("error")
               and not r.get("impossible_for_retrieval")}
        valid_ids_by_variant.append(ids)
    common_ids = set.intersection(*valid_ids_by_variant) if valid_ids_by_variant else set()
    common_summary = {}
    for variant in variants:
        common_records = [to_eval(r) for r in all_records
                          if r.get("variant") == variant and r["instance_id"] in common_ids]
        common_summary[variant] = summarize(common_records)

    output = {
        "variants": variants,
        "variant_signals": VARIANT_SIGNALS,
        "top_k": args.top_k,
        "window_budget": args.window_budget,
        "scope": args.scope,
        "task_count": len(instance_ids),
        "skips": dict(skips),
        "summary": summary,
        "common_valid_summary": {
            "task_count": len(common_ids),
            "by_variant": common_summary,
        },
        "records": all_records,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
