from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingSeverity


VALIDATION_ROUND_VERSION = "validation_round_v1"
VALIDATION_CONSENSUS_VERSION = "validation_consensus_v1"
VALIDATOR_CONSENSUS_POLICY_VERSION = "validator_consensus_policy_v1"
VALIDATION_DISPUTE_VERSION = "validation_dispute_v1"
VALIDATION_ESCALATION_POLICY_VERSION = "validation_escalation_policy_v1"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ValidationRoundType(str, Enum):
    INITIAL = "initial"
    ESCALATION = "escalation"


class ValidationRoundStatus(str, Enum):
    OPEN = "open"
    EVIDENCE_READY = "evidence_ready"
    FINALIZED = "finalized"


class ValidationConsensusLifecycle(str, Enum):
    CALCULATED = "calculated"
    FINALIZED = "finalized"


class ValidationConsensusOutcome(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE = "unsafe"
    UNSUPPORTED = "unsupported"
    DISPUTED = "disputed"
    NO_QUORUM = "no_quorum"


class ConsensusComponentType(str, Enum):
    VALIDITY = "validity"
    REPRODUCTION = "reproduction"
    ROOT_CAUSE = "root_cause"
    SEVERITY = "severity"
    IMPACT = "impact"


class ConsensusUnresolvedReason(str, Enum):
    NO_QUORUM = "no_quorum"
    NO_SUPERMAJORITY = "no_supermajority"
    NOT_REQUIRED = "not_required"


class ValidationDisputeReason(str, Enum):
    VALIDITY_DISAGREEMENT = "validity_disagreement"
    REPRODUCTION_DISAGREEMENT = "reproduction_disagreement"
    ROOT_CAUSE_DISAGREEMENT = "root_cause_disagreement"
    SEVERITY_DISAGREEMENT = "severity_disagreement"
    IMPACT_DISAGREEMENT = "impact_disagreement"
    NO_QUORUM = "no_quorum"
    ATTESTATION_INCONSISTENCY = "attestation_inconsistency"
    REPRODUCTION_EVIDENCE_INCONSISTENCY = "reproduction_evidence_inconsistency"
    OPERATOR_DIVERSITY_VIOLATION = "operator_diversity_violation"


class ValidationDisputeStatus(str, Enum):
    OPEN = "open"
    ESCALATION_PLANNED = "escalation_planned"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    BLOCKED_INSUFFICIENT_VALIDATORS = "blocked_insufficient_validators"


class ValidatorConsensusPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validator_consensus_policy_v1"] = (
        VALIDATOR_CONSENSUS_POLICY_VERSION
    )
    quorum_missing_tolerance: int = Field(default=1, ge=0, le=10)
    supermajority_numerator: Literal[2] = 2
    supermajority_denominator: Literal[3] = 3
    reproduction_required: Literal[True] = True


class ValidationEscalationPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal["validation_escalation_policy_v1"] = (
        VALIDATION_ESCALATION_POLICY_VERSION
    )
    additional_authoritative_validators: int = Field(default=4, ge=1, le=100)
    max_escalation_rounds: Literal[1] = 1
    exclude_all_prior_validator_operators: Literal[True] = True
    shadow_slots: Literal[0] = 0


def _identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Consensus identifiers must be strings")
    value = value.strip()
    if not SAFE_IDENTIFIER.fullmatch(value) or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("Invalid consensus identifier")
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Consensus timestamps must be timezone-aware")
    return value


def _canonical_ids(value: Any) -> list[str]:
    return sorted({_identifier(item) for item in value})


def _ordered_ids(value: Any) -> list[str]:
    items = [_identifier(item) for item in value]
    if len(items) != len(set(items)):
        raise ValueError("Consensus identifier list cannot contain duplicates")
    return items


class ConsensusValueEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str
    count: int = Field(..., ge=0)
    operator_ids: list[str]
    attestation_ids: list[str]
    reproduction_record_ids: list[str] = Field(default_factory=list)

    @field_validator("operator_ids", "attestation_ids", "reproduction_record_ids", mode="before")
    @classmethod
    def canonical_ids(cls, value):
        return _canonical_ids(value)


class ConsensusComponentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_type: ConsensusComponentType
    authoritative_target_size: int = Field(..., ge=1)
    valid_response_count: int = Field(..., ge=0)
    quorum_required: int = Field(..., ge=1)
    supermajority_required: int = Field(..., ge=1)
    value_counts: dict[str, int]
    value_evidence: list[ConsensusValueEvidence]
    consensus_reached: bool
    consensus_value: str | None = None
    supporting_attestation_ids: list[str] = Field(default_factory=list)
    dissenting_attestation_ids: list[str] = Field(default_factory=list)
    unresolved_reason: ConsensusUnresolvedReason | None = None

    @field_validator("supporting_attestation_ids", "dissenting_attestation_ids", mode="before")
    @classmethod
    def canonical_ids(cls, value):
        return _canonical_ids(value)

    @model_validator(mode="after")
    def validate_result(self) -> "ConsensusComponentResult":
        if sum(self.value_counts.values()) != self.valid_response_count:
            raise ValueError("Component counts must match valid responses")
        if self.consensus_reached != (self.consensus_value is not None):
            raise ValueError("Component consensus flag and value are inconsistent")
        if self.consensus_reached and self.unresolved_reason is not None:
            raise ValueError("Resolved component cannot have unresolved reason")
        if not self.consensus_reached and self.unresolved_reason is None:
            raise ValueError("Unresolved component requires a reason")
        return self


class ValidationRound(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_round_id: str
    round_version: Literal["validation_round_v1"] = VALIDATION_ROUND_VERSION
    project_id: str
    routing_id: str
    finding_cluster_id: str
    round_number: int = Field(..., ge=1)
    round_type: ValidationRoundType
    validator_committee_id: str
    parent_round_id: str | None = None
    trigger_consensus_id: str | None = None
    status: ValidationRoundStatus
    source_fingerprint: str
    created_at: datetime
    finalized_at: datetime | None = None

    @field_validator("validation_round_id", "project_id", "routing_id", "finding_cluster_id", "validator_committee_id", "parent_round_id", "trigger_consensus_id")
    @classmethod
    def ids(cls, value):
        return None if value is None else _identifier(value)

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Round fingerprint must be SHA-256")
        return value

    @field_validator("created_at", "finalized_at")
    @classmethod
    def times(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def lifecycle(self) -> "ValidationRound":
        if self.round_number == 1 and self.round_type != ValidationRoundType.INITIAL:
            raise ValueError("Round one must be initial")
        if self.round_number > 1 and self.round_type != ValidationRoundType.ESCALATION:
            raise ValueError("Later rounds must be escalation rounds")
        if self.status == ValidationRoundStatus.FINALIZED and self.finalized_at is None:
            raise ValueError("Finalized round requires finalized_at")
        if self.status != ValidationRoundStatus.FINALIZED and self.finalized_at is not None:
            raise ValueError("Only finalized rounds may set finalized_at")
        return self


class ValidationConsensus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_consensus_id: str
    consensus_protocol_version: Literal["validation_consensus_v1"] = VALIDATION_CONSENSUS_VERSION
    consensus_policy: ValidatorConsensusPolicyV1
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validation_round_number: int = Field(..., ge=1)
    included_round_ids: list[str]
    included_committee_ids: list[str]
    authoritative_target_size: int = Field(..., ge=1)
    valid_authoritative_attestation_count: int = Field(..., ge=0)
    quorum_required: int = Field(..., ge=1)
    supermajority_required: int = Field(..., ge=1)
    lifecycle_status: ValidationConsensusLifecycle
    consensus_outcome: ValidationConsensusOutcome
    validity_result: ConsensusComponentResult
    reproduction_result: ConsensusComponentResult
    root_cause_result: ConsensusComponentResult
    severity_result: ConsensusComponentResult
    impact_result: ConsensusComponentResult
    final_normalized_severity: FindingSeverity | None = None
    dispute_reason_codes: list[ValidationDisputeReason] = Field(default_factory=list)
    source_fingerprint: str
    calculation_fingerprint: str
    created_at: datetime
    calculated_at: datetime
    finalized_at: datetime | None = None

    @field_validator("validation_consensus_id", "project_id", "routing_id", "finding_cluster_id")
    @classmethod
    def ids(cls, value):
        return _identifier(value)

    @field_validator("included_round_ids", "included_committee_ids", mode="before")
    @classmethod
    def canonical_ids(cls, value):
        return _ordered_ids(value)

    @field_validator("dispute_reason_codes", mode="before")
    @classmethod
    def canonical_reasons(cls, value):
        return sorted({ValidationDisputeReason(item).value for item in value})

    @field_validator("source_fingerprint", "calculation_fingerprint")
    @classmethod
    def fingerprints(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Consensus fingerprints must be SHA-256")
        return value

    @field_validator("created_at", "calculated_at", "finalized_at")
    @classmethod
    def times(cls, value):
        return _aware(value)

    @model_validator(mode="after")
    def lifecycle(self) -> "ValidationConsensus":
        if self.lifecycle_status == ValidationConsensusLifecycle.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("Finalized consensus requires finalized_at")
        elif self.finalized_at is not None:
            raise ValueError("Calculated consensus cannot set finalized_at")
        if self.consensus_outcome == ValidationConsensusOutcome.CONFIRMED:
            if self.final_normalized_severity is None:
                raise ValueError("Confirmed consensus requires final severity")
        elif self.final_normalized_severity is not None:
            raise ValueError("Only confirmed consensus has final severity")
        return self


class ValidationDispute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_dispute_id: str
    dispute_version: Literal["validation_dispute_v1"] = VALIDATION_DISPUTE_VERSION
    escalation_policy: ValidationEscalationPolicyV1 = Field(default_factory=ValidationEscalationPolicyV1)
    project_id: str
    routing_id: str
    finding_cluster_id: str
    triggering_consensus_id: str
    triggering_round_id: str
    reason_codes: list[ValidationDisputeReason]
    status: ValidationDisputeStatus
    escalation_round_id: str | None = None
    escalation_committee_id: str | None = None
    resolution_consensus_id: str | None = None
    source_fingerprint: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    @field_validator("validation_dispute_id", "project_id", "routing_id", "finding_cluster_id", "triggering_consensus_id", "triggering_round_id", "escalation_round_id", "escalation_committee_id", "resolution_consensus_id")
    @classmethod
    def ids(cls, value):
        return None if value is None else _identifier(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def reasons(cls, value):
        return sorted({ValidationDisputeReason(item).value for item in value})

    @field_validator("source_fingerprint")
    @classmethod
    def fingerprint(cls, value):
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Dispute fingerprint must be SHA-256")
        return value

    @field_validator("created_at", "updated_at", "resolved_at")
    @classmethod
    def times(cls, value):
        return _aware(value)


class ConsensusVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    validation_consensus_id: str
    valid: bool
    checks: dict[str, bool]
    errors: list[str]


class ValidationConsensusActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationConsensusListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(..., ge=0)
    consensus: list[ValidationConsensus]


class ValidationDisputeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(..., ge=0)
    disputes: list[ValidationDispute]


class ValidationEscalationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispute: ValidationDispute
    validation_round: ValidationRound | None = None
    validator_committee_id: str | None = None
