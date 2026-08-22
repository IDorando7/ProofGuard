from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.reward import RewardCycleStatus, RewardPoolKind
from app.schemas.routing import RoutingStatus
from app.schemas.subnet_reward import PROTOCOL_POINTS_QUANTUM
from app.schemas.task_reward import TaskRewardBudgetStatus
from app.schemas.validator_attestation import (
    ValidatorAssignmentRole,
    ValidatorAssignmentStatus,
)
from app.schemas.validator_committee import ValidatorCommitteeStatus
from app.schemas.validator_consensus import (
    ValidationConsensusLifecycle,
    ValidationConsensusOutcome,
    ValidationDisputeStatus,
    ValidationRoundStatus,
)
from app.schemas.validator_performance import ValidationQualityAssessmentStatus
from app.schemas.validator_reward import (
    VALIDATOR_REWARD_CYCLE_VERSION,
    VALIDATOR_REWARD_EVENT_VERSION,
    VALIDATOR_REWARD_POLICY_VERSION,
    RewardPoolConsumption,
    ValidatorRewardAllocation,
    ValidatorRewardCycle,
    ValidatorRewardCycleCreateRequest,
    ValidatorRewardCycleVerification,
    ValidatorRewardEvent,
    ValidatorRewardPolicyV1,
    ValidatorRewardProcessingStatus,
    ValidatorRewardUndistributedReason,
    ValidatorRewardVerificationStatus,
)
from app.services.node_registry_service import load_node
from app.services.finding_cluster_service import list_task_finding_clusters
from app.services.reward_pool_consumption_service import (
    RewardPoolConsumptionError,
    consume_reward_pool,
    load_reward_pool_consumption,
)
from app.services.subnet_reward_allocation_service import (
    WeightedRewardItem,
    allocate_proportional_rewards,
)
from app.services.subnet_router_service import load_routing_record
from app.services.task_reward_budget_service import load_task_reward_budget
from app.services.validation_attestation_service import list_validation_attestations
from app.services.validation_quality_assessment_service import (
    list_validation_quality_assessments,
)
from app.services.validator_assignment_service import load_validator_assignment
from app.services.validator_committee_service import (
    list_validator_committees,
    load_validator_committee,
)
from app.services.validator_consensus_service import (
    list_validation_consensus,
    list_validation_disputes,
    load_validation_round,
)
from app.services.validator_protocol_compliance_service import (
    list_validator_protocol_violations,
)
from app.services.validator_reproduction_service import (
    list_validator_reproduction_records,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


ZERO = Decimal("0.000000")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class ValidatorRewardCycleError(ValueError):
    pass


class ValidatorRewardCycleNotFoundError(ValidatorRewardCycleError):
    pass


class ValidatorRewardCycleConflictError(ValidatorRewardCycleError):
    pass


class ValidatorRewardCycleStateError(ValidatorRewardCycleError):
    pass


class ValidatorRewardCycleSourceChangedError(ValidatorRewardCycleError):
    pass


class ValidatorRewardCycleStorageError(ValidatorRewardCycleError):
    pass


def _identifier(value: str, label: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value) or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValidatorRewardCycleConflictError(f"Invalid {label} identifier")
    return value


def calculate_validator_quality_factor(value: Decimal) -> Decimal:
    """Pure v1 quality scaling: exact six-decimal VQ squared."""
    if isinstance(value, float):
        raise ValidatorRewardCycleConflictError("Validation quality must use Decimal")
    try:
        quality = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValidatorRewardCycleConflictError("Validation quality must be Decimal") from exc
    if (
        not quality.is_finite()
        or quality < 0
        or quality > 1
        or quality != quality.quantize(PROTOCOL_POINTS_QUANTUM)
    ):
        raise ValidatorRewardCycleConflictError(
            "Validation quality must be within [0,1] at six decimals"
        )
    return (quality * quality).quantize(PROTOCOL_POINTS_QUANTUM)


def get_validator_reward_cycle_path(
    protocol_data_root: Path, routing_id: str, reward_cycle_id: str
) -> Path:
    _identifier(routing_id, "routing")
    _identifier(reward_cycle_id, "validator reward cycle")
    return (
        protocol_data_root
        / "task-rewards"
        / "validator-cycles"
        / "routing"
        / routing_id
        / reward_cycle_id
        / "cycle.json"
    )


def get_validator_reward_event_path(
    protocol_data_root: Path, reward_cycle_id: str, reward_event_id: str
) -> Path:
    _identifier(reward_cycle_id, "validator reward cycle")
    _identifier(reward_event_id, "validator RewardEvent")
    return (
        protocol_data_root
        / "task-rewards"
        / "events"
        / "validator"
        / reward_cycle_id
        / f"{reward_event_id}.json"
    )


def create_validator_reward_cycle(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    request: ValidatorRewardCycleCreateRequest,
    policy: ValidatorRewardPolicyV1 | None = None,
) -> tuple[ValidatorRewardCycle, bool]:
    policy = policy or ValidatorRewardPolicyV1()
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise ValidatorRewardCycleNotFoundError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise ValidatorRewardCycleStateError("Validator rewards require finalized routing")
    budget = load_task_reward_budget(protocol_data_root, project_id, request.task_reward_budget_id)
    if budget is None:
        raise ValidatorRewardCycleNotFoundError("TaskRewardBudget not found")
    if budget.routing_id != routing_id:
        raise ValidatorRewardCycleConflictError("TaskRewardBudget belongs to another routing")
    if budget.status != TaskRewardBudgetStatus.FINALIZED:
        raise ValidatorRewardCycleStateError("Validator rewards require a finalized TaskRewardBudget")
    if budget.routing_source_fingerprint != routing.source_fingerprint:
        raise ValidatorRewardCycleSourceChangedError("TaskRewardBudget routing source changed")
    request_payload = {
        "cycle_version": VALIDATOR_REWARD_CYCLE_VERSION,
        "policy": policy,
        "project_id": project_id,
        "routing_id": routing_id,
        "routing_source_fingerprint": routing.source_fingerprint,
        "task_reward_budget_id": budget.task_reward_budget_id,
        "task_reward_budget_source_fingerprint": budget.source_fingerprint,
        "pool_kind": RewardPoolKind.VALIDATOR,
        "validator_pool_points": budget.validator_pool_points,
    }
    request_fingerprint = protocol_fingerprint(request_payload)
    cycle_id = "validator_reward_cycle_" + protocol_fingerprint(
        {
            "cycle_version": VALIDATOR_REWARD_CYCLE_VERSION,
            "task_reward_budget_id": budget.task_reward_budget_id,
            "pool_kind": RewardPoolKind.VALIDATOR,
        }
    )[:32]
    existing = find_validator_reward_cycle_by_budget(
        protocol_data_root, budget.task_reward_budget_id
    )
    if existing is not None:
        if existing.request_fingerprint == request_fingerprint:
            return existing, False
        raise ValidatorRewardCycleConflictError(
            "A different validator reward cycle already exists for this TaskRewardBudget"
        )
    consumed = load_reward_pool_consumption(
        protocol_data_root, budget.task_reward_budget_id, RewardPoolKind.VALIDATOR
    )
    if consumed is not None:
        raise ValidatorRewardCycleConflictError("Validator pool is already consumed")
    now = _utc_now()
    cycle = ValidatorRewardCycle(
        reward_cycle_id=cycle_id,
        reward_policy=policy,
        project_id=project_id,
        routing_id=routing_id,
        routing_source_fingerprint=routing.source_fingerprint,
        task_reward_budget_id=budget.task_reward_budget_id,
        task_reward_budget_source_fingerprint=budget.source_fingerprint,
        validator_pool_points=budget.validator_pool_points,
        status=RewardCycleStatus.DRAFT,
        allocations=[],
        authoritative_work_units=0,
        completed_work_units=0,
        resolved_quality_units=0,
        unresolved_units=0,
        distributed_validator_points=ZERO,
        undistributed_validator_points=budget.validator_pool_points,
        reward_event_count=0,
        reward_event_ids=[],
        request_fingerprint=request_fingerprint,
        description=request.description,
        created_at=now,
        updated_at=now,
    )
    cycle_path = get_validator_reward_cycle_path(
        protocol_data_root, routing_id, cycle.reward_cycle_id
    )
    if atomic_create_json(cycle_path, cycle, temporary_prefix=".validator-reward-cycle-create-"):
        return cycle, True
    raced = load_validator_reward_cycle(
        protocol_data_root, project_id, routing_id, cycle.reward_cycle_id
    )
    if raced is None or raced.request_fingerprint != request_fingerprint:
        raise ValidatorRewardCycleConflictError(
            "A different validator reward cycle already exists for this TaskRewardBudget"
        )
    return raced, False


def calculate_validator_reward_cycle(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    reward_cycle_id: str,
) -> tuple[ValidatorRewardCycle, ValidatorRewardProcessingStatus]:
    cycle = _load_required_cycle(protocol_data_root, project_id, routing_id, reward_cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        raise ValidatorRewardCycleStateError("Finalized validator reward cycle is immutable")
    allocations, source_fingerprint, calculation_fingerprint, summary = _calculate_snapshot(
        protocol_data_root, cycle
    )
    if (
        cycle.status == RewardCycleStatus.CALCULATED
        and cycle.source_fingerprint == source_fingerprint
        and cycle.calculation_fingerprint == calculation_fingerprint
    ):
        return cycle, ValidatorRewardProcessingStatus.UNCHANGED
    now = _utc_now()
    updated = cycle.model_copy(
        update={
            "status": RewardCycleStatus.CALCULATED,
            "allocations": allocations,
            **summary,
            "reward_event_count": 0,
            "reward_event_ids": [],
            "source_fingerprint": source_fingerprint,
            "calculation_fingerprint": calculation_fingerprint,
            "calculation_revision": cycle.calculation_revision + 1,
            "calculated_at": now,
            "updated_at": now,
        }
    )
    return _save_cycle(protocol_data_root, updated), ValidatorRewardProcessingStatus.CALCULATED


def finalize_validator_reward_cycle(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    reward_cycle_id: str,
    fail_after_event_writes: int | None = None,
    fail_after_consumption: bool = False,
) -> tuple[ValidatorRewardCycle, ValidatorRewardProcessingStatus, int, int]:
    cycle = _load_required_cycle(protocol_data_root, project_id, routing_id, reward_cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        _verify_finalized_or_raise(protocol_data_root, cycle)
        return cycle, ValidatorRewardProcessingStatus.ALREADY_FINALIZED, 0, len(cycle.reward_event_ids)
    if cycle.status != RewardCycleStatus.CALCULATED:
        raise ValidatorRewardCycleStateError("Validator reward cycle must be calculated before finalization")
    allocations, source_fp, calculation_fp, _ = _calculate_snapshot(protocol_data_root, cycle)
    if source_fp != cycle.source_fingerprint or calculation_fp != cycle.calculation_fingerprint or allocations != cycle.allocations:
        raise ValidatorRewardCycleSourceChangedError("Validator reward source changed after calculation")
    finalization_fp = protocol_fingerprint(
        {
            "cycle_version": cycle.cycle_version,
            "reward_cycle_id": cycle.reward_cycle_id,
            "source_fingerprint": source_fp,
            "calculation_fingerprint": calculation_fp,
            "pool_kind": RewardPoolKind.VALIDATOR,
        }
    )
    events = _expected_events(cycle, finalization_fp)
    created = existing = 0
    for event in events:
        path = get_validator_reward_event_path(
            protocol_data_root, cycle.reward_cycle_id, event.reward_event_id
        )
        if atomic_create_json(path, event, temporary_prefix=".validator-reward-event-"):
            created += 1
        else:
            stored = load_validator_reward_event(
                protocol_data_root, project_id, routing_id, cycle.reward_cycle_id, event.reward_event_id
            )
            if stored is None or _event_comparison_payload(stored) != _event_comparison_payload(event):
                raise ValidatorRewardCycleConflictError("Conflicting validator RewardEvent exists")
            existing += 1
        if fail_after_event_writes is not None and created >= fail_after_event_writes:
            raise RuntimeError("Injected validator RewardEvent publication failure")
    stored_events = list_validator_reward_events(
        protocol_data_root, project_id, routing_id, cycle.reward_cycle_id
    )
    if [item.reward_event_id for item in stored_events] != [item.reward_event_id for item in events]:
        raise ValidatorRewardCycleConflictError("Validator RewardEvent set is incomplete or conflicting")
    try:
        consume_reward_pool(
            protocol_data_root,
            task_reward_budget_id=cycle.task_reward_budget_id,
            pool_kind=RewardPoolKind.VALIDATOR,
            reward_cycle_id=cycle.reward_cycle_id,
            consumed_points=cycle.validator_pool_points,
            finalization_source_fingerprint=finalization_fp,
        )
    except RewardPoolConsumptionError as exc:
        raise ValidatorRewardCycleConflictError(str(exc)) from exc
    if fail_after_consumption:
        raise RuntimeError("Injected validator pool-consumption failure")
    now = _utc_now()
    finalized = cycle.model_copy(
        update={
            "status": RewardCycleStatus.FINALIZED,
            "reward_event_count": len(events),
            "reward_event_ids": [event.reward_event_id for event in events],
            "finalization_source_fingerprint": finalization_fp,
            "finalized_at": now,
            "updated_at": now,
        }
    )
    finalized = _save_cycle(protocol_data_root, finalized)
    _verify_finalized_or_raise(protocol_data_root, finalized)
    return finalized, ValidatorRewardProcessingStatus.FINALIZED, created, existing


def verify_validator_reward_cycle(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    reward_cycle_id: str,
) -> ValidatorRewardCycleVerification:
    cycle = _load_required_cycle(protocol_data_root, project_id, routing_id, reward_cycle_id)
    errors: list[str] = []
    source_match: bool | None = None
    calculation_match: bool | None = None
    closed = False
    try:
        allocations, source_fp, calculation_fp, _ = _calculate_snapshot(protocol_data_root, cycle)
        closed = True
        if cycle.status != RewardCycleStatus.DRAFT:
            source_match = cycle.source_fingerprint == source_fp
            calculation_match = cycle.calculation_fingerprint == calculation_fp and cycle.allocations == allocations
            if not source_match:
                errors.append("source fingerprint mismatch")
            if not calculation_match:
                errors.append("calculation fingerprint mismatch")
    except ValidatorRewardCycleError as exc:
        errors.append(str(exc))
    accounting = (
        cycle.distributed_validator_points + cycle.undistributed_validator_points
        == cycle.validator_pool_points
        and (
            sum((item.base_work_unit_budget for item in cycle.allocations), ZERO)
            == cycle.validator_pool_points
            if cycle.allocations
            else cycle.undistributed_validator_points == cycle.validator_pool_points
        )
        and sum((item.total_reward for item in cycle.allocations), ZERO)
        == cycle.distributed_validator_points
        and (
            sum((item.undistributed_points for item in cycle.allocations), ZERO)
            == cycle.undistributed_validator_points
            if cycle.allocations
            else True
        )
        and all(item.total_reward + item.undistributed_points == item.base_work_unit_budget for item in cycle.allocations)
    )
    if not accounting:
        errors.append("validator pool accounting mismatch")
    finalization_fp = cycle.finalization_source_fingerprint or (
        protocol_fingerprint(
            {
                "cycle_version": cycle.cycle_version,
                "reward_cycle_id": cycle.reward_cycle_id,
                "source_fingerprint": cycle.source_fingerprint,
                "calculation_fingerprint": cycle.calculation_fingerprint,
                "pool_kind": RewardPoolKind.VALIDATOR,
            }
        )
        if cycle.status != RewardCycleStatus.DRAFT
        else None
    )
    expected = _expected_events(cycle, finalization_fp) if finalization_fp else []
    stored = list_validator_reward_events(protocol_data_root, project_id, routing_id, reward_cycle_id)
    stored_event_payloads = [_event_comparison_payload(event) for event in stored]
    expected_event_payloads = [_event_comparison_payload(event) for event in expected]
    event_complete = stored_event_payloads == expected_event_payloads
    valid_event_prefix = stored_event_payloads == expected_event_payloads[: len(stored_event_payloads)]
    if stored and not event_complete:
        errors.append("validator RewardEvent set mismatch")
    consumption = load_reward_pool_consumption(protocol_data_root, cycle.task_reward_budget_id, RewardPoolKind.VALIDATOR)
    consumption_ok = False
    if consumption is not None and finalization_fp is not None:
        consumption_payload = {
            "consumption_version": "reward_pool_consumption_v1",
            "task_reward_budget_id": cycle.task_reward_budget_id,
            "pool_kind": RewardPoolKind.VALIDATOR.value,
            "reward_cycle_id": cycle.reward_cycle_id,
            "consumed_points": cycle.validator_pool_points,
            "finalization_source_fingerprint": finalization_fp,
        }
        expected_consumption_fp = protocol_fingerprint(consumption_payload)
        consumption_ok = (
            consumption.reward_cycle_id == reward_cycle_id
            and consumption.consumed_points == cycle.validator_pool_points
            and consumption.source_fingerprint == expected_consumption_fp
            and consumption.reward_pool_consumption_id
            == "reward_pool_consumption_" + expected_consumption_fp
        )
    if cycle.status == RewardCycleStatus.FINALIZED and not consumption_ok:
        errors.append("validator pool consumption mismatch")
    if cycle.status == RewardCycleStatus.DRAFT:
        status = ValidatorRewardVerificationStatus.DRAFT
    elif source_match is False or calculation_match is False:
        status = ValidatorRewardVerificationStatus.STALE_CALCULATION
    elif cycle.status == RewardCycleStatus.CALCULATED and not stored and consumption is None:
        status = ValidatorRewardVerificationStatus.CALCULATED_NOT_FINALIZED
    elif not event_complete:
        status = ValidatorRewardVerificationStatus.PARTIAL_EVENT_PUBLICATION if valid_event_prefix and len(stored) < len(expected) else ValidatorRewardVerificationStatus.FINALIZED_EVENT_MISMATCH
    elif cycle.status != RewardCycleStatus.FINALIZED and (stored or consumption is not None):
        status = ValidatorRewardVerificationStatus.EVENT_SET_COMPLETE_STATUS_NOT_FINALIZED
    elif cycle.status == RewardCycleStatus.FINALIZED and not errors:
        status = ValidatorRewardVerificationStatus.CLEAN_FINALIZED
    elif cycle.status == RewardCycleStatus.CALCULATED:
        status = ValidatorRewardVerificationStatus.CALCULATED_NOT_FINALIZED
    else:
        status = ValidatorRewardVerificationStatus.CORRUPT_REFERENCES
    ok = status in {
        ValidatorRewardVerificationStatus.DRAFT,
        ValidatorRewardVerificationStatus.CALCULATED_NOT_FINALIZED,
        ValidatorRewardVerificationStatus.CLEAN_FINALIZED,
    } and not errors
    return ValidatorRewardCycleVerification(
        reward_cycle_id=cycle.reward_cycle_id,
        verification_status=status,
        ok=ok,
        source_fingerprint_match=source_match,
        calculation_fingerprint_match=calculation_match,
        accounting_conserved=accounting,
        work_snapshot_closed=closed,
        event_set_complete=event_complete,
        pool_consumption_consistent=consumption_ok if cycle.status == RewardCycleStatus.FINALIZED else consumption is None or consumption.reward_cycle_id == reward_cycle_id,
        safe_retry_finalize=cycle.status == RewardCycleStatus.CALCULATED and source_match is not False and calculation_match is not False and (not stored or event_complete or (valid_event_prefix and len(stored) < len(expected))),
        expected_event_count=len(expected),
        stored_event_count=len(stored),
        errors=list(dict.fromkeys(errors)),
    )


def load_validator_reward_cycle(
    protocol_data_root: Path, project_id: str, routing_id: str, reward_cycle_id: str
) -> ValidatorRewardCycle | None:
    path = get_validator_reward_cycle_path(protocol_data_root, routing_id, reward_cycle_id)
    if not path.exists():
        return None
    try:
        cycle = ValidatorRewardCycle.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValidatorRewardCycleStorageError("Stored validator reward cycle is malformed") from exc
    if cycle.project_id != project_id or cycle.routing_id != routing_id or cycle.reward_cycle_id != reward_cycle_id:
        raise ValidatorRewardCycleConflictError("Validator reward cycle belongs to another scope")
    return cycle


def list_validator_reward_cycles(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> list[ValidatorRewardCycle]:
    root = protocol_data_root / "task-rewards" / "validator-cycles" / "routing" / routing_id
    values = []
    for path in sorted(root.glob("*/cycle.json")) if root.exists() else []:
        value = load_validator_reward_cycle(protocol_data_root, project_id, routing_id, path.parent.name)
        if value is not None:
            values.append(value)
    return sorted(values, key=lambda item: (item.created_at, item.reward_cycle_id))


def find_validator_reward_cycle_by_budget(
    protocol_data_root: Path, task_reward_budget_id: str
) -> ValidatorRewardCycle | None:
    root = protocol_data_root / "task-rewards" / "validator-cycles" / "routing"
    matches = []
    for path in sorted(root.glob("*/*/cycle.json")) if root.exists() else []:
        try:
            cycle = ValidatorRewardCycle.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidatorRewardCycleStorageError("Stored validator reward cycle is malformed") from exc
        if cycle.task_reward_budget_id == task_reward_budget_id:
            matches.append(cycle)
    if len(matches) > 1:
        raise ValidatorRewardCycleConflictError("Multiple validator reward cycles reference one budget")
    return matches[0] if matches else None


def load_validator_reward_event(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    reward_cycle_id: str,
    reward_event_id: str,
) -> ValidatorRewardEvent | None:
    path = get_validator_reward_event_path(protocol_data_root, reward_cycle_id, reward_event_id)
    if not path.exists():
        return None
    try:
        event = ValidatorRewardEvent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValidatorRewardCycleStorageError("Stored validator RewardEvent is malformed") from exc
    if event.project_id != project_id or event.routing_id != routing_id or event.reward_cycle_id != reward_cycle_id:
        raise ValidatorRewardCycleConflictError("Validator RewardEvent belongs to another scope")
    return event


def list_validator_reward_events(
    protocol_data_root: Path, project_id: str, routing_id: str, reward_cycle_id: str
) -> list[ValidatorRewardEvent]:
    root = protocol_data_root / "task-rewards" / "events" / "validator" / reward_cycle_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        event = load_validator_reward_event(protocol_data_root, project_id, routing_id, reward_cycle_id, path.stem)
        if event is not None:
            values.append(event)
    return sorted(values, key=lambda item: (item.validator_assignment_id, item.reward_event_id))


def _calculate_snapshot(
    protocol_data_root: Path, cycle: ValidatorRewardCycle
) -> tuple[list[ValidatorRewardAllocation], str, str, dict[str, Any]]:
    routing = load_routing_record(protocol_data_root, cycle.project_id, cycle.routing_id)
    budget = load_task_reward_budget(protocol_data_root, cycle.project_id, cycle.task_reward_budget_id)
    if routing is None or budget is None:
        raise ValidatorRewardCycleNotFoundError("Routing or TaskRewardBudget is missing")
    if routing.status != RoutingStatus.FINALIZED or budget.status != TaskRewardBudgetStatus.FINALIZED:
        raise ValidatorRewardCycleStateError("Routing and TaskRewardBudget must remain finalized")
    if routing.source_fingerprint != cycle.routing_source_fingerprint or budget.source_fingerprint != cycle.task_reward_budget_source_fingerprint or budget.validator_pool_points != cycle.validator_pool_points:
        raise ValidatorRewardCycleSourceChangedError("Reward budget or routing changed")
    work = _discover_closed_work(protocol_data_root, cycle.project_id, cycle.routing_id)
    weights = [
        WeightedRewardItem(
            allocation_id=item["assignment"].validator_assignment_id,
            node_id=item["assignment"].validator_node_id,
            submission_id=item["assignment"].validator_assignment_id,
            raw_weight=cycle.reward_policy.work_unit_weight,
            contribution_score=Decimal("1"),
        )
        for item in work
    ]
    base_budgets = allocate_proportional_rewards(cycle.validator_pool_points, weights)
    source_units = []
    allocations = []
    for item in work:
        assignment = item["assignment"]
        base = base_budgets[assignment.validator_assignment_id]
        split = allocate_proportional_rewards(
            base,
            [
                WeightedRewardItem("completion", assignment.validator_node_id, assignment.validator_assignment_id, cycle.reward_policy.completion_share, Decimal("1")),
                WeightedRewardItem("quality", assignment.validator_node_id, assignment.validator_assignment_id, cycle.reward_policy.quality_share, Decimal("1")),
            ],
        ) if base else {"completion": ZERO, "quality": ZERO}
        completion_budget = split["completion"]
        quality_budget = split["quality"]
        completion_reward = completion_budget if item["completed"] else ZERO
        assessment = item["assessment"]
        quality_score = assessment.validation_quality_score if assessment else None
        quality_factor = calculate_validator_quality_factor(quality_score) if quality_score is not None else None
        quality_reward = (
            (quality_budget * quality_factor).quantize(PROTOCOL_POINTS_QUANTUM, rounding=ROUND_DOWN)
            if item["completed"] and quality_factor is not None
            else ZERO
        )
        total = completion_reward + quality_reward
        undistributed = base - total
        reasons = []
        if not item["completed"]:
            reasons.append(ValidatorRewardUndistributedReason.INCOMPLETE_ASSIGNMENT)
        if not item["resolved"]:
            reasons.append(ValidatorRewardUndistributedReason.NO_FINAL_RESOLUTION)
        elif quality_factor == 0:
            reasons.append(ValidatorRewardUndistributedReason.ZERO_QUALITY)
        if item["completed"] and quality_factor is not None and quality_reward < quality_budget:
            reasons.append(ValidatorRewardUndistributedReason.QUALITY_REMAINDER)
        source_unit = {
            "assignment_id": assignment.validator_assignment_id,
            "assignment_fingerprint": assignment.source_fingerprint,
            "assignment_status": assignment.status,
            "role": assignment.assignment_role,
            "committee_id": item["committee"].validator_committee_id,
            "committee_fingerprint": item["committee"].source_fingerprint,
            "round_id": item["round"].validation_round_id,
            "round_fingerprint": item["round"].source_fingerprint,
            "cluster_id": assignment.finding_cluster_id,
            "node_id": assignment.validator_node_id,
            "operator_id": assignment.validator_operator_id,
            "attestation_id": item["attestation"].attestation_id if item["attestation"] else None,
            "attestation_fingerprint": item["attestation"].source_fingerprint if item["attestation"] else None,
            "reproduction_id": item["reproduction"].validator_reproduction_id if item["reproduction"] else None,
            "reproduction_fingerprint": item["reproduction"].source_fingerprint if item["reproduction"] else None,
            "completed": item["completed"],
            "resolved": item["resolved"],
            "consensus_id": item["consensus"].validation_consensus_id,
            "consensus_source_fingerprint": item["consensus"].source_fingerprint,
            "consensus_calculation_fingerprint": item["consensus"].calculation_fingerprint,
            "consensus_outcome": item["consensus"].consensus_outcome,
            "reproduction_required": item["consensus"].consensus_policy.reproduction_required,
            "assessment_id": assessment.validation_quality_assessment_id if assessment else None,
            "assessment_fingerprint": assessment.source_fingerprint if assessment else None,
            "vq": quality_score,
            "base_budget": base,
            "completion_budget": completion_budget,
            "quality_budget": quality_budget,
        }
        source_units.append(source_unit)
        allocation_payload = {
            "allocation_version": "validator_reward_allocation_v1",
            "reward_cycle_id": cycle.reward_cycle_id,
            **source_unit,
            "completion_reward": completion_reward,
            "quality_factor": quality_factor,
            "quality_reward": quality_reward,
            "total_reward": total,
            "undistributed": undistributed,
            "reasons": reasons,
        }
        allocations.append(
            ValidatorRewardAllocation(
                validator_reward_allocation_id="validator_reward_allocation_" + protocol_fingerprint({"reward_cycle_id": cycle.reward_cycle_id, "assignment_id": assignment.validator_assignment_id}),
                reward_cycle_id=cycle.reward_cycle_id,
                task_reward_budget_id=cycle.task_reward_budget_id,
                project_id=cycle.project_id,
                routing_id=cycle.routing_id,
                finding_cluster_id=assignment.finding_cluster_id,
                validation_round_id=item["round"].validation_round_id,
                validator_committee_id=item["committee"].validator_committee_id,
                validator_assignment_id=assignment.validator_assignment_id,
                validator_node_id=assignment.validator_node_id,
                validator_operator_id=assignment.validator_operator_id,
                category=assignment.category,
                work_unit_weight=cycle.reward_policy.work_unit_weight,
                base_work_unit_budget=base,
                completion_eligible=item["completed"],
                completion_budget=completion_budget,
                completion_reward=completion_reward,
                final_validation_consensus_id=item["consensus"].validation_consensus_id,
                validation_quality_assessment_id=assessment.validation_quality_assessment_id if assessment else None,
                validation_quality_score=quality_score,
                quality_squared_factor=quality_factor,
                quality_budget=quality_budget,
                quality_reward=quality_reward,
                total_reward=total,
                undistributed_points=undistributed,
                undistributed_reason_codes=reasons,
                source_fingerprint=protocol_fingerprint(allocation_payload),
            )
        )
    source_payload = {
        "reward_policy": cycle.reward_policy,
        "project_id": cycle.project_id,
        "routing_id": cycle.routing_id,
        "routing_fingerprint": routing.source_fingerprint,
        "task_reward_budget_id": budget.task_reward_budget_id,
        "budget_fingerprint": budget.source_fingerprint,
        "validator_pool": budget.validator_pool_points,
        "pool_kind": RewardPoolKind.VALIDATOR,
        "closure": "terminal_validation_work_v1",
        "work_units": source_units,
    }
    source_fp = protocol_fingerprint(source_payload)
    distributed = sum((item.total_reward for item in allocations), ZERO)
    undistributed = sum((item.undistributed_points for item in allocations), ZERO)
    if not allocations:
        undistributed = cycle.validator_pool_points
    calculation_payload = {
        "source_fingerprint": source_fp,
        "allocations": allocations,
        "distributed": distributed,
        "undistributed": undistributed,
    }
    summary = {
        "authoritative_work_units": len(allocations),
        "completed_work_units": sum(item.completion_eligible for item in allocations),
        "resolved_quality_units": sum(item.validation_quality_assessment_id is not None for item in allocations),
        "unresolved_units": sum(ValidatorRewardUndistributedReason.NO_FINAL_RESOLUTION in item.undistributed_reason_codes for item in allocations),
        "distributed_validator_points": distributed,
        "undistributed_validator_points": undistributed,
    }
    return allocations, source_fp, protocol_fingerprint(calculation_payload), summary


def _discover_closed_work(protocol_data_root: Path, project_id: str, routing_id: str) -> list[dict[str, Any]]:
    committees = list_validator_committees(protocol_data_root, project_id, routing_id)
    if any(item.status != ValidatorCommitteeStatus.FINALIZED for item in committees):
        raise ValidatorRewardCycleStateError("Validation work is not closed: committee remains planned")
    by_cluster: dict[str, list[Any]] = {}
    for committee in committees:
        by_cluster.setdefault(committee.finding_cluster_id, []).append(committee)
    cluster_ids = {
        cluster.finding_cluster_id
        for cluster in list_task_finding_clusters(
            protocol_data_root, project_id, routing_id
        )
    }
    if cluster_ids != set(by_cluster):
        raise ValidatorRewardCycleStateError(
            "Validation work is not closed: every finding cluster must have a terminal validator flow"
        )
    work = []
    terminal_disputes = {
        ValidationDisputeStatus.RESOLVED,
        ValidationDisputeStatus.UNRESOLVED,
        ValidationDisputeStatus.BLOCKED_INSUFFICIENT_VALIDATORS,
    }
    resolved_outcomes = {
        ValidationConsensusOutcome.CONFIRMED,
        ValidationConsensusOutcome.REJECTED,
        ValidationConsensusOutcome.OUT_OF_SCOPE,
        ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE,
        ValidationConsensusOutcome.UNSAFE,
        ValidationConsensusOutcome.UNSUPPORTED,
    }
    attestations = list_validation_attestations(protocol_data_root, project_id, routing_id)
    reproductions = list_validator_reproduction_records(protocol_data_root, project_id, routing_id)
    attestation_by_assignment: dict[str, Any] = {}
    reproduction_by_assignment: dict[str, Any] = {}
    for attestation in attestations:
        if attestation.validator_assignment_id in attestation_by_assignment:
            raise ValidatorRewardCycleConflictError("Duplicate finalized attestation for one assignment")
        attestation_by_assignment[attestation.validator_assignment_id] = attestation
    for reproduction in reproductions:
        if reproduction.validator_assignment_id in reproduction_by_assignment:
            raise ValidatorRewardCycleConflictError("Duplicate reproduction record for one assignment")
        reproduction_by_assignment[reproduction.validator_assignment_id] = reproduction
    assessments = list_validation_quality_assessments(protocol_data_root, project_id, routing_id)
    for cluster_id, cluster_committees in sorted(by_cluster.items()):
        consensus_values = [item for item in list_validation_consensus(protocol_data_root, project_id, routing_id, finding_cluster_id=cluster_id) if item.lifecycle_status == ValidationConsensusLifecycle.FINALIZED]
        if not consensus_values:
            raise ValidatorRewardCycleStateError("Validation work is not closed: cluster has no finalized consensus")
        consensus = consensus_values[-1]
        resolved = consensus.consensus_outcome in resolved_outcomes
        if not resolved:
            disputes = list_validation_disputes(protocol_data_root, project_id, routing_id, finding_cluster_id=cluster_id)
            if not disputes or disputes[-1].status not in terminal_disputes:
                raise ValidatorRewardCycleStateError("Validation work is not closed: dispute may still add work units")
        committee_ids = {item.validator_committee_id for item in cluster_committees}
        if committee_ids != set(consensus.included_committee_ids):
            raise ValidatorRewardCycleStateError("Validation work is not closed: committee set differs from final consensus")
        seen_round_operators: set[str] = set()
        for committee_id in consensus.included_committee_ids:
            committee = load_validator_committee(protocol_data_root, project_id, routing_id, committee_id)
            if committee is None or committee.status != ValidatorCommitteeStatus.FINALIZED:
                raise ValidatorRewardCycleConflictError("Included committee is missing or not finalized")
            committee_operators = {
                seat.operator_id
                for seat in committee.authoritative_seats + committee.shadow_seats
            }
            if seen_round_operators.intersection(committee_operators):
                raise ValidatorRewardCycleConflictError(
                    "Validator operator was reused across included validation rounds"
                )
            seen_round_operators.update(committee_operators)
            round_candidates = [rid for rid in consensus.included_round_ids if (round_value := load_validation_round(protocol_data_root, project_id, routing_id, rid)) is not None and round_value.validator_committee_id == committee_id]
            if len(round_candidates) != 1:
                raise ValidatorRewardCycleConflictError("Committee must map to exactly one included validation round")
            round_value = load_validation_round(protocol_data_root, project_id, routing_id, round_candidates[0])
            if round_value is None or round_value.status != ValidationRoundStatus.FINALIZED:
                raise ValidatorRewardCycleStateError("Validation work is not closed: round not finalized")
            for seat in committee.authoritative_seats:
                assignment = load_validator_assignment(protocol_data_root, project_id, routing_id, seat.validator_assignment_id)
                if assignment is None:
                    raise ValidatorRewardCycleConflictError("Authoritative assignment is missing")
                if assignment.status == ValidatorAssignmentStatus.CANCELLED:
                    continue
                if assignment.assignment_role != ValidatorAssignmentRole.AUTHORITATIVE or assignment.validator_committee_id != committee_id or assignment.validator_node_id != seat.validator_node_id or assignment.validator_operator_id != seat.operator_id or assignment.finding_cluster_id != cluster_id:
                    raise ValidatorRewardCycleConflictError("Authoritative assignment attribution is corrupt")
                node = load_node(protocol_data_root, assignment.validator_node_id)
                if node is None or node.operator_id != assignment.validator_operator_id:
                    raise ValidatorRewardCycleConflictError("Validator operator mapping is stale or corrupt")
                attestation = attestation_by_assignment.get(assignment.validator_assignment_id)
                reproduction = reproduction_by_assignment.get(assignment.validator_assignment_id)
                linked = bool(
                    attestation
                    and reproduction
                    and attestation.validator_reproduction_id == reproduction.validator_reproduction_id
                    and attestation.validator_node_id == assignment.validator_node_id
                    and reproduction.validator_node_id == assignment.validator_node_id
                    and attestation.validator_operator_id == assignment.validator_operator_id
                    and reproduction.validator_operator_id == assignment.validator_operator_id
                    and attestation.finding_cluster_id == cluster_id
                    and reproduction.finding_cluster_id == cluster_id
                )
                violations = list_validator_protocol_violations(protocol_data_root, project_id, routing_id, validator_assignment_id=assignment.validator_assignment_id)
                completed = assignment.status == ValidatorAssignmentStatus.ATTESTED and linked and not violations
                assessment = None
                if completed and resolved:
                    matching = [value for value in assessments if value.validator_assignment_id == assignment.validator_assignment_id and value.final_validation_consensus_id == consensus.validation_consensus_id and value.status == ValidationQualityAssessmentStatus.FINALIZED]
                    if len(matching) != 1:
                        raise ValidatorRewardCycleStateError("Resolved completed validation requires exactly one finalized Day 5 quality assessment")
                    assessment = matching[0]
                    if assessment.validator_node_id != assignment.validator_node_id or assessment.validator_operator_id != assignment.validator_operator_id or assessment.attestation_id != attestation.attestation_id or assessment.validator_reproduction_id != reproduction.validator_reproduction_id:
                        raise ValidatorRewardCycleConflictError("Validation quality assessment attribution is corrupt")
                work.append({"assignment": assignment, "committee": committee, "round": round_value, "consensus": consensus, "resolved": resolved, "attestation": attestation, "reproduction": reproduction, "assessment": assessment, "completed": completed})
    return sorted(work, key=lambda item: (item["assignment"].finding_cluster_id, item["assignment"].validation_round, item["assignment"].seat_index or 0, item["assignment"].validator_operator_id, item["assignment"].validator_assignment_id))


def _expected_events(cycle: ValidatorRewardCycle, finalization_fp: str) -> list[ValidatorRewardEvent]:
    events = []
    now = cycle.finalized_at or _utc_now()
    for allocation in cycle.allocations:
        if allocation.total_reward <= 0:
            continue
        identity = {"event_version": VALIDATOR_REWARD_EVENT_VERSION, "reward_cycle_id": cycle.reward_cycle_id, "validator_assignment_id": allocation.validator_assignment_id, "event_type": "client_task_validator_reward"}
        event_id = "validator_reward_event_" + protocol_fingerprint(identity)
        payload = {
            **identity,
            "task_reward_budget_id": cycle.task_reward_budget_id,
            "project_id": cycle.project_id,
            "routing_id": cycle.routing_id,
            "finding_cluster_id": allocation.finding_cluster_id,
            "validation_round_id": allocation.validation_round_id,
            "validator_node_id": allocation.validator_node_id,
            "validator_operator_id": allocation.validator_operator_id,
            "validation_quality_assessment_id": allocation.validation_quality_assessment_id,
            "allocation_fingerprint": allocation.source_fingerprint,
            "cycle_finalization_fingerprint": finalization_fp,
            "completion_reward": allocation.completion_reward,
            "quality_reward": allocation.quality_reward,
            "total_reward": allocation.total_reward,
        }
        events.append(ValidatorRewardEvent(
            reward_event_id=event_id,
            reward_cycle_id=cycle.reward_cycle_id,
            task_reward_budget_id=cycle.task_reward_budget_id,
            project_id=cycle.project_id,
            routing_id=cycle.routing_id,
            finding_cluster_id=allocation.finding_cluster_id,
            validation_round_id=allocation.validation_round_id,
            validator_assignment_id=allocation.validator_assignment_id,
            validator_node_id=allocation.validator_node_id,
            validator_operator_id=allocation.validator_operator_id,
            completion_reward=allocation.completion_reward,
            quality_reward=allocation.quality_reward,
            total_reward=allocation.total_reward,
            validation_quality_assessment_id=allocation.validation_quality_assessment_id,
            allocation_fingerprint=allocation.source_fingerprint,
            cycle_finalization_fingerprint=finalization_fp,
            source_fingerprint=protocol_fingerprint(payload),
            created_at=now,
            finalized_at=now,
        ))
    return sorted(events, key=lambda item: (item.validator_assignment_id, item.reward_event_id))


def _verify_finalized_or_raise(protocol_data_root: Path, cycle: ValidatorRewardCycle) -> None:
    result = verify_validator_reward_cycle(protocol_data_root, project_id=cycle.project_id, routing_id=cycle.routing_id, reward_cycle_id=cycle.reward_cycle_id)
    if not result.ok:
        raise ValidatorRewardCycleConflictError("Finalized validator reward cycle failed verification: " + "; ".join(result.errors))


def _event_comparison_payload(event: ValidatorRewardEvent) -> dict[str, Any]:
    """Compare every immutable economic field while excluding generated times."""
    return event.model_dump(exclude={"created_at", "finalized_at"})


def _load_required_cycle(protocol_data_root: Path, project_id: str, routing_id: str, reward_cycle_id: str) -> ValidatorRewardCycle:
    cycle = load_validator_reward_cycle(protocol_data_root, project_id, routing_id, reward_cycle_id)
    if cycle is None:
        raise ValidatorRewardCycleNotFoundError("Validator reward cycle not found")
    return cycle


def _save_cycle(protocol_data_root: Path, cycle: ValidatorRewardCycle) -> ValidatorRewardCycle:
    atomic_write_json(get_validator_reward_cycle_path(protocol_data_root, cycle.routing_id, cycle.reward_cycle_id), cycle, temporary_prefix=".validator-reward-cycle-")
    return cycle


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
