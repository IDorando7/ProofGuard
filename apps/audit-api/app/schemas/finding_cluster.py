from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory, FindingSeverity
from app.schemas.node import normalize_category
from app.schemas.validation import ValidationStatus


FINDING_CLUSTER_VERSION = "finding_cluster_v1"
FINDING_CLUSTER_POLICY_VERSION = "root_cause_cluster_v1"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
CLUSTER_ID = re.compile(r"^finding_cluster_[0-9a-f]{64}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class FindingClusterStatus(str, Enum):
    OPEN = "open"
    FINALIZED = "finalized"


class FindingClusterMemberRelation(str, Enum):
    CANONICAL = "canonical"
    INDEPENDENT_DUPLICATE = "independent_duplicate"


class FindingClusterValidationAuthority(str, Enum):
    LEGACY_BACKEND = "legacy_backend"
    VALIDATOR_CONSENSUS = "validator_consensus"


class FindingClusterMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str = Field(..., min_length=1, max_length=256)
    finding_id: str = Field(..., min_length=1, max_length=256)
    node_id: str = Field(..., min_length=1, max_length=256)
    operator_id: str = Field(..., min_length=1, max_length=128)
    validation_id: str = Field(..., min_length=1, max_length=256)
    reproduction_id: str | None = Field(default=None, min_length=1, max_length=256)
    submitted_at: datetime
    relation: FindingClusterMemberRelation
    source_fingerprint: str

    @field_validator(
        "submission_id",
        "finding_id",
        "node_id",
        "operator_id",
        "validation_id",
        "reproduction_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if (
            not SAFE_IDENTIFIER.fullmatch(cleaned)
            or cleaned in {".", ".."}
            or "/" in cleaned
            or "\\" in cleaned
        ):
            raise ValueError("Invalid cluster member identifier")
        return cleaned

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Member fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("submitted_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Cluster member timestamp must be timezone-aware")
        return value


class FindingCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_cluster_id: str
    cluster_version: Literal["finding_cluster_v1"] = FINDING_CLUSTER_VERSION
    policy_version: Literal["root_cause_cluster_v1"] = FINDING_CLUSTER_POLICY_VERSION
    project_id: str = Field(..., min_length=1, max_length=256)
    routing_id: str = Field(..., min_length=1, max_length=256)
    category: FindingCategory
    canonical_finding_id: str = Field(..., min_length=1, max_length=256)
    canonical_submission_id: str = Field(..., min_length=1, max_length=256)
    final_validation_status: Literal[ValidationStatus.ACCEPTED] = ValidationStatus.ACCEPTED
    final_severity: FindingSeverity
    validation_authority: FindingClusterValidationAuthority = (
        FindingClusterValidationAuthority.LEGACY_BACKEND
    )
    final_validation_consensus_id: str | None = None
    validator_consensus_outcome: str | None = None
    validator_consensus_severity: FindingSeverity | None = None
    root_cause_key: str = Field(..., min_length=1, max_length=1024)
    root_cause_fingerprint: str
    members: list[FindingClusterMember] = Field(..., min_length=1)
    report_count: int = Field(..., ge=1)
    distinct_operator_count: int = Field(..., ge=1)
    status: FindingClusterStatus
    source_fingerprint: str
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None = None

    @field_validator("finding_cluster_id")
    @classmethod
    def validate_cluster_id(cls, value: str) -> str:
        if not CLUSTER_ID.fullmatch(value):
            raise ValueError("Invalid finding cluster identifier")
        return value

    @field_validator(
        "project_id",
        "routing_id",
        "canonical_finding_id",
        "canonical_submission_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        cleaned = value.strip()
        if (
            not SAFE_IDENTIFIER.fullmatch(cleaned)
            or cleaned in {".", ".."}
            or "/" in cleaned
            or "\\" in cleaned
        ):
            raise ValueError("Invalid finding cluster identifier field")
        return cleaned

    @field_validator("final_validation_consensus_id")
    @classmethod
    def validate_consensus_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not SAFE_IDENTIFIER.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid final validation consensus identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_cluster_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("root_cause_fingerprint", "source_fingerprint")
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("created_at", "updated_at", "finalized_at")
    @classmethod
    def require_aware_timestamp(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Finding cluster timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_cluster_invariants(self) -> "FindingCluster":
        ordered = sorted(
            self.members,
            key=lambda member: (member.submitted_at, member.submission_id),
        )
        if self.members != ordered:
            raise ValueError("Cluster members must use deterministic submission order")
        submission_ids = [member.submission_id for member in self.members]
        finding_ids = [member.finding_id for member in self.members]
        if len(submission_ids) != len(set(submission_ids)):
            raise ValueError("Cluster member submission identifiers must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("Cluster member finding identifiers must be unique")
        if self.report_count != len(self.members):
            raise ValueError("report_count must match cluster member count")
        if self.distinct_operator_count != len(
            {member.operator_id for member in self.members}
        ):
            raise ValueError("distinct_operator_count must count unique operators")
        canonical = [
            member
            for member in self.members
            if member.relation == FindingClusterMemberRelation.CANONICAL
        ]
        if len(canonical) != 1:
            raise ValueError("A finding cluster requires exactly one canonical member")
        if (
            canonical[0].finding_id != self.canonical_finding_id
            or canonical[0].submission_id != self.canonical_submission_id
        ):
            raise ValueError("Canonical identifiers must reference the canonical member")
        if self.status == FindingClusterStatus.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("Finalized clusters require finalized_at")
        elif self.finalized_at is not None:
            raise ValueError("Open clusters cannot have finalized_at")
        if self.validation_authority == FindingClusterValidationAuthority.VALIDATOR_CONSENSUS:
            if self.final_validation_consensus_id is None or self.validator_consensus_outcome is None:
                raise ValueError("Validator consensus authority requires its immutable reference")
        elif any(
            value is not None
            for value in (
                self.final_validation_consensus_id,
                self.validator_consensus_outcome,
                self.validator_consensus_severity,
            )
        ):
            raise ValueError("Legacy validation cannot claim validator consensus fields")
        return self


class FindingClusterRebuildRequest(BaseModel):
    """Protocol-derived rebuild; client-controlled membership is forbidden."""

    model_config = ConfigDict(extra="forbid")


class FindingClusterFinalizeRequest(BaseModel):
    """Protocol lifecycle operation; no cluster fields are client-controlled."""

    model_config = ConfigDict(extra="forbid")


class FindingClusterFinalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    finalized_clusters: int = Field(..., ge=0)
    already_finalized_clusters: int = Field(..., ge=0)
    clusters: list[FindingCluster]

    @model_validator(mode="after")
    def validate_counts(self) -> "FindingClusterFinalizationResult":
        if self.finalized_clusters + self.already_finalized_clusters != len(self.clusters):
            raise ValueError("FindingCluster finalization counts must match clusters")
        if any(cluster.status != FindingClusterStatus.FINALIZED for cluster in self.clusters):
            raise ValueError("FindingCluster finalization must return finalized clusters")
        return self


class FindingClusterRebuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    total_clusters: int = Field(..., ge=0)
    created_clusters: int = Field(..., ge=0)
    updated_clusters: int = Field(..., ge=0)
    unchanged_clusters: int = Field(..., ge=0)
    clusters: list[FindingCluster]

    @model_validator(mode="after")
    def validate_totals(self) -> "FindingClusterRebuildResult":
        if self.total_clusters != len(self.clusters):
            raise ValueError("Cluster total must match returned clusters")
        if (
            self.created_clusters
            + self.updated_clusters
            + self.unchanged_clusters
            != self.total_clusters
        ):
            raise ValueError("Cluster rebuild counters must match total")
        return self


class FindingClusterListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    routing_id: str
    total: int = Field(..., ge=0)
    clusters: list[FindingCluster]


class SubmissionFindingClusterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    finding_cluster: FindingCluster
