# RepoFix 详细设计 05：Workspace & Policy

> 版本：V1.0（已冻结；ADR-10 详细化）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)。
> 模块定位：保证 Agent 的代码操作在受控环境中进行。前提：Main Coding Agent + Read-only Review Agent、单 Runtime、单任务工作区。

---

## 一、设计目标与选型

解决三个问题：① 模型能否安全地修改和运行真实仓库？② Main 与 Reviewer 如何使用不同权限？③ 文件修改、测试执行、异常中断后，怎样确定实际工作区状态？

沿用选型：**独立仓库副本 + Docker Sandbox + 明确的权限策略**。借鉴 OpenHands 的执行环境解耦思想与 Codex 的执行隔离 / 权限控制思想。来源区分：OpenHands SDK 的 Workspace 主要负责配置和承载执行环境，其工具并非全部通过统一的 Workspace 文件 API 执行；**我们选择自行设计统一的 RepoFix Workspace 接口**，确保文件工具和命令工具遵守同样的环境约束——这是我们的裁剪设计。

| 方案 | 主要问题 | 决策 |
| --- | --- | --- |
| 直接使用用户仓库 | Agent 可能破坏用户正在开发的代码 | 不采用 |
| Git Worktree | 工作目录隔离，但共享部分 Git 元数据 | 不采用 |
| **独立仓库副本 + Docker** | 创建副本有成本，但执行边界清晰 | **采用** |
| 远程 VM / Sandbox | 隔离能力更强，但部署复杂 | 暂不实现 |

## 二、内部组成与 Workspace 接口

三个必要组件（不需要 RemoteWorkspace、SandboxPool、动态权限管理器、复杂容器调度）：

| 组件 | 职责 |
| --- | --- |
| `WorkspaceManager` | 初始化独立仓库副本、管理工作区生命周期 |
| `DockerWorkspace` | 统一提供文件、搜索、Patch 和命令执行能力 |
| `PermissionPolicy` | 根据 Agent 身份和操作类型判断是否允许执行 |

Workspace 逻辑接口：

| 操作 | 输入 | 输出 |
| --- | --- | --- |
| `initialize` | 仓库、Base Commit、环境配置 | WorkspaceHandle |
| `read_file` | 路径、行号范围 | 内容、文件 Hash |
| `search_code` / `list_files` | 搜索条件 / 相对目录 | 匹配结果 / 文件列表 |
| `apply_patch` | 路径、Patch、期望 Hash | 修改结果、新文件 Hash |
| `run_command` | argv、cwd、timeout | 退出码、stdout/stderr、执行状态 |
| `get_changes` | WorkspaceHandle | **相对基线的文件变化与 Diff** |
| `close` | WorkspaceHandle | 资源清理结果 |

工具层负责参数 Schema；Workspace 负责实际文件操作和执行约束。**`WorkspaceHandle` 只由可信 Runtime 持有，不能让模型指定 Docker 容器 ID 或宿主机路径。**

## 三、工作区创建与生命周期

```
用户 Git 仓库 → 解析并验证 Base Commit → 创建独立临时 Clone
→ Checkout 指定 Commit → 创建受限 Docker 执行环境 → Environment Preflight
   ├── 环境不可用 → 终止并报告
   └── 环境可用 → Agent Runtime → 代码检索与修改 → 测试 / Review
→ 导出 Patch / Trace → 资源清理
```

约定：Base Commit 执行前解析为完整 Hash，后续不跟随原仓库分支变化；独立副本不使用共享对象存储或 Worktree；Docker 只能访问任务副本及明确配置的临时目录，**不能挂载原始仓库**；模型密钥、SQLite 数据库和执行产物目录不挂载进 Agent 容器；仓库初始化、环境检查和结果导出由可信控制进程负责。

### Preflight 检查什么

| 检查 | 处理 |
| --- | --- |
| Git 仓库和 Commit 有效 | 否则拒绝启动 |
| Docker 执行环境可用 | 否则拒绝启动 |
| Python 与测试依赖可用 | 否则报告环境问题 |
| 测试命令合法且可启动 | 记录验证能力 |
| 基线测试 | 可行时运行并记录结果 |

**关键细节：基线测试失败 ≠ Preflight 失败。** 某些测试本来就是因为 Bug 而失败——Preflight 必须区分"测试成功运行但断言失败"（有价值的基线）与"缺少依赖、测试根本无法运行"（环境阻塞）。

依赖准备：MVP 采用预构建环境或任务预先指定的测试镜像；**Agent 不负责运行时自行联网安装依赖**。

## 四、Docker 安全边界

| 安全维度 | MVP 策略 |
| --- | --- |
| 文件访问 | 仅挂载任务工作区和必要临时目录 |
| 网络访问 | 默认禁用 |
| 执行用户 | 非 root |
| 进程权限 | 禁止特权容器、不暴露 Docker Socket、移除不必要的 Linux Capabilities |
| 系统调用 | Docker 默认 seccomp 限制 |
| 资源预算 | CPU、内存、进程数和执行超时 |
| 模型凭据 | 只保留在可信 Runtime |
| 命令执行 | 受限容器内执行，不在宿主机直接运行仓库代码 |

Rootless Docker 可降低 Daemon / 容器的宿主机权限要求（来源：Docker Docs）。**优先支持 Linux + Docker 作为 MVP 正式执行环境**；Windows/macOS 经验证的 Linux Docker 环境使用，暂不承诺相同隔离行为。Docker 是一层隔离机制，**不能宣称能绝对安全运行任意恶意代码**——真正不可信、多租户、高风险仓库应考虑独立 VM，当前不进 MVP。

### Prompt Injection 与代码泄露边界

仓库 README、注释、测试输出都可能包含诱导文本——**仓库内容和工具输出始终是低信任数据，不能改变 Runtime 的权限或工具规则**。

数据保密边界必须明确：容器网络虽禁用，但**模型请求仍会把仓库代码片段发送给外部 LLM API**。首版只用于用户有权提交给所选模型服务处理的仓库；不能把 Docker 网络隔离误认为完整的数据保密机制。

## 五、Main 与 Reviewer 共享 Workspace：一个候选工作区，两种访问权限

| 维度 | Main Agent | Reviewer |
| --- | --- | --- |
| 访问内容 | 当前任务代码 | Review 启动时的候选代码 |
| 文件读取 / 修改 | 允许 / 允许 | 允许 / **禁止** |
| Shell 执行 | 受限允许 | 禁止 |
| 工作区挂载 | 受控读写 | 只读 |
| 能否改变候选 Patch | 是 | 否 |

**版本边界**：① Main 提交 Finish → ② Verifier 完成最终检查 → ③ Runtime 计算候选 Diff 的 `patch_hash` → ④ Reviewer 基于该候选状态审查 → ⑤ Review 过程中 Main 不允许并发修改 → ⑥ 审查结束前后核对 `patch_hash`，不一致则审查无效。**`patch_hash` 与 Verifier 的 `candidate_revision` 使用同一生成函数（见 design/08），保证审查对象与验证对象是同一份代码。**

**不需要为 Reviewer 创建第二份完整 Git Clone**——只读文件访问 + 写权限限制 + 工具层权限校验共同实现；Reviewer 没有 `run_command`，也不获得其他可间接执行代码的接口。审查后 Main 再修改代码的，原 ReviewReport 仍作为历史反馈保存，但**不能标记为对最终 Patch 的审查认可**。

## 六、Patch 与异常副作用

**Patch 修改**（延续模块 4）：单文件、Hash 校验、新建验证不存在、验证失败不留部分修改、路径限工作区内、禁止符号链接绕过。单文件修改采用**"校验后写入临时文件，再替换目标文件"的原子替换**；版本检查与写入之间仍防范并发变化。

**Shell 副作用**：`run_command` 正常执行也可能修改文件——**不能只依赖 apply_patch 记录确定最终改动**。最终代码变化必须从实际工作区与 Base Commit 比较得到（`get_changes`）；测试缓存、构建产物、意外新文件在 Patch 导出时检查。

**超时与未知副作用（冻结规则）**：命令超时由 Workspace 终止进程及子进程；能确认已停止且状态可检查 → 记录确定结果；无法确认 → 返回未知执行状态；**Runtime 对未知副作用不自动重试**，终止任务并保留诊断信息（与模块 2 故障恢复协议一致）。

## 七、不实现与关键验证

不实现：远程 Sandbox、自动依赖安装、用户仓库写回、自动 Commit/Push、沙箱提权审批、跨进程任务恢复、多租户隔离。

**关键验证**：原仓库始终不被修改；Reviewer 无法产生写入；Patch 冲突和 Shell 超时不触发盲目重试。
