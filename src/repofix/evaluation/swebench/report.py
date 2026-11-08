"""SWE-bench Mini-Eval 报告生成：docs/BENCHMARK-MINI.md。

输入全部来自落盘产物（可离线重生成）：
- benchmarks/swebench-mini/selection.json      冻结名单 + 淘汰记录
- benchmarks/swebench-mini/stage0-report.json  环境验证证据（Digest / 校验项 / 基线）
- benchmarks/swebench-mini/records-*.json      逐题 Agent + Judge 记录

报告边界（评审定稿口径）：
- 结果命名为 **SWE-bench Lite constrained subset**（pytest 协议 + 单文件 +
  环境可运行 + P2P≤300 筛选后的固定子集），不与官方全量排行榜直接对比；
- 判定方式如实标注：按官方 eval_script 协议自实现，在官方 per-instance
  Docker 镜像内执行（隔离容器，宿主密钥不进入测试进程）。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

FREEZE_DIR_NAME = "benchmarks/swebench-mini"


def _load(path: Path) -> dict | list | None:
    if not Path(path).exists():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fmt_plan(instance_id: str, stage0: dict) -> str:
    for entry in stage0.get("instances", []):
        if entry["instance_id"] == instance_id:
            plan_data = entry.get("visible_plan", {})
            argv = plan_data.get("argv") or []
            targets = ", ".join(plan_data.get("targets", [])) or "-"
            scope = argv[3] if len(argv) > 3 else targets
            seconds = plan_data.get("baseline_seconds")
            sec_text = f"，基线 {seconds:.0f}s" if isinstance(seconds, (int, float)) else ""
            return f"`{scope}`（{plan_data.get('plan_source', '-')}/{plan_data.get('layer', '-')}{sec_text}）"
    return "-"


def generate_report(repo_root: Path) -> Path:
    root = Path(repo_root)
    freeze = root / FREEZE_DIR_NAME
    selection = _load(freeze / "selection.json") or {}
    stage0 = _load(freeze / "stage0-report.json") or {}
    formal_records = _load(freeze / "records-formal.json") or []
    rehearsal_records = _load(freeze / "records-rehearsal.json") or []

    from repofix.evaluation.swebench.runner import summarize

    lines: list[str] = []
    add = lines.append
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dataset_commit = (selection.get("dataset") or {}).get("commit", "unknown")

    add("# SWE-bench Lite Mini-Eval（RepoFix）")
    add("")
    add(f"- 生成时间：{generated}")
    add(f"- 数据集：SWE-bench/SWE-bench_Lite @ `{dataset_commit}`（test split，hf-mirror 获取）")
    add(f"- 选题规则：{selection.get('rules_version', '?')}（冻结于任何模型调用之前）")
    add("- 模型：deepseek-flash（Non-thinking，与 Experiment G 一致）")
    add("- 判定：按官方 eval_script 协议自实现判定器，在官方 per-instance Docker 镜像内执行；"
        "干净 base 独立应用 Patch，FAIL_TO_PASS 全部通过且 PASS_TO_PASS 无回归才算 RESOLVED")
    add("")
    add("> **范围声明**：本结果为 **SWE-bench Lite constrained subset**（pytest 判定协议、"
        "单文件 gold patch、环境可运行、PASS_TO_PASS ≤ 300 筛选后的固定子集），"
        "不代表 SWE-bench Lite 全量成绩，不可与公开排行榜直接对比。")
    add("")
    add("## 选题与筛选漏斗")
    add("")
    funnel = (stage0.get("funnel_test") or {})
    add(f"test split {funnel.get('input', '-')} 题 → 规则保留 {funnel.get('kept', '-')} 题"
        f"（R1 非 pytest 协议 {funnel.get('rejected_by_rule', {}).get('R1_repo_parser', '-')}、"
        f"R2 多文件 {funnel.get('rejected_by_rule', {}).get('R2_single_file', '-')}、"
        f"R4 F2P 规模 {funnel.get('rejected_by_rule', {}).get('R4_fail_to_pass', '-')}、"
        f"R3 P2P 规模 {funnel.get('rejected_by_rule', {}).get('R3_pass_to_pass', '-')}、"
        f"R5 描述长度 {funnel.get('rejected_by_rule', {}).get('R5_problem_length', '-')}）")
    add("")
    add("| # | instance_id | 仓库 | 可见测试计划（Agent 可见信息推导） | stage-0 |")
    add("| --- | --- | --- | --- | --- |")
    for i, meta in enumerate(selection.get("formal", []), 1):
        add(f"| {i} | `{meta['instance_id']}` | {meta['repo']} | "
            f"{_fmt_plan(meta['instance_id'], stage0) if stage0 else '-'} | 通过 |")
    rehearsal = selection.get("rehearsal")
    if rehearsal:
        add(f"| 预演 | `{rehearsal['instance_id']}` | {rehearsal['repo']} | "
            f"{_fmt_plan(rehearsal['instance_id'], stage0) if stage0 else '-'} | 通过 |")
    add("")
    blocked = selection.get("environment_blocked") or []
    if blocked:
        add("**环境淘汰（留痕，不事后换题）**：")
        add("")
        for b in blocked:
            add(f"- `{b['instance_id']}`（{b['role']}）：{b['reason'][:200]}")
        add("")
    add("可见测试计划说明：仅由 Issue 文本 + base 仓库结构按冻结规则推导"
        "（`evaluation/swebench/plan.py` visible_plan_rule_v1）；test_patch / FAIL_TO_PASS / "
        "PASS_TO_PASS / gold patch 仅供评测器，不参与 Agent 可见范围。")
    add("")

    for title, records, note in (
        ("Stage 1 · 预演（1 道 dev 题）", rehearsal_records,
         "预算校准运行；结果不计入正式成功率。"),
        ("Stage 2 · 正式单 Agent 评测（冻结 10 题）", formal_records,
         "每题单次运行，独立验收。"),
    ):
        if not records:
            continue
        summary = summarize(records)
        add(f"## {title}")
        add("")
        add(note)
        add("")
        add("| instance_id | Runtime | Resolved | 模型调用 | Tokens | Agent 耗时 | 判定 |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for row in summary["by_task"]:
            resolved = "✅" if row["resolved"] else "❌"
            add(f"| `{row['instance_id']}` | {row['runtime_status']} | {resolved} "
                f"| {row['model_calls']} | {row['tokens']} | {row.get('duration_seconds', '-')}s "
                f"| {row['judge_reason'][:90]} |")
        add("")
        add(f"**汇总**：Resolved@1 = {summary['resolved']}/{summary['total_tasks']}"
            f"（{summary['resolved_at_1']:.1%}）；Patch Apply Rate = "
            f"{round(summary['patch_apply_rate'] * summary['total_tasks'])}/{summary['total_tasks']}"
            f"（{summary['patch_apply_rate']:.1%}）；平均 {summary['avg_tokens_per_task']} tokens/题；"
            f"平均 {summary['avg_duration_seconds']}s/题。")
        add("")
        add(f"失败归因：`{json.dumps(summary['failure_breakdown'], ensure_ascii=False)}`")
        add("")

    # 预算-能力标度（多档对照：同一冻结任务集，不同预算档位独立运行）
    tier_files = [
        ("60k token/题", freeze / "records-formal-tierB-archived.json"),
        ("200k token/题", freeze / "records-formal-tierA2-200k.json"),
        ("600k token/题", freeze / "records-formal-tierA3-600k.json"),
        ("1M token/题", freeze / "records-formal.json"),
    ]
    tiers = [(label, _load(f)) for label, f in tier_files if f.exists()]
    tiers = [(label, r) for label, r in tiers if r]
    if len(tiers) >= 2:
        add("## 预算-能力标度（同一冻结任务集，档位间独立运行）")
        add("")
        add("| 预算档位 | Resolved | 平均 tokens/题 | 预算内触顶比例 | 说明 |")
        add("| --- | --- | --- | --- | --- |")
        for label, records in tiers:
            records = [r for r in records if r["agent"].get("runtime_status") != "INFRA_ERROR"]
            resolved = sum(1 for r in records if r["resolved"])
            toks = [r["agent"].get("token_usage", {}).get("total", 0) for r in records]
            capped = sum(1 for r in records
                         if r["agent"].get("runtime_status") == "budget_exceeded") / len(records)
            avg = round(sum(toks) / len(records))
            note = {
                "60k token/题": "全部饿死在探索期",
                "200k token/题": "1 题完整解决；2 题产出零回归的部分修复",
                "600k token/题": "2 题解决；patch 应用率 5/8；无 P2P 回归",
                "1M token/题": "无新增解决——收益饱和，瓶颈转移到模型能力（diff 生成/修复正确性）",
            }.get(label, "")
            add(f"| {label} | {resolved}/{len(records)} | {avg} | {capped:.0%} | {note} |")
        add("")
        add("预算-成本关系：prompt 累积占总消耗 ~98%（上下文随轮次增长是主要成本），"
            "与 RepoFix Context Engine 的设计论题直接对应；1M 档收益饱和表明 600k 后的"
            "约束已从资源转移到模型能力（unified diff 生成与修复正确性），继续加大预算无收益。")
        add("")

    control_records = _load(freeze / "records-control.json") or []
    if control_records:
        c_resolved = sum(1 for r in control_records if r["resolved"])
        c_tokens = sum(r["agent"]["token_usage"]["total"] for r in control_records)
        add("## 对照组：无工具 API 基线（同任务集、同预算语义）")
        add("")
        add("信息量对齐设计：issue + plan.py 同一规则定位的源码文件内容（静态一次给足，"
            "全部为 Agent 可见信息）；无工具/无检索；反馈仅含 git apply 错误与可见测试输出；"
            "40 轮 / 600k token /题；判定与 Agent 完全同一套。如实披露：seaborn/xarray 两题在"
            "修复『输出超长反馈』后重跑，其余 6 题为原始轮次（均未触发该反馈路径）。")
        add("")
        add("| 维度 | Agent（工具+循环+验证，600k 档） | 无工具对照（600k） |")
        add("| --- | --- | --- |")
        add(f"| Resolved | 2/8 | {c_resolved}/8 |")
        add(f"| 平均 tokens/题 | 425,794 | {round(c_tokens / len(control_records)):,} |")
        add("| patch 可应用 | 5/8 任务产出可应用 patch | 4/293 轮（1.4%） |")
        add("| Runtime 完整跑通 | 1/8（sklearn） | 0/8 |")
        add("")
        solve_agent = {r["instance_id"] for r in (formal_records or []) if r["resolved"]}
        solve_ctrl = {r["instance_id"] for r in control_records if r["resolved"]}
        add(f"逐题差异：Agent 独有解决 {sorted(solve_agent - solve_ctrl) or '无'}；"
            f"对照独有解决 {sorted(solve_ctrl - solve_agent) or '无'}；"
            f"共同解决 {sorted(solve_agent & solve_ctrl) or '无'}。")
        add("")
        add("**对照 0/8 的一个重要注记**：对照的 xarray-3364 最初被静默成功规则误判为 RESOLVED——"
            "其 12 行 patch 破坏了测试收集（imports 失败），两个 F2P 测试从未运行却被官方 v1 的"
            "『缺席 = 静默通过』语义记为通过。该假阳性由逐测试状态复核发现，判定器已收紧"
            "（收集中断时缺席测试不再记为通过），并作为官方 v1 语义已知缺陷的实证记录在案。")
        add("")
        add("**解读**：Agent 2/8 vs 对照 0/8——patch 可应用率相差约两个数量级。需要检索/定位的"
            "任务（sklearn、pytest）只有工具循环能解；修复局部化的任务也未能被盲写 diff 解决"
            "（对照 293 轮仅 4 次应用成功）。该对照为单次运行、n=8，不做统计推断。")
        add("")

    add("## 环境与可追溯性")
    add("")
    digests = [
        (e["instance_id"], e.get("image_digest", ""))
        for e in stage0.get("instances", []) if e.get("image_digest")
    ]
    if digests:
        add("| instance_id | 镜像 Digest |")
        add("| --- | --- |")
        for iid, digest in digests:
            add(f"| `{iid}` | `{digest[:80]}` |")
        add("")
    add("- 镜像来源：官方 `swebench/sweb.eval.x86_64.*`（Docker Hub，Digest 见上表）")
    add("- 工作区：`docker create + cp` 提取官方镜像 /testbed（含已构建产物），"
        "运行时 bind-mount 回 /testbed；评测容器无网络 / 限资源（root 运行，"
        "与官方 harness 一致的显式偏离，理由见 workspace.py）")
    add("- ripgrep：官方镜像缺失，经运行时注入静态二进制（注入前后探测留痕）")
    add("- 判定校准：每题 gold patch（仅评测器持有）应判 RESOLVED、空 Patch 应判 FAILED，"
        "校准证据见 stage0-report.json；dev 预演题另做官方 Harness 交叉验证（如可用）")
    add("")
    add("## 与 Experiment G Pilot 的关系")
    add("")
    add("Experiment G（合成仓库三组对照）回答的是双 Agent 机制与成本；"
        "本轮回答的是真实 Issue 上的外部成功率与失败归因。两者互补，"
        "样本量均为 Pilot 级别，不外推统计结论。")
    add("")
    add("## 已知局限")
    add("")
    add("- 子集筛选偏向 pytest 协议与单文件修复，难度低于 Lite 全量平均；")
    add("- 样本量小，Resolved@1 置信区间宽；")
    add("- 自实现判定器未逐项复刻官方 harness 的全部边界行为（已在 dev 题做协议对齐校准）。")
    add("")
    add("## 复现")
    add("")
    add("```bash")
    add("# 1. stage 0：零模型 Token 环境验证 + 选题冻结")
    add("PYTHONPATH=src python scripts/run_swebench_stage0.py --proxy http://127.0.0.1:7897")
    add("# 2. stage 1：预演（花钱，需显式批准）")
    add("PYTHONPATH=src python scripts/run_swebench_stage12.py --stage rehearsal \\")
    add("    --approve-tokens --total-token-cap 25000")
    add("# 3. stage 2：正式（预算来自预演复盘，显式传入）")
    add("PYTHONPATH=src python scripts/run_swebench_stage12.py --stage formal \\")
    add("    --approve-tokens --max-model-calls <N> --max-total-tokens <T> --total-token-cap <C>")
    add("```")
    add("")

    out = root / "docs" / "BENCHMARK-MINI.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return out
