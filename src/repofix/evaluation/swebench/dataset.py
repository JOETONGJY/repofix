"""SWE-bench Lite 数据集加载（评测专用，不进入 Agent 执行链路）。

数据来源：HuggingFace `SWE-bench/SWE-bench_Lite`（test 300 / dev 23）。
网络路径：hf-mirror.com 直连（无需代理）；文件缓存到本地后可完全离线复用。
解析依赖：pyarrow（评测可选依赖，不进入 RepoFix 主依赖）。

字段口径（与 HF dataset card 一致，评测只读以下字段）：
- instance_id / repo / base_commit / version / created_at
- problem_statement：GitHub Issue 正文（Agent 唯一可见的任务输入）
- patch：gold patch（仅用于选题统计与报告对照，绝不进入 Agent 工作区）
- test_patch：测试变更（仅判定器使用，绝不进入 Agent 工作区）
- FAIL_TO_PASS / PASS_TO_PASS：判定器测试身份清单（JSON 字符串，仅判定器使用）
"""
import json
from pathlib import Path

import pyarrow.parquet as pq

HF_DATASET = "SWE-bench/SWE-bench_Lite"
HF_MIRROR_BASE = "https://hf-mirror.com"
_SPLITS = ("test", "dev")


class DatasetError(RuntimeError):
    """数据集下载或解析失败。"""


def _parquet_url(split: str) -> str:
    return f"{HF_MIRROR_BASE}/datasets/{HF_DATASET}/resolve/main/data/{split}-00000-of-00001.parquet"


def download_split(split: str, cache_dir: Path, timeout: int = 120) -> Path:
    """下载一个 split 的 parquet 到缓存目录（幂等：已存在且非空则跳过）。"""
    if split not in _SPLITS:
        raise DatasetError(f"unknown split: {split} (expected one of {_SPLITS})")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{split}.parquet"
    if target.exists() and target.stat().st_size > 0:
        return target
    import urllib.request

    req = urllib.request.Request(_parquet_url(split), headers={"User-Agent": "repofix-mini-eval"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001 —— 网络层错误统一转 DatasetError
        raise DatasetError(f"cannot download {split} split from hf-mirror: {exc}") from exc
    if len(data) < 1024:
        raise DatasetError(f"suspiciously small download for {split}: {len(data)} bytes")
    target.write_bytes(data)
    return target


def load_split(split: str, cache_dir: Path) -> list[dict]:
    """加载一个 split 的全部实例（原始 dict 列表，字段见模块 docstring）。"""
    path = download_split(split, cache_dir)
    try:
        table = pq.read_table(path)
    except Exception as exc:  # noqa: BLE001
        raise DatasetError(f"cannot parse {path.name}: {exc}") from exc
    return table.to_pylist()


def get_instance(split: str, instance_id: str, cache_dir: Path) -> dict:
    """按 instance_id 取单个实例；不存在抛 DatasetError（选题冻结后不允许换题）。"""
    for row in load_split(split, cache_dir):
        if row["instance_id"] == instance_id:
            return row
    raise DatasetError(f"instance not found in {split} split: {instance_id}")


def parse_test_list(raw: str | list | None) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS 字段 → 测试身份列表（数据集中是 JSON 字符串）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def patch_files(patch_text: str) -> list[str]:
    """统计一个 unified diff 触及的文件（去重保序；选题规则用，不给 Agent）。"""
    files: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip().split("\t")[0]
            if path.startswith("b/"):
                path = path[2:]
            if path not in files:
                files.append(path)
    return files


def test_patch_files(test_patch: str) -> list[str]:
    """test_patch 触及的测试文件（判定器 reset + apply 的目标清单）。"""
    return patch_files(test_patch)
