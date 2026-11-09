# RepoFix 工具效率优化 v1（SWE-bench 扩展前）

- 日期：2026-11-08
- 范围：GPT 评审定义的 "SWE-bench Scalability & Tool Efficiency Optimization v1"——P0-1/2/3 + P1-1 全部落地，P1-2 为设计草图，P1-3 离线分析完成
- 原则：不改动 Main + Reviewer 架构、不开放任意 Shell、不引入 RAG/LLM Summary、不给模型任何隐藏信息；全部改动保持沙箱边界、审计与预算语义
- 测试：**326 passed, 3 skipped**（两轮合计新增 25 项）；ruff 全绿；端到端链路冒烟 20 项全过（零 Token）

## 一、实际修改

### P0-1 命令权限档位（strict / coding_benchmark）

- `docker_workspace.py`：`STRICT_COMMANDS`（v0 白名单，原 `ALLOWED_COMMANDS` 别名保留）+ 受控只读层 `grep / rg / ls`（`READONLY_TIER_PROGRAMS`）。
- 参数级校验 `validate_readonly_command`：拒绝绝对路径/`..` 逃逸、`grep -f/--file`、`rg --pre/--pre-glob/--hostname-bin` 等可执行外部程序的入口、组合短选项中的危险字母（`-f/-d/-z/-P`）。argv 数组无 Shell 的边界不变。
- `RunConfig.command_policy`（strict/coding_benchmark，默认 strict）+ CLI `--command-policy`；档位写入 Preflight 摘要 → Trace，运行期模型不可修改。
- Workspace（执行期）与 Dispatcher（校验期）双层同款校验——纵深防御。
- Reviewer 不受影响：其注册表视图本就无 `run_command`。

### P0-2 结构化检索增强

- `search_code` 新增 `max_per_file`（默认 5；v0 固定 `--max-count 1` 隐藏同文件后续匹配）与 `context`（默认 0，`-A N` 尾随上下文）参数，输出 Token 可控。
- `list_files` 重写为带剪枝的确定性遍历（`iter_workspace_files`）：`__pycache__/.git/build/dist/node_modules/.venv` 等 16 类产物目录不进入，大仓库不再全树扫描；输出格式与确定性不变。
- 工具描述与 System Prompt 明确：结构化工具优先，`grep/rg/ls` 是补充（`prompt_version` 语义随 System Prompt 变化，见下）。

### P0-3 Context 近期完整窗口（确定性摘要）

- `RunConfig.context_recent_full_turns`（默认 0 = 关闭，v0 行为不变）：最近 N 个模型-工具轮次保留完整工具输出，更早轮次确定性摘要——assistant 消息体置为占位、`tool_calls` 保留 id/name（配对必需）、arguments 置 `{}`、工具结果占位为 `[output elided (N chars): tool status]`。
- 配对完整性协议不受影响（tool_call_id 一一对应，回归测试锁定）；文件哈希仍由 WorkingMemory 块提供（旧读被摘要后 `apply_patch` 的 `expected_sha256` 依然可得）；SQLite 原始 Trace 完整保留。
- **重要发现（离线分析，见第三节）**：prompt 的大头不是工具输出，而是**模型自身历史输出（completion）逐轮重发计费**——所以摘要同时覆盖 assistant 消息体，这是与"只压工具输出"的本质区别。

### P1-1 命令拒绝的级联语义修订

- Trace 实证：`grep/ls` 拒绝按写失败级联，造成同批后续合法调用连坐跳过（600k 档 2 次）。
- 修订：`run_command` 的白名单校验**前移到 Dispatcher 语义校验期**（任何执行之前）；Scheduler 对**只读意图命令的校验期拒绝不再级联**（无副作用）；执行期失败、以及非只读意图（如 `pip install`）被拒的级联语义**不变**——保守边界未放松。
- RT-14（apply_patch 校验失败级联）等既有语义全部保留，回归测试锁定。

### P1-2 环境命令授权接口（设计草图，本轮不实现）

未来 20-50 题出现"需要 `pip install` / 仓库脚本"的任务时：

- `RunConfig.approved_setup_commands: list[list[str]]`——由 Benchmark Preflight 或受信任配置**显式列出**（精确 argv 模板，非前缀通配）；
- Preflight 在环境验证阶段实际执行并验证这些命令（安装产物进入镜像层或任务副本），Agent 运行期对它们的调用按已批准 argv 精确匹配放行；
- 授权清单、执行输出、环境变化全部写入 Trace 与评测报告；Agent 无法自行提升权限；安装步骤不接触 test_patch / F2P / 判定器文件。

### P1-3 离线分析工具

`scripts/analyze_context_efficiency.py`：从 trajectory.jsonl 统计每轮 prompt/completion/工具输出注入量（渲染后口径），并模拟近期窗口的确定性节省。可对任意历史 Trace 复用，零模型 Token。

## 二、离线效率分析结果（600k 档真实 Trace，诚实数据）

> **数据勘误（第二轮 Review 后）**：初版分析用的是陈旧 Trace 副本（run_id 改为
> 每次尝试唯一后，导出路径未同步，各档 trajectory.jsonl 都是 200k 档的旧内容；
> 该导出 bug 已修复，24 条历史 Trace 已从权威 SQLite 按 token 量匹配恢复）。
> 下表为恢复后的正确数据。

| 运行 | 轮数 | prompt 总量 | N=6 预计节省 | N=12 预计节省 |
| --- | --- | --- | --- | --- |
| sklearn-10297（completed） | 21 | 232,818 | 7.9% | 0.0% |
| matplotlib-18869 | 46 | 587,877 | 41.9% | 19.2% |
| sphinx-10325 | 41 | 606,974 | 44.9% | 24.0% |
| seaborn-2848 | 38 | 596,060 | 51.4% | 12.6% |
| pylint-5859 | 43 | 506,082 | 28.7% | 10.7% |

**三个诚实的结论**：

1. **真实 600k 档（21-46 轮长运行）上，近期窗口收益显著**：N=6 省 8-51%、N=12 省 0-24%。N=6 优于 N=12——更激进的窗口在长运行上收益更大，A/B 时建议以 N=6 为主配置、N=12 为对照。
2. **成本结构的关键发现（测量而非猜测）**：matplotlib 第 6-10 轮每轮仅注入 24-59 字符工具输出，prompt 却每轮增长 ~900 token——**大头是模型自身历史输出（每轮 ~1k completion token 的失败 diff 原文等）被逐轮重发计费**。context 摘要必须覆盖 assistant 消息体（已实现），仅压工具输出省不出成本。
3. **数据卫生教训**：Trace 副本与权威 SQLite 的不一致差点让优化建立在错误结论上（初版 0-9% 全部来自陈旧数据）。"链路上验证启用"必须包含"产物与存储一致"。

工具摩擦数据（P1-3 前置统计）：21 次 `permission_denied`（去重后 5/8 任务命中，`grep -n`/`ls` 为主）、2 次只读拒绝引发的批次级联——P0-1/P1-1 直接对症。

## 二b、第二轮 Review 修订（5650030 后）

| 项 | 修订 |
| --- | --- |
| **P0-1** | 评测入口 `build_task_stack` 的 `DockerWorkspace` 现在接收 `command_policy=config.command_policy`；新增回归测试锁定"两层同策略"（漏传会让 Dispatcher 放行的命令在执行层被 strict 打回） |
| **P0-2** | 只读命令校验从黑名单改为**允许选项白名单**：`--pre=COMMAND` 等值绕过、未知长选项、组合短选项中的未知字母全部拒绝；数值选项（`-A2`/`--max-count=3`）要求正整数取值；`--` 之后的操作数照常校验路径 |
| **P1a** | `list_files` 遍历不再跟随目录 symlink（`is_dir()` 会跟随）：越界 symlink 静默跳过，文件型 symlink 仅当解析后仍位于工作区内时列出 |
| **P1b** | CLI 暴露 `--context-recent-full-turns`；评测逐题记录（PREFLIGHT_COMPLETED 事件 + preflight_summary）写入实际生效的 `command_policy` 与窗口值——取自 RunConfig 本身而非脚本参数 |
| **P1c** | README 对照组结果同步为 0/8（含假阳性注记）、v0/v1 版本口径说明 |
| **P2** | `parse_rg_output` 用正则严格区分匹配行（`path:N:`）与上下文行（`path-N-`）：内容含冒号不再误判，上下文行不占 limit 名额，`--` 分组忽略；匹配/上下文行在渲染中用 `:`/`-` 前缀区分 |
| **Trace 导出 bug（链路校验发现）** | `run_task` 导出 trajectory 用的是固定 `run_{instance_id}`，而 run_id 已改为每次尝试唯一——导出的 Trace 实际是陈旧内容。已改为导出本次尝试的 run_id，并从权威 SQLite 恢复了 24 条历史 Trace |

**端到端链路冒烟（`scripts/smoke_tool_efficiency_v1.py`，零模型 Token，真实容器）**：走通 eval runner 装配 → Dispatcher → DockerWorkspace → 官方派生镜像容器内执行。20 项检查全部通过——coding_benchmark 下 grep/rg/ls 两层放行且容器内真实执行成功、strict 下同样命令两层皆拒、危险参数（`--pre=`/`-f`/绝对路径/`..`）两层皆拒、绕过 Workspace 直接调用执行层同样拒绝（纵深防御）、pytest 命令不受影响、两层策略一致（P0-1 回归防护）。

## 三、兼容性与安全

- **默认全部 v0 行为**：`command_policy="strict"`、`context_recent_full_turns=0`——不传新参数的旧实验逐字节可复现。
- 新增命令为只读层且经参数级校验；容器边界（非 root/禁网/资源限制/仅挂载工作区）不变；Reviewer 无命令执行权不变；UNKNOWN 副作用语义不变；`finish` 整批拒绝语义不变。
- RT-14、TL-14 等既有验收语义保留并有测试锁定；specs/02 级联规则以修订注记形式记录（scheduler docstring 同步）。

## 四、测试

新增 14 项：命令档位 6（strict 不变 / 只读层放行 / 危险参数与路径拒绝 / 只读意图判定）、search 多匹配与上下文解析 3、list_files 剪枝 1、调度器级联语义 2（只读拒绝不级联 / `pip install` 拒绝仍级联）、Context 近期窗口 4（旧轮摘要、配对完整、默认关闭、与预算压缩叠加）。全套 **315 passed**，ruff 全绿。

## 五、A/B 提案（待批准后才调用模型）

- **A 组（v1 配置）**：现有 8 题冻结集，`--command-policy coding_benchmark --context-recent-full-turns 6`，预算与 600k 档对齐（600k/题、60 调用），单次运行。N=6 依据勘误后的分析（长运行上 N=6 节省 8-51%，优于 N=12 的 0-24%）。
- **对照基线**：v0 的 600k 档记录（2/8、425k tok/题）不重跑、不覆盖。
- **观测指标**：Resolved@1、命令拒绝次数（预期 →~0）、批次级联次数（预期 →~0）、平均 tokens/题、每轮平均 prompt、patch 应用率、失败类型分布。
- **预算**：≤5M token（与 600k 档实际消耗上限对齐）。
- 判定与信息隔离协议不变；结果无论好坏都追加进 BENCHMARK-MINI.md 的版本演进表（v0 严格白名单 → v1 受控放宽），不覆盖历史。

## 六、对 20-50 题扩展的预期

命令摩擦清零 + search 多匹配 + 批次不连坐，直接作用于"探索期预算耗尽"这一最大失败桶；Context 窗口在长运行（>25 轮）上收益显现。真正的天花板仍是模型 diff 生成能力（matplotlib 类失败），这属于模型侧，Harness 侧的改进空间本轮已按证据收口。
