"""Test Agent 公开接口（MVP v0.4 功能 B）。"""
from repofix.test_agent.generator import TestAgentGenerator, TestGenerationResult
from repofix.test_agent.runner import (
    GENERATED_TEST_PATH,
    TestAgentRunner,
    classify_reproduction,
    should_trigger,
    validate_candidate_source,
)
from repofix.test_agent.schemas import (
    ReproStatus,
    TestAgentReport,
    TestCandidate,
)

__all__ = [
    "GENERATED_TEST_PATH",
    "ReproStatus",
    "TestAgentGenerator",
    "TestAgentReport",
    "TestAgentRunner",
    "TestCandidate",
    "TestGenerationResult",
    "classify_reproduction",
    "should_trigger",
    "validate_candidate_source",
]
