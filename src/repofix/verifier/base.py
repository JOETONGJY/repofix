"""Verifier 接口（design/08）：确定性验证组件，不是第三个 Agent。

不调用 LLM 判断代码正确性，不维护推理历史；
依据真实工作区、Patch 和测试执行结果生成 VerificationReport。
M1 使用 FakeVerifier（tests/）；M3 接入真实 PatchValidator + TestRunner。
"""
from typing import Protocol

from repofix.contracts import VerificationReport, VerificationRequest


class Verifier(Protocol):
    """Runtime → Verifier 的 Final Verification 入口。test_plan 初始化时确定，不可被 finish 改写。"""

    def verify(self, request: VerificationRequest) -> VerificationReport: ...
