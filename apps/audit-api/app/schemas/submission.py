import json
import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.node import NodeType, normalize_category


MAX_METADATA_BYTES = 4096
_SENSITIVE_METADATA_TOKENS = {
    "api_key",
    "attack_path",
    "authentication_token",
    "category",
    "contracts",
    "finding",
    "finding_content",
    "functions",
    "impact",
    "mnemonic",
    "poc",
    "poc_code",
    "poc_content",
    "private_key",
    "recommended_fix",
    "root_cause",
    "secret",
    "seed",
    "seed_phrase",
    "token",
}


class SubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    REPRODUCTION_PENDING = "reproduction_pending"
    VALIDATION_PENDING = "validation_pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEEDS_REVIEW = "needs_review"
    UNSAFE = "unsafe"
    UNSUPPORTED = "unsupported"
    REWARD_PENDING = "reward_pending"
    REWARDED = "rewarded"
    PENALIZED = "penalized"


class SubmissionRewardStatus(str, Enum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    REWARDED = "rewarded"
    PENALIZED = "penalized"


class _SubmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    agent_name: str | None = Field(default=None, max_length=128)
    agent_version: str | None = Field(default=None, max_length=64)
    routing_id: str | None = Field(default=None, min_length=1, max_length=256)
    routing_assignment_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "finding_id",
        "node_id",
        "routing_id",
        "routing_assignment_id",
        mode="before",
    )
    @classmethod
    def trim_ids(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("agent_name", "agent_version", mode="before")
    @classmethod
    def trim_optional_agent_fields(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata_keys(value)
        try:
            serialized = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Metadata must be JSON serializable") from exc
        if len(serialized) > MAX_METADATA_BYTES:
            raise ValueError(f"Metadata must not exceed {MAX_METADATA_BYTES} UTF-8 JSON bytes")
        return value

    @model_validator(mode="after")
    def validate_routing_linkage(self) -> "_SubmissionInput":
        if (self.routing_id is None) != (self.routing_assignment_id is None):
            raise ValueError(
                "routing_id and routing_assignment_id must be provided together"
            )
        return self


class SubmissionRequest(_SubmissionInput):
    """Public route body; project_id is always taken from the URL."""


class SubmissionCreate(_SubmissionInput):
    project_id: str = Field(..., min_length=1)

    @field_validator("project_id", mode="before")
    @classmethod
    def trim_project_id(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubmissionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    finding_id: str = Field(..., min_length=1)
    node_id: str = Field(..., min_length=1)
    node_type: str
    agent_name: str | None = Field(default=None, max_length=128)
    agent_version: str | None = Field(default=None, max_length=64)
    category: str
    finding_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    status: SubmissionStatus
    reproduction_id: str | None = Field(default=None, min_length=1)
    validation_id: str | None = Field(default=None, min_length=1)
    routing_id: str | None = Field(default=None, min_length=1, max_length=256)
    routing_assignment_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    reward_status: SubmissionRewardStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime
    created_at: datetime
    updated_at: datetime
    status_updated_at: datetime
    status_reason: str | None = Field(default=None, max_length=500)

    @field_validator(
        "submission_id",
        "project_id",
        "finding_id",
        "node_id",
        "routing_id",
        "routing_assignment_id",
        mode="before",
    )
    @classmethod
    def trim_required_ids(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("node_type", mode="before")
    @classmethod
    def validate_node_type(cls, value: Any) -> str:
        raw_value = value.value if isinstance(value, NodeType) else value
        try:
            return NodeType(raw_value).value
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid node type") from exc

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _SubmissionInput.validate_metadata(value)

    @field_validator("submitted_at", "created_at", "updated_at", "status_updated_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Submission timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_routing_linkage(self) -> "SubmissionRecord":
        if (self.routing_id is None) != (self.routing_assignment_id is None):
            raise ValueError(
                "routing_id and routing_assignment_id must be provided together"
            )
        return self


class SubmissionStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubmissionStatus
    reason: str = Field(..., min_length=1, max_length=500)
    reproduction_id: str | None = Field(default=None, min_length=1)
    validation_id: str | None = Field(default=None, min_length=1)
    reward_status: SubmissionRewardStatus | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubmissionListResponse(BaseModel):
    total: int = Field(..., ge=0)
    submissions: list[SubmissionRecord]


def _validate_metadata_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            key_parts = set(normalized_key.split("_"))
            if normalized_key in _SENSITIVE_METADATA_TOKENS or key_parts.intersection(
                {"mnemonic", "poc", "secret", "seed", "token"}
            ):
                raise ValueError(f"Metadata field '{key}' is not allowed")
            _validate_metadata_keys(nested_value)
    elif isinstance(value, list):
        for item in value:
            _validate_metadata_keys(item)
    elif isinstance(value, str) and (
        value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise ValueError("Metadata must not contain absolute host paths")
