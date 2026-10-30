"""LLM Gateway：模型接入抽象与 Chat Completions ProviderAdapter（M3-1）。"""
from repofix.gateway.adapter import DeepSeekAdapter
from repofix.gateway.client import (
    ModelClient,
    ModelClientError,
    ModelPermanentError,
    ModelProtocolError,
    ModelTransientError,
)
from repofix.gateway.transport import (
    HttpTransport,
    TransportError,
    TransportResult,
    UrllibHttpTransport,
)

__all__ = [
    "DeepSeekAdapter",
    "HttpTransport",
    "ModelClient",
    "ModelClientError",
    "ModelPermanentError",
    "ModelProtocolError",
    "ModelTransientError",
    "TransportError",
    "TransportResult",
    "UrllibHttpTransport",
]
