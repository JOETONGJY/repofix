# RepoFix 详细设计 03：LLM Gateway

> 版本：V1.0（已冻结；ADR-05 转正）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)、[00-global-contracts.md](00-global-contracts.md)。
> 模块定位：把不同模型 API 的调用与响应规范化。**Gateway 不执行工具，Tool System 不自主调用模型，Runtime 拥有控制权。**

---

## 一、技术选型：模型接入协议（ADR-05 定稿）

**Python 原生模型 SDK + OpenAI 风格 Chat Completions Function Calling 协议 + 自定义 `ModelClient` 接口。**

| 候选方案 | 优势 | 当前判断 |
| --- | --- | --- |
| 直接在 Runtime 调模型 SDK | 实现最快 | 模型协议与业务循环耦合 |
| **自定义 Gateway + 原生 SDK** | 核心协议清楚，便于测试和更换模型 | **采用** |
| LiteLLM 等统一框架 | 多供应商功能丰富 | MVP 暂不需要广泛模型兼容 |
| 自研 HTTP 客户端 | 控制请求细节 | 重复实现成熟 SDK 的功能 |

选择 Chat Completions 作为初始内部适配目标，不代表它比所有新协议都好：DeepSeek 提供 Chat Completions 风格的工具调用（返回带 ID、工具名和 JSON 字符串参数的 `tool_calls`，且官方提醒参数未必总是有效 JSON，需客户端校验）；OpenAI 新集成更强调 Responses API。**MVP 不同时实现两套协议**，未来通过增加 Provider Adapter 接入。

### 需要主动避免的误区："OpenAI-compatible" ≠ 任意模型可替换

DeepSeek 的思考模式在部分调用场景要求保留并传回特定推理字段，不同模式对工具选择参数的支持也有差异。因此：

- 首版只承诺**一个经过端到端验证的 Provider / API 模式**；Chat Completions Function Calling 是第一套适配协议。
- 默认不启用未经适配器验证的特殊 reasoning、流式工具调用和多模型路由。
- 模型特定的消息字段由 **Provider Adapter** 负责保存和还原。
- 更换模型必须通过**协议兼容测试**，而不是只改 `model_id` 就宣称支持。

概念区分：用于**编写** RepoFix 的 GLM / DeepSeek，与 RepoFix **运行时**接入的模型，是两个独立选择。

**首版验证目标（一致性评审明确 + 实施前正式确定）：DeepSeek**——Base URL `https://api.deepseek.com`，OpenAI 兼容 Chat Completions，初始模型 `deepseek-flash`（对照可选 `deepseek-v4-pro`）。注意 **2026 年模型命名**：官方当前为 `deepseek-flash` / `deepseek-v4-pro`（均支持 Tool Calls）；旧 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07 退役，不作默认值。API Key 从 `DEEPSEEK_API_KEY` 环境变量读取，不写入源码 / 日志 / 仓库。

**Thinking Mode 集成注意**：DeepSeek 当前默认启用 Thinking Mode——首次 Tool Calling 冒烟测试**显式关闭**（减少协议变量）；后续启用时必须正确保留并在下一轮请求回传 `reasoning_content`，否则带工具的续接请求可能报错（这正是 ProviderAdapter"保存还原续接字段"职责的具体化）。

## 二、内部架构：只有三个逻辑组件

| 组件 | 职责 |
| --- | --- |
| `ModelClient` | 提供统一的模型调用接口 |
| `ProviderAdapter` | 将公共请求 / 响应转换为具体供应商协议（`ResponseNormalizer` 作为 Adapter 内函数，不强制单独类体系） |
| `UsageRecorder` | 提取 Token 用量、请求耗时与模型标识 |

**不设计** `ModelRouter`、`ModelPool`、自动模型评分器。

### Main / Reviewer 如何使用

```
Main Agent（Coding Prompt：搜索、编辑、测试、Finish）──┐
                                                      ├→ 共享 ModelClient / ProviderAdapter
Reviewer（Review Prompt：只读工具、Submit Review）──────┘
```

各自传入**独立消息历史与工具 Schema**；默认两个 Agent 可用同一模型，但不共享模型对话状态。以后是否给 Reviewer 换模型，通过 RunConfig 配置即可，无需重建模型接入系统。

## 三、核心接口契约

### ModelRequest

| 字段 | 含义 |
| --- | --- |
| `actor` | `MAIN` / `REVIEWER` |
| `model_id` | 模型名称 |
| `messages` | Context Engine 构造并通过协议校验的消息 |
| `tools` | 当前角色允许使用的工具 Schema——**由 actor 对应的 Tool Registry 生成，不能由模型决定** |
| `max_output_tokens` / `timeout_seconds` | 输出上限 / 请求超时 |
| `request_id` | 本地生成的请求关联 ID |

### ModelResponse

| 字段 | 含义 |
| --- | --- |
| `response_id` | 供应商返回的响应 ID |
| `message` | 标准化的 assistant 消息 |
| `tool_calls` | 本轮工具调用数组（每个保留 `call_id`、`tool_name`、`raw_arguments`、解析后 `arguments`） |
| `finish_reason` | 输出结束原因 |
| `usage` | Token 使用信息 |
| `provider_metadata` | 协议续接所需的受控元数据 |

**为什么同时保留 `raw_arguments` 和 `arguments`**：原始参数用于诊断和准确记录模型输出；解析后参数用于 Schema 校验和执行。

**解析失败的处理**：JSON 解析失败但 Call ID 和工具名称合法 → 仍生成关联到该 ID 的 `INVALID_ARGUMENT` ToolResult；**Call ID 丢失或重复 → 整个响应批次不可接纳**，不能自行生成 ID 假装来自模型。

### 两条协议硬约束

1. **完整响应接纳之后才能执行工具**——输出被截断、结构损坏或未完成时，不允许根据部分内容提前执行。
2. **模型历史必须按供应商原生工具消息协议重建**——不能把历史变成纯文本；不能丢失工具调用 ID、角色关系和供应商要求保留的续接字段。Event Store 除保存标准化执行结果外，还应保留协议续接所需信息，但**不得把 API Key 等敏感凭据写入 Trace**。

## 四、调用流程与异常分类

```
Runtime 提交 ModelRequest
→ Gateway 校验协议能力
→ ProviderAdapter 构造请求
→ 模型 API 返回
→ 检查响应是否完整
→ 标准化 ModelResponse
→ 返回 Runtime
```

| 情况 | 处理决策 |
| --- | --- |
| 429、临时网络失败、可重试 5xx | Gateway 分类，Runtime 在预算内批准重试 |
| API 认证失败、参数不支持 | 不重试，返回永久错误 |
| 响应不完整、输出达到长度限制 | 不执行工具，进入有限协议纠正 |
| Tool 参数 JSON 无效 | 保留 Call ID，由 Tool System 返回参数错误 |
| 未知 Tool 名称 | 交给 Tool Registry 判定 |
| Usage 缺失 | 标记未知，**不虚构 Token 和费用** |
| 特殊模型协议不支持 | 请求前拒绝，不静默忽略必需字段 |

沿用 Runtime 重试规则：单次请求最多额外重试 2 次，**实际 API 请求次数都计入总预算**；Gateway 不能自行无限退避，也不能绕过 Runtime 的模型请求预算。

## 五、模块 3 不实现什么

不做多模型自动切换、复杂路由、流式 Tool Calling、模型级缓存、自动 Prompt 优化、跨 Agent 模型会话共享。首版目标：让两个 Agent 稳定地得到**有效、可执行、可追踪**的模型响应。

**关键验收**：模型连续返回多个 Tool Call 时，ID、顺序和参数均被正确保留；无效 JSON 不触发实际执行；供应商特殊消息字段不在下一轮请求中被错误丢弃。
