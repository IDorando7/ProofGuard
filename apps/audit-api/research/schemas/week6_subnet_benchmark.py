from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

BENCHMARK_VERSION = "week_6_subnet_benchmark_v0"
REQUIRED_CASE_IDS = (
    "best_access_control_selected",
    "weak_reentrancy_not_selected",
    "new_node_exploration",
    "unsafe_node_suspended",
    "category_score_isolation",
    "multi_category_routing",
    "subnet_reward_distribution",
)
GENERATED_FILENAMES = (
    "subnet_case_results.json",
    "subnet_leaderboard.json",
    "week_6_summary.json",
    "week_6_report.md",
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Benchmark timestamps must be timezone-aware")
    return value


class BenchmarkAssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1)
    passed: bool
    expected: JsonValue = None
    actual: JsonValue = None
    details: str | None = None

    @field_validator("assertion_id", "description", "details", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubnetBenchmarkCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    passed: bool
    phase: str = Field(..., min_length=1)
    setup_summary: list[str] = Field(default_factory=list)
    service_operations: list[str] = Field(default_factory=list)
    expected_behavior: list[str] = Field(default_factory=list)
    actual_behavior: list[str] = Field(default_factory=list)
    node_ids: list[str]
    project_ids: list[str]
    subnet_ids: list[str]
    assertions: list[BenchmarkAssertionResult] = Field(..., min_length=1)
    metrics: dict[str, JsonValue]
    warnings: list[str]
    errors: list[str]
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(..., ge=0)

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator(
        "setup_summary",
        "service_operations",
        "expected_behavior",
        "actual_behavior",
        "node_ids",
        "project_ids",
        "subnet_ids",
        "warnings",
        "errors",
    )
    @classmethod
    def validate_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Benchmark string lists cannot contain empty values")
        return cleaned

    @model_validator(mode="after")
    def validate_result(self) -> "SubnetBenchmarkCaseResult":
        if self.case_id not in REQUIRED_CASE_IDS:
            raise ValueError("Unknown Week 6 benchmark case identifier")
        if len({item.assertion_id for item in self.assertions}) != len(self.assertions):
            raise ValueError("Assertion IDs must be unique within a case")
        logical_pass = all(item.passed for item in self.assertions) and not self.errors
        if self.passed and not logical_pass:
            raise ValueError("A case with a failed assertion or error cannot pass")
        if self.completed_at < self.started_at:
            raise ValueError("Case completion cannot precede its start")
        return self


class Week6CaseResultsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    cases: list[SubnetBenchmarkCaseResult]

    @model_validator(mode="after")
    def validate_cases(self) -> "Week6CaseResultsDocument":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Benchmark case IDs must be unique")
        if tuple(case_ids) != REQUIRED_CASE_IDS:
            raise ValueError("All required cases must exist in deterministic order")
        return self


class Week6BenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    protocol_version: str = Field(..., min_length=1)
    week: int = 6
    benchmark_passed: bool
    total_cases: int = Field(..., ge=0)
    passed_cases: int = Field(..., ge=0)
    failed_cases: int = Field(..., ge=0)
    total_assertions: int = Field(..., ge=0)
    passed_assertions: int = Field(..., ge=0)
    failed_assertions: int = Field(..., ge=0)
    pass_rate: float = Field(..., ge=0.0, le=1.0)
    generated_files: list[str] = Field(..., min_length=1)
    category_count: int = Field(..., ge=0)
    node_count: int = Field(..., ge=0)
    project_count: int = Field(..., ge=0)
    routing_count: int = Field(..., ge=0)
    routing_assignment_count: int = Field(..., ge=0)
    reward_cycle_count: int = Field(..., ge=0)
    reward_event_count: int = Field(..., ge=0)
    total_simulated_pool_points: str = Field(..., pattern=r"^\d+\.\d{6}$")
    total_distributed_points: str = Field(..., pattern=r"^\d+\.\d{6}$")
    total_undistributed_points: str = Field(..., pattern=r"^\d+\.\d{6}$")
    deterministic_replay_passed: bool
    failed_case_ids: list[str]
    categories: list[str]
    membership_distribution: dict[str, int]
    routing_distribution: dict[str, int]
    reward_distribution: dict[str, int]
    idempotency_checks: dict[str, bool]
    known_limitations: list[str]
    started_at: datetime
    completed_at: datetime

    @field_validator("started_at", "completed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator(
        "total_simulated_pool_points",
        "total_distributed_points",
        "total_undistributed_points",
        mode="before",
    )
    @classmethod
    def require_decimal_strings(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("Protocol-point totals must be serialized as strings")
        return value

    @model_validator(mode="after")
    def validate_totals(self) -> "Week6BenchmarkSummary":
        if self.total_cases != self.passed_cases + self.failed_cases:
            raise ValueError("Case totals do not balance")
        if self.total_assertions != self.passed_assertions + self.failed_assertions:
            raise ValueError("Assertion totals do not balance")
        expected_rate = (
            round(self.passed_assertions / self.total_assertions, 6)
            if self.total_assertions
            else 0.0
        )
        if self.pass_rate != expected_rate:
            raise ValueError("Pass rate must match assertion totals")
        if self.completed_at < self.started_at:
            raise ValueError("Benchmark completion cannot precede its start")
        if not set(GENERATED_FILENAMES).issubset(self.generated_files):
            raise ValueError("All required generated files must be listed")
        return self
