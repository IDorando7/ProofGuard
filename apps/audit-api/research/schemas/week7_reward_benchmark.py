from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


BENCHMARK_VERSION = "week7_reward_benchmark_v1"
REQUIRED_CASE_IDS = (
    "single_finder_full_reward",
    "multiple_equal_duplicates",
    "top_k_large_duplicate_cluster",
    "same_operator_multi_node",
    "early_low_quality_not_chief",
    "better_report_after_chief",
    "chief_outside_top_k",
    "no_chief_redistribution",
    "severity_disagreement",
    "uniqueness_operator_based",
    "category_pool_isolation",
    "undistributed_cluster",
    "task_pool_conservation",
    "double_reward_prevention",
    "source_change_blocks_finalize",
    "partial_crash_recovery",
    "deterministic_replay",
)
GENERATED_FILENAMES = (
    "week7_case_results.json",
    "week7_summary.json",
    "week7_cluster_allocations.json",
    "week7_operator_allocations.json",
    "week7_reward_events.json",
    "week7_invariants.json",
    "week7_determinism.json",
    "week7_report.md",
)


class Week7BenchmarkAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    passed: bool
    expected: JsonValue = None
    actual: JsonValue = None


class Week7BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    expected: dict[str, JsonValue]
    actual: dict[str, JsonValue]
    invariants: list[str]
    notes: list[str] = Field(default_factory=list)
    assertions: list[Week7BenchmarkAssertion] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_case(self) -> "Week7BenchmarkCase":
        if self.case_id not in REQUIRED_CASE_IDS:
            raise ValueError("Unknown Week 7 benchmark case")
        if self.passed != all(item.passed for item in self.assertions):
            raise ValueError("Case status must match assertion results")
        return self


class Week7CaseResultsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    cases: list[Week7BenchmarkCase]

    @model_validator(mode="after")
    def validate_cases(self) -> "Week7CaseResultsDocument":
        if tuple(item.case_id for item in self.cases) != REQUIRED_CASE_IDS:
            raise ValueError("All required Week 7 cases must use deterministic order")
        return self


class Week7BenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    benchmark_passed: bool
    total_cases: int = Field(..., ge=0)
    passed: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    nodes: int = Field(..., ge=0)
    operators: int = Field(..., ge=0)
    submissions: int = Field(..., ge=0)
    valid_submissions: int = Field(..., ge=0)
    invalid_submissions: int = Field(..., ge=0)
    finding_clusters: int = Field(..., ge=0)
    rewarded_clusters: int = Field(..., ge=0)
    undistributed_clusters: int = Field(..., ge=0)
    reward_events: int = Field(..., ge=0)
    rewarded_operators: int = Field(..., ge=0)
    chief_finder_count: int = Field(..., ge=0)
    miner_pool: str
    distributed_miner_amount: str
    undistributed_miner_amount: str
    validator_pool_reserved: str
    protocol_pool_reserved: str
    determinism_passed: bool
    crash_recovery_passed: bool
    source_revalidation_passed: bool
    double_reward_protection_passed: bool
    generated_files: list[str]
    durations_ms: dict[str, int]
    known_limitations: list[str]
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Benchmark timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "Week7BenchmarkSummary":
        if self.total_cases != self.passed + self.failed:
            raise ValueError("Case totals do not balance")
        if not set(GENERATED_FILENAMES).issubset(self.generated_files):
            raise ValueError("All generated files must be listed")
        return self


def json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
