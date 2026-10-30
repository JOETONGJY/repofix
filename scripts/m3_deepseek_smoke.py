"""M3-0：DeepSeek 协议冒烟（最小验证，控制 Token 消耗）。

三项检查（总成本预期远低于 0.01 元）：
1. GET /models —— 免费确认鉴权有效 + 获取当前可用模型 ID（plan.md 记录 2026 命名：deepseek-flash / deepseek-v4-pro）
2. 最小 chat completion（max_tokens=8）—— 验证端点、鉴权、响应与 usage 结构
3. 最小 tool calling（max_tokens=80）—— 验证 Chat Completions Function Calling 协议：
   tool_calls 结构、call_id、arguments JSON、finish_reason、usage 字段形状

安全：API Key 只从 DEEPSEEK_API_KEY 环境变量读取，不写入本文件/仓库/日志（design/03）。
运行：DEEPSEEK_API_KEY=... python scripts/m3_deepseek_smoke.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://api.deepseek.com"


def _post(path: str, payload: dict, timeout: int = 60) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("ERROR: DEEPSEEK_API_KEY not set")
        sys.exit(2)
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str, timeout: int = 30) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY")
    request = urllib.request.Request(
        BASE_URL + path, headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    # 1. 模型列表（免费）：鉴权 + 模型命名确认
    models = _get("/models")
    model_ids = [m["id"] for m in models.get("data", [])]
    print(f"step1 models: {model_ids}")
    model = model_ids[0] if model_ids else "deepseek-chat"
    # 优先使用 plan.md 记录的 2026 命名
    for preferred in ("deepseek-flash", "deepseek-v4-pro"):
        if preferred in model_ids:
            model = preferred
            break
    print(f"step1 model chosen: {model}")

    # 2. 最小 chat：验证响应与 usage 结构
    chat = _post("/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 8,
        "stream": False,
    })
    usage = chat.get("usage", {})
    print(f"step2 chat: finish_reason={chat['choices'][0].get('finish_reason')} "
          f"content={chat['choices'][0]['message'].get('content')!r} "
          f"usage_keys={sorted(usage)} total_tokens={usage.get('total_tokens')}")

    # 3. 最小 tool calling：验证 Function Calling 协议结构
    tool_resp = _post("/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "Read the file src/auth.py"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a text file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }],
        "max_tokens": 80,
        "stream": False,
    })
    msg = tool_resp["choices"][0]["message"]
    tool_calls = msg.get("tool_calls") or []
    print(f"step3 tool_call: finish_reason={tool_resp['choices'][0].get('finish_reason')} "
          f"n_tool_calls={len(tool_calls)}")
    if tool_calls:
        call = tool_calls[0]
        print(f"  call_id={call.get('id')!r} type={call.get('type')!r} "
              f"name={call['function'].get('name')!r}")
        arguments = call["function"].get("arguments", "")
        parsed = json.loads(arguments)  # 能否直接 JSON 解析（协议假设）
        print(f"  arguments_json_ok={isinstance(parsed, dict)} arguments={arguments!r}")
    print(f"  usage: total_tokens={tool_resp.get('usage', {}).get('total_tokens')}")
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}")
        sys.exit(1)
