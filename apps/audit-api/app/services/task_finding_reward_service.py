from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.finding_cluster import FindingClusterStatus
from app.schemas.reward import RewardDomain
from app.schemas.routing import RoutingStatus
from app.schemas.task_finding_reward import (
    NORMALIZED_WEIGHT_QUANTUM,
    TASK_FINDING_CALCULATION_VERSION,
    FindingAllocationScope,
    FindingCategoryPoolAllocation,
    FindingClusterRewardAllocation,
    FindingClusterValue,
    FindingParentPoolType,
    TaskFindingCalculationOutcome,
    TaskFindingCalculationProcessingStatus,
    TaskFindingCalculationStatus,
    TaskFindingRewardCalculation,
    TaskFindingRewardCalculationListResponse,
    TaskFindingRewardCalculationRequest,
    TaskFindingRewardConfig,
)
from app.schemas.task_reward import TaskRewardBudgetStatus
from app.services.finding_cluster_service import list_task_finding_clusters
from app.services.node_registry_service import load_node
from app.services.subnet_reward_allocation_service import allocate_category_pools
from app.services.subnet_router_service import load_routing_record
from app.services.task_finding_reward_calculator import (
    FindingValuationError,
    allocate_finding_cluster_pool,
    value_finding_cluster,
)
from app.services.task_reward_budget_service import load_task_reward_budget_by_routing
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


CALCULATION_FILENAME = "calculation.json"
LATEST_FILENAME = "latest.json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class TaskFindingRewardServiceError(ValueError):
    pass


class TaskFindingRewardNotFoundError(TaskFindingRewardServiceError):
    pass


class TaskFindingRewardLinkageError(TaskFindingRewardServiceError):
    pass


class TaskFindingRewardStateError(TaskFindingRewardServiceError):
    pass


class TaskFindingRewardStorageError(TaskFindingRewardServiceError):
    pass


class TaskFindingRewardConflictError(TaskFindingRewardServiceError):
    pass


def get_task_finding_calculations_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "task-rewards" / "calculations" / "routing"


def get_task_finding_routing_root(
    protocol_data_root: Path, routing_id: str
) -> Path:
    _validate_identifier(routing_id, "routing")
    root = get_task_finding_calculations_root(protocol_data_root)
    path = root / routing_id / "finding-allocation"
    _ensure_within(path, root)
    return path


def get_task_finding_calculation_path(
    protocol_data_root: Path, routing_id: str, calculation_id: str
) -> Path:
    _validate_identifier(calculation_id, "calculation")
    root = get_task_finding_routing_root(protocol_data_root, routing_id)
    path = root / calculation_id / CALCULATION_FILENAME
    _ensure_within(path, root)
    return path


def calculate_task_finding_rewards(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    request: TaskFindingRewardCalculationRequest,
    *,
    configured_policy: TaskFindingRewardConfig | None = None,
    policy_version: str | None = None,
    configuration_version: str | None = None,
) -> tuple[TaskFindingRewardCalculation, TaskFindingCalculationProcessingStatus]:
    """Create an immutable cluster-valuation snapshot; no payout side effect occurs."""
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise TaskFindingRewardNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise TaskFindingRewardLinkageError("Routing belongs to another project")
    if routing.status != RoutingStatus.FINALIZED:
        raise TaskFindingRewardStateError("Finding allocation requires finalized routing")

    budget = load_task_reward_budget_by_routing(protocol_data_root, routing_id)
    if budget is None:
        raise TaskFindingRewardNotFoundError("Task reward budget not found")
    if budget.task_reward_budget_id != request.task_reward_budget_id:
        raise TaskFindingRewardLinkageError(
            "Task reward budget does not match the requested routing budget"
        )
    if budget.project_id != project_id or budget.routing_id != routing_id:
        raise TaskFindingRewardLinkageError(
            "Task reward budget belongs to another project or routing"
        )
    if budget.reward_domain != RewardDomain.CLIENT_TASK:
        raise TaskFindingRewardLinkageError(
            "Only client_task budgets may feed FindingCluster allocation"
        )
    if budget.status != TaskRewardBudgetStatus.FINALIZED:
        raise TaskFindingRewardStateError(
            "Finding allocation requires a finalized TaskRewardBudget"
        )

    settings = get_settings()
    config = TaskFindingRewardConfig.model_validate(
        configured_policy or settings.task_finding_reward
    )
    scope = request.allocation_scope or config.default_allocation_scope
    if scope == FindingAllocationScope.GLOBAL and request.category_weights is not None:
        raise TaskFindingRewardLinkageError(
            "Global finding allocation does not accept category weights"
        )
    version = policy_version or settings.task_finding_policy_version
    config_version = (
        configuration_version or settings.task_finding_configuration_version
    )

    clusters = list_task_finding_clusters(protocol_data_root, project_id, routing_id)
    routed_categories = {result.category for result in routing.results}
    for cluster in clusters:
        if cluster.project_id != project_id or cluster.routing_id != routing_id:
            raise TaskFindingRewardLinkageError(
                "FindingCluster belongs to another project or routing"
            )
        if cluster.status != FindingClusterStatus.FINALIZED:
            raise TaskFindingRewardStateError(
                "Only finalized FindingClusters may enter economic valuation"
            )
        if not cluster.members or cluster.distinct_operator_count < 1:
            raise TaskFindingRewardStateError(
                "Reward-relevant FindingClusters require at least one valid operator"
            )
        if cluster.category not in routed_categories:
            raise TaskFindingRewardLinkageError(
                "FindingCluster category is not part of the task routing"
            )
        authoritative_operator_ids: set[str] = set()
        for member in cluster.members:
            node = load_node(protocol_data_root, member.node_id)
            if node is None:
                raise TaskFindingRewardStateError(
                    "FindingCluster member node is missing from NodeRegistry"
                )
            if node.operator_id != member.operator_id:
                raise TaskFindingRewardStateError(
                    "FindingCluster operator attribution is stale; cluster rebuild is required"
                )
            authoritative_operator_ids.add(node.operator_id)
        if len(authoritative_operator_ids) != cluster.distinct_operator_count:
            raise TaskFindingRewardStateError(
                "FindingCluster distinct operator count does not match NodeRegistry"
            )
    try:
        values = [value_finding_cluster(cluster, config) for cluster in clusters]
    except FindingValuationError as exc:
        raise TaskFindingRewardStateError(str(exc)) from exc
    values.sort(key=lambda item: item.finding_cluster_id)

    category_pools: list[FindingCategoryPoolAllocation] = []
    category_weights: dict[Any, Decimal] = {}
    reward_by_cluster: dict[str, Decimal] = {}
    parent_by_cluster: dict[str, tuple[FindingParentPoolType, str, Decimal]] = {}
    if scope == FindingAllocationScope.GLOBAL:
        reward_by_cluster = allocate_finding_cluster_pool(
            budget.miner_pool_points, values
        )
        for value in values:
            parent_by_cluster[value.finding_cluster_id] = (
                FindingParentPoolType.MINER_POOL,
                budget.task_reward_budget_id,
                budget.miner_pool_points,
            )
    else:
        categories = [result.category for result in routing.results]
        supplied_weights = request.category_weights
        try:
            allocated_pools = allocate_category_pools(
                budget.miner_pool_points, categories, supplied_weights
            )
        except ValueError as exc:
            raise TaskFindingRewardLinkageError(str(exc)) from exc
        raw_weights = supplied_weights or {
            category: Decimal("1") for category in categories
        }
        category_weights = {category: raw_weights[category] for category in categories}
        total_category_weight = sum(category_weights.values(), Decimal("0"))
        for category in sorted(categories, key=lambda item: item.value):
            category_values = [value for value in values if value.category == category]
            category_rewards = allocate_finding_cluster_pool(
                allocated_pools[category.value], category_values
            )
            reward_by_cluster.update(category_rewards)
            pool_id = f"category_pool_{category.value}"
            for value in category_values:
                parent_by_cluster[value.finding_cluster_id] = (
                    FindingParentPoolType.CATEGORY_POOL,
                    pool_id,
                    allocated_pools[category.value],
                )
            distributed = sum(category_rewards.values(), Decimal("0"))
            category_pools.append(
                FindingCategoryPoolAllocation(
                    category=category,
                    parent_pool_id=pool_id,
                    requested_weight=category_weights[category],
                    normalized_weight=(
                        category_weights[category] / total_category_weight
                    ).quantize(NORMALIZED_WEIGHT_QUANTUM, rounding=ROUND_HALF_EVEN),
                    allocated_pool_points=allocated_pools[category.value],
                    distributed_cluster_points=distributed,
                    undistributed_cluster_points=(
                        allocated_pools[category.value] - distributed
                    ),
                    eligible_cluster_count=len(category_values),
                    positive_score_cluster_count=sum(
                        value.finding_score > 0 for value in category_values
                    ),
                    zero_weight_cluster_count=sum(
                        value.finding_score == 0 for value in category_values
                    ),
                    finding_cluster_ids=[
                        value.finding_cluster_id for value in category_values
                    ],
                )
            )

    allocations = [
        _build_cluster_allocation(
            value,
            parent_by_cluster[value.finding_cluster_id],
            reward_by_cluster[value.finding_cluster_id],
        )
        for value in values
    ]
    distributed = sum(
        (allocation.cluster_reward_points for allocation in allocations), Decimal("0")
    )
    undistributed = budget.miner_pool_points - distributed
    positive_count = sum(value.finding_score > 0 for value in values)
    outcome = _outcome(len(values), positive_count, distributed, undistributed)
    source_payload = _source_payload(
        policy_version=version,
        configuration_version=config_version,
        budget=budget,
        allocation_scope=scope,
        config=config,
        category_weights=category_weights,
        category_pools=category_pools,
        allocations=allocations,
    )
    source_fingerprint = protocol_fingerprint(source_payload)
    calculation_id = f"task_finding_reward_calculation_{source_fingerprint[:32]}"

    existing_same = load_task_finding_calculation(
        protocol_data_root, project_id, routing_id, calculation_id
    )
    if existing_same is not None:
        if existing_same.status == TaskFindingCalculationStatus.SUPERSEDED:
            raise TaskFindingRewardConflictError(
                "The identical source belongs to a superseded historical calculation"
            )
        return existing_same, TaskFindingCalculationProcessingStatus.UNCHANGED
    previous = load_latest_task_finding_calculation(
        protocol_data_root, project_id, routing_id
    )
    now = datetime.now(timezone.utc)
    record = TaskFindingRewardCalculation(
        calculation_id=calculation_id,
        calculation_version=TASK_FINDING_CALCULATION_VERSION,
        policy_version=version,
        configuration_version=config_version,
        status=TaskFindingCalculationStatus.CALCULATED,
        outcome=outcome,
        project_id=project_id,
        routing_id=routing_id,
        task_reward_budget_id=budget.task_reward_budget_id,
        task_reward_budget_source_fingerprint=budget.source_fingerprint,
        allocation_scope=scope,
        miner_pool_points=budget.miner_pool_points,
        validator_pool_points_snapshot=budget.validator_pool_points,
        protocol_pool_points_snapshot=budget.protocol_pool_points,
        severity_weights=config.severity_weights,
        uniqueness_config=config.uniqueness,
        category_weights=category_weights,
        category_pool_allocations=category_pools,
        cluster_allocations=allocations,
        total_finding_score=sum(
            (value.finding_score for value in values), Decimal("0.000000")
        ),
        eligible_cluster_count=len(values),
        positive_score_cluster_count=positive_count,
        zero_weight_cluster_count=len(values) - positive_count,
        distributed_cluster_points=distributed,
        undistributed_cluster_points=undistributed,
        source_fingerprint=source_fingerprint,
        supersedes_calculation_id=(previous.calculation_id if previous else None),
        superseded_by_calculation_id=None,
        created_at=now,
        calculated_at=now,
    )
    _persist_new_calculation(protocol_data_root, record)
    processing = TaskFindingCalculationProcessingStatus.CALCULATED
    if previous is not None and previous.calculation_id != calculation_id:
        superseded = TaskFindingRewardCalculation.model_validate(
            {
                **previous.model_dump(),
                "status": TaskFindingCalculationStatus.SUPERSEDED,
                "superseded_by_calculation_id": calculation_id,
            }
        )
        atomic_write_json(
            get_task_finding_calculation_path(
                protocol_data_root, routing_id, previous.calculation_id
            ),
            superseded,
            temporary_prefix=".task-finding-supersede-",
        )
        processing = TaskFindingCalculationProcessingStatus.SUPERSEDED
    atomic_write_json(
        get_task_finding_routing_root(protocol_data_root, routing_id) / LATEST_FILENAME,
        {
            "project_id": project_id,
            "routing_id": routing_id,
            "calculation_id": calculation_id,
        },
        temporary_prefix=".task-finding-latest-",
    )
    return record, processing


def load_task_finding_calculation(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    calculation_id: str,
) -> TaskFindingRewardCalculation | None:
    _validate_identifier(project_id, "project")
    path = get_task_finding_calculation_path(
        protocol_data_root, routing_id, calculation_id
    )
    if not path.exists():
        return None
    record = _read_calculation(path)
    if record.project_id != project_id or record.routing_id != routing_id:
        raise TaskFindingRewardStorageError(
            "Stored finding reward calculation identity does not match its path"
        )
    return record


def load_latest_task_finding_calculation(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> TaskFindingRewardCalculation | None:
    root = get_task_finding_routing_root(protocol_data_root, routing_id)
    path = root / LATEST_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        calculation_id = payload["calculation_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TaskFindingRewardStorageError(
            "Stored latest calculation index is malformed"
        ) from exc
    if payload.get("project_id") != project_id or payload.get("routing_id") != routing_id:
        raise TaskFindingRewardStorageError(
            "Latest calculation index has wrong identity"
        )
    record = load_task_finding_calculation(
        protocol_data_root, project_id, routing_id, calculation_id
    )
    if record is None:
        raise TaskFindingRewardStorageError("Latest index references a missing calculation")
    return record


def list_task_finding_calculations(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> TaskFindingRewardCalculationListResponse:
    root = get_task_finding_routing_root(protocol_data_root, routing_id)
    calculations: list[TaskFindingRewardCalculation] = []
    if root.exists():
        for path in sorted(root.glob(f"*/{CALCULATION_FILENAME}")):
            record = _read_calculation(path)
            if record.project_id != project_id or record.routing_id != routing_id:
                raise TaskFindingRewardStorageError(
                    "Stored calculation linkage is malformed"
                )
            calculations.append(record)
    calculations.sort(key=lambda item: (item.created_at, item.calculation_id))
    return TaskFindingRewardCalculationListResponse(
        project_id=project_id,
        routing_id=routing_id,
        total=len(calculations),
        calculations=calculations,
    )


def _build_cluster_allocation(
    value: FindingClusterValue,
    parent: tuple[FindingParentPoolType, str, Decimal],
    reward: Decimal,
) -> FindingClusterRewardAllocation:
    parent_type, parent_id, parent_points = parent
    payload = {
        **value.model_dump(),
        "parent_pool_type": parent_type,
        "parent_pool_id": parent_id,
        "parent_pool_points": parent_points,
        "cluster_reward_points": reward,
    }
    return FindingClusterRewardAllocation(
        **payload, source_fingerprint=protocol_fingerprint(payload)
    )


def _source_payload(
    *,
    policy_version: str,
    configuration_version: str,
    budget,
    allocation_scope: FindingAllocationScope,
    config: TaskFindingRewardConfig,
    category_weights: dict[Any, Decimal],
    category_pools: list[FindingCategoryPoolAllocation],
    allocations: list[FindingClusterRewardAllocation],
) -> dict[str, Any]:
    return {
        "calculation_version": TASK_FINDING_CALCULATION_VERSION,
        "policy_version": policy_version,
        "configuration_version": configuration_version,
        "task_reward_budget_id": budget.task_reward_budget_id,
        "task_reward_budget_source_fingerprint": budget.source_fingerprint,
        "miner_pool_points": budget.miner_pool_points,
        "allocation_scope": allocation_scope,
        "category_weights": category_weights,
        "category_pool_allocations": [pool.model_dump() for pool in category_pools],
        "severity_weights": config.severity_weights.model_dump(),
        "uniqueness": config.uniqueness.model_dump(),
        "protocol_quantum": Decimal("0.000001"),
        "eligible_finding_clusters": [
            allocation.model_dump(exclude={"source_fingerprint"})
            for allocation in allocations
        ],
    }


def _outcome(
    eligible_count: int,
    positive_count: int,
    distributed: Decimal,
    undistributed: Decimal,
) -> TaskFindingCalculationOutcome:
    if eligible_count == 0:
        return TaskFindingCalculationOutcome.NO_ELIGIBLE_FINDINGS
    if positive_count == 0:
        return TaskFindingCalculationOutcome.NO_POSITIVE_FINDING_SCORE
    if undistributed > 0:
        return TaskFindingCalculationOutcome.PARTIALLY_ALLOCATED
    return TaskFindingCalculationOutcome.ALLOCATED


def _persist_new_calculation(
    protocol_data_root: Path, record: TaskFindingRewardCalculation
) -> None:
    path = get_task_finding_calculation_path(
        protocol_data_root, record.routing_id, record.calculation_id
    )
    try:
        created = atomic_create_json(
            path, record, temporary_prefix=".task-finding-create-"
        )
    except OSError as exc:
        raise TaskFindingRewardStorageError(
            "Unable to persist finding reward calculation"
        ) from exc
    if not created:
        existing = _read_calculation(path)
        if existing != record:
            raise TaskFindingRewardConflictError(
                "A conflicting calculation was created concurrently"
            )


def _read_calculation(path: Path) -> TaskFindingRewardCalculation:
    if not path.is_file():
        raise TaskFindingRewardStorageError("Calculation is not a regular file")
    try:
        return TaskFindingRewardCalculation.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise TaskFindingRewardStorageError("Stored finding calculation is malformed") from exc


def _validate_identifier(value: str, label: str) -> None:
    if (
        not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise TaskFindingRewardLinkageError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise TaskFindingRewardLinkageError("Unsafe finding calculation path") from exc
