from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


BENCHMARK_VERSION = "week8_validator_benchmark_v1"
WEEK_8_BENCHMARK_SEED = 8008

REQUIRED_CASE_IDS = (
    "validator_self_validation_blocked",
    "same_operator_multiple_nodes_one_seat",
    "insufficient_operator_diversity",
    "high_assurance_no_silent_downgrade",
    "category_capability_filter",
    "committee_selection_deterministic",
    "shadow_non_authoritative",
    "independent_reproduction_identity",
    "unsafe_artifact_preflight",
    "standard_four_of_five_confirmed",
    "simple_majority_three_two_disputed",
    "high_assurance_five_of_seven",
    "high_assurance_four_three_disputed",
    "no_quorum",
    "reproduction_only_dispute",
    "severity_only_dispute",
    "root_cause_only_dispute",
    "impact_only_dispute",
    "shadow_vote_excluded",
    "escalation_standard_five_to_nine",
    "escalation_high_assurance_seven_to_eleven",
    "previous_operator_excluded_from_escalation",
    "reporter_excluded_from_escalation",
    "escalation_insufficient_new_validators",
    "escalation_still_disputed",
    "no_result_shopping",
    "original_minority_later_correct",
    "validation_quality_known_vector",
    "validation_quality_na_renormalization",
    "shadow_validator_performance",
    "agent_validator_skill_isolation",
    "validator_category_isolation",
    "validator_score_experience_shrinkage",
    "validator_membership_cold_start",
    "validator_reward_perfect_quality",
    "validator_reward_correct_minority",
    "validator_reward_wrong_majority",
    "validator_reward_unresolved_completion_only",
    "validator_reward_no_show",
    "validator_reward_no_severity_multiplier",
    "validator_reward_no_membership_multiplier",
    "validator_reward_no_categoryscore_multiplier",
    "validator_reward_shadow_ineligible",
    "escalation_does_not_increase_validator_pool",
    "validator_pool_exact_conservation",
    "miner_validator_pool_isolation",
    "validator_pool_double_spend_prevention",
    "reward_finalize_retry_idempotent",
    "reward_partial_write_recovery",
    "reward_corruption_detection",
    "deterministic_replay",
    "insertion_order_independence",
)

GENERATED_FILENAMES = (
    "week_8_case_results.json",
    "week_8_committee_summary.json",
    "week_8_consensus_summary.json",
    "week_8_validator_quality_summary.json",
    "week_8_validator_performance_summary.json",
    "week_8_validator_reward_summary.json",
    "week_8_accounting_report.json",
    "week_8_determinism_report.json",
    "week_8_performance_report.json",
    "week_8_summary.json",
    "week_8_report.md",
)


class Week8BenchmarkMode(str, Enum):
    CI = "ci"
    MEDIUM = "medium"
    FULL = "full"


class Week8BenchmarkAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(..., min_length=1, max_length=160)
    description: str = Field(..., min_length=1)
    passed: bool
    expected: JsonValue = None
    actual: JsonValue = None


class Week8BenchmarkCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    group: str
    description: str
    status: str
    expected: dict[str, JsonValue]
    actual: dict[str, JsonValue]
    invariant_ids: list[str] = Field(..., min_length=1)
    duration_ms: int = Field(default=0, ge=0)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    assertions: list[Week8BenchmarkAssertion] = Field(..., min_length=1)

    @model_validator(mode="after")
    def valid_case(self) -> "Week8BenchmarkCaseResult":
        if self.case_id not in REQUIRED_CASE_IDS:
            raise ValueError("Unknown Week 8 benchmark case")
        passed = all(item.passed for item in self.assertions)
        if self.status != ("pass" if passed else "fail"):
            raise ValueError("Case status must match assertion results")
        return self


class Week8CaseResultsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    cases: list[Week8BenchmarkCaseResult]

    @model_validator(mode="after")
    def ordered_complete_cases(self) -> "Week8CaseResultsDocument":
        if tuple(item.case_id for item in self.cases) != REQUIRED_CASE_IDS:
            raise ValueError("All required Week 8 cases must use deterministic order")
        return self


class Week8BenchmarkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_version: str = BENCHMARK_VERSION
    mode: Week8BenchmarkMode
    seed: int
    benchmark_passed: bool
    cases_total: int = Field(..., ge=0)
    passed: int = Field(..., ge=0)
    failed: int = Field(..., ge=0)
    projects: int = Field(..., ge=1)
    routings: int = Field(..., ge=1)
    categories: int = Field(..., ge=1)
    reporting_operators: int = Field(..., ge=1)
    validator_operators: int = Field(..., ge=1)
    validator_nodes: int = Field(..., ge=1)
    clusters: int = Field(..., ge=1)
    committees: int = Field(..., ge=1)
    authoritative_assignments: int = Field(..., ge=1)
    shadow_assignments: int = Field(..., ge=0)
    reproduction_records: int = Field(..., ge=1)
    attestations: int = Field(..., ge=1)
    quality_assessments: int = Field(..., ge=1)
    performance_events: int = Field(..., ge=1)
    validator_reward_cycles: int = Field(..., ge=1)
    validator_reward_events: int = Field(..., ge=1)
    consensus_confirmed: int = Field(..., ge=0)
    consensus_rejected: int = Field(..., ge=0)
    consensus_disputed: int = Field(..., ge=0)
    consensus_no_quorum: int = Field(..., ge=0)
    escalations: int = Field(..., ge=0)
    unresolved_disputes: int = Field(..., ge=0)
    conflict_of_interest_pass: bool
    operator_diversity_pass: bool
    consensus_pass: bool
    escalation_pass: bool
    minority_correctness_pass: bool
    accounting_pass: bool
    pool_isolation_pass: bool
    idempotency_pass: bool
    recovery_pass: bool
    determinism_pass: bool
    regression_pass: bool
    deterministic_state_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    total_runtime_ms: int = Field(..., ge=0)
    full_test_count: int = Field(default=0, ge=0)
    failed_test_count: int = Field(default=0, ge=0)
    generated_files: list[str]
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
    def balanced(self) -> "Week8BenchmarkSummary":
        if self.cases_total != self.passed + self.failed:
            raise ValueError("Case totals do not balance")
        if not set(GENERATED_FILENAMES).issubset(self.generated_files):
            raise ValueError("All generated files must be listed")
        return self


def json_safe(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value
