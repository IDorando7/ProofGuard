from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.reward import RewardCycleStatus, RewardDomain
from app.schemas.finding_cluster import (
    FindingClusterStatus,
    is_finding_cluster_reward_eligible,
)
from app.schemas.routing import RoutingStatus
from app.schemas.task_finding_reward import (
    FindingAllocationScope,
    TaskFindingRewardConfig,
    TaskFindingRewardCalculation,
    TaskFindingRewardCalculationRequest,
)
from app.schemas.task_operator_reward import (
    TaskOperatorRewardCalculation,
    TaskOperatorRewardCalculationRequest,
)
from app.schemas.task_reward import TaskRewardBudgetStatus
from app.schemas.week7_reward_cycle import (
    WEEK7_REWARD_CONFIGURATION_VERSION,
    WEEK7_REWARD_CYCLE_VERSION,
    WEEK7_REWARD_EVENT_VERSION,
    WEEK7_REWARD_POLICY_VERSION,
    Week7RewardHistorySubject,
    Week7RewardPolicySnapshot,
    Week7RewardProcessingStatus,
    Week7RewardVerificationStatus,
    Week7TaskRewardCycle,
    Week7TaskRewardCycleCreateRequest,
    Week7TaskRewardCycleListResponse,
    Week7TaskRewardCycleResponse,
    Week7TaskRewardEvent,
    Week7TaskRewardEventListResponse,
    Week7TaskRewardHistorySummary,
    Week7TaskRewardCycleVerification,
)
from app.services.finding_cluster_service import (
    list_task_finding_clusters,
    load_finding_cluster,
)
from app.services.node_registry_service import load_node
from app.services.subnet_router_service import load_routing_record
from app.services.task_finding_reward_service import (
    TaskFindingRewardStateError,
    _source_payload as build_task_finding_source_payload,
    calculate_task_finding_rewards,
    load_task_finding_calculation,
)
from app.services.task_finding_reward_calculator import value_finding_cluster
from app.services.task_operator_reward_calculator import calculate_cluster_operator_payout
from app.services.task_operator_reward_service import (
    _operator_summaries,
    _resolve_member_report,
    calculate_task_operator_rewards,
    load_task_operator_calculation,
)
from app.services.task_reward_budget_service import load_task_reward_budget
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


CYCLE_FILENAME = "cycle.json"
EVENT_SUFFIX = ".json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class Week7RewardCycleError(ValueError):
    pass


class Week7RewardCycleNotFoundError(Week7RewardCycleError):
    pass


class Week7RewardCycleConflictError(Week7RewardCycleError):
    pass


class Week7RewardCycleSourceChangedError(Week7RewardCycleError):
    pass


class Week7RewardCycleStateError(Week7RewardCycleError):
    pass


class Week7RewardEventConflictError(Week7RewardCycleError):
    pass


class Week7RewardCycleStorageError(Week7RewardCycleError):
    pass


class Week7RewardCycleConservationError(Week7RewardCycleError):
    pass


def get_week7_reward_cycles_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "task-rewards" / "cycles" / "routing"


def get_week7_reward_cycle_path(
    protocol_data_root: Path, routing_id: str, reward_cycle_id: str
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(reward_cycle_id, "reward cycle")
    root = get_week7_reward_cycles_root(protocol_data_root)
    path = root / routing_id / reward_cycle_id / CYCLE_FILENAME
    _ensure_within(path, root)
    return path


def get_week7_reward_events_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "task-rewards" / "events" / "cycles"


def get_week7_reward_event_path(
    protocol_data_root: Path, reward_cycle_id: str, reward_event_id: str
) -> Path:
    _validate_identifier(reward_cycle_id, "reward cycle")
    _validate_identifier(reward_event_id, "reward event")
    root = get_week7_reward_events_root(protocol_data_root)
    path = root / reward_cycle_id / f"{reward_event_id}{EVENT_SUFFIX}"
    _ensure_within(path, root)
    return path


def create_week7_task_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
    request: Week7TaskRewardCycleCreateRequest,
    *,
    policy_version: str | None = None,
    configuration_version: str | None = None,
) -> tuple[Week7TaskRewardCycle, bool]:
    _verify_project_workspace(project_workspace, project_id)
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise Week7RewardCycleNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise Week7RewardCycleConflictError("Routing belongs to another project")
    if routing.status != RoutingStatus.FINALIZED:
        raise Week7RewardCycleStateError("Week 7 reward cycles require finalized routing")
    budget = load_task_reward_budget(
        protocol_data_root, project_id, request.task_reward_budget_id
    )
    if budget is None:
        raise Week7RewardCycleNotFoundError("TaskRewardBudget not found")
    if budget.project_id != project_id or budget.routing_id != routing_id:
        raise Week7RewardCycleConflictError(
            "TaskRewardBudget belongs to another project or routing"
        )
    if budget.reward_domain != RewardDomain.CLIENT_TASK:
        raise Week7RewardCycleConflictError(
            "Only client_task TaskRewardBudget records may fund Week 7 cycles"
        )
    if budget.status != TaskRewardBudgetStatus.FINALIZED:
        raise Week7RewardCycleStateError(
            "Week 7 reward cycles require a finalized TaskRewardBudget"
        )
    if budget.routing_source_fingerprint != routing.source_fingerprint:
        raise Week7RewardCycleSourceChangedError(
            "TaskRewardBudget routing source no longer matches finalized routing"
        )

    settings = get_settings()
    policy = policy_version or settings.week7_reward_cycle_policy_version
    config_version = (
        configuration_version or settings.week7_reward_cycle_configuration_version
    )
    scope = request.allocation_scope or settings.task_finding_reward.default_allocation_scope
    weights = request.category_weights or {}
    if scope == FindingAllocationScope.GLOBAL and weights:
        raise Week7RewardCycleConflictError(
            "Global Week 7 allocation cannot contain category weights"
        )
    request_payload = {
        "cycle_version": WEEK7_REWARD_CYCLE_VERSION,
        "policy_version": policy,
        "configuration_version": config_version,
        "project_id": project_id,
        "routing_id": routing_id,
        "routing_source_fingerprint": routing.source_fingerprint,
        "task_reward_budget_id": budget.task_reward_budget_id,
        "task_reward_budget_source_fingerprint": budget.source_fingerprint,
        "allocation_scope": scope,
        "category_weights": weights,
        "policy_snapshot": _policy_snapshot(),
        "protocol_quantum": Decimal("0.000001"),
    }
    request_fingerprint = protocol_fingerprint(request_payload)
    reward_cycle_id = f"week7_task_reward_cycle_{request_fingerprint[:32]}"
    existing_for_budget = find_week7_reward_cycle_by_budget(
        protocol_data_root, budget.task_reward_budget_id
    )
    if existing_for_budget is not None:
        if existing_for_budget.request_fingerprint == request_fingerprint:
            return existing_for_budget, False
        raise Week7RewardCycleConflictError(
            "A different Week 7 reward cycle already exists for this TaskRewardBudget"
        )

    now = _utc_now()
    cycle = Week7TaskRewardCycle(
        reward_cycle_id=reward_cycle_id,
        policy_version=policy,
        configuration_version=config_version,
        policy_snapshot=_policy_snapshot(),
        project_id=project_id,
        routing_id=routing_id,
        routing_source_fingerprint=routing.source_fingerprint,
        task_reward_budget_id=budget.task_reward_budget_id,
        task_reward_budget_source_fingerprint=budget.source_fingerprint,
        status=RewardCycleStatus.DRAFT,
        allocation_scope=scope,
        category_weights=weights,
        total_budget_points=budget.total_budget_points,
        miner_pool_points=budget.miner_pool_points,
        validator_pool_points=budget.validator_pool_points,
        protocol_pool_points=budget.protocol_pool_points,
        distributed_miner_points=Decimal("0.000000"),
        undistributed_miner_points=budget.miner_pool_points,
        reward_event_count=0,
        rewarded_operator_count=0,
        rewarded_cluster_count=0,
        chief_finder_count=0,
        reward_event_ids=[],
        request_fingerprint=request_fingerprint,
        description=request.description,
        created_at=now,
        updated_at=now,
    )
    return save_week7_reward_cycle(protocol_data_root, cycle), True


def calculate_week7_task_reward_cycle(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
) -> tuple[Week7TaskRewardCycle, Week7RewardProcessingStatus]:
    cycle = _load_required_cycle(protocol_data_root, project_id, reward_cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        raise Week7RewardCycleStateError("Finalized Week 7 cycles cannot be recalculated")
    day4, day5, fingerprint = _calculate_current_snapshot(protocol_data_root, cycle)
    _validate_pipeline_conservation(cycle, day4, day5)
    if (
        cycle.status == RewardCycleStatus.CALCULATED
        and cycle.calculation_fingerprint == fingerprint
    ):
        return cycle, Week7RewardProcessingStatus.UNCHANGED

    superseded = list(cycle.superseded_calculation_fingerprints)
    processing = Week7RewardProcessingStatus.CALCULATED
    if cycle.calculation_fingerprint is not None:
        superseded.append(cycle.calculation_fingerprint)
        processing = Week7RewardProcessingStatus.RECALCULATED
    now = _utc_now()
    rewarded_allocations = _positive_allocations(day5)
    updated = Week7TaskRewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": RewardCycleStatus.CALCULATED,
            "finding_calculation_id": day4.calculation_id,
            "finding_calculation_source_fingerprint": day4.source_fingerprint,
            "operator_calculation_id": day5.calculation_id,
            "operator_calculation_source_fingerprint": day5.source_fingerprint,
            "distributed_miner_points": day5.distributed_operator_points,
            "undistributed_miner_points": (
                day4.undistributed_cluster_points
                + day5.undistributed_cluster_points
            ),
            "rewarded_operator_count": len(
                {allocation.operator_id for allocation in rewarded_allocations}
            ),
            "rewarded_cluster_count": len(
                {allocation.finding_cluster_id for allocation in rewarded_allocations}
            ),
            "chief_finder_count": sum(
                allocation.chief_finder for allocation in rewarded_allocations
            ),
            "calculation_fingerprint": fingerprint,
            "calculation_revision": cycle.calculation_revision + 1,
            "superseded_calculation_fingerprints": superseded,
            "calculated_at": now,
            "updated_at": now,
        }
    )
    return save_week7_reward_cycle(protocol_data_root, updated), processing


def finalize_week7_task_reward_cycle(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
    *,
    fail_after_event_writes: int | None = None,
) -> Week7TaskRewardCycleResponse:
    """Revalidate authoritative sources and materialize crash-safe immutable events."""
    cycle = _load_required_cycle(protocol_data_root, project_id, reward_cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        events = list_week7_reward_events(
            protocol_data_root, reward_cycle_id=cycle.reward_cycle_id
        )
        expected_events = None
        if cycle.operator_calculation_id and cycle.finding_calculation_id:
            try:
                day4 = load_task_finding_calculation(
                    protocol_data_root,
                    cycle.project_id,
                    cycle.routing_id,
                    cycle.finding_calculation_id,
                )
                day5 = load_task_operator_calculation(
                    protocol_data_root,
                    cycle.project_id,
                    cycle.routing_id,
                    cycle.operator_calculation_id,
                )
                if day4 is not None and day5 is not None:
                    expected_events = _build_expected_events(
                        cycle,
                        day4,
                        day5,
                        cycle.finalization_source_fingerprint,
                    )
            except (ValueError, OSError):
                # Legacy/synthetic persisted cycles may not retain nested artifacts;
                # their immutable IDs and conserved total remain the retry guard.
                expected_events = None
        if (
            sorted(event.reward_event_id for event in events)
            != cycle.reward_event_ids
            or sum(
                (event.total_reward_points for event in events), Decimal("0")
            )
            != cycle.distributed_miner_points
            or (
                expected_events is not None
                and {
                    event.reward_event_id: _event_identity(event)
                    for event in events
                }
                != {
                    event.reward_event_id: _event_identity(event)
                    for event in expected_events
                }
            )
        ):
            raise Week7RewardCycleConservationError(
                "Finalized Week 7 event ledger does not match the cycle"
            )
        return Week7TaskRewardCycleResponse(
            processing_status=Week7RewardProcessingStatus.ALREADY_FINALIZED,
            cycle=cycle,
            reward_events_created=0,
            reward_events_existing=len(events),
            message="Week 7 reward cycle was already finalized; no events were duplicated.",
        )
    if cycle.status != RewardCycleStatus.CALCULATED:
        raise Week7RewardCycleStateError(
            "Week 7 reward cycle must be calculated before finalization"
        )
    other_cycle = find_finalized_week7_reward_cycle_by_budget(
        protocol_data_root, cycle.task_reward_budget_id, exclude_cycle_id=cycle.reward_cycle_id
    )
    if other_cycle is not None:
        raise Week7RewardCycleConflictError(
            "TaskRewardBudget was already consumed by another finalized Week 7 cycle"
        )

    try:
        day4, day5, current_fingerprint = _calculate_current_snapshot(
            protocol_data_root, cycle
        )
    except TaskFindingRewardStateError as exc:
        raise Week7RewardCycleSourceChangedError(
            "Authoritative Day 4 source state changed after cycle calculation"
        ) from exc
    _validate_pipeline_conservation(cycle, day4, day5)
    if (
        current_fingerprint != cycle.calculation_fingerprint
        or day4.calculation_id != cycle.finding_calculation_id
        or day4.source_fingerprint
        != cycle.finding_calculation_source_fingerprint
        or day5.calculation_id != cycle.operator_calculation_id
        or day5.source_fingerprint
        != cycle.operator_calculation_source_fingerprint
    ):
        raise Week7RewardCycleSourceChangedError(
            "Calculated Week 7 sources no longer match authoritative current sources"
        )
    finalization_fingerprint = _finalization_fingerprint(
        cycle, day4, day5, current_fingerprint
    )
    expected_events = _build_expected_events(
        cycle, day4, day5, finalization_fingerprint
    )
    _validate_event_set(cycle, day5, expected_events)

    foreign_events = [
        event
        for event in list_week7_reward_events(
            protocol_data_root,
            task_reward_budget_id=cycle.task_reward_budget_id,
        )
        if event.reward_cycle_id != cycle.reward_cycle_id
    ]
    if foreign_events:
        raise Week7RewardCycleConflictError(
            "TaskRewardBudget already has client-task RewardEvents from another cycle"
        )
    for expected in expected_events:
        existing = load_week7_reward_event(
            protocol_data_root, cycle.reward_cycle_id, expected.reward_event_id
        )
        if existing is not None and _event_identity(existing) != _event_identity(expected):
            raise Week7RewardEventConflictError(
                "A conflicting deterministic Week 7 RewardEvent already exists"
            )

    created = 0
    existing_count = 0
    processed = 0
    for event in expected_events:
        _, was_created = save_week7_reward_event_exclusively(
            protocol_data_root, event
        )
        created += int(was_created)
        existing_count += int(not was_created)
        processed += 1
        if fail_after_event_writes is not None and processed >= fail_after_event_writes:
            raise RuntimeError("Simulated Week 7 RewardEvent finalization crash")

    stored_events = list_week7_reward_events(
        protocol_data_root, reward_cycle_id=cycle.reward_cycle_id
    )
    expected_ids = sorted(event.reward_event_id for event in expected_events)
    if sorted(event.reward_event_id for event in stored_events) != expected_ids:
        raise Week7RewardCycleConservationError(
            "Stored Week 7 RewardEvents do not match calculated positive allocations"
        )
    event_total = sum(
        (event.total_reward_points for event in stored_events), Decimal("0")
    )
    if event_total != cycle.distributed_miner_points:
        raise Week7RewardCycleConservationError(
            "RewardEvent total does not equal distributed miner points"
        )
    if event_total + cycle.undistributed_miner_points != cycle.miner_pool_points:
        raise Week7RewardCycleConservationError(
            "Finalized RewardEvents do not conserve the miner pool"
        )

    now = _utc_now()
    finalized = Week7TaskRewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": RewardCycleStatus.FINALIZED,
            "reward_event_count": len(stored_events),
            "reward_event_ids": expected_ids,
            "finalization_source_fingerprint": finalization_fingerprint,
            "finalized_at": now,
            "updated_at": now,
        }
    )
    saved = save_week7_reward_cycle(protocol_data_root, finalized)
    return Week7TaskRewardCycleResponse(
        processing_status=Week7RewardProcessingStatus.FINALIZED,
        cycle=saved,
        reward_events_created=created,
        reward_events_existing=existing_count,
        message="Week 7 client-task cycle finalized into immutable protocol-point RewardEvents.",
    )


def save_week7_reward_cycle(
    protocol_data_root: Path, cycle: Week7TaskRewardCycle
) -> Week7TaskRewardCycle:
    validated = Week7TaskRewardCycle.model_validate(cycle.model_dump())
    existing = load_week7_reward_cycle(
        protocol_data_root,
        validated.project_id,
        validated.reward_cycle_id,
    )
    if existing is not None:
        if existing.status == RewardCycleStatus.FINALIZED:
            if existing == validated:
                return existing
            raise Week7RewardCycleStateError("Finalized Week 7 cycles are immutable")
        if _immutable_request_payload(existing) != _immutable_request_payload(validated):
            raise Week7RewardCycleConflictError(
                "Week 7 reward-cycle request inputs are immutable"
            )
        validated = Week7TaskRewardCycle.model_validate(
            {**validated.model_dump(), "created_at": existing.created_at}
        )
    path = get_week7_reward_cycle_path(
        protocol_data_root, validated.routing_id, validated.reward_cycle_id
    )
    try:
        if existing is None:
            created = atomic_create_json(
                path, validated, temporary_prefix=".week7-cycle-create-"
            )
            if not created:
                concurrent = load_week7_reward_cycle(
                    protocol_data_root,
                    validated.project_id,
                    validated.reward_cycle_id,
                )
                if concurrent == validated:
                    return concurrent
                raise Week7RewardCycleConflictError(
                    "A conflicting Week 7 reward cycle was created concurrently"
                )
        else:
            atomic_write_json(path, validated, temporary_prefix=".week7-cycle-")
    except OSError as exc:
        raise Week7RewardCycleStorageError(
            "Unable to persist Week 7 reward cycle"
        ) from exc
    return validated


def load_week7_reward_cycle(
    protocol_data_root: Path, project_id: str, reward_cycle_id: str
) -> Week7TaskRewardCycle | None:
    _validate_identifier(project_id, "project")
    _validate_identifier(reward_cycle_id, "reward cycle")
    root = get_week7_reward_cycles_root(protocol_data_root)
    if not root.exists():
        return None
    matches: list[Week7TaskRewardCycle] = []
    for path in sorted(root.glob(f"*/*/{CYCLE_FILENAME}")):
        if path.parent.name != reward_cycle_id:
            continue
        cycle = _read_cycle(path)
        if cycle.project_id == project_id:
            matches.append(cycle)
    if len(matches) > 1:
        raise Week7RewardCycleStorageError("Week 7 reward-cycle ID is not unique")
    return matches[0] if matches else None


def load_week7_reward_cycle_by_routing(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> Week7TaskRewardCycle | None:
    _validate_identifier(routing_id, "routing")
    root = get_week7_reward_cycles_root(protocol_data_root) / routing_id
    if not root.exists():
        return None
    cycles = []
    for path in sorted(root.glob(f"*/{CYCLE_FILENAME}")):
        cycle = _read_cycle(path)
        if cycle.project_id == project_id and cycle.routing_id == routing_id:
            cycles.append(cycle)
    if len(cycles) > 1:
        raise Week7RewardCycleStorageError(
            "Routing has more than one Week 7 reward cycle"
        )
    return cycles[0] if cycles else None


def list_week7_reward_cycles(
    protocol_data_root: Path,
    *,
    project_id: str | None = None,
    status: RewardCycleStatus | None = None,
) -> list[Week7TaskRewardCycle]:
    root = get_week7_reward_cycles_root(protocol_data_root)
    if not root.exists():
        return []
    cycles = []
    for path in sorted(root.glob(f"*/*/{CYCLE_FILENAME}")):
        cycle = _read_cycle(path)
        if project_id is not None and cycle.project_id != project_id:
            continue
        if status is not None and cycle.status != status:
            continue
        cycles.append(cycle)
    return sorted(cycles, key=lambda item: (item.created_at, item.reward_cycle_id))


def list_project_week7_reward_cycles(
    protocol_data_root: Path, project_id: str
) -> Week7TaskRewardCycleListResponse:
    cycles = list_week7_reward_cycles(protocol_data_root, project_id=project_id)
    return Week7TaskRewardCycleListResponse(
        project_id=project_id, total=len(cycles), cycles=cycles
    )


def find_week7_reward_cycle_by_budget(
    protocol_data_root: Path, task_reward_budget_id: str
) -> Week7TaskRewardCycle | None:
    matches = [
        cycle
        for cycle in list_week7_reward_cycles(protocol_data_root)
        if cycle.task_reward_budget_id == task_reward_budget_id
    ]
    if len(matches) > 1:
        raise Week7RewardCycleStorageError(
            "TaskRewardBudget has multiple Week 7 reward cycles"
        )
    return matches[0] if matches else None


def find_finalized_week7_reward_cycle_by_budget(
    protocol_data_root: Path,
    task_reward_budget_id: str,
    *,
    exclude_cycle_id: str | None = None,
) -> Week7TaskRewardCycle | None:
    matches = [
        cycle
        for cycle in list_week7_reward_cycles(
            protocol_data_root, status=RewardCycleStatus.FINALIZED
        )
        if cycle.task_reward_budget_id == task_reward_budget_id
        and cycle.reward_cycle_id != exclude_cycle_id
    ]
    if len(matches) > 1:
        raise Week7RewardCycleStorageError(
            "TaskRewardBudget has conflicting finalized Week 7 cycles"
        )
    return matches[0] if matches else None


def save_week7_reward_event_exclusively(
    protocol_data_root: Path, event: Week7TaskRewardEvent
) -> tuple[Week7TaskRewardEvent, bool]:
    validated = Week7TaskRewardEvent.model_validate(event.model_dump())
    path = get_week7_reward_event_path(
        protocol_data_root, validated.reward_cycle_id, validated.reward_event_id
    )
    try:
        created = atomic_create_json(
            path, validated, temporary_prefix=".week7-reward-event-"
        )
    except OSError as exc:
        raise Week7RewardCycleStorageError(
            "Unable to persist Week 7 RewardEvent"
        ) from exc
    if created:
        return validated, True
    stored = load_week7_reward_event(
        protocol_data_root, validated.reward_cycle_id, validated.reward_event_id
    )
    if stored is None or _event_identity(stored) != _event_identity(validated):
        raise Week7RewardEventConflictError(
            "A conflicting deterministic Week 7 RewardEvent already exists"
        )
    return stored, False


def load_week7_reward_event(
    protocol_data_root: Path, reward_cycle_id: str, reward_event_id: str
) -> Week7TaskRewardEvent | None:
    path = get_week7_reward_event_path(
        protocol_data_root, reward_cycle_id, reward_event_id
    )
    if not path.exists():
        return None
    try:
        event = Week7TaskRewardEvent.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise Week7RewardCycleStorageError(
            "Stored Week 7 RewardEvent is malformed"
        ) from exc
    if event.reward_cycle_id != reward_cycle_id or event.reward_event_id != reward_event_id:
        raise Week7RewardCycleStorageError(
            "Stored Week 7 RewardEvent identity does not match path"
        )
    return event


def list_week7_reward_events(
    protocol_data_root: Path,
    *,
    reward_cycle_id: str | None = None,
    task_reward_budget_id: str | None = None,
    project_id: str | None = None,
    routing_id: str | None = None,
    operator_id: str | None = None,
    node_id: str | None = None,
    submission_id: str | None = None,
    finding_cluster_id: str | None = None,
) -> list[Week7TaskRewardEvent]:
    root = get_week7_reward_events_root(protocol_data_root)
    if not root.exists():
        return []
    events = []
    paths = (
        sorted((root / reward_cycle_id).glob(f"*{EVENT_SUFFIX}"))
        if reward_cycle_id is not None
        else sorted(root.glob(f"*/*{EVENT_SUFFIX}"))
    )
    for path in paths:
        event = load_week7_reward_event(
            protocol_data_root, path.parent.name, path.stem
        )
        if event is None:
            continue
        filters = (
            (task_reward_budget_id, event.task_reward_budget_id),
            (project_id, event.project_id),
            (routing_id, event.routing_id),
            (operator_id, event.operator_id),
            (node_id, event.node_id),
            (submission_id, event.submission_id),
            (finding_cluster_id, event.finding_cluster_id),
        )
        if any(expected is not None and expected != actual for expected, actual in filters):
            continue
        events.append(event)
    return sorted(
        events,
        key=lambda item: (
            item.finding_cluster_id,
            item.quality_rank,
            item.operator_id,
            item.reward_event_id,
        ),
    )


def list_week7_reward_events_response(
    protocol_data_root: Path, **filters: Any
) -> Week7TaskRewardEventListResponse:
    events = list_week7_reward_events(protocol_data_root, **filters)
    return Week7TaskRewardEventListResponse(total=len(events), events=events)


def get_week7_reward_history(
    protocol_data_root: Path,
    subject_type: Week7RewardHistorySubject,
    subject_id: str,
) -> Week7TaskRewardHistorySummary:
    _validate_identifier(subject_id, "reward history subject")
    filter_name = {
        Week7RewardHistorySubject.OPERATOR: "operator_id",
        Week7RewardHistorySubject.NODE: "node_id",
        Week7RewardHistorySubject.SUBMISSION: "submission_id",
        Week7RewardHistorySubject.FINDING_CLUSTER: "finding_cluster_id",
    }[subject_type]
    events = list_week7_reward_events(
        protocol_data_root, **{filter_name: subject_id}
    )
    return Week7TaskRewardHistorySummary(
        subject_type=subject_type,
        subject_id=subject_id,
        total_reward_events=len(events),
        total_client_task_reward_points=sum(
            (event.total_reward_points for event in events), Decimal("0")
        ),
        rewarded_cluster_count=len(
            {event.finding_cluster_id for event in events}
        ),
        chief_finder_count=sum(event.chief_finder for event in events),
        events=events,
    )


def verify_week7_task_reward_cycle(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
) -> Week7TaskRewardCycleVerification:
    """Audit a cycle and its ledger without recalculating or writing protocol state."""
    cycle = _load_required_cycle(protocol_data_root, project_id, reward_cycle_id)
    errors: list[str] = []

    def record(message: str) -> None:
        if message not in errors:
            errors.append(message)

    budget_conserved = (
        cycle.miner_pool_points
        + cycle.validator_pool_points
        + cycle.protocol_pool_points
        == cycle.total_budget_points
        and cycle.distributed_miner_points + cycle.undistributed_miner_points
        == cycle.miner_pool_points
    )
    if not budget_conserved:
        record("Cycle budget or miner-pool totals do not conserve protocol points.")

    source_match: bool | None = None
    calculation_match: bool | None = None
    cluster_conserved = cycle.status == RewardCycleStatus.DRAFT
    reporter_conserved = cycle.status == RewardCycleStatus.DRAFT
    immutable_sources = True
    day4: TaskFindingRewardCalculation | None = None
    day5: TaskOperatorRewardCalculation | None = None
    routing = None
    budget = None

    try:
        routing, budget = _load_authoritative_sources(protocol_data_root, cycle)
    except Week7RewardCycleError as exc:
        immutable_sources = False
        source_match = False
        record(str(exc))

    if cycle.status != RewardCycleStatus.DRAFT:
        try:
            day4 = load_task_finding_calculation(
                protocol_data_root,
                cycle.project_id,
                cycle.routing_id,
                cycle.finding_calculation_id,
            )
            day5 = load_task_operator_calculation(
                protocol_data_root,
                cycle.project_id,
                cycle.routing_id,
                cycle.operator_calculation_id,
            )
        except (ValueError, OSError) as exc:
            immutable_sources = False
            record(f"Referenced calculation could not be loaded: {exc}")
        if day4 is None or day5 is None:
            immutable_sources = False
            record("Referenced Day 4 or Day 5 calculation is missing.")
        else:
            if (
                day4.source_fingerprint
                != cycle.finding_calculation_source_fingerprint
                or day5.source_fingerprint
                != cycle.operator_calculation_source_fingerprint
            ):
                immutable_sources = False
                record("Cycle calculation references do not match stored artifact fingerprints.")
            try:
                _validate_pipeline_conservation(cycle, day4, day5)
                cluster_conserved = (
                    day4.distributed_cluster_points
                    + day4.undistributed_cluster_points
                    == cycle.miner_pool_points
                )
                reporter_conserved = (
                    day5.distributed_operator_points
                    + day5.undistributed_cluster_points
                    == day4.distributed_cluster_points
                    and day5.distributed_operator_points
                    == cycle.distributed_miner_points
                )
            except Week7RewardCycleConservationError as exc:
                cluster_conserved = False
                reporter_conserved = False
                record(str(exc))
            if routing is not None and budget is not None:
                computed = protocol_fingerprint(
                    _calculation_payload(cycle, routing, budget, day4, day5)
                )
                calculation_match = computed == cycle.calculation_fingerprint
                if not calculation_match:
                    record("Stored calculation fingerprint does not match referenced artifacts.")
                try:
                    source_match = _verify_current_cycle_sources(
                        protocol_data_root,
                        cycle,
                        routing,
                        budget,
                        day4,
                        day5,
                        record,
                    )
                except (ValueError, OSError) as exc:
                    source_match = False
                    immutable_sources = False
                    record(f"Current economic source verification failed: {exc}")

    expected_events: list[Week7TaskRewardEvent] = []
    expected_total = Decimal("0.000000")
    if day4 is not None and day5 is not None and cycle.calculation_fingerprint:
        expected_finalization = _finalization_fingerprint(
            cycle, day4, day5, cycle.calculation_fingerprint
        )
        if (
            cycle.status == RewardCycleStatus.FINALIZED
            and cycle.finalization_source_fingerprint != expected_finalization
        ):
            record("Finalization fingerprint does not match the calculated snapshot.")
        expected_events = _build_expected_events(
            cycle, day4, day5, expected_finalization
        )
        expected_total = sum(
            (event.total_reward_points for event in expected_events), Decimal("0")
        )

    stored_events: list[Week7TaskRewardEvent] = []
    ledger_readable = True
    try:
        stored_events = list_week7_reward_events(
            protocol_data_root, reward_cycle_id=cycle.reward_cycle_id
        )
    except Week7RewardCycleError as exc:
        ledger_readable = False
        record(f"RewardEvent ledger could not be read: {exc}")
    stored_total = sum(
        (event.total_reward_points for event in stored_events), Decimal("0")
    )
    economic_keys = [
        (event.finding_cluster_id, event.operator_id) for event in stored_events
    ]
    duplicates = len(economic_keys) != len(set(economic_keys))
    if duplicates:
        record("Duplicate cluster/operator RewardEvents were found.")

    expected_by_id = {event.reward_event_id: event for event in expected_events}
    stored_by_id = {event.reward_event_id: event for event in stored_events}
    identities_match = ledger_readable
    for event_id in sorted(set(expected_by_id).intersection(stored_by_id)):
        if _event_identity(expected_by_id[event_id]) != _event_identity(
            stored_by_id[event_id]
        ):
            identities_match = False
            record(f"RewardEvent {event_id} conflicts with the calculated allocation.")
    extra_ids = sorted(set(stored_by_id) - set(expected_by_id))
    if extra_ids:
        identities_match = False
        record("Unexpected RewardEvents exist for this cycle.")
    event_set_complete = (
        ledger_readable
        and identities_match
        and not duplicates
        and set(stored_by_id) == set(expected_by_id)
        and stored_total == expected_total
    )

    if cycle.status == RewardCycleStatus.FINALIZED:
        if sorted(stored_by_id) != cycle.reward_event_ids:
            event_set_complete = False
            record("Finalized cycle RewardEvent references do not match the ledger.")
        if stored_total != cycle.distributed_miner_points:
            event_set_complete = False
            record("Finalized RewardEvent total does not match distributed miner points.")

    partial_matching = (
        cycle.status == RewardCycleStatus.CALCULATED
        and bool(stored_events)
        and identities_match
        and not duplicates
        and set(stored_by_id).issubset(expected_by_id)
    )
    if not ledger_readable or (
        (day4 is None or day5 is None)
        and cycle.status != RewardCycleStatus.DRAFT
    ):
        verification_status = Week7RewardVerificationStatus.CORRUPT_REFERENCES
    elif cycle.status == RewardCycleStatus.DRAFT:
        verification_status = Week7RewardVerificationStatus.DRAFT
        if stored_events:
            verification_status = Week7RewardVerificationStatus.CORRUPT_REFERENCES
            record("Draft cycle unexpectedly has RewardEvents.")
    elif cycle.status == RewardCycleStatus.CALCULATED:
        if source_match is False or calculation_match is False:
            verification_status = Week7RewardVerificationStatus.STALE_CALCULATION
        elif event_set_complete and expected_events:
            verification_status = (
                Week7RewardVerificationStatus.EVENT_SET_COMPLETE_STATUS_NOT_FINALIZED
            )
            record("Complete RewardEvent set exists but cycle status is not finalized.")
        elif partial_matching:
            verification_status = Week7RewardVerificationStatus.PARTIAL_EVENT_PUBLICATION
            record("A matching partial RewardEvent set is awaiting finalization retry.")
        elif not stored_events:
            verification_status = Week7RewardVerificationStatus.CALCULATED_NOT_FINALIZED
        else:
            verification_status = Week7RewardVerificationStatus.CORRUPT_REFERENCES
    else:
        verification_status = (
            Week7RewardVerificationStatus.CLEAN_FINALIZED
            if event_set_complete
            and source_match is not False
            and calculation_match is not False
            and immutable_sources
            and budget_conserved
            and cluster_conserved
            and reporter_conserved
            else Week7RewardVerificationStatus.FINALIZED_EVENT_MISMATCH
        )

    ok = verification_status in {
        Week7RewardVerificationStatus.DRAFT,
        Week7RewardVerificationStatus.CALCULATED_NOT_FINALIZED,
        Week7RewardVerificationStatus.CLEAN_FINALIZED,
    }
    safe_retry = (
        cycle.status == RewardCycleStatus.CALCULATED
        and source_match is not False
        and calculation_match is not False
        and immutable_sources
        and budget_conserved
        and cluster_conserved
        and reporter_conserved
        and identities_match
        and not duplicates
        and set(stored_by_id).issubset(expected_by_id)
    )
    return Week7TaskRewardCycleVerification(
        reward_cycle_id=cycle.reward_cycle_id,
        project_id=cycle.project_id,
        routing_id=cycle.routing_id,
        lifecycle_status=cycle.status,
        verification_status=verification_status,
        ok=ok,
        source_fingerprint_match=source_match,
        calculation_fingerprint_match=calculation_match,
        budget_conserved=budget_conserved,
        cluster_rewards_conserved=cluster_conserved,
        reporter_rewards_conserved=reporter_conserved,
        event_set_complete=event_set_complete,
        duplicate_events_found=duplicates,
        immutable_source_consistent=immutable_sources,
        safe_retry_finalize=safe_retry,
        expected_event_count=len(expected_events),
        stored_event_count=len(stored_events),
        expected_event_total=expected_total,
        stored_event_total=stored_total,
        errors=errors,
    )


def _verify_current_cycle_sources(
    protocol_data_root: Path,
    cycle: Week7TaskRewardCycle,
    routing,
    budget,
    day4: TaskFindingRewardCalculation,
    day5: TaskOperatorRewardCalculation,
    record,
) -> bool:
    """Rebuild payout-relevant source relations in memory; never persist."""
    matches = True
    snapshot = cycle.policy_snapshot or _policy_snapshot()
    if (
        cycle.status != RewardCycleStatus.FINALIZED
        and cycle.policy_snapshot is not None
        and snapshot != _policy_snapshot()
    ):
        record("Current economic policy bundle differs from the cycle snapshot.")
        matches = False

    clusters = list_task_finding_clusters(
        protocol_data_root, cycle.project_id, cycle.routing_id
    )
    cluster_by_id = {
        cluster.finding_cluster_id: cluster
        for cluster in clusters
        if is_finding_cluster_reward_eligible(cluster)
    }
    allocation_by_id = {
        allocation.finding_cluster_id: allocation
        for allocation in day4.cluster_allocations
    }
    if set(cluster_by_id) != set(allocation_by_id):
        record("Current FindingCluster set differs from the Day 4 snapshot.")
        matches = False
    for cluster_id in sorted(set(cluster_by_id).intersection(allocation_by_id)):
        cluster = cluster_by_id[cluster_id]
        allocation = allocation_by_id[cluster_id]
        if (
            cluster.status != FindingClusterStatus.FINALIZED
            or cluster.source_fingerprint != allocation.cluster_source_fingerprint
            or cluster.category != allocation.category
            or cluster.final_severity != allocation.final_severity
        ):
            record(f"FindingCluster {cluster_id} changed after calculation.")
            matches = False
        operator_ids = set()
        for member in cluster.members:
            node = load_node(protocol_data_root, member.node_id)
            if node is None or node.operator_id != member.operator_id:
                record(f"FindingCluster {cluster_id} has stale operator attribution.")
                matches = False
                continue
            operator_ids.add(node.operator_id)
        if len(operator_ids) != allocation.distinct_operator_count:
            record(f"FindingCluster {cluster_id} operator count changed.")
            matches = False
        current_value = value_finding_cluster(
            cluster, snapshot.task_finding_reward
        )
        if (
            current_value.severity_weight != allocation.severity_weight
            or current_value.uniqueness != allocation.uniqueness
            or current_value.finding_score != allocation.finding_score
        ):
            record(f"FindingCluster {cluster_id} value no longer matches Day 4.")
            matches = False

    finding_config = TaskFindingRewardConfig(
        severity_weights=day4.severity_weights,
        uniqueness=day4.uniqueness_config,
        default_allocation_scope=day4.allocation_scope,
    )
    day4_payload = build_task_finding_source_payload(
        policy_version=day4.policy_version,
        configuration_version=day4.configuration_version,
        budget=budget,
        allocation_scope=day4.allocation_scope,
        config=finding_config,
        category_weights=day4.category_weights,
        category_pools=day4.category_pool_allocations,
        allocations=day4.cluster_allocations,
    )
    if protocol_fingerprint(day4_payload) != day4.source_fingerprint:
        record("Day 4 source fingerprint does not match its stored allocation snapshot.")
        matches = False

    assignments = {
        assignment.assignment_id: assignment
        for result in routing.results
        for assignment in result.assignments
    }
    current_payouts = []
    stored_payout_by_id = {
        payout.finding_cluster_id: payout for payout in day5.cluster_payouts
    }
    for allocation in day4.cluster_allocations:
        cluster = cluster_by_id.get(allocation.finding_cluster_id)
        if cluster is None:
            continue
        reports = []
        exclusions = []
        for member in cluster.members:
            report, exclusion = _resolve_member_report(
                protocol_data_root,
                cycle.project_id,
                cycle.routing_id,
                cluster,
                member,
                assignments,
                snapshot.report_quality_policy_version,
            )
            if report is not None:
                reports.append(report)
            elif exclusion is not None:
                exclusions.append(exclusion)
        payout = calculate_cluster_operator_payout(
            allocation,
            reports,
            exclusions,
            snapshot.task_operator_reward,
            policy_version=snapshot.task_operator_policy_version,
        )
        current_payouts.append(payout)
        if stored_payout_by_id.get(allocation.finding_cluster_id) != payout:
            record(
                f"Reporter allocation for {allocation.finding_cluster_id} changed after calculation."
            )
            matches = False
    current_payouts.sort(key=lambda item: item.finding_cluster_id)
    summaries = _operator_summaries(current_payouts)
    source_total = sum(
        (item.cluster_reward_points for item in current_payouts), Decimal("0")
    )
    distributed = sum(
        (item.distributed_points for item in current_payouts), Decimal("0")
    )
    day5_payload = {
        "calculation_version": day5.calculation_version,
        "policy_version": day5.policy_version,
        "configuration_version": day5.configuration_version,
        "project_id": cycle.project_id,
        "routing_id": cycle.routing_id,
        "routing_source_fingerprint": routing.source_fingerprint,
        "task_reward_budget_id": day4.task_reward_budget_id,
        "day4_calculation_id": day4.calculation_id,
        "day4_source_fingerprint": day4.source_fingerprint,
        "protocol_quantum": Decimal("0.000001"),
        "operator_reward_config": snapshot.task_operator_reward,
        "cluster_payouts": current_payouts,
        "operator_summaries": summaries,
        "source_cluster_reward_points": source_total,
        "distributed_operator_points": distributed,
        "undistributed_cluster_points": source_total - distributed,
    }
    if protocol_fingerprint(day5_payload) != day5.source_fingerprint:
        record("Day 5 source fingerprint does not match current reporter inputs.")
        matches = False
    return matches


def _calculate_current_snapshot(
    protocol_data_root: Path, cycle: Week7TaskRewardCycle
) -> tuple[TaskFindingRewardCalculation, TaskOperatorRewardCalculation, str]:
    policy_snapshot = cycle.policy_snapshot or _policy_snapshot()
    if cycle.policy_snapshot is not None and policy_snapshot != _policy_snapshot():
        raise Week7RewardCycleSourceChangedError(
            "Week 7 economic policy bundle changed after cycle creation"
        )
    routing, budget = _load_authoritative_sources(protocol_data_root, cycle)
    day4, _ = calculate_task_finding_rewards(
        protocol_data_root,
        cycle.project_id,
        cycle.routing_id,
        TaskFindingRewardCalculationRequest(
            task_reward_budget_id=cycle.task_reward_budget_id,
            allocation_scope=cycle.allocation_scope,
            category_weights=(cycle.category_weights or None),
        ),
        configured_policy=policy_snapshot.task_finding_reward,
        policy_version=policy_snapshot.task_finding_policy_version,
        configuration_version=policy_snapshot.task_finding_configuration_version,
    )
    day5, _ = calculate_task_operator_rewards(
        protocol_data_root,
        cycle.project_id,
        cycle.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=day4.calculation_id
        ),
        configured_policy=policy_snapshot.task_operator_reward,
        policy_version=policy_snapshot.task_operator_policy_version,
        configuration_version=policy_snapshot.task_operator_configuration_version,
    )
    payload = _calculation_payload(cycle, routing, budget, day4, day5)
    return day4, day5, protocol_fingerprint(payload)


def _load_authoritative_sources(protocol_data_root: Path, cycle: Week7TaskRewardCycle):
    routing = load_routing_record(
        protocol_data_root, cycle.project_id, cycle.routing_id
    )
    if routing is None:
        raise Week7RewardCycleNotFoundError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise Week7RewardCycleSourceChangedError(
            "Routing is no longer finalized"
        )
    budget = load_task_reward_budget(
        protocol_data_root, cycle.project_id, cycle.task_reward_budget_id
    )
    if budget is None:
        raise Week7RewardCycleNotFoundError("TaskRewardBudget not found")
    if (
        budget.project_id != cycle.project_id
        or budget.routing_id != cycle.routing_id
        or budget.reward_domain != RewardDomain.CLIENT_TASK
        or budget.status != TaskRewardBudgetStatus.FINALIZED
    ):
        raise Week7RewardCycleSourceChangedError(
            "TaskRewardBudget is no longer an eligible finalized client-task budget"
        )
    if (
        routing.source_fingerprint != cycle.routing_source_fingerprint
        or budget.source_fingerprint != cycle.task_reward_budget_source_fingerprint
        or budget.routing_source_fingerprint != routing.source_fingerprint
        or budget.miner_pool_points != cycle.miner_pool_points
        or budget.validator_pool_points != cycle.validator_pool_points
        or budget.protocol_pool_points != cycle.protocol_pool_points
        or budget.total_budget_points != cycle.total_budget_points
    ):
        raise Week7RewardCycleSourceChangedError(
            "Budget or routing source changed after Week 7 cycle creation"
        )
    return routing, budget


def _policy_snapshot() -> Week7RewardPolicySnapshot:
    settings = get_settings()
    return Week7RewardPolicySnapshot(
        week7_cycle_policy_version=settings.week7_reward_cycle_policy_version,
        week7_cycle_configuration_version=settings.week7_reward_cycle_configuration_version,
        task_budget_configuration_version=settings.task_reward_configuration_version,
        task_reward_pool=settings.task_reward_pool,
        report_quality_policy_version=settings.report_quality_policy_version,
        report_quality_configuration_version=settings.report_quality_configuration_version,
        report_quality=settings.report_quality,
        task_finding_policy_version=settings.task_finding_policy_version,
        task_finding_configuration_version=settings.task_finding_configuration_version,
        task_finding_reward=settings.task_finding_reward,
        task_operator_policy_version=settings.task_operator_policy_version,
        task_operator_configuration_version=settings.task_operator_configuration_version,
        task_operator_reward=settings.task_operator_reward,
        protocol_quantum=Decimal("0.000001"),
    )


def _calculation_payload(cycle, routing, budget, day4, day5) -> dict[str, Any]:
    return {
        "cycle_version": WEEK7_REWARD_CYCLE_VERSION,
        "policy_version": cycle.policy_version,
        "configuration_version": cycle.configuration_version,
        "request_fingerprint": cycle.request_fingerprint,
        "policy_snapshot": cycle.policy_snapshot or _policy_snapshot(),
        "routing": {
            "routing_id": routing.routing_id,
            "source_fingerprint": routing.source_fingerprint,
        },
        "budget": {
            "task_reward_budget_id": budget.task_reward_budget_id,
            "source_fingerprint": budget.source_fingerprint,
            "miner_pool_points": budget.miner_pool_points,
            "validator_pool_points": budget.validator_pool_points,
            "protocol_pool_points": budget.protocol_pool_points,
        },
        "day4": day4.model_dump(
            exclude={
                "created_at",
                "calculated_at",
                "status",
                "supersedes_calculation_id",
                "superseded_by_calculation_id",
            }
        ),
        "day5": day5.model_dump(
            exclude={
                "created_at",
                "calculated_at",
                "status",
                "supersedes_calculation_id",
                "superseded_by_calculation_id",
            }
        ),
        "distributed_miner_points": day5.distributed_operator_points,
        "undistributed_miner_points": (
            day4.undistributed_cluster_points
            + day5.undistributed_cluster_points
        ),
    }


def _finalization_fingerprint(cycle, day4, day5, calculation_fingerprint) -> str:
    return protocol_fingerprint(
        {
            "finalization_version": "week7_task_reward_finalization_v1",
            "reward_cycle_id": cycle.reward_cycle_id,
            "calculation_fingerprint": calculation_fingerprint,
            "task_reward_budget_source_fingerprint": cycle.task_reward_budget_source_fingerprint,
            "routing_source_fingerprint": cycle.routing_source_fingerprint,
            "day4_source_fingerprint": day4.source_fingerprint,
            "day5_source_fingerprint": day5.source_fingerprint,
            "policy_snapshot": cycle.policy_snapshot or _policy_snapshot(),
        }
    )


def _build_expected_events(
    cycle: Week7TaskRewardCycle,
    day4: TaskFindingRewardCalculation,
    day5: TaskOperatorRewardCalculation,
    finalization_fingerprint: str,
) -> list[Week7TaskRewardEvent]:
    day4_by_cluster = {
        item.finding_cluster_id: item for item in day4.cluster_allocations
    }
    event_time = _utc_now()
    events = []
    for payout in day5.cluster_payouts:
        cluster = day4_by_cluster[payout.finding_cluster_id]
        for allocation in payout.operator_allocations:
            if not allocation.rewarded or allocation.total_reward_points <= 0:
                continue
            identity = {
                "event_version": WEEK7_REWARD_EVENT_VERSION,
                "reward_cycle_id": cycle.reward_cycle_id,
                "finding_cluster_id": allocation.finding_cluster_id,
                "operator_id": allocation.operator_id,
                "rewarded_submission_id": allocation.rewarded_submission_id,
                "policy_version": cycle.policy_version,
            }
            event_id = f"week7_task_reward_event_{protocol_fingerprint(identity)}"
            economic_payload = {
                **identity,
                "cycle_finalization_fingerprint": finalization_fingerprint,
                "day4_allocation_source_fingerprint": cluster.source_fingerprint,
                "day5_payout_source_fingerprint": payout.source_fingerprint,
                "node_id": allocation.rewarded_node_id,
                "quality_assessment_id": allocation.quality_assessment_id,
                "quality_score": allocation.quality_score,
                "quality_weight": allocation.quality_weight,
                "quality_rank": allocation.quality_rank,
                "chief_finder": allocation.chief_finder,
                "chief_qualifying_submission_id": allocation.chief_qualifying_submission_id,
                "quality_reward_points": allocation.quality_reward_points,
                "chief_bonus_points": allocation.chief_bonus_points,
                "total_reward_points": allocation.total_reward_points,
            }
            events.append(
                Week7TaskRewardEvent(
                    reward_event_id=event_id,
                    reward_cycle_id=cycle.reward_cycle_id,
                    project_id=cycle.project_id,
                    routing_id=cycle.routing_id,
                    task_reward_budget_id=cycle.task_reward_budget_id,
                    finding_cluster_id=cluster.finding_cluster_id,
                    category=cluster.category,
                    final_severity=cluster.final_severity,
                    severity_weight=cluster.severity_weight,
                    distinct_operator_count=cluster.distinct_operator_count,
                    uniqueness=cluster.uniqueness,
                    finding_score=cluster.finding_score,
                    finding_cluster_reward_points=cluster.cluster_reward_points,
                    operator_id=allocation.operator_id,
                    node_id=allocation.rewarded_node_id,
                    submission_id=allocation.rewarded_submission_id,
                    quality_assessment_id=allocation.quality_assessment_id,
                    quality_score=allocation.quality_score,
                    quality_weight=allocation.quality_weight,
                    quality_rank=allocation.quality_rank,
                    chief_finder=allocation.chief_finder,
                    chief_qualifying_submission_id=allocation.chief_qualifying_submission_id,
                    quality_reward_points=allocation.quality_reward_points,
                    chief_bonus_points=allocation.chief_bonus_points,
                    total_reward_points=allocation.total_reward_points,
                    policy_version=cycle.policy_version,
                    cycle_finalization_fingerprint=finalization_fingerprint,
                    source_fingerprint=protocol_fingerprint(economic_payload),
                    created_at=event_time,
                    finalized_at=event_time,
                )
            )
    return sorted(
        events,
        key=lambda item: (
            item.finding_cluster_id,
            item.quality_rank,
            item.operator_id,
            item.reward_event_id,
        ),
    )


def _validate_pipeline_conservation(cycle, day4, day5) -> None:
    if day4.task_reward_budget_id != cycle.task_reward_budget_id:
        raise Week7RewardCycleConservationError(
            "Day 4 calculation references another TaskRewardBudget"
        )
    if day5.day4_calculation_id != day4.calculation_id:
        raise Week7RewardCycleConservationError(
            "Day 5 calculation does not consume the current Day 4 calculation"
        )
    if day4.distributed_cluster_points + day4.undistributed_cluster_points != cycle.miner_pool_points:
        raise Week7RewardCycleConservationError("Day 4 does not conserve miner pool")
    if day5.source_cluster_reward_points != day4.distributed_cluster_points:
        raise Week7RewardCycleConservationError(
            "Day 5 source total does not match Day 4 distributed clusters"
        )
    if day5.distributed_operator_points + day5.undistributed_cluster_points != day4.distributed_cluster_points:
        raise Week7RewardCycleConservationError(
            "Day 5 does not conserve allocated cluster rewards"
        )
    if (
        day5.distributed_operator_points
        + day5.undistributed_cluster_points
        + day4.undistributed_cluster_points
        != cycle.miner_pool_points
    ):
        raise Week7RewardCycleConservationError(
            "Integrated Week 7 calculation does not conserve miner pool"
        )


def _validate_event_set(cycle, day5, events) -> None:
    expected_positive = _positive_allocations(day5)
    if len(events) != len(expected_positive):
        raise Week7RewardCycleConservationError(
            "RewardEvent count does not match positive Day 5 allocations"
        )
    if sum((event.total_reward_points for event in events), Decimal("0")) != cycle.distributed_miner_points:
        raise Week7RewardCycleConservationError(
            "Expected RewardEvents do not equal distributed miner points"
        )
    for payout in day5.cluster_payouts:
        cluster_events = [
            event for event in events if event.finding_cluster_id == payout.finding_cluster_id
        ]
        if len(cluster_events) > payout.top_k_limit:
            raise Week7RewardCycleConservationError(
                "A FindingCluster exceeds its Top-K RewardEvent limit"
            )
        if len({event.operator_id for event in cluster_events}) != len(cluster_events):
            raise Week7RewardCycleConservationError(
                "An operator occupies multiple RewardEvents in one FindingCluster"
            )
        if len({event.submission_id for event in cluster_events}) != len(cluster_events):
            raise Week7RewardCycleConservationError(
                "A submission occupies multiple RewardEvents in one FindingCluster"
            )
        if sum(event.chief_finder for event in cluster_events) > 1:
            raise Week7RewardCycleConservationError(
                "A FindingCluster cannot have more than one Chief Finder"
            )


def _positive_allocations(day5: TaskOperatorRewardCalculation):
    return [
        allocation
        for payout in day5.cluster_payouts
        for allocation in payout.operator_allocations
        if allocation.rewarded and allocation.total_reward_points > 0
    ]


def _event_identity(event: Week7TaskRewardEvent) -> dict[str, Any]:
    return event.model_dump(exclude={"created_at", "finalized_at"})


def _immutable_request_payload(cycle: Week7TaskRewardCycle) -> dict[str, Any]:
    return {
        "reward_cycle_id": cycle.reward_cycle_id,
        "cycle_version": cycle.cycle_version,
        "policy_version": cycle.policy_version,
        "configuration_version": cycle.configuration_version,
        "policy_snapshot": cycle.policy_snapshot,
        "reward_domain": cycle.reward_domain,
        "reward_unit": cycle.reward_unit,
        "project_id": cycle.project_id,
        "routing_id": cycle.routing_id,
        "routing_source_fingerprint": cycle.routing_source_fingerprint,
        "task_reward_budget_id": cycle.task_reward_budget_id,
        "task_reward_budget_source_fingerprint": cycle.task_reward_budget_source_fingerprint,
        "allocation_scope": cycle.allocation_scope,
        "category_weights": cycle.category_weights,
        "total_budget_points": cycle.total_budget_points,
        "miner_pool_points": cycle.miner_pool_points,
        "validator_pool_points": cycle.validator_pool_points,
        "protocol_pool_points": cycle.protocol_pool_points,
        "request_fingerprint": cycle.request_fingerprint,
        "description": cycle.description,
    }


def _load_required_cycle(
    protocol_data_root: Path, project_id: str, reward_cycle_id: str
) -> Week7TaskRewardCycle:
    cycle = load_week7_reward_cycle(
        protocol_data_root, project_id, reward_cycle_id
    )
    if cycle is None:
        raise Week7RewardCycleNotFoundError("Week 7 reward cycle not found")
    return cycle


def _read_cycle(path: Path) -> Week7TaskRewardCycle:
    if not path.is_file():
        raise Week7RewardCycleStorageError("Week 7 reward-cycle path is not a file")
    try:
        cycle = Week7TaskRewardCycle.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise Week7RewardCycleStorageError(
            "Stored Week 7 reward cycle is malformed"
        ) from exc
    if path.parent.name != cycle.reward_cycle_id or path.parent.parent.name != cycle.routing_id:
        raise Week7RewardCycleStorageError(
            "Stored Week 7 reward-cycle identity does not match path"
        )
    return cycle


def _verify_project_workspace(project_workspace: Path, project_id: str) -> None:
    metadata_path = project_workspace / "metadata.json"
    if not project_workspace.is_dir() or not metadata_path.is_file():
        raise Week7RewardCycleNotFoundError("Project workspace not found")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Week7RewardCycleStorageError("Project metadata is malformed") from exc
    if not isinstance(metadata, dict) or metadata.get("project_id") != project_id:
        raise Week7RewardCycleConflictError(
            "Project workspace does not match requested project"
        )


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise Week7RewardCycleConflictError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise Week7RewardCycleConflictError(
            "Week 7 reward path escapes protocol root"
        ) from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
