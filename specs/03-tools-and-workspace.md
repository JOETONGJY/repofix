# RepoFix 实现规格 03：工具与工作区（Tools & Workspace）

> 版本：V0.4（M1 修订见 V0.3 记录；M2 评审修订：工具计数统一为五个工作区工具 + 两个控制动作）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[../design/04-tool-system.md](../design/04-tool-system.md)。
> 依赖：[01-contracts.md](01-contracts.md)。

---

## 工具注册与通用约定

- 工具注册表：**五个**工作区工具（list_files / search_code / read_file / apply_patch / run_command）+ `finish` / `submit_review` 两个 Runtime 控制动作。名称、参数 Schema、副作用类别固定如下，**不得增删改**。
- **静态注册**：工具在程序启动时注册；不支持模型运行中安装、发现或注册新工具；不支持 MCP。
- **权限按 actor 区分**：Main 与 Reviewer 复用同一 Registry，按角色生成不同工具视图——Reviewer 视图中不存在 `apply_patch / run_command / finish`。只读约束由注册表 + Workspace 权限共同保证，**不能仅写在 System Prompt 中**。Agent 角色由 Runtime 设置，不能来自模型参数。
- 副作用类别决定调度行为（见 02）：`read_only` 失败后同类可继续；`write` / `process` 失败即中止批次。
- **校验顺序（V0.3 修订，对齐 design/04 四层校验）：存在性 → 权限校验 → Schema 校验 → 语义前置条件 → 预算 → 执行。** 无权限的调用不解析参数——如 Reviewer 请求 `apply_patch`，即使参数畸形也返回 `PERMISSION_DENIED`，拒绝结果不受参数内容影响。
- 所有路径相对工作区副本根。**输出截断为双层限制（V0.3 裁决）**：
  - **Tool Dispatcher 硬上限 = 32 KiB（按 UTF-8 字节计）**：截断标记计入上限，最终输出（含标记）不得越界；超限 `truncated=true`，保留有价值的开头 / 结尾 / 错误摘要。
  - **Context Engine 软预算 = RunConfig.tool_output_truncate_chars（默认 2000 字符）**：在上下文构造时按内容重要性与剩余预算取舍，**不得无条件截断所有工具结果**（否则 read_file 稍长的源文件时模型总看不到关键函数）。
  - 完整输出（stdout / stderr 全文）存 SQLite，不进模型上下文（specs/01）。

## 七个动作契约

### list_files — 副作用：read_only

| 参数 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| path | str | "." | 相对副本根；resolve 后不得逃逸 |
| max_depth | int | 3 | ≤ 6 |
| limit | int | 100 | ≤ 500 |

返回：相对文件路径列表、`truncated` 标志。

### search_code — 副作用：read_only

| 参数 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| pattern | str | 必填 | 非法正则（仅 regex=true 时）→ INVALID_ARGUMENT |
| path | str | "." | 同上 |
| regex | bool | **false** | 默认普通字符串搜索；显式 true 才启用正则 |
| glob | str \| null | null | 如 "tests/**" |
| limit | int | 100 | ≤ 500 |

返回：`[{path, line, match_text}]`、`truncated`。底层 ripgrep，**不经 Shell 字符串拼接**。**搜索无匹配是正常结果（SUCCEEDED 空结果），不是失败。**

### read_file — 副作用：read_only

| 参数 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| path | str | 必填 | 同上 |
| start_line | int | 1 | ≥ 1 |
| max_lines | int | 200 | ≤ 1000 |

返回：`content`（**带行号**）、`start_line`、`total_lines`、`sha256`（**当前文件内容 Hash，供 apply_patch 版本校验**）、`truncated`。

只支持受限大小的文本文件；二进制 / 超大文件 → INVALID_ARGUMENT（明确拒绝）。文件不存在 → EXECUTION_ERROR（业务反馈，让模型重新定位）。

### apply_patch — 副作用：write（**单文件原子操作**）

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| path | str | 是 | 相对副本根 |
| patch | str | 是 | 针对**该文件**的 Unified Diff |
| expected_sha256 | str | 修改已有文件时必填 | 模型从 read_file 获得的内容 Hash |

**MVP 每次 `apply_patch` 只处理一个文件**——多文件任务通过多次调用完成（D4-01 裁剪）。

校验链（顺序执行，任一失败即拒绝，**不留下部分编辑**）：

1. 路径规范化：resolve 后必须在副本根内；拒绝绝对路径与 `..` 逃逸 → INVALID_ARGUMENT。
2. 符号链接校验：目标（含中间目录）不得逃逸副本根 → INVALID_ARGUMENT。
3. 版本校验：当前内容 sha256 ≠ expected_sha256 → **CONFLICT**（提示重新 read_file）。修改已有文件必须提供 Hash。
4. 新建文件：显式"文件不存在"前置条件；**目标已存在 → CONFLICT**，不能覆盖。
5. Patch 上下文匹配失败 → EXECUTION_ERROR（提示重新读取后重生成）。

不支持：任意路径、二进制文件、Git 元数据修改、文件重命名与删除（有实际任务需求再加）。**事务语义**：先持久化执行意图（pre_state_hash、expected_result）与 STARTED，再应用修改，成功后补 SUCCEEDED。

现实限制：Hash 防护只覆盖观察到的工作区状态，**不能替代 Workspace 的文件系统权限和路径安全检查**。

### run_command — 副作用：process

| 参数 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| argv | list[str] | 必填 | **参数数组，不是 Shell 字符串**（避免引号 / 管道 / 重定向解析歧义） |
| cwd | str | "." | 相对副本根 |
| timeout_seconds | int | config.command_timeout_seconds（默认 120） | ≤ 600 |

**允许命令集合（显式白名单）**：`pytest`、`python -m pytest`、`python -m unittest`、`python -m compileall`、环境中已配置的 `ruff`。其他命令（含 `pip install`、`bash`、任意 `python -c`）→ PERMISSION_DENIED，模型不能自行扩展；扩展需显式策略变更。CLI 的 `--test-command` 必须在 Preflight 阶段转换并校验为允许命令，**不能绕过命令策略**。

执行环境（必须实际生效，不能用字符串黑名单判断危险性）：容器内非 root、网络默认禁用、CPU/内存/PID 限制、无 Docker Socket、无凭据注入。**即使只运行 pytest，测试代码也能产生副作用——允许列表只是额外约束，真正隔离依赖 Docker Workspace。**

返回：exit_code、stdout/stderr（分存全文，模型可见合并输出截断）、`truncated`、duration_ms。超时 / 取消必须**终止实际进程树**。

### finish — 控制动作，仅 Main Agent

| 参数 | 类型 | 必填 |
| --- | --- | --- |
| summary | str | 是 |

**契约简化（design/03–04 修订 design/01）**：不再要求模型自报 `changed_files`——Final Check 所需修改清单由 Runtime 从 WorkingState 与工作区实际状态**推导**。审查修正阶段的回应说明并入 `summary` 或 assistant 文本。语义见 specs/02 Finish 协议；单独成批；Reviewer 调用 → PERMISSION_DENIED。

### submit_review — 控制动作，仅 Review Agent

| 参数 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| status | APPROVED / FINDINGS / INCONCLUSIVE | 是 | |
| findings | list[ReviewFinding] | FINDINGS 时必填 | ≤ 5 条；每条必须有 evidence；**APPROVED 不允许携带未解决 Finding** |
| summary | str | 否 | |

提交 `ReviewReport` 并结束本轮审查；Main Agent 调用 → PERMISSION_DENIED。

## 四层参数校验

| 层次 | 校验内容 | 失败结果 |
| --- | --- | --- |
| 1. 工具权限 | 当前 Agent 能否调用该工具 | PERMISSION_DENIED |
| 2. 参数 Schema | 必填、类型、范围、未知字段（Pydantic 严格模式：不静默类型转换、不接受未声明字段） | INVALID_ARGUMENT |
| 3. 语义前置条件 | 文件存在、Hash 匹配、命令在白名单 | CONFLICT / INVALID_ARGUMENT / PERMISSION_DENIED |
| 4. Workspace 强制约束 | 路径、实际文件访问、容器资源边界 | 对应执行或权限错误 |

**模型生成的参数即使通过 JSON Schema 也不是可信输入**——路径操作必须由 Workspace 在实际文件访问时重新检查规范化路径与符号链接。

## 单次调用执行流程

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

Dispatcher 只负责单次调用结果；批次调度（中止、SKIPPED）归 Runtime；Dispatcher **不直接调用 LLM 重新规划**。

## ToolResult 语义速查

| 情况 | ToolResult | Runtime 行为 |
| --- | --- | --- |
| 搜索无匹配 | SUCCEEDED，空结果 | 继续正常批次 |
| pytest 退出码 1 | SUCCEEDED，exit_code=1 | 结束批次，反馈模型分析 |
| Patch Hash 冲突 | FAILED / CONFLICT | 停止批次，反馈模型 |
| Shell 进程无法启动 | FAILED / EXECUTION_ERROR | 停止批次 |
| 超时且副作用未知 | 持久化 side_effect_status=UNKNOWN | 停止 Run，不盲目重试 |
| Reviewer 请求 apply_patch | FAILED / PERMISSION_DENIED | 不执行，工作区不受影响 |

## Workspace 契约

### 副本创建

```
git clone <repo_url_or_path> <task_dir>   # 独立对象存储，不共享用户仓库元数据
git -C <task_dir> checkout <base_commit>
```

- **MVP 只处理 base_commit 快照，且默认拒绝脏工作区**（design/01）：已跟踪文件修改、未跟踪且未忽略文件 → CLI 直接拒绝；被忽略文件不影响。不自动 Commit / Stash / 清理用户工作区。
- 所有内容操作统一经 **WorkspaceAdapter** 作用于 `<task_dir>`；Runtime 业务代码禁止直接 `open()` 副本文件。MVP：宿主进程对副本目录执行文件操作 + `docker exec` 执行命令（副本 bind-mount 进容器）；后续可整体替换为容器内实现。
- **Workspace 逻辑接口（design/05）**：`initialize / read_file / search_code / list_files / apply_patch / run_command / get_changes / close`。`get_changes` 返回**相对 Base Commit 的文件变化与 Diff**——`run_command` 也可能修改文件，**最终 Patch 必须从实际工作区与基线比较导出，不能只依赖 apply_patch 记录**（测试缓存、构建产物、意外新文件在导出时检查）。`WorkspaceHandle` 只由可信 Runtime 持有，模型不得指定容器 ID 或宿主机路径。
- **apply_patch 原子写入**：校验后写入临时文件，再替换目标文件；版本检查与写入之间防范并发变化。

### 容器配置

| 项 | 值 |
| --- | --- |
| 用户 | 非 root |
| 网络 | 默认 none |
| 资源 | CPU / 内存 / PID 限制（默认 1 CPU / 1GB / 100 pids，可配置） |
| 挂载 | 仅 `<task_dir>`（读写）；不挂载 Docker Socket；不注入任何 API Key |
| Review 视图 | Reviewer 使用同一副本的**受限只读视图**；不能间接执行 Shell、不能修改任务或原始仓库 |

### Preflight（Run 开始前，全部通过才进入 RUNNING）

```
Repository Validation → Identify Base Commit → Create Isolated Workspace
→ Detect Python Environment → Check Test Commands（含 --test-command 白名单校验）
→ Run Baseline Tests (when feasible) → Begin Agent Loop
```

- 任一步失败 → Run FAILED（fail_layer=WORKSPACE），不进入 Agent Loop。
- **基线测试失败 ≠ Preflight 失败**（design/05）：区分"测试成功运行但断言失败"（有价值的基线，记录后继续）与"缺少依赖、测试无法运行"（环境阻塞，Preflight 失败）。依赖准备采用**预构建环境或任务指定的测试镜像**，Agent 不在运行时联网安装依赖。
- 基线结果（通过/失败测试清单）入库，供 Verifier / Evaluation 归因（既有失败 / 回归 / 任务失败）。
- 执行环境：**优先 Linux + Docker 为正式环境**；Windows/macOS 经验证的 Linux Docker 使用，不承诺相同隔离行为。

### Verifier 与评测的执行边界（design/08–09）

- Verifier 的 `TestRunner` **复用 Workspace 命令执行能力**，不另建 Shell Executor；VerificationRequest.test_plan 在任务初始化时确定，finish 时不可改写。
- CLI 的 `--test-command`（字符串）在**任务初始化时统一解析校验为 argv 数组**——与 Tool System 同一套命令策略，不形成两套执行路径。
- **独立评测在全新的干净工作区执行**（design/09）：从 Base Commit 初始化 → 应用候选 Patch → 运行隐藏测试；不在 Agent 工作区直接跑（避免缓存 / 临时文件干扰，检验 Patch 可独立复现）。隐藏测试文件**不进入 Agent 可见工作区**；评测中执行的仓库代码不接触评测凭据与宿主机资源。

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | Runtime 正常完成，且规定的可见验证通过 |
| 1 | Runtime 正常完成，但没有足够的可见验证，结果未充分确认 |
| 2 | 输入参数、仓库或配置校验失败（含脏工作区拒绝） |
| 3 | Runtime / Workspace 不可恢复错误，或未完成规定验证（含任务失败 / no_progress / 上下文超限） |
| 4 | 时间、模型调用或工具调用预算耗尽 |
| 130 | 用户主动中断 |

退出码 0 也不代表隐藏 Benchmark 验收成功。

### 输出协议

默认 `<output_dir>/runs/<run_id>/`（默认 `repofix-data`，`--output-dir` 可覆盖）：`result.json`（RunResult）、`changes.patch`（含修改与新增文件，不含忽略 / 缓存 / 敏感内容）、`trajectory.jsonl`、`test-report.json`。**不依赖独立 Evaluation 才能产出**；Patch 导出不能只做普通 `git diff`（漏未跟踪新文件）。
