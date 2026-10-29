# RepoFix 实现规格 04：验收测试（Acceptance Tests）

> 版本：V0.1（自 V1.3 架构文档提炼）。上位文档：[../技术方案探讨.md](../技术方案探讨.md)。
> 用途：定义每个契约"实现完成"的判定标准。这些是**架构正确性测试**，不依赖 20–30 个 Benchmark 任务。

---

## Vertical Slice（第一里程碑，先于一切模块补强）

**定义**：Mock Model 按脚本依次提出 `search_code → read_file → apply_patch → run_command(pytest) → finish`，让**真实的** Runtime、Tool System、Workspace 接口完整跑通。

**验收标准**：

1. 无真实 LLM、无外网依赖即可运行（Mock ModelClient 返回预设序列）。
2. 每步 ToolCall 与 ToolResult 按 call_id 一一配对，全部事件入库（runs / events / tool_calls / usage）。
3. apply_patch 的 PENDING/STARTED/SUCCEEDED 生命周期完整，pre_state_hash 与 expected_result 已记录。
4. Final Check 通过，产出 `changes.patch` + `trajectory.jsonl` + `result.json` + `test-report.json`，退出码 0。
5. 全部契约测试通过（见下矩阵）。
6. Vertical Slice 跑在 `review_enabled=False`（单 Agent Baseline）路径；Review 链路由 RV 系列测试单独覆盖。

## 测试矩阵

### Runtime（对应 specs/02）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| RT-01 | 模型返回 0 个 ToolCall（空响应） | 触发一次协议错误重发；再次为空 → FAILED(MODEL_API) |
| RT-02 | 模型返回 1 个 ToolCall | 正常执行、配对、回流 |
| RT-03 | 模型返回 N 个 ToolCall | 顺序执行；结果顺序与 call_id 对应 |
| RT-04 | 批次中写入类调用失败 | 剩余调用全部 SKIPPED 且带 skipped_reason；成功结果保留 |
| RT-05 | 只读调用失败后跟写入调用 | 写入调用 SKIPPED |
| RT-06 | finish 与其他调用混合返回 | **整批拒绝**：无任何工具执行（含只读、无副作用）；finish 得 INVALID_ARGUMENT、其余 SKIPPED（batch_rejected_finish_mixed），配对完整；下一轮单独 finish 可接受 |
| RT-07 | finish 后 Final Check 断言失败 | 状态 VERIFYING → RUNNING，反馈回流；预算内可继续 |
| RT-08 | Final Check 环境错误 | COMPLETED + UNDETERMINABLE 报告；不重验 |
| RT-09 | 状态转换表 | 表中每条合法转换可达、非法转换抛异常 |
| RT-10 | 预算触顶（次数/Token/时间，参数化） | BUDGET_EXCEEDED，事件入库 |
| RT-11 | 无进展 | 3 次重复注入 NO_PROGRESS_WARNING 一次；达 5 次终止 |
| RT-12 | 越权操作（MVP 无审批） | 直接 PERMISSION_DENIED 回流，无任何审批交互；WAITING_APPROVAL 全程不出现 |
| RT-13 | 模型仅输出文本（无工具调用、无 finish） | 不结束任务；按协议错误处理（同 RT-01 策略） |

### Tool System（对应 specs/03）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| TL-01 | 调用未注册工具 | TOOL_NOT_FOUND + 可用工具列表 |
| TL-02 | 每个工具的参数校验（缺参/类型错/越界，参数化） | INVALID_ARGUMENT，不执行 |
| TL-03 | read_file 不存在的文件 | EXECUTION_ERROR 业务反馈 |
| TL-04 | apply_patch：路径逃逸（绝对路径 / `..` / symlink） | INVALID_ARGUMENT，无文件修改 |
| TL-05 | apply_patch：expected_sha256 不匹配 | CONFLICT |
| TL-06 | apply_patch：上下文不匹配 | EXECUTION_ERROR |
| TL-07 | apply_patch 成功（单文件） | 事务内 PENDING→STARTED→SUCCEEDED；pre/expected hash 已记录；文件原子替换 |
| TL-08 | run_command 超时 | TIMEOUT；**进程树被终止**（无残留子进程） |
| TL-09 | 白名单外命令（如 pip install / bash / python -c） | PERMISSION_DENIED |
| TL-10 | apply_patch 新建文件：目标已存在 | CONFLICT，不覆盖 |
| TL-11 | search_code 无匹配 | SUCCEEDED 空结果（非失败），批次继续 |
| TL-12 | 输出超限（search>100 / read>200 行 / >32KiB） | truncated=true，保留开头/结尾/错误摘要 |
| TL-13 | read_file 返回 sha256 | 与文件实际内容哈希一致，可被 apply_patch 版本校验使用 |

### Workspace（对应 specs/03）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| WS-01 | 隔离性：任务执行前后用户原仓库 | 原仓库 HEAD、工作区文件、git status 完全不变 |
| WS-02 | 副本基于 base_commit | checkout 后 HEAD == base_commit |
| WS-03 | 脏工作区（已跟踪修改 / 未跟踪未忽略文件） | **拒绝执行**；原仓库保持不变 |
| WS-04 | 容器约束 | 非 root、无网络、资源限制生效、无凭据泄露 |
| WS-05 | Preflight 各步失败（参数化） | FAILED(WORKSPACE)，不进入 Agent Loop |
| WS-06 | Preflight 基线记录 | 既有失败测试清单入库，供归因；**基线断言失败 ≠ Preflight 失败**（环境阻塞才 FAIL） |
| WS-07 | Patch 导出完整性 | 经 `get_changes` 与 Base Commit 比较导出；**包含 run_command 产生的文件变化**；排除缓存/忽略文件 |
| WS-08 | 超时与未知副作用 | 进程树终止；副作用未知时不自动重试，Run 终止并保留诊断 |

### Context（对应 specs/02 Context Policy）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| CX-01 | 长 Shell 输出截断 | 模型可见输出 ≤ 上限且保留错误尾部 |
| CX-02 | 配对完整性 | 任意轮次消息中 ToolCall 与 ToolResult 一一配对，无孤儿 |
| CX-03 | 压缩后关键事实 | 任务约束、已修改文件列表、未解决错误仍在上下文 |
| CX-04 | 预算超限 | 紧急压缩一次；仍超限 → CONTEXT_BUDGET_EXCEEDED 受控失败，无静默截断 |
| CX-05 | 轮次单位选择 | 裁剪以完整模型—工具轮次为单位；不出现只有 Result 没有对应 Call 的孤立消息 |
| CX-06 | 文件版本失效 | 文件修改后，Working Memory 与上下文不继续引用过期片段；未重读的文件不标记"已确认最新" |
| CX-07 | Reviewer 上下文不足 | Diff 超预算且无法补充证据时返回 INCONCLUSIVE；**不计为代码验证失败** |

### State / 恢复（对应 specs/01、02）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| ST-01 | 崩溃注入：STARTED 后进程被 kill，patch 已落盘 | 恢复对账 → 补记 SUCCEEDED，不重复修改 |
| ST-02 | 崩溃注入：patch 未落盘 | 恢复对账 → 判定未生效，按规则重试或 SKIPPED |
| ST-03 | 崩溃注入：文件处于无法解释状态 | 补记 UNKNOWN，Run 终止，标记人工确认 |
| ST-04 | Trace 导出 | trajectory.jsonl 与 SQLite 内容一致；仅由导出产生 |

### Verifier / Evaluation（对应 specs/02）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| VF-01 | 失败归因 | 断言失败 / 环境错误 / 超时 / 无法判定四类正确分类，反馈策略不同 |
| VF-02 | 双状态统计 | Runtime COMPLETED + 验收 FAILED 可同时成立；分母按 NOT_EVALUATED / SUCCESS / FAILED / INCONCLUSIVE 分别计数 |
| VF-03 | 既有失败排除 | 按测试身份比较基线：基线失败+最终通过=修复完成；基线通过+最终失败=回归；**不能把基线失败测试全部排除出验收** |
| VF-04 | finish 但测试失败 | Runtime 返回修复循环，不接受 Finish |
| VF-05 | 无可用测试 | Outcome=INCONCLUSIVE，**不能错误产生 PASSED**；可进 Review |
| VF-06 | 验证后代码变化 | 旧 VerificationReport 失效，不能用于接受最终 Patch（candidate_revision 绑定） |
| VF-07 | test_plan 不可改写 | Main 在 finish 时无法改写规定测试计划 |
| VF-08 | Bug 修复需有效代码变化 | 无 Diff 的纯解释 finish → PatchValidator 判 EMPTY → FAILED 反馈 |

### LLM Gateway（对应 design/03）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| GW-01 | 模型连续返回多个 Tool Call | ID、顺序、参数（含 raw_arguments）均正确保留 |
| GW-02 | Tool 参数为无效 JSON | 不触发执行；生成关联 call_id 的 INVALID_ARGUMENT |
| GW-03 | call_id 丢失或重复 | 整个响应批次不可接纳；不自行生成 ID |
| GW-04 | 响应不完整 / 达到输出长度限制 | 不执行工具，进入有限协议纠正 |
| GW-05 | 供应商特殊字段（如 reasoning） | 下一轮请求中被正确保留，不被丢弃 |
| GW-06 | 429 / 5xx / 网络失败 | 退避重试生效；实际 API 次数计入预算；超 model_retry_limit 终止 |
| GW-07 | Usage 缺失 | 标记未知，不虚构 Token / 费用 |

### Review Agent（对应 design/02，只保留影响架构正确性的场景）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| RV-01 | Review 触发 | 仅当有效候选 Patch 通过规定 Final Check 后启动；断言失败 / Patch 为空 / 环境错误时不启动 |
| RV-02 | 权限隔离 | Reviewer 请求 apply_patch / run_command / finish → PERMISSION_DENIED（注册表层面不注册） |
| RV-03 | 上下文隔离 | ReviewRequest 不含 Main 的消息历史；Reviewer 行为不依赖 Main 推理记录 |
| RV-04 | 反馈闭环 | FINDINGS 后 Main 在修正预算（≤3 次模型调用）内修正并重新 Final Check；不再二次触发 Review |
| RV-05 | 有界执行 | Reviewer 超 3 次模型调用或 8 次工具调用即被截断；整个任务最多一轮审查 |
| RV-06 | 故障隔离 | Reviewer 不可用 / INCONCLUSIVE 时，已通过确定性验证的任务正常 COMPLETED，报告标记审查未完成 |
| RV-07 | Patch 版本一致性 | 报告中的 reviewed_patch_hash 与审查时 Diff 一致；审查后代码再变更不得声称已审查 |

### Evaluation Harness（对应 design/09）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| EV-01 | 隐藏测试隔离 | 隐藏测试文件不进入 Agent 可见工作区与上下文 |
| EV-02 | 独立复现 | 相同 Patch 在干净工作区（Base Commit 重初始化）应用并验收，结果可复现 |
| EV-03 | 统计口径区分 | Runtime 失败 / 隐藏测试失败 / 评测基础设施异常正确区分；预算耗尽任务留在成功率分母 |
| EV-04 | INCONCLUSIVE 单独报告 | 评测机制异常不混入 FAILED；数量与原因单独输出 |
| EV-05 | 双 Agent 对照 | A/B（+预算匹配对照组）使用同一固定任务清单；Reviewer Finding Precision 可计算 |

### CLI（对应 specs/03 退出码）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| CLI-01 | 完成且验证通过 / 完成但验证不足 / 输入校验失败 / 运行错误 / 预算耗尽 / 取消 | 退出码分别为 0 / 1 / 2 / 3 / 4 / 130 |
| CLI-02 | 输出产物 | changes.patch 可 `git apply` 且含新增文件；trajectory.jsonl 合法 JSONL；result.json 符合 RunResult 契约；均不依赖 Evaluation |

### Task Interface（对应 design/01）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| TI-01 | 合法仓库与任务 | 创建 TaskSpec、RunConfig 并启动 Runtime |
| TI-02 | 同时提供 `--task` 与 `--task-file` | 参数错误 |
| TI-03 | 空任务 | 拒绝执行 |
| TI-04 | 无效 Git 路径 | 拒绝执行 |
| TI-05 | 工作区有未提交修改 | 拒绝执行，原仓库不变 |
| TI-06 | base_commit 不存在 | 拒绝执行 |
| TI-07 | 模型配置缺失 | 拒绝执行 |
| TI-08 | `Ctrl+C` | 请求取消，不再新增任务动作 |
| TI-09 | 正常结束但没有测试 | verification_status=NOT_RUN，退出码 1，不声称修复成功 |
| TI-10 | Patch 导出 | 包含修改与新增文件，不含忽略文件 |
| TI-11 | 失败 / 预算耗尽 | 返回明确终止原因与诊断路径 |
| TI-12 | 同一 TaskSpec 执行两次 | 不同 run_id、相互隔离的工作区与记录 |

## 跨模块一致性（对应全局一致性评审）

| 编号 | 测试 | 断言 |
| --- | --- | --- |
| CONS-01 | 双 Agent 预算 | review_enabled=True 总模型调用 ≤36（30+3+3），False ≤30；Reviewer 工具调用计入全局 max_tool_calls 且受角色上限 8 约束——无双重计数、无遗漏 |
| CONS-02 | 状态枚举不混用 | ToolResult.status ∈ {SUCCEEDED, FAILED, SKIPPED}；UNKNOWN 仅出现在 tool_calls.side_effect_status |
| CONS-03 | 版本绑定 | candidate_revision = patch_hash = sha256(get_changes diff)；验证后 / 审查后工作区变化 → 旧报告失效 |
| CONS-04 | finish 契约唯一 | finish 仅 summary 字段；不存在 review_response 等派生字段 |
| CONS-05 | 测试命令单一路径 | CLI 字符串仅在初始化解析为 argv / test_plan；Runtime / Verifier / Tool System 无二次 Shell 解析 |

## 给 Coding LLM 的任务指令模板

```
请根据项目提供的 Architecture ADR（../技术方案探讨.md）、specs/01-contracts.md、
specs/02-runtime-semantics.md（及涉及模块对应的 specs/03），实现 RepoFix <模块名> 的第 <N> 阶段。

本次只允许实现：
- <明确列出的类/函数/行为，来自 specs>
- 对应的单元测试

要求：
1. 严格使用已定义的数据结构与接口，不自行修改公共契约（01 的枚举与模型不得增删）。
2. 本阶段使用 Mock ModelClient / Mock ToolExecutor（如适用），不接入真实 LLM 或 Docker。
3. 覆盖上表列出的对应测试编号：<列出编号>。
4. 为无法从文档确定的行为列出「待决策项」，不擅自增加功能。
5. 运行全部相关测试，报告通过结果与未完成项。

验收要求：<引用本文件对应行的断言，如 "Runtime 在 Mock 环境完成一次确定性执行闭环，
正常终止并产生结构化事件"= Vertical Slice 的子集>。
```

## 完成判定

- 每个契约：schema 校验测试 + 行为测试全部通过。
- CI 运行全部测试矩阵，绿了才算该模块完成。
- 测试无法覆盖的行为 → 必须作为「待决策项」记录到本文件附录，回报后更新规格，而不是在代码里静默选择。
