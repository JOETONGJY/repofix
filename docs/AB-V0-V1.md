# RepoFix v0 vs v1 版本对照（SWE-bench Mini-Eval 8 题，A/B）

- 日期：2026-11-09
- 代码：v0 = b941bd6 前的 strict 白名单 + 原始 Context（历史记录，元数据：deepseek-flash Non-thinking、
  冻结 8 题 selection_rules_v1、官方镜像 + 官方协议判定器、600k token/题档）；v1 = `0658430`
  （coding_benchmark 命令档 + Context Recency N=6），650k token/题、60 模型调用/题
- 运行：每题单次，失败不重跑不换题；判定与信息隔离协议两臂完全一致
- 数据来源：全部指标取自真实 Trace + 权威 SQLite（`scripts/compare_arms.py`，无离线模拟）；
  两臂 Trace 的 PREFLIGHT_COMPLETED 事件均记录实际生效配置（v1 全 8 题为
  `('coding_benchmark', 6)`；v0 为历史档无此字段）；v1 的 3 个 RESOLVED 经独立复验，
  F2P 均为显式 PASSED（非 silent-success 假阳性），证据 `repofix-data/swebench/v1-resolved-reverify.json`

## 结果总表

| 指标 | v0（600k 档） | v1（650k 档） | 变化 |
| --- | --- | --- | --- |
| **Resolved@1** | **2/8** | **3/8（37.5%）** | +1（pylint-5859 新解决） |
| Patch Apply Rate | 5/8 | 6/8 | +1 |
| 平均 tokens/题 | 425,794 | 402,964 | -5.4% |
| 平均每轮 prompt | 12,218 | 7,074 | **-42.1%** |
| 每轮最大 prompt | 19,656 | 10,346 | **-47.4%** |
| tokens/模型调用 | 12,854 | 7,310 | **-43.1%** |
| 预算耗尽（token） | 4/8 | 0/8 | token 不再是约束 |
| 预算耗尽（工具调用） | 0/8 | 7/8 | 新瓶颈（见下） |
| no_progress 终止 | 3/8 | 0/8 | 循环卡死消失 |
| finish 完整走通 | 1/8 | 1/8 | — |
| 命令拒绝 | 11 | 19 | 类别不同（见下） |
| 批次级联 | 2 | 2 | — |
| Trace 完整/一致 | 8/8 | 8/8 | run_id 与记录一致，无陈旧副本 |
| INFRA/INCONCLUSIVE | 0 | 0 | — |

逐题（res = RESOLVED，p = 应用成功/尝试次数）：

| instance | v0 | v1 |
| --- | --- | --- |
| pytest-11143 | ✅ 204k 20c | ✅ 548k 58c |
| scikit-learn-10297 | ✅ 237k 21c | ✅ 427k 59c |
| pylint-5859 | ❌ 518k 43c | ✅ **353k 54c（新解决，比 v0 少花 32% token）** |
| matplotlib-18869 | ❌ 602k 46c | ❌ 430k 54c |
| sphinx-10325 | ❌ 616k 41c | ❌ 325k 51c |
| seaborn-2848 | ❌ 603k 38c | ❌ 379k 53c |
| xarray-3364 | ❌ 327k 28c | ❌ 377k 52c |
| flask-4992 | ❌ 300k 28c | ❌ 384k 60c |

## 三个层面的核验结论（对照预定的决策标准）

**① 系统稳定性：通过。** 8/8 题记录、Trace、Judge 结果完整；Trace run_id 与逐题记录一致（无陈旧副本）；
零 INFRA_ERROR、零 INCONCLUSIVE；两臂判定协议相同。正常失败（预算/能力）均有 Trace 归因。

**② 优化真实生效（Trace 实证，非模拟）：**
- 只读命令（grep/rg/ls）真实使用：8/8 任务共 **23 次成功执行**（seaborn 8、sklearn 5、pytest 4）——
  v0 中这些全部是被拒的无效轮次；
- Context Recency 实测生效：**平均每轮 prompt 12,218 → 7,074（-42%）**、峰值 19,656 → 10,346（-47%），
  平均 tokens/题下降 5.4%（调用数增加的背景下）；
- 命令拒绝性质变化：v0 的 11 次全是"白名单没有该命令"（grep/ls 类摩擦）；v1 的 19 次里
  13 次是 `python -c`（内联实验代码，属白名单设计边界）、4 次 `sed`、1 次 `cat`、1 次 `ls ..`（越界被正确拒绝）；
- 批次级联 2→2（每臂 1 次发生在旧只读语义、1 次在新语义下的越界拒绝——量级未放大）。

**③ 解决率信号：正向。** Resolved@1 2/8 → 3/8（+1）；新增解决的 pylint-5859 是 v0 在全部四个
预算档位（200k/600k/1M/650k）从未解决的题，且 v1 花费比 v0 少 32%。无任务回退（v0 解决的两题 v1 均保持解决）。

## 新瓶颈与新观察（如实在案）

1. **约束从 token 转移到工具调用次数**：v1 有 7/8 题触顶 60 次调用上限（token 只剩约束的影子——
   平均 403k 远低于 650k 上限）。`max_tool_calls` 与 `max_model_calls` 的配比（60:1）成为新的
   主要约束，扩展评测前应重新校准（如 100-120 次调用）。
2. **`python -c` 被拒 13 次**：模型想用内联脚本做小实验验证假设。这是白名单的**设计边界**
   （`python -c` 可执行任意代码，与任意 Shell 等价，拒绝是正确的），但值得在 System Prompt 中
   明确告知"内联脚本不可用，请写临时测试文件用 pytest 验证"，减少无效轮次。
3. **apply_patch 失败率仍高**（v1 181 次 failed vs 15 次 ok）——模型 diff 生成能力依旧是最终
   天花板（与四档预算实验结论一致）。
4. **实验口径说明**：A 臂为历史基线（600k 档记录），B 臂为新运行（650k/题），预算档位相差 8%
   且两臂非同步运行——本对照是**版本演进对照**，不是严格受控实验；n=8 不做统计显著性推断。

## 定版决策（对照预定标准）

> 预定标准："链路稳定，v1 表现更好 → 冻结 v1.0，进入 20-50 题扩展评测"

链路稳定性通过（①），优化真实生效（②，-42% 每轮 prompt、只读命令 23 次真实使用），
解决率正向（③，2/8 → 3/8、零回退）。**满足冻结 v1.0 的条件**。

- v1.0 = commit `0658430`，配置：`--command-policy coding_benchmark --context-recent-full-turns 6`
- v0 基线（2/8@600k）与本报告作为历史对照永久保留，不覆盖
- 定版 ≠ 证明 v1 优于 v0 的统计结论（n=8 版本对照）；20-50 题扩展评测才是回答
- 扩展评测前的两个配置校准项：max_model_calls/max_tool_calls 配比、System Prompt 的
  `python -c` 边界告知

## 复现

```bash
# B 臂（v1）
PYTHONPATH=src python scripts/run_swebench_stage12.py --stage formal --approve-tokens   --arm-tag v1-ab --command-policy coding_benchmark --context-recent-full-turns 6   --max-model-calls 60 --max-total-tokens 650000 --total-token-cap 5200000 --timeout 5400
# 对照分析（零 Token）
PYTHONPATH=src python scripts/compare_arms.py   --a-records benchmarks/swebench-mini/records-formal-tierA3-600k.json   --a-traces repofix-data/swebench/runs-tierA3-600k --a-label v0-600k   --b-records benchmarks/swebench-mini/records-formal-v1-ab.json   --b-traces repofix-data/swebench/runs --b-label v1-650k
```
