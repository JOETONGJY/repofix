"""Gateway 传输层：把 HTTP 细节从协议适配中隔离（可测试性——GW 测试注入假传输）。"""
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


class TransportError(RuntimeError):
    """网络层失败（连接失败 / 超时 / DNS）——可重试。"""


@dataclass
class TransportResult:
    status: int
    body: str
    headers: dict = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class HttpTransport(Protocol):
    """POST 一个 JSON 载荷，返回状态码与响应体。HTTP 4xx/5xx 不抛异常（由适配层分类）。"""

    def post(self, path: str, payload: dict, timeout: int) -> TransportResult: ...


class UrllibHttpTransport:
    """基于标准库 urllib 的真实传输（M3 不引入额外 HTTP 依赖）。"""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def post(self, path: str, payload: dict, timeout: int) -> TransportResult:
        request = urllib.request.Request(
            self._base_url + path,
            data=Serialize.json_bytes(payload),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return TransportResult(
                    status=resp.status,
                    body=resp.read().decode("utf-8", errors="replace"),
                    headers=dict(resp.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            # HTTP 错误状态交给适配层分类（429/5xx 重试，4xx 永久）
            return TransportResult(
                status=exc.code,
                body=exc.read().decode("utf-8", errors="replace"),
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"network failure: {exc}") from exc


class Serialize:
    @staticmethod
    def json_bytes(payload: dict) -> bytes:
        import json

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
