"""LLM Gateway：统一模型调用接口（design/03）。

Gateway 不执行工具；Tool System 不自主调用模型；Runtime 拥有控制权。
M1 仅定义 Protocol 与错误分类；真实 ProviderAdapter（Chat Completions）在 M3 接入，
ScriptedModelClient 放 tests/（与真实组件同接口契约）。
"""
from typing import Protocol

from repofix.contracts import ModelRequest, ModelResponse


class ModelClientError(RuntimeError):
    """模型调用失败基类。"""


class ModelPermanentError(ModelClientError):
    """不可重试：认证失败、参数不支持等（fail_layer=MODEL_API，终止）。"""


class ModelTransientError(ModelClientError):
    """可重试：429 / 5xx / 超时 / 连接错误（指数退避，受 model_retry_limit 约束）。"""


class ModelProtocolError(ModelClientError):
    """响应结构损坏 / 不完整 / call_id 丢失或重复 / 输出被截断。

    完整响应接纳之后才能执行工具（design/03 硬约束）；此类错误由 Runtime
    做有限协议纠正（重发一次），不得根据部分内容提前执行。
    """


class ModelClient(Protocol):
    """Runtime → Gateway 的唯一模型入口。实现方负责协议转换与响应标准化。"""

    def complete(self, request: ModelRequest) -> ModelResponse: ...
