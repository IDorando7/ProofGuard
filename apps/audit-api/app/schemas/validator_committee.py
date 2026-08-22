from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import NodeType
from app.schemas.validator_attestation import ValidatorAssignmentRole


VALIDATOR_COMMITTEE_VERSION = "validator_committee_v1"
VALIDATOR_COMMITTEE_POLICY_VERSION = "validator_committee_policy_v1"
VALIDATOR_ASSIGNMENT_USAGE_VERSION = "validator_assignment_usage_v1"

SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ValidationAssuranceMode(str, Enum):
    STANDARD = "standard"
    HIGH_ASSURANCE = "high_assurance"


class ValidatorCommitteeStatus(str, Enum):
    PLANNED = "planned"
    FINALIZED = "finalized"


class ValidatorCommitteePolicyV1(BaseModel):
    """Trusted, versioned protocol policy; callers never provide committee members."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validator_committee_policy_v1"] = (
        VALIDATOR_COMMITTEE_POLICY_VERSION
    )
    standard_authoritative_size: int = Field(default=5, ge=1, le=100)
    high_assurance_authoritative_size: int = Field(default=7, ge=1, le=100)
    standard_shadow_slots: int = Field(default=1, ge=0, le=100)
    high_assurance_shadow_slots: int = Field(default=1, ge=0, le=100)
    allow_partial_authoritative_committee: Literal[False] = False
    require_unique_operator_per_seat: Literal[True] = True
    shadow_slots_required: Literal[False] = False
    max_concurrent_validator_assignments_per_node: int = Field(
        default=5, ge=1, le=1000
    )
    recent_history_days: int = Field(default=30, ge=1, le=3650)

    def authoritative_size(self, mode: ValidationAssuranceMode) -> int:
        if mode == ValidationAssuranceMode.HIGH_ASSURANCE:
            return self.high_assurance_authoritative_size
        return self.standard_authoritative_size

    def shadow_slots(self, mode: ValidationAssuranceMode) -> int:
        if mode == ValidationAssuranceMode.HIGH_ASSURANCE:
            return self.high_assurance_shadow_slots
        return self.standard_shadow_slots


class ValidatorCandidateRank(BaseModel):
    """Selection-relevant validator snapshot with a neutral Day 2 skill hook."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_node_id: str
    operator_id: str
    node_type: NodeType
    supported_categories: list[FindingCategory]
    validator_skill_score: float | None = Field(default=None, ge=0.0, le=1.0)
    validator_skill_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    current_authoritative_assignments: int = Field(..., ge=0)
    current_shadow_assignments: int = Field(..., ge=0)
    authoritative_assignments_last_30_days: int = Field(..., ge=0)
    shadow_assignments_last_30_days: int = Field(..., ge=0)
    lifetime_authoritative_assignments: int = Field(..., ge=0)
    lifetime_shadow_assignments: int = Field(..., ge=0)
    last_authoritative_assignment_at: datetime | None = None
    last_shadow_assignment_at: datetime | None = None
    operator_current_authoritative_assignments: int = Field(..., ge=0)
    operator_current_shadow_assignments: int = Field(..., ge=0)
    operator_authoritative_assignments_last_30_days: int = Field(..., ge=0)
    operator_shadow_assignments_last_30_days: int = Field(..., ge=0)
    operator_lifetime_authoritative_assignments: int = Field(..., ge=0)
    operator_lifetime_shadow_assignments: int = Field(..., ge=0)
    operator_last_authoritative_assignment_at: datetime | None = None
    operator_last_shadow_assignment_at: datetime | None = None
    relevant_assignment_ids: list[str]
    deterministic_tie_break: str

    @field_validator(
        "validator_node_id",
        "operator_id",
        "deterministic_tie_break",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("supported_categories", mode="before")
    @classmethod
    def canonical_categories(cls, value):
        return sorted(set(value), key=lambda item: getattr(item, "value", item))

    @field_validator("relevant_assignment_ids", mode="before")
    @classmethod
    def canonical_assignments(cls, value):
        return sorted({_identifier(item) for item in value})

    @field_validator(
        "last_authoritative_assignment_at",
        "last_shadow_assignment_at",
        "operator_last_authoritative_assignment_at",
        "operator_last_shadow_assignment_at",
    )
    @classmethod
    def aware_timestamps(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class ValidatorCommitteeSeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_role: ValidatorAssignmentRole
    seat_index: int = Field(..., ge=1, le=100)
    validator_node_id: str
    operator_id: str
    validator_assignment_id: str

    @field_validator("validator_node_id", "operator_id", "validator_assignment_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _identifier(value)


class ValidatorCommittee(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_committee_id: str
    committee_version: Literal["validator_committee_v1"] = VALIDATOR_COMMITTEE_VERSION
    policy_version: Literal["validator_committee_policy_v1"] = (
        VALIDATOR_COMMITTEE_POLICY_VERSION
    )
    policy: ValidatorCommitteePolicyV1
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validation_round: int = Field(default=1, ge=1)
    excluded_operator_ids: list[str] = Field(default_factory=list)
    category: FindingCategory
    assurance_mode: ValidationAssuranceMode
    authoritative_target_size: int = Field(..., ge=1)
    shadow_target_size: int = Field(..., ge=0)
    actual_shadow_size: int = Field(..., ge=0)
    candidate_ranks: list[ValidatorCandidateRank]
    authoritative_seats: list[ValidatorCommitteeSeat]
    shadow_seats: list[ValidatorCommitteeSeat]
    candidate_source_fingerprint: str
    source_fingerprint: str
    status: ValidatorCommitteeStatus
    created_at: datetime
    calculated_at: datetime
    finalized_at: datetime | None = None

    @field_validator(
        "validator_committee_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("excluded_operator_ids", mode="before")
    @classmethod
    def canonical_excluded_operators(cls, value):
        return sorted({_identifier(item) for item in value})

    @field_validator("candidate_source_fingerprint", "source_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Committee fingerprints must be lowercase SHA-256")
        return value

    @field_validator("created_at", "calculated_at", "finalized_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def validate_committee(self) -> "ValidatorCommittee":
        if self.policy.policy_version != self.policy_version:
            raise ValueError("Committee policy snapshot must match policy_version")
        if len(self.authoritative_seats) != self.authoritative_target_size:
            raise ValueError("Authoritative seats must exactly satisfy policy target")
        if len(self.shadow_seats) != self.actual_shadow_size:
            raise ValueError("actual_shadow_size must match shadow seats")
        if self.actual_shadow_size > self.shadow_target_size:
            raise ValueError("Shadow seats cannot exceed policy target")
        for role, seats in (
            (ValidatorAssignmentRole.AUTHORITATIVE, self.authoritative_seats),
            (ValidatorAssignmentRole.SHADOW, self.shadow_seats),
        ):
            if [seat.seat_index for seat in seats] != list(range(1, len(seats) + 1)):
                raise ValueError("Committee seat indexes must be contiguous from one")
            if any(seat.assignment_role != role for seat in seats):
                raise ValueError("Committee seat role is inconsistent")
        seats = self.authoritative_seats + self.shadow_seats
        operators = [seat.operator_id for seat in seats]
        nodes = [seat.validator_node_id for seat in seats]
        assignments = [seat.validator_assignment_id for seat in seats]
        if len(operators) != len(set(operators)):
            raise ValueError("One operator may occupy at most one committee seat")
        if len(nodes) != len(set(nodes)) or len(assignments) != len(set(assignments)):
            raise ValueError("Committee node and assignment identities must be unique")
        if self.status == ValidatorCommitteeStatus.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("Finalized committee requires finalized_at")
        elif self.finalized_at is not None:
            raise ValueError("Planned committee cannot set finalized_at")
        return self


class ValidatorAssignmentUsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    usage_event_id: str
    usage_version: Literal["validator_assignment_usage_v1"] = (
        VALIDATOR_ASSIGNMENT_USAGE_VERSION
    )
    validator_committee_id: str
    validator_assignment_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validation_round: int = Field(default=1, ge=1)
    validator_node_id: str
    operator_id: str
    category: FindingCategory
    role: ValidatorAssignmentRole
    assigned_at: datetime
    source_fingerprint: str

    @field_validator(
        "usage_event_id",
        "validator_committee_id",
        "validator_assignment_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
        "validator_node_id",
        "operator_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("assigned_at")
    @classmethod
    def validate_assigned_at(cls, value: datetime) -> datetime:
        return _aware(value)  # type: ignore[return-value]

    @field_validator("source_fingerprint")
    @classmethod
    def validate_source_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Usage fingerprint must be lowercase SHA-256")
        return value


class ValidatorCommitteeCreateRequest(BaseModel):
    """Public creation is STANDARD-only; assurance is trusted service policy."""

    model_config = ConfigDict(extra="forbid")


class ValidatorCommitteeFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidatorCommitteeFinalizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    committee: ValidatorCommittee
    assignments_created: int = Field(..., ge=0)
    assignments_existing: int = Field(..., ge=0)
    usage_events_created: int = Field(..., ge=0)
    usage_events_existing: int = Field(..., ge=0)


class ValidatorCommitteeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    committees: list[ValidatorCommittee]

    @model_validator(mode="after")
    def validate_total(self) -> "ValidatorCommitteeListResponse":
        if self.total != len(self.committees):
            raise ValueError("Committee total must match records")
        return self


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("Invalid validator committee identifier")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Validator committee timestamps must be timezone-aware")
    return value
