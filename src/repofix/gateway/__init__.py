"""LLM Gateway：模型接入抽象（M1 仅 Protocol；M3 接入 Chat Completions ProviderAdapter）。"""
from repofix.gateway.client import (
    ModelClient,
    ModelClientError,
    ModelPermanentError,
    ModelTransientError,
)

__all__ = ["ModelClient", "ModelClientError", "ModelPermanentError", "ModelTransientError"]
