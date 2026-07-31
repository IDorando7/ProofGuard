import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category


SUBNET_REGISTRY_VERSION = "subnet_registry_v0"
SUBNET_MEMBERSHIP_VERSION = "subnet_membership_v0"
SAFE_SUBNET_ID = re.compile(r"^subnet_[a-z0-9]+(?:_[a-z0-9]+)*$")
SAFE_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class SubnetStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class SubnetMemberStatus(str, Enum):
    CANDIDATE = "candidate"
    PROBATION = "probation"
    ACTIVE = "active"
    EXPERT = "expert"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class _SubnetConfiguration(BaseModel):
    category: FindingCategory
    name: str | None = Field(default=None, min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    minimum_category_score: float = Field(default=0.60, ge=0.0, le=1.0)
    minimum_finalized_submissions: int = Field(default=5, ge=0, le=10_000)
    maximum_active_nodes: int = Field(default=20, ge=1, le=10_000)
    exploration_ratio: float = Field(default=0.20, ge=0.0, le=1.0)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_subnet_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubnetCreate(_SubnetConfiguration):
    model_config = ConfigDict(extra="forbid")


class SubnetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    minimum_category_score: float | None = Field(default=None, ge=0.0, le=1.0)
    minimum_finalized_submissions: int | None = Field(default=None, ge=0, le=10_000)
    maximum_active_nodes: int | None = Field(default=None, ge=1, le=10_000)
    exploration_ratio: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubnetStatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: SubnetStatus
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class SubnetRecord(_SubnetConfiguration):
    model_config = ConfigDict(extra="forbid")

    subnet_id: str = Field(..., min_length=1, max_length=128)
    registry_version: Literal["subnet_registry_v0"] = SUBNET_REGISTRY_VERSION
    name: str = Field(..., min_length=3, max_length=100)
    status: SubnetStatus
    status_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    status_updated_at: datetime

    @field_validator("subnet_id")
    @classmethod
    def validate_subnet_id(cls, value: str) -> str:
        if not SAFE_SUBNET_ID.fullmatch(value):
            raise ValueError("Invalid subnet identifier")
        return value

    @field_validator("created_at", "updated_at", "status_updated_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Subnet timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_category_identity(self) -> "SubnetRecord":
        expected_id = f"subnet_{self.category.value}"
        if self.subnet_id != expected_id:
            raise ValueError("Subnet identifier must match its vulnerability category")
        return self


class SubnetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(..., ge=0)
    subnets: list[SubnetRecord]


class SubnetMemberRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subnet_id: str = Field(..., min_length=1, max_length=128)
    node_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    status: SubnetMemberStatus
    category_score: float = Field(..., ge=0.0, le=1.0)
    rank: int | None = Field(default=None, gt=0)
    finalized_submissions: int = Field(..., ge=0)
    accepted_unique_submissions: int = Field(..., ge=0)
    exploration_assignments: int = Field(..., ge=0)
    last_assigned_at: datetime | None = None
    joined_at: datetime
    updated_at: datetime
    status_reason: str | None = Field(default=None, max_length=500)
    membership_version: Literal["subnet_membership_v0"] = SUBNET_MEMBERSHIP_VERSION
    policy_version: str = Field(default="membership_policy_v0", min_length=1, max_length=64)
    category_score_id: str | None = Field(default=None, min_length=1, max_length=256)
    category_score_source_fingerprint: str | None = None
    performance_id: str | None = Field(default=None, min_length=1, max_length=256)
    membership_source_fingerprint: str | None = None
    last_decision_id: str | None = Field(default=None, min_length=1, max_length=256)
    last_evaluated_at: datetime | None = None
    status_updated_at: datetime | None = None
    status_reason_codes: list[str] = Field(default_factory=list)
    administrative_lock: bool = False

    @field_validator("category", mode="before")
    @classmethod
    def normalize_member_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("subnet_id")
    @classmethod
    def validate_subnet_id(cls, value: str) -> str:
        if not SAFE_SUBNET_ID.fullmatch(value):
            raise ValueError("Invalid subnet identifier")
        return value

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not SAFE_NODE_ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid node identifier")
        return value

    @field_validator(
        "last_assigned_at",
        "joined_at",
        "updated_at",
        "last_evaluated_at",
        "status_updated_at",
    )
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Subnet member timestamps must be timezone-aware")
        return value

    @field_validator(
        "category_score_source_fingerprint",
        "membership_source_fingerprint",
    )
    @classmethod
    def validate_optional_fingerprint(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("status_reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Status reason codes must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("Status reason codes must be unique")
        return value

    @model_validator(mode="after")
    def validate_category_identity(self) -> "SubnetMemberRecord":
        expected_id = f"subnet_{self.category.value}"
        if self.subnet_id != expected_id:
            raise ValueError("Member category must match the subnet category")
        return self


class SubnetMemberListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subnet_id: str
    category: FindingCategory
    total: int = Field(..., ge=0)
    members: list[SubnetMemberRecord]
