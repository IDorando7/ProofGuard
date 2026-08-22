from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.routing import RoutingStatus
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS, PROTOCOL_POINTS_QUANTUM
from app.schemas.reward import RewardDomain
from app.schemas.task_reward import (
    TASK_REWARD_BUDGET_VERSION,
    TASK_REWARD_POLICY_VERSION,
    TaskRewardBudget,
    TaskRewardBudgetCreateRequest,
    TaskRewardBudgetStatus,
    TaskRewardPoolConfig,
    TaskRewardPoolSplit,
)
from app.services.subnet_reward_allocation_service import (
    WeightedRewardItem,
    allocate_proportional_rewards,
)
from app.services.subnet_router_service import (
    InvalidRoutingIdentifierError,
    RoutingStorageError,
    load_routing_record_by_id,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


TASK_REWARD_BUDGET_FILENAME = "budget.json"
TASK_REWARD_ROUTING_DIRECTORY = "routing"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class TaskRewardBudgetServiceError(ValueError):
    """Base error for client-funded task reward budget operations."""


class TaskRewardProjectNotFoundError(TaskRewardBudgetServiceError):
    pass


class TaskRewardRoutingNotFoundError(TaskRewardBudgetServiceError):
    pass


class TaskRewardRoutingNotFinalizedError(TaskRewardBudgetServiceError):
    pass


class TaskRewardProjectMismatchError(TaskRewardBudgetServiceError):
    pass


class TaskRewardBudgetNotFoundError(TaskRewardBudgetServiceError):
    pass


class TaskRewardBudgetConflictError(TaskRewardBudgetServiceError):
    pass


class TaskRewardBudgetAlreadyFinalizedError(TaskRewardBudgetServiceError):
    pass


class TaskRewardBudgetConservationError(TaskRewardBudgetServiceError):
    pass


class InvalidTaskRewardIdentifierError(TaskRewardBudgetServiceError):
    pass


class TaskRewardBudgetStorageError(TaskRewardBudgetServiceError):
    pass


def get_task_rewards_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "task-rewards"


def get_task_reward_budgets_root(protocol_data_root: Path) -> Path:
    return get_task_rewards_root(protocol_data_root) / "budgets"


def get_task_reward_budget_path(
    protocol_data_root: Path,
    routing_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    root = get_task_reward_budgets_root(protocol_data_root) / TASK_REWARD_ROUTING_DIRECTORY
    routing_root = _safe_child(root, routing_id)
    return routing_root / TASK_REWARD_BUDGET_FILENAME


def split_task_reward_budget(
    total_budget: Decimal,
    config: TaskRewardPoolConfig,
) -> TaskRewardPoolSplit:
    """Split an exact task budget using Week 6 largest-remainder accounting.

    Residual units follow the reused Week 6 order: largest fractional remainder,
    then larger share, then the stable miner/validator/protocol identifiers.
    """
    validated_config = TaskRewardPoolConfig.model_validate(config)
    total = _validate_total_budget(total_budget)
    shares = {
        "miner": validated_config.miner_share,
        "validator": validated_config.validator_share,
        "protocol": validated_config.protocol_share,
    }
    tie_order = {"miner": "0_miner", "validator": "1_validator", "protocol": "2_protocol"}
    weighted_items = [
        WeightedRewardItem(
            allocation_id=tie_order[name],
            node_id=tie_order[name],
            submission_id=tie_order[name],
            raw_weight=share,
            contribution_score=Decimal("0"),
        )
        for name, share in shares.items()
        if share > 0
    ]
    allocated = allocate_proportional_rewards(total, weighted_items)
    pools = {
        name: allocated.get(tie_order[name], Decimal("0.000000"))
        for name in shares
    }
    split = TaskRewardPoolSplit(
        total_budget_points=total,
        miner_pool_points=pools["miner"],
        validator_pool_points=pools["validator"],
        protocol_pool_points=pools["protocol"],
    )
    if (
        split.miner_pool_points
        + split.validator_pool_points
        + split.protocol_pool_points
        != total
    ):
        raise TaskRewardBudgetConservationError(
            "Task reward pool allocation failed to conserve protocol points"
        )
    return split


def build_task_reward_request_payload(
    project_id: str,
    routing_id: str,
    routing_source_fingerprint: str,
    total_budget_points: Decimal,
    config: TaskRewardPoolConfig,
    configuration_version: str,
) -> dict[str, Any]:
    return {
        "budget_version": TASK_REWARD_BUDGET_VERSION,
        "policy_version": TASK_REWARD_POLICY_VERSION,
        "reward_domain": RewardDomain.CLIENT_TASK.value,
        "project_id": project_id,
        "routing_id": routing_id,
        "routing_source_fingerprint": routing_source_fingerprint,
        "total_budget_points": total_budget_points,
        "miner_share": config.miner_share,
        "validator_share": config.validator_share,
        "protocol_share": config.protocol_share,
        "configuration_version": configuration_version,
        "protocol_quantum": PROTOCOL_POINTS_QUANTUM,
    }


def build_task_reward_source_payload(
    request_payload: dict[str, Any],
    split: TaskRewardPoolSplit,
) -> dict[str, Any]:
    return {
        **request_payload,
        "miner_pool_points": split.miner_pool_points,
        "validator_pool_points": split.validator_pool_points,
        "protocol_pool_points": split.protocol_pool_points,
    }


def compute_task_reward_fingerprint(payload: dict[str, Any]) -> str:
    return protocol_fingerprint(payload)


def create_task_reward_budget(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    request: TaskRewardBudgetCreateRequest,
    *,
    configured_pool: TaskRewardPoolConfig | None = None,
    configuration_version: str | None = None,
) -> tuple[TaskRewardBudget, bool]:
    _verify_project_workspace(project_workspace, project_id)
    _validate_identifier(project_id, "project")
    try:
        routing = load_routing_record_by_id(protocol_data_root, request.routing_id)
    except InvalidRoutingIdentifierError as exc:
        raise InvalidTaskRewardIdentifierError(str(exc)) from exc
    except RoutingStorageError as exc:
        raise TaskRewardBudgetStorageError("Stored routing data is malformed") from exc
    if routing is None:
        raise TaskRewardRoutingNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise TaskRewardProjectMismatchError(
            "Routing does not belong to the requested project"
        )
    if routing.status != RoutingStatus.FINALIZED:
        raise TaskRewardRoutingNotFinalizedError(
            "Task reward budgets require finalized routing"
        )

    settings = get_settings()
    base_config = configured_pool or settings.task_reward_pool
    config = request.pool_split or base_config
    config = TaskRewardPoolConfig.model_validate(config)
    version = configuration_version or settings.task_reward_configuration_version
    if request.pool_split is not None:
        version = f"{version}:request_override"
    split = split_task_reward_budget(request.total_budget_points, config)
    request_payload = build_task_reward_request_payload(
        project_id,
        routing.routing_id,
        routing.source_fingerprint,
        split.total_budget_points,
        config,
        version,
    )
    request_fingerprint = compute_task_reward_fingerprint(request_payload)
    source_fingerprint = compute_task_reward_fingerprint(
        build_task_reward_source_payload(request_payload, split)
    )
    existing = load_task_reward_budget_by_routing(
        protocol_data_root, routing.routing_id
    )
    if existing is not None:
        if (
            existing.request_fingerprint == request_fingerprint
            and existing.source_fingerprint == source_fingerprint
        ):
            return existing, False
        raise TaskRewardBudgetConflictError(
            "A differently configured task reward budget already exists for routing"
        )

    now = _utc_now()
    budget = TaskRewardBudget(
        task_reward_budget_id=f"task_reward_budget_{source_fingerprint[:32]}",
        project_id=project_id,
        routing_id=routing.routing_id,
        routing_source_fingerprint=routing.source_fingerprint,
        total_budget_points=split.total_budget_points,
        miner_pool_points=split.miner_pool_points,
        validator_pool_points=split.validator_pool_points,
        protocol_pool_points=split.protocol_pool_points,
        miner_share=config.miner_share,
        validator_share=config.validator_share,
        protocol_share=config.protocol_share,
        status=TaskRewardBudgetStatus.DRAFT,
        configuration_version=version,
        request_fingerprint=request_fingerprint,
        source_fingerprint=source_fingerprint,
        description=request.description,
        created_at=now,
        updated_at=now,
        finalized_at=None,
    )
    return save_task_reward_budget(protocol_data_root, budget), True


def finalize_task_reward_budget(
    protocol_data_root: Path,
    project_id: str,
    task_reward_budget_id: str,
) -> tuple[TaskRewardBudget, bool]:
    budget = load_task_reward_budget(
        protocol_data_root, project_id, task_reward_budget_id
    )
    if budget is None:
        raise TaskRewardBudgetNotFoundError("Task reward budget not found")
    if budget.status == TaskRewardBudgetStatus.FINALIZED:
        return budget, False
    now = _utc_now()
    finalized = TaskRewardBudget.model_validate(
        {
            **budget.model_dump(),
            "status": TaskRewardBudgetStatus.FINALIZED,
            "updated_at": now,
            "finalized_at": now,
        }
    )
    return save_task_reward_budget(protocol_data_root, finalized), True


def save_task_reward_budget(
    protocol_data_root: Path,
    budget: TaskRewardBudget,
) -> TaskRewardBudget:
    validated = TaskRewardBudget.model_validate(budget.model_dump())
    existing = load_task_reward_budget_by_routing(
        protocol_data_root, validated.routing_id
    )
    if existing is not None:
        if existing.task_reward_budget_id != validated.task_reward_budget_id:
            raise TaskRewardBudgetConflictError(
                "Only one task reward budget may exist for routing"
            )
        if existing.status == TaskRewardBudgetStatus.FINALIZED:
            if existing == validated:
                return existing
            raise TaskRewardBudgetAlreadyFinalizedError(
                "Finalized task reward budgets are immutable"
            )
        if _immutable_budget_payload(existing) != _immutable_budget_payload(validated):
            raise TaskRewardBudgetConflictError(
                "Task reward budget economic inputs are immutable"
            )
        values = validated.model_dump()
        values["created_at"] = existing.created_at
        validated = TaskRewardBudget.model_validate(values)
    path = get_task_reward_budget_path(protocol_data_root, validated.routing_id)
    try:
        if existing is None:
            created = atomic_create_json(
                path,
                validated,
                temporary_prefix=".task-reward-budget-create-",
            )
            if not created:
                concurrent = load_task_reward_budget_by_routing(
                    protocol_data_root, validated.routing_id
                )
                if concurrent == validated:
                    return concurrent
                raise TaskRewardBudgetConflictError(
                    "A conflicting task reward budget was created concurrently"
                )
        else:
            atomic_write_json(
                path,
                validated,
                temporary_prefix=".task-reward-budget-",
            )
    except OSError as exc:
        raise TaskRewardBudgetStorageError(
            "Unable to persist task reward budget"
        ) from exc
    return validated


def load_task_reward_budget_by_routing(
    protocol_data_root: Path,
    routing_id: str,
) -> TaskRewardBudget | None:
    path = get_task_reward_budget_path(protocol_data_root, routing_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise TaskRewardBudgetStorageError(
            "Stored task reward budget is not a regular file"
        )
    try:
        budget = TaskRewardBudget.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise TaskRewardBudgetStorageError(
            "Stored task reward budget is malformed"
        ) from exc
    if budget.routing_id != routing_id or path.parent.name != routing_id:
        raise TaskRewardBudgetStorageError(
            "Stored task reward budget identity does not match its path"
        )
    return budget


def load_task_reward_budget(
    protocol_data_root: Path,
    project_id: str,
    task_reward_budget_id: str,
) -> TaskRewardBudget | None:
    _validate_identifier(project_id, "project")
    _validate_identifier(task_reward_budget_id, "task reward budget")
    matches = [
        budget
        for budget in list_project_task_reward_budgets(protocol_data_root, project_id)
        if budget.task_reward_budget_id == task_reward_budget_id
    ]
    if len(matches) > 1:
        raise TaskRewardBudgetStorageError(
            "Task reward budget identifier is not unique"
        )
    return matches[0] if matches else None


def list_project_task_reward_budgets(
    protocol_data_root: Path,
    project_id: str,
) -> list[TaskRewardBudget]:
    _validate_identifier(project_id, "project")
    root = get_task_reward_budgets_root(protocol_data_root) / TASK_REWARD_ROUTING_DIRECTORY
    if not root.exists():
        return []
    budgets: list[TaskRewardBudget] = []
    for path in sorted(root.glob(f"*/{TASK_REWARD_BUDGET_FILENAME}")):
        budget = load_task_reward_budget_by_routing(
            protocol_data_root, path.parent.name
        )
        if budget is not None and budget.project_id == project_id:
            budgets.append(budget)
    return sorted(
        budgets,
        key=lambda item: (-item.created_at.timestamp(), item.task_reward_budget_id),
    )


def _immutable_budget_payload(budget: TaskRewardBudget) -> dict[str, Any]:
    return budget.model_dump(
        exclude={"status", "updated_at", "finalized_at"}
    )


def _validate_total_budget(value: Decimal) -> Decimal:
    if isinstance(value, float):
        raise ValueError("Task reward values must use Decimal")
    total = value if isinstance(value, Decimal) else Decimal(str(value))
    if not total.is_finite() or total <= 0:
        raise ValueError("Task reward total budget must be positive and finite")
    if total > MAX_PROTOCOL_POINTS:
        raise ValueError(
            f"Task reward total budget cannot exceed {MAX_PROTOCOL_POINTS}"
        )
    if total != total.quantize(PROTOCOL_POINTS_QUANTUM):
        raise ValueError("Task reward total budget must use at most six decimal places")
    return total


def _verify_project_workspace(project_workspace: Path, project_id: str) -> None:
    metadata_path = project_workspace / "metadata.json"
    if not project_workspace.is_dir() or not metadata_path.is_file():
        raise TaskRewardProjectNotFoundError("Project not found")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskRewardProjectNotFoundError("Project metadata is unavailable") from exc
    if not isinstance(metadata, dict) or metadata.get("project_id") != project_id:
        raise TaskRewardProjectMismatchError(
            "Project workspace does not match the requested project"
        )


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise InvalidTaskRewardIdentifierError(f"Invalid {label} identifier")


def _safe_child(root: Path, identifier: str) -> Path:
    _validate_identifier(identifier, "task reward")
    child = root / identifier
    try:
        child.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidTaskRewardIdentifierError(
            "Invalid task reward identifier"
        ) from exc
    return child


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
