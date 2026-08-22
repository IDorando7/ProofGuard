from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory, FindingSeverity
from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus


VALIDATOR_ASSIGNMENT_VERSION = "validator_assignment_v1"
VALIDATOR_REPRODUCTION_VERSION = "validator_reproduction_v1"
VALIDATION_ATTESTATION_VERSION = "validation_attestation_v1"

SAFE_PROTOCOL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ValidatorAssignmentRole(str, Enum):
    AUTHORITATIVE = "authoritative"
    SHADOW = "shadow"


class ValidatorAssignmentStatus(str, Enum):
    ASSIGNED = "assigned"
    REPRODUCTION_RECORDED = "reproduction_recorded"
    ATTESTED = "attested"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ValidatorReproductionMode(str, Enum):
    SUBMITTED_POC = "submitted_poc"
    INDEPENDENT_REPRODUCTION = "independent_reproduction"


class RootCauseDecision(str, Enum):
    CONFIRMED = "confirmed"
    MISMATCH = "mismatch"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ImpactDecision(str, Enum):
    VALIDATED = "validated"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class AttestationReasonCode(str, Enum):
    REPRODUCTION_CONFIRMED = "reproduction_confirmed"
    REPRODUCTION_FAILED = "reproduction_failed"
    ROOT_CAUSE_CONFIRMED = "root_cause_confirmed"
    ROOT_CAUSE_MISMATCH = "root_cause_mismatch"
    IMPACT_CONFIRMED = "impact_confirmed"
    IMPACT_UNSUBSTANTIATED = "impact_unsubstantiated"
    SCOPE_CONFIRMED = "scope_confirmed"
    SCOPE_MISMATCH = "scope_mismatch"
    SEVERITY_ADJUSTMENT_REQUIRED = "severity_adjustment_required"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSAFE_REPRODUCTION = "unsafe_reproduction"
    UNSUPPORTED_ENVIRONMENT = "unsupported_environment"
    REPRODUCTION_TIMEOUT = "reproduction_timeout"
    SANDBOX_ERROR = "sandbox_error"


FINAL_REPRODUCTION_STATUSES = {
    ReproductionStatus.REPRODUCED,
    ReproductionStatus.FAILED,
    ReproductionStatus.ERROR,
    ReproductionStatus.TIMEOUT,
    ReproductionStatus.UNSUPPORTED,
    ReproductionStatus.REJECTED_UNSAFE,
    ReproductionStatus.SANDBOX_ERROR,
}

ATTESTATION_VALIDITY_STATUSES = {
    ValidationStatus.ACCEPTED,
    ValidationStatus.REJECTED,
    ValidationStatus.OUT_OF_SCOPE,
    ValidationStatus.INSUFFICIENT_EVIDENCE,
    ValidationStatus.UNSAFE_POC,
    ValidationStatus.UNSUPPORTED,
}


def _validate_identifier(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Validator protocol identifiers must be strings")
    cleaned = value.strip()
    if (
        not SAFE_PROTOCOL_IDENTIFIER.fullmatch(cleaned)
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise ValueError("Invalid validator protocol identifier")
    return cleaned


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Validator protocol timestamps must be timezone-aware")
    return value


def _canonical_identifiers(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Evidence references must be a list")
    return sorted({_validate_identifier(item) for item in value})


def _canonical_reason_codes(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Reason codes must be a list")
    normalized = [
        item.value if isinstance(item, AttestationReasonCode) else item
        for item in value
    ]
    validated = [AttestationReasonCode(item).value for item in normalized]
    return sorted(set(validated))


class ValidatorAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_assignment_id: str
    assignment_version: Literal["validator_assignment_v1"] = VALIDATOR_ASSIGNMENT_VERSION
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validator_committee_id: str | None = None
    validation_round: int = Field(default=1, ge=1)
    validator_node_id: str
    validator_operator_id: str
    category: FindingCategory
    assignment_role: ValidatorAssignmentRole
    seat_index: int | None = Field(default=None, ge=1, le=100)
    status: ValidatorAssignmentStatus
    source_fingerprint: str
    assigned_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    attested_at: datetime | None = None

    @field_validator(
        "validator_assignment_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
        "validator_committee_id",
        "validator_node_id",
        "validator_operator_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Assignment fingerprint must be lowercase SHA-256")
        return value

    @field_validator("assigned_at", "updated_at", "expires_at", "attested_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ValidatorAssignment":
        if (self.validator_committee_id is None) != (self.seat_index is None):
            raise ValueError("Committee assignment requires committee ID and seat index")
        if self.updated_at < self.assigned_at:
            raise ValueError("Assignment updated_at cannot precede assigned_at")
        if self.expires_at is not None and self.expires_at <= self.assigned_at:
            raise ValueError("Assignment expires_at must follow assigned_at")
        if self.status == ValidatorAssignmentStatus.ATTESTED:
            if self.attested_at is None:
                raise ValueError("Attested assignment requires attested_at")
        elif self.attested_at is not None:
            raise ValueError("Only attested assignments may set attested_at")
        return self


class ValidatorReproductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_node_id: str
    reproduction_mode: ValidatorReproductionMode
    underlying_reproduction_result_id: str
    poc_artifact_fingerprint: str | None = None
    environment_fingerprint: str | None = None

    @field_validator("validator_node_id", "underlying_reproduction_result_id")
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("poc_artifact_fingerprint", "environment_fingerprint")
    @classmethod
    def validate_optional_fingerprints(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_HEX.fullmatch(value):
            raise ValueError("Reproduction fingerprints must be lowercase SHA-256")
        return value


class ValidatorReproductionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_reproduction_id: str
    reproduction_protocol_version: Literal["validator_reproduction_v1"] = (
        VALIDATOR_REPRODUCTION_VERSION
    )
    validator_assignment_id: str
    validator_committee_id: str | None = None
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validator_node_id: str
    validator_operator_id: str
    assignment_role: ValidatorAssignmentRole | None = None
    reproduction_mode: ValidatorReproductionMode
    underlying_reproduction_request_id: str | None = None
    underlying_reproduction_request_fingerprint: str | None = None
    underlying_reproduction_result_id: str
    underlying_reproduction_result_storage_ref: str | None = None
    underlying_reproduction_result_fingerprint: str
    reproduction_status: ReproductionStatus
    poc_artifact_fingerprint: str | None = None
    environment_fingerprint: str | None = None
    source_revision: str | None = None
    source_snapshot_fingerprint: str | None = None
    source_fingerprint: str
    created_at: datetime
    finalized_at: datetime

    @field_validator(
        "validator_reproduction_id",
        "validator_assignment_id",
        "validator_committee_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
        "validator_node_id",
        "validator_operator_id",
        "underlying_reproduction_request_id",
        "underlying_reproduction_result_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator(
        "underlying_reproduction_result_fingerprint",
        "underlying_reproduction_request_fingerprint",
        "poc_artifact_fingerprint",
        "environment_fingerprint",
        "source_fingerprint",
        "source_snapshot_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_HEX.fullmatch(value):
            raise ValueError("Reproduction fingerprint must be lowercase SHA-256")
        return value

    @field_validator("underlying_reproduction_result_storage_ref")
    @classmethod
    def validate_storage_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("/")
        if (
            len(parts) != 4
            or parts[0:2] != ["reproductions", "validator"]
            or parts[3] != "reproduction.json"
            or not SAFE_PROTOCOL_IDENTIFIER.fullmatch(parts[2])
            or "\\" in value
        ):
            raise ValueError("Invalid attributed reproduction storage reference")
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str | None) -> str | None:
        if value is not None and (
            not SAFE_PROTOCOL_IDENTIFIER.fullmatch(value)
            or value in {".", ".."}
        ):
            raise ValueError("Invalid source revision")
        return value

    @field_validator("created_at", "finalized_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_final_record(self) -> "ValidatorReproductionRecord":
        if self.reproduction_status not in FINAL_REPRODUCTION_STATUSES:
            raise ValueError("Validator reproduction requires a final Week 3 result")
        if self.finalized_at < self.created_at:
            raise ValueError("Reproduction finalized_at cannot precede created_at")
        day3_fields = (
            self.assignment_role,
            self.underlying_reproduction_request_id,
            self.underlying_reproduction_request_fingerprint,
            self.underlying_reproduction_result_storage_ref,
            self.environment_fingerprint,
            self.source_revision,
            self.source_snapshot_fingerprint,
        )
        if self.validator_committee_id is not None and any(
            value is None for value in day3_fields
        ):
            raise ValueError(
                "Committee reproduction requires complete Day 3 provenance"
            )
        return self


class ValidationAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_node_id: str
    validator_reproduction_id: str
    validity_decision: ValidationStatus
    root_cause_decision: RootCauseDecision
    normalized_severity: FindingSeverity | None = None
    impact_decision: ImpactDecision
    reason_codes: list[AttestationReasonCode] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)

    @field_validator("validator_node_id", "validator_reproduction_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def canonicalize_reason_codes(cls, value: Any) -> list[str]:
        return _canonical_reason_codes(value)

    @field_validator("evidence_references", mode="before")
    @classmethod
    def canonicalize_evidence_references(cls, value: Any) -> list[str]:
        return _canonical_identifiers(value)

    @model_validator(mode="after")
    def validate_decisions(self) -> "ValidationAttestationRequest":
        if self.validity_decision not in ATTESTATION_VALIDITY_STATUSES:
            raise ValueError("Unsupported validator attestation validity decision")
        if self.validity_decision == ValidationStatus.ACCEPTED:
            if self.normalized_severity is None:
                raise ValueError("Accepted attestations require a severity opinion")
        elif self.validity_decision in {
            ValidationStatus.REJECTED,
            ValidationStatus.OUT_OF_SCOPE,
            ValidationStatus.UNSAFE_POC,
            ValidationStatus.UNSUPPORTED,
        } and self.normalized_severity is not None:
            raise ValueError("This validity decision requires undetermined severity")
        return self


class ValidationAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str
    attestation_protocol_version: Literal["validation_attestation_v1"] = (
        VALIDATION_ATTESTATION_VERSION
    )
    validator_assignment_id: str
    project_id: str
    routing_id: str
    finding_cluster_id: str
    validator_node_id: str
    validator_operator_id: str
    assignment_role: ValidatorAssignmentRole
    validity_decision: ValidationStatus
    root_cause_decision: RootCauseDecision
    reproduction_decision: ReproductionStatus
    normalized_severity: FindingSeverity | None = None
    impact_decision: ImpactDecision
    reason_codes: list[AttestationReasonCode]
    evidence_references: list[str]
    validator_reproduction_id: str
    source_fingerprint: str
    status: Literal["finalized"] = "finalized"
    created_at: datetime
    finalized_at: datetime

    @field_validator(
        "attestation_id",
        "validator_assignment_id",
        "project_id",
        "routing_id",
        "finding_cluster_id",
        "validator_node_id",
        "validator_operator_id",
        "validator_reproduction_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def canonicalize_reason_codes(cls, value: Any) -> list[str]:
        return _canonical_reason_codes(value)

    @field_validator("evidence_references", mode="before")
    @classmethod
    def canonicalize_evidence_references(cls, value: Any) -> list[str]:
        return _canonical_identifiers(value)

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Attestation fingerprint must be lowercase SHA-256")
        return value

    @field_validator("created_at", "finalized_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _require_aware(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_final_attestation(self) -> "ValidationAttestation":
        if self.validity_decision not in ATTESTATION_VALIDITY_STATUSES:
            raise ValueError("Unsupported validator attestation validity decision")
        if self.reproduction_decision not in FINAL_REPRODUCTION_STATUSES:
            raise ValueError("Attestation requires a final reproduction decision")
        if self.validity_decision == ValidationStatus.ACCEPTED:
            if self.normalized_severity is None:
                raise ValueError("Accepted attestations require a severity opinion")
        elif self.validity_decision in {
            ValidationStatus.REJECTED,
            ValidationStatus.OUT_OF_SCOPE,
            ValidationStatus.UNSAFE_POC,
            ValidationStatus.UNSUPPORTED,
        } and self.normalized_severity is not None:
            raise ValueError("This validity decision requires undetermined severity")
        if self.finalized_at < self.created_at:
            raise ValueError("Attestation finalized_at cannot precede created_at")
        return self


class ValidatorAssignmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    assignments: list[ValidatorAssignment]

    @model_validator(mode="after")
    def validate_total(self) -> "ValidatorAssignmentListResponse":
        if self.total != len(self.assignments):
            raise ValueError("Assignment total must match records")
        return self


class ValidationAttestationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    attestations: list[ValidationAttestation]

    @model_validator(mode="after")
    def validate_total(self) -> "ValidationAttestationListResponse":
        if self.total != len(self.attestations):
            raise ValueError("Attestation total must match records")
        return self
