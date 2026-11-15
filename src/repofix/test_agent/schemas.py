"""Test Agent 数据合同（MVP v0.4 功能 B）。

TestCandidate 是模型输出的唯一合法结构（模型只生成测试内容，不决定路径/
Shell 命令/生产代码）；TestAgentReport 是 Harness 写入 test-agent/report.json
的结构化报告。复现状态只有四类有效值，另加三个生命周期状态（未触发/生成
失败/模型拒绝），不与 VerificationOutcome 混用。
"""
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 生成测试注入到测试工作区的固定路径（Harness 决定，模型无权指定）
GENERATED_TEST_PATH = "tests/test_repofix_generated.py"
# 生成测试文件内容上限（字节）
MAX_TEST_SOURCE_BYTES = 8 * 1024


class TestCandidate(BaseModel):
    """模型生成测试的唯一输出合同（specs 任务书 §7）。"""

    test_source: str = Field(min_length=1)
    explanation: str = ""


class ReproStatus(str, Enum):
    """候选测试在 Bug 基线上的复现结果（任务书 §9 四类 + 三个生命周期状态）。"""

    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INVALID_TEST = "invalid_test"
    ENVIRONMENT_ERROR = "environment_error"
    # 生命周期状态（非复现结果）：未触发 / 模型调用失败 / 模型声明无法生成
    NOT_TRIGGERED = "not_triggered"
    GENERATION_FAILED = "generation_failed"
    DECLINED = "declined"


class TestAgentReport(BaseModel):
    """一次 Test Agent 参与运行的完整记录 → test-agent/report.json。

    Token 用量必须来自 Gateway 返回值；缺失时记录为 None（未知），不得伪造为零。
    authoritative_verification 恒为 False（MVP）：生成测试只是补充验证信号，
    不改变 Final Verifier / Evaluation 的权威语义。
    """

    # pytest 采集豁免（类名 Test* 前缀的运行时组件）；dunder 不进入 Pydantic 字段
    __test__ = False

    enabled: bool = False
    triggered: bool = False
    trigger_reason: str = ""
    status: ReproStatus = ReproStatus.NOT_TRIGGERED
    status_detail: str = ""
    source: Literal["llm_generated", "none"] = "none"
    # 注入到测试工作区的路径（workspace-relative）与产物文件名
    test_file: str = ""
    artifact_test_file: str = "candidate_test.py"
    baseline_exit_code: int | None = None
    baseline_failures: list[str] = Field(default_factory=list)  # 落在生成测试内的失败身份
    post_patch_exit_code: int | None = None
    post_patch_status: Literal["passed", "failed", "error", "not_run"] = "not_run"
    post_patch_detail: str = ""
    fail_to_pass: bool = False
    # P1（评审补充验证可信度）：基线运行若污染了测试工作区的受跟踪文件，
    # verify_post_patch 会先重建干净副本再应用 Patch，并在此标记
    test_workspace_rebuilt: bool = False
    model_calls: int = 0
    token_usage: dict = Field(
        default_factory=lambda: {"prompt": None, "completion": None, "total": None}
    )
    generation_duration_ms: int | None = None
    violations: list[str] = Field(default_factory=list)
    authoritative_verification: bool = False

    # model_calls / model_* 字段与 pydantic 受保护命名空间冲突仅为命名告警，
    # 本模型不需要那层保护（contracts 现有模型同样带 model_id 字段）
    model_config = ConfigDict(protected_namespaces=())
