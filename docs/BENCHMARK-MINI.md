# SWE-bench Lite Mini-Eval（RepoFix）

- 生成时间：2026-11-08 03:54 UTC
- 数据集：SWE-bench/SWE-bench_Lite @ `b0dde1093fe417d83b7184254edf8199c1f0dff5`（test split，hf-mirror 获取）
- 选题规则：selection_rules_v1（冻结于任何模型调用之前）
- 模型：deepseek-flash（Non-thinking，与 Experiment G 一致）
- 判定：按官方 eval_script 协议自实现判定器，在官方 per-instance Docker 镜像内执行；干净 base 独立应用 Patch，FAIL_TO_PASS 全部通过且 PASS_TO_PASS 无回归才算 RESOLVED

> **范围声明**：本结果为 **SWE-bench Lite constrained subset**（pytest 判定协议、单文件 gold patch、环境可运行、PASS_TO_PASS ≤ 300 筛选后的固定子集），不代表 SWE-bench Lite 全量成绩，不可与公开排行榜直接对比。

## 选题与筛选漏斗

test split 300 题 → 规则保留 95 题（R1 非 pytest 协议 191、R2 多文件 0、R4 F2P 规模 2、R3 P2P 规模 7、R5 描述长度 5）

| # | instance_id | 仓库 | 可见测试计划（Agent 可见信息推导） | stage-0 |
| --- | --- | --- | --- | --- |
| 1 | `scikit-learn__scikit-learn-10297` | scikit-learn/scikit-learn | `sklearn/linear_model/tests/test_ridge.py`（issue_scoped/L1） | 通过 |
| 2 | `matplotlib__matplotlib-18869` | matplotlib/matplotlib | `lib/matplotlib/tests/test_image.py`（issue_scoped/L1） | 通过 |
| 3 | `sphinx-doc__sphinx-10325` | sphinx-doc/sphinx | `tests/test_docutilsconf.py`（issue_scoped/L2） | 通过 |
| 4 | `pytest-dev__pytest-11143` | pytest-dev/pytest | `testing/example_scripts/marks/marks_considered_keywords/test_marks_as_keywords.py`（issue_scoped/L2） | 通过 |
| 5 | `pylint-dev__pylint-5859` | pylint-dev/pylint | `tests/config/test_config.py`（issue_scoped/L2） | 通过 |
| 6 | `mwaskom__seaborn-2848` | mwaskom/seaborn | `tests/_core/test_plot.py`（issue_scoped/L2） | 通过 |
| 7 | `pydata__xarray-3364` | pydata/xarray | `xarray/tests/test_coding.py`（issue_scoped/L2） | 通过 |
| 8 | `pallets__flask-4992` | pallets/flask | `tests/test_appctx.py`（issue_scoped/L2） | 通过 |
| 预演 | `pylint-dev__astroid-1196` | pylint-dev/astroid | `tests/unittest_inference.py`（issue_scoped/L3） | 通过 |

**环境淘汰（留痕，不事后换题）**：

- `astropy__astropy-12907`（formal）：[{"command": "python -m pip install -e .[test] --verbose", "rc": 1, "mode": "official", "fallback_rc": 1, "fallback_command": "python -m pip install -e .[test] --verbose --no-build-isolation --no-deps
- `psf__requests-1963`（formal）：outcome=FAILED f2p_failed=7/7 p2p_regressed=42 fail_to_pass not all passing (7/7 failed); pass_to_pass regressions: 42
- `pvlib__pvlib-python-1072`（rehearsal）：outcome=INCONCLUSIVE f2p_failed=1/1 p2p_regressed=18 no test statuses parsed from output; parser or env mismatch

可见测试计划说明：仅由 Issue 文本 + base 仓库结构按冻结规则推导（`evaluation/swebench/plan.py` visible_plan_rule_v1）；test_patch / FAIL_TO_PASS / PASS_TO_PASS / gold patch 仅供评测器，不参与 Agent 可见范围。

## Stage 1 · 预演（1 道 dev 题）

预算校准运行；结果不计入正式成功率。

| instance_id | Runtime | Resolved | 模型调用 | Tokens | Agent 耗时 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| `pylint-dev__astroid-1196` | budget_exceeded | ❌ | 6 | 22027 | 16.6s | no test statuses parsed and test command failed; parser or env mismatch |

**汇总**：Resolved@1 = 0/1（0.0%）；Patch Apply Rate = 1/1（100.0%）；平均 22027 tokens/题；平均 16.6s/题。

失败归因：`{"agent_budget_exceeded": 1}`

## Stage 2 · 正式单 Agent 评测（冻结 10 题）

每题单次运行，独立验收。

| instance_id | Runtime | Resolved | 模型调用 | Tokens | Agent 耗时 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| `pytest-dev__pytest-11143` | budget_exceeded | ✅ | 20 | 204401 | 55.3s | all FAIL_TO_PASS and PASS_TO_PASS tests passed on independently applied patch (official se |
| `scikit-learn__scikit-learn-10297` | completed | ✅ | 21 | 236668 | 331.2s | all FAIL_TO_PASS and PASS_TO_PASS tests passed on independently applied patch (official se |
| `matplotlib__matplotlib-18869` | failed | ❌ | 36 | 359871 | 468.4s | fail_to_pass failures: 4/4; pass_to_fail regressions: 0 (official semantics: explicit FAIL |
| `sphinx-doc__sphinx-10325` | budget_exceeded | ❌ | 56 | 881960 | 264.6s | fail_to_pass failures: 1/1; pass_to_fail regressions: 0 (official semantics: explicit FAIL |
| `pylint-dev__pylint-5859` | budget_exceeded | ❌ | 58 | 752676 | 245.3s | fail_to_pass failures: 1/1; pass_to_fail regressions: 0 (official semantics: explicit FAIL |
| `mwaskom__seaborn-2848` | budget_exceeded | ❌ | 54 | 952892 | 179.6s | fail_to_pass failures: 1/1; pass_to_fail regressions: 0 (official semantics: explicit FAIL |
| `pydata__xarray-3364` | budget_exceeded | ❌ | 48 | 770356 | 156.8s | fail_to_pass failures: 2/2; pass_to_fail regressions: 2 (official semantics: explicit FAIL |
| `pallets__flask-4992` | budget_exceeded | ❌ | 59 | 737609 | 208.1s | test collection/import failed with the applied patch (broken patch); no per-test statuses  |

**汇总**：Resolved@1 = 2/8（25.0%）；Patch Apply Rate = 6/8（75.0%）；平均 612054 tokens/题；平均 238.7s/题。

失败归因：`{"resolved": 2, "f2p_not_fixed": 3, "patch_apply_failed": 2, "f2p_fail_and_regression": 1}`

## 预算-能力标度（同一冻结任务集，档位间独立运行）

| 预算档位 | Resolved | 平均 tokens/题 | 预算内触顶比例 | 说明 |
| --- | --- | --- | --- | --- |
| 60k token/题 | 0/7 | 63968 | 100% | 全部饿死在探索期 |
| 200k token/题 | 1/8 | 207327 | 100% | 1 题完整解决；2 题产出零回归的部分修复 |
| 600k token/题 | 2/8 | 425794 | 50% | 2 题解决；patch 应用率 5/8；无 P2P 回归 |
| 1M token/题 | 2/8 | 612054 | 75% | 无新增解决——收益饱和，瓶颈转移到模型能力（diff 生成/修复正确性） |

预算-成本关系：prompt 累积占总消耗 ~98%（上下文随轮次增长是主要成本），与 RepoFix Context Engine 的设计论题直接对应；1M 档收益饱和表明 600k 后的约束已从资源转移到模型能力（unified diff 生成与修复正确性），继续加大预算无收益。

## 对照组：无工具 API 基线（同任务集、同预算语义）

信息量对齐设计：issue + plan.py 同一规则定位的源码文件内容（静态一次给足，全部为 Agent 可见信息）；无工具/无检索；反馈仅含 git apply 错误与可见测试输出；40 轮 / 600k token /题；判定与 Agent 完全同一套。如实披露：seaborn/xarray 两题在修复『输出超长反馈』后重跑，其余 6 题为原始轮次（均未触发该反馈路径）。

| 维度 | Agent（工具+循环+验证，600k 档） | 无工具对照（600k） |
| --- | --- | --- |
| Resolved | 2/8 | 0/8 |
| 平均 tokens/题 | 425,794 | 253,707 |
| patch 可应用 | 5/8 任务产出可应用 patch | 4/293 轮（1.4%） |
| Runtime 完整跑通 | 1/8（sklearn） | 0/8 |

逐题差异：Agent 独有解决 ['pytest-dev__pytest-11143', 'scikit-learn__scikit-learn-10297']；对照独有解决 无；共同解决 无。

**对照 0/8 的一个重要注记**：对照的 xarray-3364 最初被静默成功规则误判为 RESOLVED——其 12 行 patch 破坏了测试收集（imports 失败），两个 F2P 测试从未运行却被官方 v1 的『缺席 = 静默通过』语义记为通过。该假阳性由逐测试状态复核发现，判定器已收紧（收集中断时缺席测试不再记为通过），并作为官方 v1 语义已知缺陷的实证记录在案。

**解读**：Agent 2/8 vs 对照 0/8——patch 可应用率相差约两个数量级。需要检索/定位的任务（sklearn、pytest）只有工具循环能解；修复局部化的任务也未能被盲写 diff 解决（对照 293 轮仅 4 次应用成功）。该对照为单次运行、n=8，不做统计推断。

## 环境与可追溯性

| instance_id | 镜像 Digest |
| --- | --- |
| `scikit-learn__scikit-learn-10297` | `swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-10297@sha256:58b80e9100` |
| `matplotlib__matplotlib-18869` | `swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-18869@sha256:b336640dcab060` |
| `pytest-dev__pytest-11143` | `swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-11143@sha256:e08ce142adec5ec985` |
| `astropy__astropy-12907` | `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907@sha256:e082963099ed7d5a5f75` |
| `psf__requests-1963` | `swebench/sweb.eval.x86_64.psf_1776_requests-1963@sha256:5ca751c160affa98d1608316` |
| `pylint-dev__pylint-5859` | `swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-5859@sha256:12412d9d1d7a06b8fa3` |
| `mwaskom__seaborn-2848` | `swebench/sweb.eval.x86_64.mwaskom_1776_seaborn-2848@sha256:d719470351e66623e532a` |
| `pydata__xarray-3364` | `swebench/sweb.eval.x86_64.pydata_1776_xarray-3364@sha256:8591047f512d8d26b4d0e12` |
| `pallets__flask-4992` | `swebench/sweb.eval.x86_64.pallets_1776_flask-4992@sha256:bb1460876a7bc667f71b46a` |
| `pvlib__pvlib-python-1072` | `swebench/sweb.eval.x86_64.pvlib_1776_pvlib-python-1072@sha256:c8e43bebd10d7d2330` |
| `sphinx-doc__sphinx-10325` | `swebench/sweb.eval.x86_64.sphinx-doc_1776_sphinx-10325@sha256:32e0fe0fa8f3c0b48f` |
| `pylint-dev__astroid-1196` | `swebench/sweb.eval.x86_64.pylint-dev_1776_astroid-1196@sha256:ddf6350d124714cb8c` |
| `sqlfluff__sqlfluff-1517` | `swebench/sweb.eval.x86_64.sqlfluff_1776_sqlfluff-1517@sha256:6e93dc479c481210c7a` |

- 镜像来源：官方 `swebench/sweb.eval.x86_64.*`（Docker Hub，Digest 见上表）
- 工作区：`docker create + cp` 提取官方镜像 /testbed（含已构建产物），运行时 bind-mount 回 /testbed；评测容器无网络 / 限资源（root 运行，与官方 harness 一致的显式偏离，理由见 workspace.py）
- ripgrep：官方镜像缺失，经运行时注入静态二进制（注入前后探测留痕）
- 判定校准：每题 gold patch（仅评测器持有）应判 RESOLVED、空 Patch 应判 FAILED，校准证据见 stage0-report.json；dev 预演题另做官方 Harness 交叉验证（如可用）

## 与 Experiment G Pilot 的关系

Experiment G（合成仓库三组对照）回答的是双 Agent 机制与成本；本轮回答的是真实 Issue 上的外部成功率与失败归因。两者互补，样本量均为 Pilot 级别，不外推统计结论。

## 已知局限

- 子集筛选偏向 pytest 协议与单文件修复，难度低于 Lite 全量平均；
- 样本量小，Resolved@1 置信区间宽；
- 自实现判定器未逐项复刻官方 harness 的全部边界行为（已在 dev 题做协议对齐校准）。

## 复现

```bash
# 1. stage 0：零模型 Token 环境验证 + 选题冻结
PYTHONPATH=src python scripts/run_swebench_stage0.py --proxy http://127.0.0.1:7897
# 2. stage 1：预演（花钱，需显式批准）
PYTHONPATH=src python scripts/run_swebench_stage12.py --stage rehearsal \
    --approve-tokens --total-token-cap 25000
# 3. stage 2：正式（预算来自预演复盘，显式传入）
PYTHONPATH=src python scripts/run_swebench_stage12.py --stage formal \
    --approve-tokens --max-model-calls <N> --max-total-tokens <T> --total-token-cap <C>
```
