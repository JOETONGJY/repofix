# RepoFix 工具效率优化 v1（SWE-bench 扩展前）

- 日期：2026-11-08（第三轮 Review 修订：2026-11-09）
- 范围：GPT 评审定义的 "SWE-bench Scalability & Tool Efficiency Optimization v1"——P0-1/2/3 + P1-1 全部落地，P1-2 为设计草图，P1-3 离线分析完成；第三轮评审的 **P1-2（离线分析索引错误：确认为真实缺陷并修复）/ P1-3（search_code Schema：5650030 已实现，本轮补齐校验测试并收紧范围）/ 三项 P2** 已处置，见二c（逐项标注"哪轮修了什么"，避免把既有实现算成本轮新增）
- 原则：不改动 Main + Reviewer 架构、不开放任意 Shell、不引入 RAG/LLM Summary、不给模型任何隐藏信息；全部改动保持沙箱边界、审计与预算语义
- 测试：**358 passed, 2 skipped**（三轮合计新增 59 项）；ruff 全绿；端到端链路冒烟 20 项全过（零 Token）

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
>
> **第二次勘误（第三轮 Review，评审 P1-2）**：模拟器的近期窗口索引算错了——
> `first_elided = idx - elided` 与 `last_elided = idx - 1 - N` 把窗口首尾都从
> 右侧算起，第 8 轮请求（N=6）循环为空、第 14 轮只算第 7 轮，窗口之外的
> 1..N 轮历史从未被计入。改为 `for rnd in range(1, elided + 1)` 后节省比例
> 显著上调（见下表）。修正后的口径与独立实现（闭式
> `saved = Σ_i cost(i)·max(0, R−i−N)`）在 9 条 Trace × 2 个 N 上逐条吻合，
> 并由 `tests/test_context_analysis.py` 锁定。

| 运行 | 轮数 | prompt 总量 | N=6 节省（修正后） | N=12 节省（修正后） |
| --- | --- | --- | --- | --- |
| sklearn-10297（completed） | 21 | 232,818 | 52.9%（原 7.9%） | 21.4%（原 0.0%） |
| matplotlib-18869 | 46 | 587,877 | 64.4%（原 41.9%） | 47.5%（原 19.2%） |
| sphinx-10325 | 41 | 606,974 | 78.9%（原 44.9%） | 57.0%（原 24.0%） |
| seaborn-2848 | 38 | 596,060 | 75.8%（原 51.4%） | 49.2%（原 12.6%） |
| pylint-5859 | 43 | 506,082 | 65.8%（原 28.7%） | 49.3%（原 10.7%） |

**四个诚实的结论**：

1. **近期窗口的收益被旧分析严重低估**：修正后 N=6 省 52.9-78.9%、N=12 省 21.4-57.0%；旧口径分别是 7.9-51.4% 与 0.0-24.0%。旧公式只统计"窗口内最靠右的少数轮次"，漏掉了窗口之外的全部历史。
2. **N=6 仍优于 N=12**（5 条长运行逐条成立）——结论方向不变，但差距比原文档更大、更一致：窗口越小、越早开始摘要，节省越多。A/B 仍以 N=6 为主配置、N=12 为对照；但 N=12 不再是"接近无效"的对照（21-57%），因此对照组本身也有可观收益。
3. **成本结构的关键发现（测量而非猜测）**：matplotlib 第 6-10 轮每轮仅注入 24-59 字符工具输出，prompt 却每轮增长 ~900 token——**大头是模型自身历史输出（每轮 ~1k completion token 的失败 diff 原文等）被逐轮重发计费**。context 摘要必须覆盖 assistant 消息体（已实现），仅压工具输出省不出成本。
4. **数据卫生教训（现在是两条）**：既包括"Trace 副本与权威 SQLite 不一致"，也包括"模拟器自身的索引公式未被交叉验证"——定量结论必须与独立实现对照，不能只看脚本能跑通。

补充口径（同批 9 条 600k 档 Trace 全部重跑）：`pylint-dev__astroid-1196` 只有 6 轮、窗口未满（N=6 / N=12 均为 0%）；`pytest-11143`（20 轮）N=6 27.8% / N=12 9.7%；`pallets__flask-4992`（28 轮）53.9% / 30.7%；`pydata__xarray-3364`（28 轮）65.1% / 38.5%。轮数越多、收益越大；14 轮左右开始出现节省。

工具摩擦数据（P1-3 前置统计）：21 次 `permission_denied`（去重后 5/8 任务命中，`grep -n`/`ls` 为主）、2 次只读拒绝引发的批次级联——P0-1/P1-1 直接对症。

## 二b、第二轮 Review 修订（5650030 后）

| 项 | 修订 |
| --- | --- |
| **P0-1** | 评测入口 `build_task_stack` 的 `DockerWorkspace` 现在接收 `command_policy=config.command_policy`；新增回归测试锁定"两层同策略"（漏传会让 Dispatcher 放行的命令在执行层被 strict 打回） |
| **P0-2** | 只读命令校验从黑名单改为**允许选项白名单**：`--pre=COMMAND` 等值绕过、未知长选项、组合短选项中的未知字母全部拒绝；数值选项（`-A2`/`--max-count=3`）要求正整数取值；`--` 之后的操作数照常校验路径 |
| **P1a** | `list_files` 遍历不再跟随目录 symlink（`is_dir()` 会跟随）：越界 symlink 静默跳过，文件型 symlink 仅当解析后仍位于工作区内时列出 |
| **P1b** | CLI 暴露 `--context-recent-full-turns`；评测逐题记录（PREFLIGHT_COMPLETED 事件 + preflight_summary）写入实际生效的 `command_policy` 与窗口值——取自 RunConfig 本身而非脚本参数 |
| **P1c** | README 对照组结果同步为 0/8（含假阳性注记）、v0/v1 版本口径说明 |
| **P2** | `parse_rg_output` 用正则严格区分匹配行（`path:N:`）与上下文行（`path-N-`）：内容含冒号不再误判，上下文行不占 limit 名额，`--` 分组忽略；匹配/上下文行在渲染中用 `:`/`-` 前缀区分（**第三轮已进一步改为 `rg --json`，见二c——行格式在文件名本身含 `:数字:` 时仍会歧义**） |
| **Trace 导出 bug（链路校验发现）** | `run_task` 导出 trajectory 用的是固定 `run_{instance_id}`，而 run_id 已改为每次尝试唯一——导出的 Trace 实际是陈旧内容。已改为导出本次尝试的 run_id，并从权威 SQLite 恢复了 24 条历史 Trace |

**端到端链路冒烟（`scripts/smoke_tool_efficiency_v1.py`，零模型 Token，真实容器）**：走通 eval runner 装配 → Dispatcher → DockerWorkspace → 官方派生镜像容器内执行。20 项检查全部通过——coding_benchmark 下 grep/rg/ls 两层放行且容器内真实执行成功、strict 下同样命令两层皆拒、危险参数（`--pre=`/`-f`/绝对路径/`..`）两层皆拒、绕过 Workspace 直接调用执行层同样拒绝（纵深防御）、pytest 命令不受影响、两层策略一致（P0-1 回归防护）。

**冒烟脚本的覆盖边界（第三轮 P2 修订）**：脚本只组装执行栈并直接驱动 Dispatcher，**不运行 AgentRunner 主循环**——而 `PREFLIGHT_COMPLETED` 事件由 `AgentRunner.run()` 生成。原 docstring 第 ⑤ 条"实际生效的档位写入 Trace"属于无法由该脚本证明的声明，已删除并改由 Scripted Model 测试覆盖：`test_context_engine.py::test_preflight_summary_recorded_when_provided`（断言事件载荷里的 `command_policy` / `context_recent_full_turns`）与 `test_swebench_eval.py::test_effective_preflight_summary_uses_runconfig_not_script_args`（断言摘要取自 RunConfig 而非启动参数）。

## 二c、第三轮 Review 修订（b941bd6 后）

| 项 | 修订 |
| --- | --- |
| **P1-2 离线分析索引错误** | `scripts/analyze_context_efficiency.py` 的占位集合改为 `elided_rounds(idx, N) = 1..idx-1-N`（`for rnd in range(1, elided + 1)`）。旧公式首尾都从窗口右侧算起：第 8 轮请求为空、第 14 轮只剩第 7 轮 → **系统性低估节省比例**。修正后 N=6 省 52.9-78.9%、N=12 省 21.4-57.0%；"N=6 优于 N=12"的方向仍成立（5/5 条长运行），但旧口径严重失真（见第二节表格）。新增 `tests/test_context_analysis.py`（15 项）与独立闭式实现交叉验证，另加 `simulated_elided_requests` 便于审计。Runtime 侧的 `ContextBuilder` 近期窗口逻辑未受影响——问题只在离线模拟器 |
| **P1-3 search_code Schema** | **核对结论：该参数早已在 5650030 声明**（评审看到的快照更早，SearchCodeArgs 当时确实只有 5 个字段）。本轮做三件事：① `max_per_file` 上限 50 → 20（评审建议的合理范围），`context` 保持 0-5；② Tool Description 明确两者语义与默认值（此前描述只说 "up to max_per_file"，没有给范围）；③ **补齐此前完全缺失的 Dispatcher Schema 校验测试**——原缺口是"没有任何测试证明能力真的穿过 Schema → Dispatcher → Executor"，`tests/test_tool_schema.py`（11 项）锁定 Schema 暴露/边界/默认值/描述、校验通过、越界拒绝、未知字段仍拒绝、参数原样到达 Executor |
| **P2 CLI 参数** | **核对结论：`--context-recent-full-turns` 已在 b941bd6 加入并传入 `RunConfig`**（评审快照同样更早）。本轮补 3 项回归锁定"文档支持 = 入口支持"：Click 参数确实注册、值进入 RunConfig、默认 0 保持 v0 行为 |
| **P2 Trace 冒烟声明** | 见二b 末段：删除冒烟脚本无法证明的第 ⑤ 条，改由 Scripted Model Trace 测试 + 摘要组装单测覆盖 |
| **P2 rg 解析歧义** | `build_rg_argv` 改用 `rg --json`，`parse_rg_output` 解析结构化事件流（path / line_number / lines.text 分字段）；文件名含 `:数字:` 或 `-数字-` 不再有歧义，非 UTF-8 路径走 base64 分支，begin/end/summary 忽略，match 计 limit、context 不占名额。渲染给模型的格式不变（`path:line:` / `path-line-`），因此不影响 prompt 语义与既有 Trace 口径 |

## 三、兼容性与安全

- **默认全部 v0 行为**：`command_policy="strict"`、`context_recent_full_turns=0`——不传新参数的旧实验逐字节可复现。
- 新增命令为只读层且经参数级校验；容器边界（非 root/禁网/资源限制/仅挂载工作区）不变；Reviewer 无命令执行权不变；UNKNOWN 副作用语义不变；`finish` 整批拒绝语义不变。
- RT-14、TL-14 等既有验收语义保留并有测试锁定；specs/02 级联规则以修订注记形式记录（scheduler docstring 同步）。

## 四、测试

第二轮新增 14 项：命令档位 6（strict 不变 / 只读层放行 / 危险参数与路径拒绝 / 只读意图判定）、search 多匹配与上下文解析 3、list_files 剪枝 1、调度器级联语义 2（只读拒绝不级联 / `pip install` 拒绝仍级联）、Context 近期窗口 4（旧轮摘要、配对完整、默认关闭、与预算压缩叠加）。

第三轮新增 34 项：离线分析索引 15（占位集合、8/14/21/46 轮 × N=6/12 的闭式交叉验证、窗口未满、关闭、单调性）、`search_code` Schema/Dispatcher 11（Schema 暴露与边界、合法通过、越界拒绝、未知字段拒绝、参数到达 Executor）、CLI `--context-recent-full-turns` 3、评测摘要组装 1（`effective_preflight_summary` 取自 RunConfig）、rg JSON 解析 net +4（4 项行格式用例改写为 8 项 JSON 用例，含文件名含 `:数字:` 的歧义回归与非 UTF-8 路径）。

全套 **358 passed, 2 skipped**（不含 Docker 集成与真实 API 用例），ruff 全绿。本机缺少 Docker daemon 与宿主 `rg` 时，两项环境依赖用例（`test_auto_discovery_runs_pytest`、`test_derive_visible_plan_empty_when_no_match`）会失败——与本轮改动无关，属环境前置缺失。

## 五、A/B 提案（待批准后才调用模型）

- **A 组（v1 配置）**：现有 8 题冻结集，`--command-policy coding_benchmark --context-recent-full-turns 6`，预算与 600k 档对齐（600k/题、60 调用），单次运行。N=6 依据修正后的分析（长运行上 N=6 节省 52.9-78.9%，优于 N=12 的 21.4-57.0%）。
- **对照基线**：v0 的 600k 档记录（2/8、425k tok/题）不重跑、不覆盖。
- **观测指标**：Resolved@1、命令拒绝次数（预期 →~0）、批次级联次数（预期 →~0）、平均 tokens/题、每轮平均 prompt、patch 应用率、失败类型分布。
- **预算**：≤5M token（与 600k 档实际消耗上限对齐）。
- 判定与信息隔离协议不变；结果无论好坏都追加进 BENCHMARK-MINI.md 的版本演进表（v0 严格白名单 → v1 受控放宽），不覆盖历史。

## 六、对 20-50 题扩展的预期

命令摩擦清零 + search 多匹配 + 批次不连坐，直接作用于"探索期预算耗尽"这一最大失败桶；Context 窗口在 20 轮以上运行上收益显著（修正后 27.8-78.9%，14 轮左右开始出现节省）。真正的天花板仍是模型 diff 生成能力（matplotlib 类失败），这属于模型侧，Harness 侧的改进空间本轮已按证据收口。
