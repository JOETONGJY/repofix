# RepoFix 详细设计 04：Tool System

> 版本：V1.1（M1 集成评审修订：权限校验先于 Schema；输出截断双层限制；执行器异常的副作用未知语义）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)、[03-llm-gateway.md](03-llm-gateway.md)。
> 模块定位：把模型提出的动作转换成受校验、受权限约束、可审计的执行结果。参考 OpenHands 的 Action → Executor → Observation 思想，不复刻完整 SDK。

---

## 一、设计目标与组件

解决三个问题：① 模型提出的调用是否有效？② 该 Agent 是否有权执行？③ 执行后如何产生统一可信的结果？

**采用**：静态 Tool Registry + Pydantic Schema + 单次 Tool Dispatcher + 结构化 ToolResult。"静态"指工具在程序启动时注册，**不支持模型运行中安装、发现或注册新工具**。

| 组件 | 职责 |
| --- | --- |
| `ToolRegistry` | 工具名称、描述、参数 Schema、角色权限 |
| `ToolDispatcher` | 单次调用的校验与执行 |
| `ToolExecutor` | 将具体操作委托给 Workspace 或 Runtime |
| `ToolResult` | 统一的执行状态、数据与错误 |

用 Python Protocol + 少量实现类，不做复杂继承体系。

## 二、工具接口定稿

### 代码检索工具

| 工具 | 参数 | 输出 |
| --- | --- | --- |
| `list_files` | `path="."`、`max_depth=3`、`limit=100` | 相对文件路径、是否截断 |
| `search_code` | `pattern`、`path="."`、`regex=false`、`glob=null`、`limit=100` | 文件路径、行号、匹配文本 |
| `read_file` | `path`、`start_line=1`、`max_lines=200` | 文件内容（带行号）、SHA-256 文件版本、截断标记 |

约定：路径相对工作区根；**默认普通字符串搜索，显式 `regex=true` 才启用正则**；底层 ripgrep，**不经 Shell 字符串拼接**；搜索无匹配是正常结果不是失败；`read_file` 只支持受限大小的文本文件（二进制 / 超大文件明确拒绝）。

**`read_file` 返回文件 Hash 的原因**：让下一次 `apply_patch` 能检测文件版本冲突。

### 代码编辑：`apply_patch(path, patch, expected_sha256)`

**MVP 每次只处理一个文件**——多文件任务通过多次调用完成，最终输出统一 Git Diff。裁剪理由：多文件 Patch 需要考虑部分应用、跨文件事务和复杂冲突恢复；单文件编辑的成功 / 失败语义明确，对首版 Bug 修复足够。

| 参数 | 含义 |
| --- | --- |
| `path` | 目标文件的工作区相对路径 |
| `patch` | 针对该文件的 Unified Diff |
| `expected_sha256` | 模型读取该文件时获得的内容 Hash |

规则：修改已有文件必须提供匹配 Hash；创建新文件使用显式"文件不存在"前置条件（不能覆盖已存在路径）；Patch 不匹配或 Hash 改变 → `CONFLICT`；**单文件原子替换**（验证失败不留部分编辑）；不支持任意路径、二进制文件、Git 元数据修改；**MVP 不支持重命名和删除**（有实际任务需求再加）。

现实限制注明：Hash 防护只能检查它观察到的工作区状态，**不能替代 Workspace 的文件系统权限和路径安全检查**。

### 命令工具：`run_command(argv, cwd=".", timeout_seconds=120)`

`argv` 是**参数数组**，不是需要 Shell 解释的整段命令——避免引号、管道、重定向产生解析歧义：

```json
{ "argv": ["python", "-m", "pytest", "tests/test_auth.py", "-q"], "cwd": ".", "timeout_seconds": 120 }
```

MVP 使用显式允许命令集合：`pytest`、`python -m pytest`、`python -m unittest`、`python -m compileall`、执行环境中已配置的 `ruff`。其他命令需显式扩展策略——**模型不能自行运行 `pip install`、`bash` 或任意 `python -c`**。

注意：即使只运行 pytest，测试代码本身也能产生副作用——允许列表只是额外约束，**真正的执行隔离仍依赖 Docker Workspace**。CLI 的 `--test-command` 必须在 Preflight 阶段转换并检查为允许的命令，不能绕过命令策略。

### 控制工具

| 工具 | Agent | 输入 | 行为 |
| --- | --- | --- | --- |
| `finish` | Main | `summary` | 提交完成请求，由 Runtime 验证 |
| `submit_review` | Reviewer | `status`、`findings`、`summary` | 提交 ReviewReport |

**`finish` 契约简化说明（对 design/01 的修订）**：输入只保留 `summary`。Final Check 所需的修改文件清单由 Runtime 从 WorkingState 与工作区实际状态**推导**，不再信任模型自报 `changed_files`——模型无法谎报修改范围。审查修正阶段的回应说明并入 `summary` 或 assistant 文本。**`finish` 必须单独成批**：与其他调用混合返回时 Runtime **整批拒绝**（无任何执行与副作用；finish 得 INVALID_ARGUMENT、其余 SKIPPED，见 design/02）。`submit_review` 的 status 限定 `APPROVED / FINDINGS / INCONCLUSIVE`；最多 5 条 Finding；**APPROVED 不允许同时包含未解决 Finding**。两者都是 Runtime 控制动作，不进 Workspace 执行。

## 三、标准执行流程

```
ToolCall
→ 检查 Agent 身份与工具权限
→ 查找 ToolRegistry
→ 解析 JSON / Pydantic Schema 校验
→ 工具语义与路径约束检查
→ Workspace Policy 判断
→ 持久化 Tool 执行意图
→ 执行 Workspace 操作
→ 构造 ToolResult
→ Runtime 记录并决定后续调度
```

职责严格区分：**Tool System 负责单次调用；Runtime 负责批次整体调度**。模型一次返回三个调用、第二个失败时——Dispatcher 只返回第二个的失败结果；Runtime 决定停止后续并为第三个生成 SKIPPED；Context Engine 负责把结果转换为下一轮模型可见消息。**Dispatcher 不直接调用 LLM 让模型重新规划。**

## 四、四层参数校验

| 层次 | 校验内容 | 失败结果 |
| --- | --- | --- |
| 工具权限 | 当前 Agent 能否调用该工具 | `PERMISSION_DENIED` |
| 参数 Schema | 必填、类型、范围、未知字段 | `INVALID_ARGUMENT` |
| 语义前置条件 | 文件是否存在、Hash 是否匹配、命令是否允许 | `CONFLICT` / `INVALID_ARGUMENT` / `PERMISSION_DENIED` |
| Workspace 强制约束 | 路径、实际文件访问、容器资源边界 | 对应的执行或权限错误 |

Pydantic 实现第二层，**不能代替后三层**。Schema 用严格模式：不静默强制类型转换、不接受未声明额外字段；超限时报错或截断并准确标识是哪种行为。

**模型生成的参数即使通过 JSON Schema 也不是可信输入**——路径操作必须由 Workspace 在实际文件访问时重新检查规范化路径与符号链接，不能只看字符串里有没有 `..`。

## 五、ToolResult 最终语义

| 字段 | 含义 |
| --- | --- |
| `call_id` | 原始工具调用 ID |
| `status` | `SUCCEEDED` / `FAILED` / `SKIPPED` |
| `content` | 模型可见的结构化结果 |
| `error_code` | 如 `CONFLICT`、`TIMEOUT`、`PERMISSION_DENIED` |
| `exit_code` | 命令退出码，可为空 |
| `duration_ms` | 执行耗时 |
| `truncated` | 返回内容是否经过截断 |

**`UNKNOWN` 保留在持久化的工具调用状态（side_effect_status）中，不伪装成已完成的 ToolResult**。

关键区分：

| 情况 | ToolResult | Runtime 行为 |
| --- | --- | --- |
| 搜索没有匹配 | `SUCCEEDED`，空结果 | 继续正常批次 |
| pytest 退出码 1 | `SUCCEEDED`，`exit_code=1` | 结束批次，反馈模型分析 |
| Patch Hash 冲突 | `FAILED / CONFLICT` | 停止批次，反馈模型 |
| Shell 进程无法启动 | `FAILED / EXECUTION_ERROR` | 停止批次 |
| 执行超时且副作用未知 | 持久化 `UNKNOWN` | 停止 Run，不盲目重试 |
| 执行器抛异常（副作用可能已发生，V1.1） | `FAILED / EXECUTION_ERROR` | **持久化 `side_effect_status=UNKNOWN`**——不能仅凭异常认定无副作用；停止批次与 Run，不自动重试 |
| Reviewer 请求 `apply_patch` | `FAILED / PERMISSION_DENIED` | 不执行，工作区不受影响 |

**工具的执行状态与命令的业务退出状态必须分离**——这使工具故障率统计可靠，不会把测试断言失败误算为系统故障。

**输出控制（V1.1 修订：双层限制）**：Search 默认最多 100 条结果；Read 默认最多 200 行；**Tool Dispatcher 硬上限 = 32 KiB（按 UTF-8 字节计，截断标记计入上限、最终输出不得越界）**，超限明确 `truncated=true`，保留有价值的开头、结尾或错误摘要；**Context 层软预算 = RunConfig.tool_output_truncate_chars（默认 2000 字符）**，在上下文构造时按内容重要性与剩余预算取舍，**不得无条件截断所有工具结果**（否则 read_file 稍长的源文件时模型总看不到关键函数）。完整日志作为受限大小的运行产物保存，不无限写入 SQLite 或模型上下文。完整运行日志 ≠ 模型可见输出。

**校验顺序（V1.1 明确）**：权限校验先于参数 Schema——无权限调用不解析参数（Reviewer 请求 `apply_patch` 无论参数是否畸形一律 `PERMISSION_DENIED`），拒绝结果稳定、不受参数内容影响，与双 Agent 角色隔离设计一致。

## 六、双 Agent 权限的真正实现

不需要两个 Tool System——**同一个 Registry 按 Agent 角色生成不同工具视图**：

| Main Agent | Review Agent |
| --- | --- |
| list_files / search_code / read_file | list_files / search_code / read_file |
| apply_patch | — |
| run_command | — |
| finish | submit_review |

运行时第二层强制约束：Main 使用可写受限任务工作区；Reviewer 使用只读工作区视图；Reviewer 不能通过其他工具间接执行 Shell；Reviewer 无法修改任务仓库或原始仓库；**Agent 角色由 Runtime 设置，不能来自模型生成的参数**。

## 七、明确不支持什么

不支持 MCP、动态工具安装、任意 Shell、后台持久进程、工具并行执行、任意跨文件事务、运行时权限提升。这些能力都可能有用，但当前没有充分的 MVP 业务理由。

## 八、四项关键决策

| 编号 | 决策 | 为什么重要 |
| --- | --- | --- |
| D3-01 | Gateway 以 Chat Completions Function Calling 为第一套协议 | 限制初始适配范围，避免过早支持多个 API 格式 |
| D3-02 | Main 与 Reviewer 共享 Gateway，但各自独立上下文与工具集合 | 双 Agent 角色隔离，不增加模型基础设施 |
| D4-01 | `apply_patch` 一次修改一个文件，强制版本校验 | 降低写入冲突和失败恢复复杂度 |
| D4-02 | `run_command` 使用 argv 与允许命令集合，在沙箱内执行 | 保留测试反馈能力的同时控制执行边界 |

D4-01 / D4-02 是**明确的 MVP 裁剪，不是行业通用规范**——若真实任务证明单文件 Patch 或命令允许列表限制了修复效果，用数据决定是否放宽。

### 关键验收（三项）

1. 非法参数、未知工具或越权调用不触发实际执行。
2. Patch 版本冲突时不修改文件，也不自动重复应用。
3. pytest 断言失败与工具执行失败能被正确区分。
