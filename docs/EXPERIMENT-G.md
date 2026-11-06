# Experiment G：单 Agent vs 双 Agent vs 预算匹配对照（Pilot，n=2/组）

- 日期：2026-11-06
- 模型：deepseek-flash（Non-thinking，thinking 显式 disabled）
- RepoFix 版本：c74401e + 边界修复（fdb6d0e）
- 仓库：合成 Bug 仓库 ×2（slugify 空白处理 / clamp 边界颠倒），隔离副本 + Docker 沙箱
- Token 消耗：总计 73,294（全部真实 DeepSeek API 调用）

## 三组配置

| 组 | 配置 | 任务 |
| --- | --- | --- |
| A | 单 Agent Baseline（--no-review） | slugify + clamp |
| B | 双 Agent（--review，Reviewer 3 轮/8 只读工具独立预算） | 同上 |
| bm | 预算对照单 Agent（上限对齐 B 组**实际消耗**；实际每任务仍仅用 6/7 次——**观察性对照，非严格预算匹配**） | 同上 |

## 结果

| Group | Task | Status | Verify | Review | Calls | Tokens |
|---|---|---|---|---|---|---|
| A | slugify | completed | passed | - | 6 | 11,162 |
| A | clamp | completed | passed | - | 6 | 11,613 |
| B | slugify | completed | passed | APPROVED | 7 | 14,072 |
| B | clamp | completed | passed | INCONCLUSIVE | 6 | 11,052 |
| BM | slugify | completed | passed | - | 6 | 11,289 |
| BM | clamp | completed | passed | - | 7 | 14,106 |

**汇总**：A = 2/2 成功、22,775 tokens；B = 2/2 成功、25,124 tokens（**+10.3%**）；BM = 2/2 成功、25,395 tokens。三组独立验收（可见测试）全部通过。

## 结论（如实，基于小样本 n=2/组——本报告为 **Pilot**，验证评测流程；不构成双 Agent 质量结论）

1. **简单任务上三组成功率相同（2/2）**——Reviewer 没有带来额外修复收益，也没有造成损失。
2. **双 Agent 成本 +10.3%**（Review 轮的额外模型调用与 Token），来自 Reviewer 探索 + 结论提交。
3. **Reviewer Finding 表现**：1 次 APPROVED（绑定 patch_hash）、1 次 INCONCLUSIVE；**Finding 数量 0，Finding Precision = N/A（暂无可评估样本）**——不据此声称 Reviewer 有效或无效。
4. **预算语义验证**：组 B clamp 的 Reviewer INCONCLUSIVE 恰好证明 Reviewer 独立预算（3 轮）在不阻断主任务的前提下正确截断（RV-05/RV-06）。
5. **样本量声明**：n=2/组不足以推广到复杂任务；本实验验证的是**评测器与三组对照流程端到端可用**，扩展到 20–30 任务后的数据才有统计意义（design/09 的既定立场）。

## 回答核心问题（design/09）

> 独立审查是否比单纯增加推理预算更有价值？

**在简单修复任务上：当前无差异，双 Agent 多花 10% Token。** 这不是 Reviewer 无价值的最终结论——简单任务本就难以暴露审查价值——但它客观量化了成本，并验证了评测框架能回答这个问题。扩展任务集后的对照实验可直接复用本流程。

## 复现

```bash
# 每组按 plan.md/README 的 Quick Start 构建 repofix-env 镜像后：
repofix run --repo <repo> --task "<task>" --test-command "python -m pytest -q --tb=no -rf" \
  --output-dir repofix-data/experiment-g/group-a --max-model-calls 6    # 组 A
# 组 B 加 --review；组 bm 用 --max-model-calls 13
```

完整 Trace：`repofix-data/experiment-g/group-{a,b,bm}/runs/<run_id>/`（trajectory.jsonl / result.json / changes.patch / test-report.json）。


## 追加：独立隐藏测试验收（评审 P0-3 闭环证据）

> 口径区分：正文的可见测试通过（6/6）是 **Agent 工作区内**的验证；本节隐藏验收是**独立工作区**的判据——两者不同。B 组隐藏验收结果为 **1/2**（clamp SUCCESS；slugify 发现真实缺陷）。

评审后补跑 ResultJudge 真实隐藏验收（隔离容器内执行，`isolated=True`，宿主密钥不进测试进程）：

| 任务 | 候选来源 | 应用 | 隐藏测试 | 结果 |
| --- | --- | --- | --- | --- |
| clamp | 组 B APPROVED Patch | 成功 | 通过（浮点/边界隐藏用例） | **SUCCESS** |
| slugify | 组 B APPROVED Patch | 成功 | **失败**（Tab 字符未折叠——Agent 修复与 Reviewer 审查均未覆盖） | **FAILED（真实缺陷）** |

**发现**：slugify 的修复只处理了空格，未处理任务契约中的 Tab 字符——可见测试、Reviewer 审查均未发现，独立隐藏测试验收抓到了这个真实缺陷。这验证了 design/09 的核心判断：**Reviewer APPROVED ≠ 修复正确，独立隐藏验收才是可信的成功判据**。评测隔离执行（非 root 容器、无宿主密钥）同时得到验证。

证据：`repofix-data/m4-hidden-acceptance.json`（汇总）+ 两次 judge 的完整记录。