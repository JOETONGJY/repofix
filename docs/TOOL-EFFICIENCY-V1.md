# RepoFix 工具效率优化 v1（SWE-bench 扩展前）

- 日期：2026-11-08
- 范围：GPT 评审定义的 "SWE-bench Scalability & Tool Efficiency Optimization v1"——P0-1/2/3 + P1-1 全部落地，P1-2 为设计草图，P1-3 离线分析完成
- 原则：不改动 Main + Reviewer 架构、不开放任意 Shell、不引入 RAG/LLM Summary、不给模型任何隐藏信息；全部改动保持沙箱边界、审计与预算语义
- 测试：**315 passed, 2 skipped**（新增 14 项）；ruff 全绿

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

| 运行 | 轮数 | prompt 总量 | N=6 预计节省 | N=12 预计节省 |
| --- | --- | --- | --- | --- |
| sklearn-10297（completed） | 12 | 61,823 | 0.0% | 0.0% |
| matplotlib-18869 | 18 | 195,206 | 8.6% | 0.0% |
| sphinx-10325 | 12 | 60,493 | 0.0% | 0.0% |
| seaborn-2848 | 14 | 66,131 | 0.1% | 0.0% |

**两个诚实的结论**：

1. **当前 12-18 轮的短运行上，近期窗口收益有限（0-9%）**——窗口未及展开运行就结束了。收益随运行长度增长（1M 档 46-59 轮的运行会显著受益，但该批 Trace 已被对照组覆盖，列为下一步 A/B 的实测项）。
2. **成本结构的关键发现**：matplotlib 第 6-10 轮每轮仅注入 24-59 字符工具输出，prompt 却每轮增长 ~900 token——**大头是模型自身历史输出（每轮 ~1k completion token 的失败 diff 原文等）被逐轮重发计费**。这把"98% prompt"的构成从猜测变成了测量：context 摘要必须覆盖 assistant 消息体（已实现），仅压工具输出省不出成本。

工具摩擦数据（P1-3 前置统计）：21 次 `permission_denied`（去重后 5/8 任务命中，`grep -n`/`ls` 为主）、2 次只读拒绝引发的批次级联——P0-1/P1-1 直接对症。

## 三、兼容性与安全

- **默认全部 v0 行为**：`command_policy="strict"`、`context_recent_full_turns=0`——不传新参数的旧实验逐字节可复现。
- 新增命令为只读层且经参数级校验；容器边界（非 root/禁网/资源限制/仅挂载工作区）不变；Reviewer 无命令执行权不变；UNKNOWN 副作用语义不变；`finish` 整批拒绝语义不变。
- RT-14、TL-14 等既有验收语义保留并有测试锁定；specs/02 级联规则以修订注记形式记录（scheduler docstring 同步）。

## 四、测试

新增 14 项：命令档位 6（strict 不变 / 只读层放行 / 危险参数与路径拒绝 / 只读意图判定）、search 多匹配与上下文解析 3、list_files 剪枝 1、调度器级联语义 2（只读拒绝不级联 / `pip install` 拒绝仍级联）、Context 近期窗口 4（旧轮摘要、配对完整、默认关闭、与预算压缩叠加）。全套 **315 passed**，ruff 全绿。

## 五、A/B 提案（待批准后才调用模型）

- **A 组（v1 配置）**：现有 8 题冻结集，`--command-policy coding_benchmark --context-recent-full-turns 12`，预算与 600k 档对齐（600k/题、60 调用），单次运行。
- **对照基线**：v0 的 600k 档记录（2/8、425k tok/题）不重跑、不覆盖。
- **观测指标**：Resolved@1、命令拒绝次数（预期 →~0）、批次级联次数（预期 →~0）、平均 tokens/题、每轮平均 prompt、patch 应用率、失败类型分布。
- **预算**：≤5M token（与 600k 档实际消耗上限对齐）。
- 判定与信息隔离协议不变；结果无论好坏都追加进 BENCHMARK-MINI.md 的版本演进表（v0 严格白名单 → v1 受控放宽），不覆盖历史。

## 六、对 20-50 题扩展的预期

命令摩擦清零 + search 多匹配 + 批次不连坐，直接作用于"探索期预算耗尽"这一最大失败桶；Context 窗口在长运行（>25 轮）上收益显现。真正的天花板仍是模型 diff 生成能力（matplotlib 类失败），这属于模型侧，Harness 侧的改进空间本轮已按证据收口。
