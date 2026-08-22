from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


BENCHMARK_VERSION = "week7_reward_benchmark_v2"
REQUIRED_CASE_IDS = (
    "quality_formula_known_vector",
    "uniqueness_single_operator",
    "uniqueness_same_operator_multiple_nodes",
    "uniqueness_many_independent_operators",
    "severity_ordering",
    "common_critical_vs_unique_high",
    "global_cluster_reward_distribution",
    "category_pool_isolation",
    "operator_best_report",
    "operator_multi_node_top_k_protection",
    "top_k_boundary",
    "top_k_does_not_change_uniqueness",
    "chief_basic",
    "chief_best_vs_earliest_qualifying",
    "chief_early_low_quality",
    "chief_outside_top_k",
    "no_chief_quality_pool_fallback",
    "single_operator_full_cluster_reward",
    "q_squared_distribution",
    "zero_q_non_distributable",
    "historical_performance_reward_independence",
    "multi_routing_project_isolation",
    "deterministic_replay",
    "insertion_order_independence",
    "persistence_reload_verification",
    "stale_severity_blocks_finalize",
    "stale_quality_blocks_finalize",
    "reputation_change_does_not_block_finalize",
    "finalize_retry_no_double_reward",
    "partial_crash_recovery",
    "complete_event_set_recovery",
    "event_corruption_detection",
    "full_accounting_conservation",
)
GENERATED_FILENAMES = (
    "week7_case_results.json",
    "week7_summary.json",
    "week_7_summary.json",
    "week7_cluster_allocations.json",
    "week_7_cluster_summary.json",
    "week7_operator_allocations.json",
    "week_7_operator_summary.json",
    "week7_reward_events.json",
    "week7_invariants.json",
    "week7_determinism.json",
    "week_7_determinism_report.json",
    "week_7_accounting_report.json",
    "week_7_performance_report.json",
    "week7_report.md",
    "week_7_report.md",
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
    name: str
    status: str
    invariant_group: str
    duration_ms: int = Field(default=0, ge=0)
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
        if self.status != ("pass" if self.passed else "fail"):
            raise ValueError("Case status must match pass/fail state")
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
    seed: int
    runtime_version: str
    reward_policy_version: str
    fingerprint_version: str
    protocol_quantum: str
    benchmark_passed: bool
    total_cases: int = Field(..., ge=0)
    passed: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    projects: int = Field(..., ge=1)
    routings: int = Field(..., ge=1)
    categories: int = Field(..., ge=1)
    nodes: int = Field(..., ge=0)
    operators: int = Field(..., ge=0)
    submissions: int = Field(..., ge=0)
    valid_submissions: int = Field(..., ge=0)
    invalid_submissions: int = Field(..., ge=0)
    finding_clusters: int = Field(..., ge=0)
    quality_assessments: int = Field(..., ge=0)
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
    insertion_order_passed: bool
    accounting_conservation_passed: bool
    verification_passed: bool
    crash_recovery_passed: bool
    source_revalidation_passed: bool
    double_reward_protection_passed: bool
    deterministic_state_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    regression_suite_passed: bool
    full_test_count: int = Field(..., ge=0)
    failed_test_count: int = Field(..., ge=0)
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
