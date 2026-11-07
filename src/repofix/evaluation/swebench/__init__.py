"""SWE-bench Lite Mini-Eval（docs/BENCHMARK-MINI.md 的实现支撑）。

组件边界（GPT 方案修订版，与用户确认的四项决策）：
- dataset.py：数据集加载（hf-mirror 直连 + pyarrow 解析，零代理依赖）；
- selection.py：冻结选题规则（跑前固定，不因模型结果换题）；
- judge.py：官方评测协议的自实现判定器（官方 per-instance Docker 镜像内执行）；
- workspace.py：Agent 执行环境适配（官方实例镜像 + /testbed 挂载 + rg 派生层）；
- runner.py：per-task AgentRunner 装配与 token 台账；
- report.py：docs/BENCHMARK-MINI.md 汇总生成。

本目录只做评测适配，不改动 RepoFix 核心 Harness。
"""
