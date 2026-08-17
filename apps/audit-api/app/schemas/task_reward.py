from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.reward import RewardDomain, RewardUnit
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM


TASK_REWARD_BUDGET_VERSION = "task_reward_budget_v1"
TASK_REWARD_POLICY_VERSION = "task_reward_v1"
TASK_REWARD_CONFIGURATION_VERSION = "task_reward_config_v1"
SAFE_TASK_REWARD_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$"
)
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class TaskRewardBudgetStatus(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"


class TaskRewardBudgetProcessingStatus(str, Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


def _reject_binary_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("Task reward values must be supplied as decimal strings")
    return value


def _require_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if (
        not SAFE_TASK_REWARD_IDENTIFIER.fullmatch(cleaned)
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise ValueError(f"Invalid {label} identifier")
    return cleaned


def _require_points_quantum(value: Decimal, label: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if value != value.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError(f"{label} must use at most six decimal places")
    return value


class TaskRewardPoolConfig(BaseModel):
    """Centralized client-task budget split; never silently normalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    miner_share: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    validator_share: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    protocol_share: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)

    @field_validator(
        "miner_share",
        "validator_share",
        "protocol_share",
        mode="before",
    )
    @classmethod
    def require_decimal_input(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @model_validator(mode="after")
    def validate_exact_total(self) -> "TaskRewardPoolConfig":
        total = self.miner_share + self.validator_share + self.protocol_share
        if total != Decimal("1"):
            raise ValueError("Task reward pool shares must sum exactly to 1")
        return self


class TaskRewardPoolSplit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_budget_points: Decimal = Field(..., gt=0, le=MAX_PROTOCOL_POINTS)
    miner_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)
    validator_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)
    protocol_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)

    @field_validator(
        "total_budget_points",
        "miner_pool_points",
        "validator_pool_points",
        "protocol_pool_points",
    )
    @classmethod
    def validate_point_precision(cls, value: Decimal, info) -> Decimal:
        return _require_points_quantum(value, info.field_name)

    @model_validator(mode="after")
    def validate_conservation(self) -> "TaskRewardPoolSplit":
        if (
            self.miner_pool_points
            + self.validator_pool_points
            + self.protocol_pool_points
            != self.total_budget_points
        ):
            raise ValueError("Task reward pool split must conserve the total budget")
        return self


class TaskRewardBudgetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing_id: str = Field(..., min_length=1, max_length=256)
    total_budget_points: Decimal = Field(..., gt=0, le=MAX_PROTOCOL_POINTS)
    pool_split: TaskRewardPoolConfig | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("routing_id")
    @classmethod
    def validate_routing_id(cls, value: str) -> str:
        return _require_identifier(value, "routing")

    @field_validator("total_budget_points", mode="before")
    @classmethod
    def require_decimal_budget(cls, value: Any) -> Any:
        return _reject_binary_float(value)

    @field_validator("total_budget_points")
    @classmethod
    def validate_budget_precision(cls, value: Decimal) -> Decimal:
        return _require_points_quantum(value, "total_budget_points")

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class TaskRewardBudgetFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRewardBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_reward_budget_id: str
    budget_version: Literal["task_reward_budget_v1"] = TASK_REWARD_BUDGET_VERSION
    policy_version: Literal["task_reward_v1"] = TASK_REWARD_POLICY_VERSION
    reward_domain: Literal[RewardDomain.CLIENT_TASK] = RewardDomain.CLIENT_TASK
    reward_unit: Literal[RewardUnit.PROTOCOL_POINTS] = RewardUnit.PROTOCOL_POINTS
    project_id: str
    routing_id: str
    routing_source_fingerprint: str
    total_budget_points: Decimal = Field(..., gt=0, le=MAX_PROTOCOL_POINTS)
    miner_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)
    validator_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)
    protocol_pool_points: Decimal = Field(..., ge=0, le=MAX_PROTOCOL_POINTS)
    miner_share: Decimal = Field(..., ge=0, le=1)
    validator_share: Decimal = Field(..., ge=0, le=1)
    protocol_share: Decimal = Field(..., ge=0, le=1)
    status: TaskRewardBudgetStatus
    configuration_version: str = Field(..., min_length=1, max_length=128)
    request_fingerprint: str
    source_fingerprint: str
    description: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None = None

    @field_validator(
        "task_reward_budget_id",
        "project_id",
        "routing_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str, info) -> str:
        return _require_identifier(value, info.field_name)

    @field_validator(
        "routing_source_fingerprint",
        "request_fingerprint",
        "source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator(
        "total_budget_points",
        "miner_pool_points",
        "validator_pool_points",
        "protocol_pool_points",
    )
    @classmethod
    def validate_point_precision(cls, value: Decimal, info) -> Decimal:
        return _require_points_quantum(value, info.field_name)

    @field_validator("created_at", "updated_at", "finalized_at")
    @classmethod
    def require_aware_timestamp(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Task reward timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_budget_invariants(self) -> "TaskRewardBudget":
        TaskRewardPoolConfig(
            miner_share=self.miner_share,
            validator_share=self.validator_share,
            protocol_share=self.protocol_share,
        )
        TaskRewardPoolSplit(
            total_budget_points=self.total_budget_points,
            miner_pool_points=self.miner_pool_points,
            validator_pool_points=self.validator_pool_points,
            protocol_pool_points=self.protocol_pool_points,
        )
        if self.status == TaskRewardBudgetStatus.FINALIZED:
            if self.finalized_at is None:
                raise ValueError("Finalized task reward budgets require finalized_at")
        elif self.finalized_at is not None:
            raise ValueError("Draft task reward budgets cannot have finalized_at")
        return self


class TaskRewardBudgetCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskRewardBudgetProcessingStatus
    budget: TaskRewardBudget
    message: str = Field(..., min_length=1)


class TaskRewardBudgetFinalizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskRewardBudgetProcessingStatus
    budget: TaskRewardBudget
    message: str = Field(..., min_length=1)
