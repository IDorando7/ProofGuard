import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory


class NodeType(str, Enum):
    AGENT = "agent"
    VALIDATOR = "validator"
    HYBRID = "hybrid"


class NodeStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    BANNED = "banned"


def normalize_category(category: str | FindingCategory) -> str:
    raw_value = category.value if isinstance(category, FindingCategory) else category
    if not isinstance(raw_value, str):
        raise ValueError("Category must be a string")

    normalized = re.sub(r"[\s-]+", "_", raw_value.strip().lower())
    supported = {item.value for item in FindingCategory}
    if normalized not in supported:
        values = ", ".join(sorted(supported))
        raise ValueError(f"Unsupported vulnerability category '{normalized}'. Supported values: {values}")
    return normalized


def normalize_categories(categories: list[str | FindingCategory]) -> list[str]:
    if not isinstance(categories, list):
        raise ValueError("Supported categories must be a list")

    normalized: list[str] = []
    seen: set[str] = set()
    for category in categories:
        value = normalize_category(category)
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


class NodeStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_submissions: int = Field(default=0, ge=0)
    accepted_submissions: int = Field(default=0, ge=0)
    rejected_submissions: int = Field(default=0, ge=0)
    duplicate_submissions: int = Field(default=0, ge=0)
    out_of_scope_submissions: int = Field(default=0, ge=0)
    unsafe_submissions: int = Field(default=0, ge=0)


class _NodeMetadataValidation(BaseModel):
    display_name: str = Field(..., min_length=3, max_length=80)
    public_key: str | None = Field(default=None, max_length=512)
    supported_categories: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("display_name", mode="before")
    @classmethod
    def validate_display_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("public_key", mode="before")
    @classmethod
    def validate_public_key(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("Public key must not be empty")
        return value

    @field_validator("supported_categories", mode="before")
    @classmethod
    def validate_supported_categories(cls, value: Any) -> list[str]:
        return normalize_categories(value)


class NodeCreate(_NodeMetadataValidation):
    model_config = ConfigDict(extra="forbid")

    node_type: NodeType
    operator_id: str = Field(..., min_length=1, max_length=128)

    @field_validator("operator_id", mode="before")
    @classmethod
    def validate_operator_id(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_categories_for_node_type(self) -> "NodeCreate":
        if self.node_type in {NodeType.AGENT, NodeType.HYBRID} and not self.supported_categories:
            raise ValueError("Agent and hybrid nodes must declare at least one supported category")
        return self


class NodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=3, max_length=80)
    public_key: str | None = Field(default=None, max_length=512)
    supported_categories: list[str] | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("display_name", mode="before")
    @classmethod
    def validate_display_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("public_key", mode="before")
    @classmethod
    def validate_public_key(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            raise ValueError("Public key must not be empty")
        return value

    @field_validator("supported_categories", mode="before")
    @classmethod
    def validate_supported_categories(cls, value: Any) -> list[str] | None:
        return None if value is None else normalize_categories(value)


class NodeStatusChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: NodeStatus
    reason: str = Field(..., min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class NodeRecord(_NodeMetadataValidation):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1)
    node_type: NodeType
    operator_id: str = Field(..., min_length=1, max_length=128)
    status: NodeStatus
    reputation_score: float = Field(..., ge=0.0, le=1.0)
    statistics: NodeStatistics
    status_reason: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    status_updated_at: datetime

    @model_validator(mode="after")
    def validate_record_categories(self) -> "NodeRecord":
        if self.node_type in {NodeType.AGENT, NodeType.HYBRID} and not self.supported_categories:
            raise ValueError("Agent and hybrid nodes must declare at least one supported category")
        return self


class NodeListResponse(BaseModel):
    total: int = Field(..., ge=0)
    nodes: list[NodeRecord]
