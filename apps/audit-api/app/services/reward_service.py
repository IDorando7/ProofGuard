import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.contribution import ContributionEligibilityStatus, ContributionScoreRecord
from app.schemas.node import NodeRecord, normalize_category
from app.schemas.reputation import (
    ReputationEvent,
    ReputationEventApplicationStatus,
)
from app.schemas.reward import (
    NodeRewardSummary,
    RewardAllocation,
    RewardCycle,
    RewardCycleCreate,
    RewardCycleStatus,
    RewardEligibilityReasonCode,
    RewardEligibilityResult,
    RewardEvent,
    RewardEventStatus,
    RewardUnit,
    RewardWeightComponents,
)
from app.schemas.submission import (
    SubmissionRecord,
    SubmissionRewardStatus,
    SubmissionStatus,
    SubmissionStatusUpdate,
)
from app.services.contribution_scoring_service import load_contribution_score
from app.services.finding_service import load_project_finding
from app.services.node_registry_service import load_node
from app.services.reproduction_service import load_reproduction_result
from app.services.reputation_service import load_reputation_event_by_submission
from app.services.submission_service import (
    list_submissions,
    load_submission,
    update_submission_status,
)
from app.services.validation_service import load_validation_decision


REWARD_VERSION = "reward_v0"
REWARD_CYCLE_FILENAME = "cycle.json"
REWARD_EVENT_FILENAME = "reward_event.json"
SAFE_REWARD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SIX_PLACES = Decimal("0.000001")


class RewardServiceError(ValueError):
    """Base error for deterministic reward simulation failures."""


class RewardCycleNotFoundError(RewardServiceError):
    pass


class RewardSubmissionNotFoundError(RewardServiceError):
    pass


class RewardInputMismatchError(RewardServiceError):
    pass


class RewardCycleNotCalculatedError(RewardServiceError):
    pass


class RewardCycleAlreadyFinalizedError(RewardServiceError):
    pass


class RewardCycleSourceChangedError(RewardServiceError):
    pass


class RewardEventConflictError(RewardServiceError):
    pass


class InvalidRewardIdentifierError(RewardServiceError):
    pass


class RewardStorageError(RewardServiceError):
    pass


def get_rewards_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "rewards"


def get_reward_cycles_root(protocol_data_root: Path) -> Path:
    return get_rewards_root(protocol_data_root) / "cycles"


def get_reward_events_root(protocol_data_root: Path) -> Path:
    return get_rewards_root(protocol_data_root) / "events"


def get_reward_cycle_dir(protocol_data_root: Path, cycle_id: str) -> Path:
    _validate_reward_id(cycle_id)
    root = get_reward_cycles_root(protocol_data_root)
    return _safe_child(root, cycle_id)


def get_reward_event_dir(protocol_data_root: Path, submission_id: str) -> Path:
    _validate_reward_id(submission_id)
    root = get_reward_events_root(protocol_data_root)
    return _safe_child(root, submission_id)


def calculate_reputation_multiplier(reputation_score: float) -> float:
    score = Decimal(str(reputation_score))
    if score < 0 or score > 1:
        raise ValueError("Reputation score must be between 0 and 1")
    if score < Decimal("0.30"):
        return 0.5
    if score < Decimal("0.50"):
        return 0.8
    if score < Decimal("0.70"):
        return 1.0
    if score < Decimal("0.90"):
        return 1.15
    return 1.25


def calculate_category_multiplier(category: str) -> float:
    normalize_category(category)
    return 1.0


def calculate_reward_weight(
    contribution_score: float,
    reputation_score: float,
    category: str,
) -> RewardWeightComponents:
    if contribution_score < 0 or contribution_score > 100:
        raise ValueError("Contribution score must be between 0 and 100")
    reputation_multiplier = calculate_reputation_multiplier(reputation_score)
    category_multiplier = calculate_category_multiplier(category)
    contribution = _decimal(contribution_score)
    raw_weight = _quantize(
        contribution * _decimal(reputation_multiplier) * _decimal(category_multiplier)
    )
    return RewardWeightComponents(
        contribution_score=float(_quantize(contribution)),
        contribution_weight=float(_quantize(contribution)),
        reputation_score=float(_quantize(_decimal(reputation_score))),
        reputation_multiplier=reputation_multiplier,
        category_multiplier=category_multiplier,
        raw_weight=float(raw_weight),
    )


def determine_reward_eligibility(
    submission: SubmissionRecord,
    contribution: ContributionScoreRecord | None,
    validation: Any | None,
    reproduction: Any | None,
    existing_reward_event: RewardEvent | None,
) -> RewardEligibilityResult:
    reputation_event_id = (
        existing_reward_event.reputation_event_id if existing_reward_event is not None else None
    )
    base = {
        "submission_id": submission.submission_id,
        "node_id": submission.node_id,
        "contribution_score_id": contribution.score_id if contribution else None,
        "reputation_event_id": reputation_event_id,
        "contribution_score": contribution.total_score if contribution else None,
        "reputation_score": None,
    }

    def result(code: RewardEligibilityReasonCode, *reasons: str) -> RewardEligibilityResult:
        return RewardEligibilityResult(
            **base,
            eligible=code == RewardEligibilityReasonCode.ELIGIBLE,
            reason_code=code,
            reasons=list(reasons),
        )

    if existing_reward_event is not None and existing_reward_event.event_status == RewardEventStatus.FINALIZED:
        return result(RewardEligibilityReasonCode.ALREADY_REWARDED, "A finalized RewardEvent already exists for the submission.")
    if submission.reward_status == SubmissionRewardStatus.REWARDED or submission.status == SubmissionStatus.REWARDED:
        return result(RewardEligibilityReasonCode.ALREADY_REWARDED, "The submission is already rewarded.")
    if submission.reward_status == SubmissionRewardStatus.PENALIZED or submission.status == SubmissionStatus.PENALIZED:
        return result(RewardEligibilityReasonCode.ALREADY_PENALIZED, "The submission is already penalized.")

    validation_status = _status(validation)
    reproduction_status = _status(reproduction)
    if submission.status == SubmissionStatus.UNSAFE or validation_status in {"unsafe", "unsafe_poc"} or reproduction_status == "rejected_unsafe":
        return result(RewardEligibilityReasonCode.UNSAFE, "Unsafe submissions cannot receive protocol points.")
    status_codes = {
        SubmissionStatus.DUPLICATE: (RewardEligibilityReasonCode.DUPLICATE, "Duplicate submissions receive no reward."),
        SubmissionStatus.OUT_OF_SCOPE: (RewardEligibilityReasonCode.OUT_OF_SCOPE, "Out-of-scope submissions receive no reward."),
        SubmissionStatus.REJECTED: (RewardEligibilityReasonCode.REJECTED, "Rejected submissions receive no reward."),
        SubmissionStatus.UNSUPPORTED: (RewardEligibilityReasonCode.UNSUPPORTED, "Unsupported submissions receive no reward."),
    }
    validation_codes = {
        "duplicate": (RewardEligibilityReasonCode.DUPLICATE, "Validation marked the finding duplicate."),
        "out_of_scope": (RewardEligibilityReasonCode.OUT_OF_SCOPE, "Validation marked the finding out of scope."),
        "rejected": (RewardEligibilityReasonCode.REJECTED, "Validation rejected the finding."),
        "unsupported": (RewardEligibilityReasonCode.UNSUPPORTED, "Validation marked the finding unsupported."),
    }
    if submission.status in status_codes:
        code, message = status_codes[submission.status]
        return result(code, message)
    if validation_status in validation_codes:
        code, message = validation_codes[validation_status]
        return result(code, message)
    if reproduction_status == "unsupported":
        return result(RewardEligibilityReasonCode.UNSUPPORTED, "The reproduction result is unsupported.")

    if contribution is None:
        return result(RewardEligibilityReasonCode.CONTRIBUTION_MISSING, "ContributionScoreRecord is missing.")
    if contribution.eligibility_status == ContributionEligibilityStatus.PENDING:
        return result(RewardEligibilityReasonCode.CONTRIBUTION_PENDING, "Contribution scoring is still pending.")
    if contribution.eligibility_status != ContributionEligibilityStatus.ELIGIBLE or not contribution.eligible_for_reward or contribution.total_score <= 0:
        return result(RewardEligibilityReasonCode.CONTRIBUTION_INELIGIBLE, "The contribution record is not reward eligible.")

    mismatches = _source_mismatches(submission, contribution, validation, reproduction)
    if mismatches:
        return result(RewardEligibilityReasonCode.SOURCE_MISMATCH, *mismatches)
    if validation is None or validation_status != "accepted":
        return result(RewardEligibilityReasonCode.VALIDATION_NOT_ACCEPTED, "ValidationDecision is missing or not accepted.")
    if reproduction is None or reproduction_status != "reproduced":
        return result(RewardEligibilityReasonCode.REPRODUCTION_NOT_CONFIRMED, "ReproductionResult is missing or not reproduced.")
    if submission.status not in {SubmissionStatus.ACCEPTED, SubmissionStatus.REWARD_PENDING}:
        return result(RewardEligibilityReasonCode.VALIDATION_NOT_ACCEPTED, "Submission lifecycle is not in an accepted reward state.")
    return result(
        RewardEligibilityReasonCode.ELIGIBLE,
        "Contribution is accepted, reproduced, unique, in scope, and reward eligible.",
    )


def build_reward_cycle_source_payload(
    project_id: str,
    reward_pool: float,
    eligible_and_ineligible_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "reward_pool": float(_quantize(_decimal(reward_pool))),
        "reward_version": REWARD_VERSION,
        "submissions": sorted(
            eligible_and_ineligible_inputs,
            key=lambda item: str(item.get("submission_id", "")),
        ),
    }


def compute_reward_source_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    payload: RewardCycleCreate,
) -> RewardCycle:
    if not project_workspace.exists() or not project_workspace.is_dir():
        raise RewardInputMismatchError("Project workspace does not exist")
    if project_workspace.name != payload.project_id:
        metadata = _read_project_metadata(project_workspace)
        if metadata is not None and metadata.get("project_id") != payload.project_id:
            raise RewardInputMismatchError("Project workspace does not match the requested project")

    if payload.submission_ids is None:
        submissions = list_submissions(protocol_data_root, project_id=payload.project_id)
        submission_ids = sorted({item.submission_id for item in submissions})
    else:
        submission_ids = sorted(set(payload.submission_ids))
        for submission_id in submission_ids:
            submission = _load_required_submission(protocol_data_root, submission_id)
            if submission.project_id != payload.project_id:
                raise RewardInputMismatchError("Submission does not belong to the reward-cycle project")

    now = _utc_now()
    draft_payload = {
        "project_id": payload.project_id,
        "reward_pool": payload.reward_pool,
        "reward_version": REWARD_VERSION,
        "submission_ids": submission_ids,
        "state": "draft",
    }
    cycle = RewardCycle(
        cycle_id=str(uuid.uuid4()),
        reward_version=REWARD_VERSION,
        project_id=payload.project_id,
        reward_pool=payload.reward_pool,
        reward_unit=RewardUnit.PROTOCOL_POINTS,
        status=RewardCycleStatus.DRAFT,
        submission_ids=submission_ids,
        allocations=[],
        eligible_submissions=0,
        ineligible_submissions=0,
        total_raw_weight=0,
        total_allocated=0,
        undistributed_amount=payload.reward_pool,
        source_fingerprint=compute_reward_source_fingerprint(draft_payload),
        description=payload.description,
        created_at=now,
        calculated_at=None,
        finalized_at=None,
        updated_at=now,
    )
    return save_reward_cycle(protocol_data_root, cycle)


def calculate_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    cycle_id: str,
) -> RewardCycle:
    cycle = _load_required_cycle(protocol_data_root, cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        raise RewardCycleAlreadyFinalizedError("Finalized reward cycles cannot be recalculated")

    bundles = [
        _load_reward_inputs(protocol_data_root, project_workspace, cycle.project_id, submission_id)
        for submission_id in sorted(cycle.submission_ids)
    ]
    allocations: list[RewardAllocation] = []
    source_inputs: list[dict[str, Any]] = []
    for bundle in bundles:
        eligibility = determine_reward_eligibility(
            bundle["submission"],
            bundle["contribution"],
            bundle["validation"],
            bundle["reproduction"],
            bundle["reward_event"],
        )
        eligibility = eligibility.model_copy(
            update={
                "reputation_event_id": _reputation_event_id(bundle["reputation_event"]),
                "reputation_score": bundle["node"].reputation_score,
            }
        )
        components = None
        if eligibility.eligible:
            components = calculate_reward_weight(
                bundle["contribution"].total_score,
                bundle["node"].reputation_score,
                bundle["submission"].category,
            )
        allocations.append(
            RewardAllocation(
                submission_id=bundle["submission"].submission_id,
                node_id=bundle["submission"].node_id,
                project_id=bundle["submission"].project_id,
                finding_id=bundle["submission"].finding_id,
                category=bundle["submission"].category,
                eligible=eligibility.eligible,
                eligibility_reason_code=eligibility.reason_code,
                eligibility_reasons=eligibility.reasons,
                weight_components=components,
                allocation_ratio=0,
                reward_amount=0,
                reward_unit=RewardUnit.PROTOCOL_POINTS,
            )
        )
        source_inputs.append(_build_submission_source_input(bundle))

    allocations = allocate_reward_pool(cycle.reward_pool, allocations)
    total_weight = _quantize(sum(
        (_decimal(item.weight_components.raw_weight) for item in allocations if item.eligible and item.weight_components),
        Decimal("0"),
    ))
    total_allocated = _quantize(sum((_decimal(item.reward_amount) for item in allocations), Decimal("0")))
    pool = _quantize(_decimal(cycle.reward_pool))
    fingerprint = compute_reward_source_fingerprint(
        build_reward_cycle_source_payload(cycle.project_id, cycle.reward_pool, source_inputs)
    )
    now = _utc_now()
    calculated = RewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": RewardCycleStatus.CALCULATED,
            "allocations": allocations,
            "eligible_submissions": sum(item.eligible for item in allocations),
            "ineligible_submissions": sum(not item.eligible for item in allocations),
            "total_raw_weight": float(total_weight),
            "total_allocated": float(total_allocated),
            "undistributed_amount": float(_quantize(pool - total_allocated)),
            "source_fingerprint": fingerprint,
            "calculated_at": now,
            "updated_at": now,
        }
    )
    return save_reward_cycle(protocol_data_root, calculated)


def allocate_reward_pool(
    reward_pool: float,
    allocations: list[RewardAllocation],
) -> list[RewardAllocation]:
    ordered = sorted(allocations, key=lambda item: item.submission_id)
    eligible = [item for item in ordered if item.eligible and item.weight_components is not None]
    total_weight = sum((_decimal(item.weight_components.raw_weight) for item in eligible), Decimal("0"))
    if not eligible or total_weight <= 0:
        return ordered

    pool = _quantize(_decimal(reward_pool))
    updated: dict[str, RewardAllocation] = {}
    for allocation in eligible:
        raw_weight = _decimal(allocation.weight_components.raw_weight)
        ratio = raw_weight / total_weight
        amount = _quantize(pool * ratio)
        updated[allocation.submission_id] = allocation.model_copy(
            update={
                "allocation_ratio": float(ratio.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)),
                "reward_amount": float(amount),
            }
        )

    rounded_total = sum((_decimal(item.reward_amount) for item in updated.values()), Decimal("0"))
    remainder = _quantize(pool - rounded_total)
    if remainder:
        recipient = sorted(
            eligible,
            key=lambda item: (-_decimal(item.weight_components.raw_weight), item.submission_id),
        )[0]
        current = updated[recipient.submission_id]
        updated[recipient.submission_id] = current.model_copy(
            update={"reward_amount": float(_quantize(_decimal(current.reward_amount) + remainder))}
        )
    return [updated.get(item.submission_id, item) for item in ordered]


def finalize_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    cycle_id: str,
) -> RewardCycle:
    cycle = _load_required_cycle(protocol_data_root, cycle_id)
    if cycle.status == RewardCycleStatus.FINALIZED:
        return cycle
    if cycle.status != RewardCycleStatus.CALCULATED:
        raise RewardCycleNotCalculatedError("Reward cycle must be calculated before finalization")

    bundles = [
        _load_reward_inputs(protocol_data_root, project_workspace, cycle.project_id, submission_id)
        for submission_id in sorted(cycle.submission_ids)
    ]
    source_inputs = [_build_submission_source_input(bundle) for bundle in bundles]
    current_fingerprint = compute_reward_source_fingerprint(
        build_reward_cycle_source_payload(cycle.project_id, cycle.reward_pool, source_inputs)
    )
    if current_fingerprint != cycle.source_fingerprint:
        raise RewardCycleSourceChangedError("Reward-cycle source data changed after calculation")

    bundle_by_submission = {bundle["submission"].submission_id: bundle for bundle in bundles}
    for allocation in cycle.allocations:
        if not allocation.eligible:
            continue
        if allocation.reward_amount <= 0:
            raise RewardEventConflictError("Eligible finalized allocations must be greater than zero")
        bundle = bundle_by_submission[allocation.submission_id]
        existing = bundle["reward_event"]
        if existing is not None:
            if not _reward_event_matches_cycle(existing, cycle, allocation):
                raise RewardEventConflictError("A conflicting RewardEvent already exists for the submission")
            _mark_submission_rewarded(protocol_data_root, bundle["submission"])
            continue
        event = build_reward_event(
            cycle,
            allocation,
            bundle["contribution"],
            bundle["node"],
            bundle["reputation_event"],
        )
        save_reward_event_exclusively(protocol_data_root, event)
        _mark_submission_rewarded(protocol_data_root, bundle["submission"])

    now = _utc_now()
    finalized = RewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": RewardCycleStatus.FINALIZED,
            "finalized_at": now,
            "updated_at": now,
        }
    )
    return save_reward_cycle(protocol_data_root, finalized)


def build_reward_event(
    cycle: RewardCycle,
    allocation: RewardAllocation,
    contribution: ContributionScoreRecord,
    node: NodeRecord,
    reputation_event: ReputationEvent | None,
) -> RewardEvent:
    if not allocation.eligible or allocation.weight_components is None:
        raise RewardEventConflictError("RewardEvents may be built only for eligible allocations")
    if (
        contribution.submission_id != allocation.submission_id
        or contribution.project_id != allocation.project_id
        or contribution.finding_id != allocation.finding_id
        or contribution.node_id != allocation.node_id
        or node.node_id != allocation.node_id
    ):
        raise RewardInputMismatchError("RewardEvent source records do not match the allocation")
    components = allocation.weight_components
    now = _utc_now()
    reason = (
        f"Submission received {allocation.reward_amount:.6f} protocol points from reward cycle "
        f"{cycle.cycle_id} using contribution score {contribution.total_score:.2f} and "
        f"reputation multiplier {components.reputation_multiplier:.2f}."
    )
    return RewardEvent(
        reward_event_id=str(uuid.uuid4()),
        reward_version=REWARD_VERSION,
        event_status=RewardEventStatus.FINALIZED,
        cycle_id=cycle.cycle_id,
        project_id=allocation.project_id,
        submission_id=allocation.submission_id,
        node_id=allocation.node_id,
        finding_id=allocation.finding_id,
        category=allocation.category,
        contribution_score_id=contribution.score_id,
        reputation_event_id=_reputation_event_id(reputation_event),
        contribution_score=components.contribution_score,
        reputation_score=node.reputation_score,
        reputation_multiplier=components.reputation_multiplier,
        category_multiplier=components.category_multiplier,
        raw_weight=components.raw_weight,
        allocation_ratio=allocation.allocation_ratio,
        reward_amount=allocation.reward_amount,
        reward_unit=RewardUnit.PROTOCOL_POINTS,
        source_fingerprint=cycle.source_fingerprint,
        reason=reason,
        created_at=now,
        finalized_at=now,
        updated_at=now,
    )


def save_reward_cycle(protocol_data_root: Path, cycle: RewardCycle) -> RewardCycle:
    existing = load_reward_cycle(protocol_data_root, cycle.cycle_id)
    if existing is not None and existing.status == RewardCycleStatus.FINALIZED:
        if existing.model_dump() == cycle.model_dump():
            return existing
        raise RewardCycleAlreadyFinalizedError("Finalized reward cycles are immutable")
    _write_model_atomic(
        get_reward_cycle_dir(protocol_data_root, cycle.cycle_id),
        REWARD_CYCLE_FILENAME,
        cycle,
        RewardStorageError("Unable to persist reward cycle"),
    )
    return cycle


def load_reward_cycle(protocol_data_root: Path, cycle_id: str) -> RewardCycle | None:
    path = get_reward_cycle_dir(protocol_data_root, cycle_id) / REWARD_CYCLE_FILENAME
    return _load_model(path, RewardCycle, "Stored reward cycle is malformed")


def list_reward_cycles(
    protocol_data_root: Path,
    project_id: str | None = None,
    status: RewardCycleStatus | None = None,
) -> list[RewardCycle]:
    root = get_reward_cycles_root(protocol_data_root)
    if not root.exists():
        return []
    cycles = []
    for path in sorted(root.glob(f"*/{REWARD_CYCLE_FILENAME}")):
        cycle = load_reward_cycle(protocol_data_root, path.parent.name)
        if cycle is None:
            continue
        if project_id is not None and cycle.project_id != project_id:
            continue
        if status is not None and cycle.status != status:
            continue
        cycles.append(cycle)
    return sorted(cycles, key=lambda item: (item.created_at, item.cycle_id))


def save_reward_event_exclusively(
    protocol_data_root: Path,
    event: RewardEvent,
) -> RewardEvent:
    event_dir = get_reward_event_dir(protocol_data_root, event.submission_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    output_path = event_dir / REWARD_EVENT_FILENAME
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=event_dir, prefix=".reward-event-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(_serialize(event))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_name, output_path)
    except FileExistsError:
        existing = load_reward_event_by_submission(protocol_data_root, event.submission_id)
        if existing is not None and (
            existing.cycle_id == event.cycle_id
            and existing.source_fingerprint == event.source_fingerprint
            and existing.submission_id == event.submission_id
            and round(existing.reward_amount, 6) == round(event.reward_amount, 6)
        ):
            return existing
        raise RewardEventConflictError("A RewardEvent already exists for the submission")
    except OSError as exc:
        raise RewardStorageError("Unable to persist reward event") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return event


def load_reward_event_by_submission(
    protocol_data_root: Path,
    submission_id: str,
) -> RewardEvent | None:
    path = get_reward_event_dir(protocol_data_root, submission_id) / REWARD_EVENT_FILENAME
    return _load_model(path, RewardEvent, "Stored reward event is malformed")


def load_reward_event_by_id(protocol_data_root: Path, reward_event_id: str) -> RewardEvent | None:
    _validate_reward_id(reward_event_id)
    for event in list_reward_events(protocol_data_root):
        if event.reward_event_id == reward_event_id:
            return event
    return None


def list_reward_events(
    protocol_data_root: Path,
    node_id: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    cycle_id: str | None = None,
) -> list[RewardEvent]:
    normalized_category = normalize_category(category) if category is not None else None
    root = get_reward_events_root(protocol_data_root)
    if not root.exists():
        return []
    events = []
    for path in sorted(root.glob(f"*/{REWARD_EVENT_FILENAME}")):
        event = load_reward_event_by_submission(protocol_data_root, path.parent.name)
        if event is None:
            continue
        if node_id is not None and event.node_id != node_id:
            continue
        if project_id is not None and event.project_id != project_id:
            continue
        if normalized_category is not None and event.category != normalized_category:
            continue
        if cycle_id is not None and event.cycle_id != cycle_id:
            continue
        events.append(event)
    return sorted(events, key=lambda item: (item.created_at, item.reward_event_id))


def get_node_reward_summary(protocol_data_root: Path, node_id: str) -> NodeRewardSummary:
    _validate_reward_id(node_id)
    events = [
        event for event in list_reward_events(protocol_data_root, node_id=node_id)
        if event.event_status == RewardEventStatus.FINALIZED
    ]
    total = _quantize(sum((_decimal(event.reward_amount) for event in events), Decimal("0")))
    last_reward_at = max((event.finalized_at or event.created_at for event in events), default=None)
    return NodeRewardSummary(
        node_id=node_id,
        total_reward_events=len(events),
        total_protocol_points=float(total),
        projects_rewarded=len({event.project_id for event in events}),
        last_reward_at=last_reward_at,
        rewards=events,
    )


def _load_reward_inputs(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    submission_id: str,
) -> dict[str, Any]:
    submission = _load_required_submission(protocol_data_root, submission_id)
    if submission.project_id != project_id:
        raise RewardInputMismatchError("Submission does not belong to the reward-cycle project")
    finding = load_project_finding(project_workspace, submission.finding_id)
    if finding is None:
        raise RewardInputMismatchError("Finding is missing for reward calculation")
    node = load_node(protocol_data_root, submission.node_id)
    if node is None:
        raise RewardInputMismatchError("Node is missing for reward calculation")
    validation = load_validation_decision(project_workspace, submission.finding_id)
    reproduction = load_reproduction_result(project_workspace, submission.finding_id)
    contribution = load_contribution_score(protocol_data_root, submission.submission_id)
    reputation_event = load_reputation_event_by_submission(protocol_data_root, submission.submission_id)
    if reputation_event is not None and reputation_event.application_status != ReputationEventApplicationStatus.APPLIED:
        reputation_event = None
    reward_event = load_reward_event_by_submission(protocol_data_root, submission.submission_id)

    if _value(finding, "project_id") != project_id or _first_value(finding, "finding_id", "id") != submission.finding_id:
        raise RewardInputMismatchError("Finding source does not match the submission")
    if normalize_category(_value(finding, "category")) != submission.category:
        raise RewardInputMismatchError("Finding category does not match the submission")
    if node.node_id != submission.node_id:
        raise RewardInputMismatchError("Node source does not match the submission")
    return {
        "submission": submission,
        "finding": finding,
        "node": node,
        "validation": validation,
        "reproduction": reproduction,
        "contribution": contribution,
        "reputation_event": reputation_event,
        "reward_event": reward_event,
    }


def _build_submission_source_input(bundle: dict[str, Any]) -> dict[str, Any]:
    submission = bundle["submission"]
    validation = bundle["validation"]
    reproduction = bundle["reproduction"]
    contribution = bundle["contribution"]
    node = bundle["node"]
    reputation_event = bundle["reputation_event"]
    reward_event = bundle["reward_event"]
    return {
        "submission_id": submission.submission_id,
        "node_id": submission.node_id,
        "project_id": submission.project_id,
        "finding_id": submission.finding_id,
        "category": submission.category,
        "submission_status": _status(submission),
        "reward_status": _enum_value(submission.reward_status),
        "submission_validation_id": submission.validation_id,
        "submission_reproduction_id": submission.reproduction_id,
        "validation_id": _value(validation, "validation_id"),
        "validation_status": _status(validation),
        "reproduction_id": _value(reproduction, "reproduction_id"),
        "reproduction_status": _status(reproduction),
        "contribution_score_id": _value(contribution, "score_id"),
        "contribution_submission_id": _value(contribution, "submission_id"),
        "contribution_node_id": _value(contribution, "node_id"),
        "contribution_project_id": _value(contribution, "project_id"),
        "contribution_finding_id": _value(contribution, "finding_id"),
        "contribution_validation_id": _value(contribution, "validation_id"),
        "contribution_reproduction_id": _value(contribution, "reproduction_id"),
        "contribution_total_score": _value(contribution, "total_score"),
        "contribution_eligibility_status": _enum_value(_value(contribution, "eligibility_status")),
        "eligible_for_reward": _value(contribution, "eligible_for_reward"),
        "reputation_event_id": _reputation_event_id(reputation_event),
        "current_node_reputation": node.reputation_score,
        "reputation_multiplier": calculate_reputation_multiplier(node.reputation_score),
        "category_multiplier": calculate_category_multiplier(submission.category),
        "existing_reward_event_id": _value(reward_event, "reward_event_id"),
    }


def _source_mismatches(submission, contribution, validation, reproduction) -> list[str]:
    mismatches = []
    expected = {
        "submission_id": submission.submission_id,
        "node_id": submission.node_id,
        "project_id": submission.project_id,
        "finding_id": submission.finding_id,
    }
    for field, value in expected.items():
        if _value(contribution, field) != value:
            mismatches.append(f"Contribution {field} does not match the submission.")
    for name, record, identifier_name, submission_identifier in (
        ("Validation", validation, "validation_id", submission.validation_id),
        ("Reproduction", reproduction, "reproduction_id", submission.reproduction_id),
    ):
        if record is None:
            continue
        if _value(record, "project_id") != submission.project_id or _value(record, "finding_id") != submission.finding_id:
            mismatches.append(f"{name} source does not match the submission.")
        record_identifier = _value(record, identifier_name)
        if submission_identifier is not None and record_identifier != submission_identifier:
            mismatches.append(f"{name} identifier does not match the submission reference.")
    if validation is not None and contribution.validation_id is not None and contribution.validation_id != _value(validation, "validation_id"):
        mismatches.append("Contribution validation reference does not match.")
    if reproduction is not None and contribution.reproduction_id is not None and contribution.reproduction_id != _value(reproduction, "reproduction_id"):
        mismatches.append("Contribution reproduction reference does not match.")
    return mismatches


def _mark_submission_rewarded(protocol_data_root: Path, submission: SubmissionRecord) -> None:
    current = load_submission(protocol_data_root, submission.submission_id)
    if current is None:
        raise RewardSubmissionNotFoundError("Submission not found")
    if current.status == SubmissionStatus.REWARDED and current.reward_status == SubmissionRewardStatus.REWARDED:
        return
    if current.status == SubmissionStatus.ACCEPTED:
        current = update_submission_status(
            protocol_data_root,
            current.submission_id,
            SubmissionStatusUpdate(
                status=SubmissionStatus.REWARD_PENDING,
                reward_status=SubmissionRewardStatus.ELIGIBLE,
                reason="Eligible allocation is pending reward-cycle finalization.",
                reproduction_id=current.reproduction_id,
                validation_id=current.validation_id,
            ),
        )
    if current.status != SubmissionStatus.REWARD_PENDING:
        raise RewardEventConflictError("Submission lifecycle does not permit reward finalization")
    update_submission_status(
        protocol_data_root,
        current.submission_id,
        SubmissionStatusUpdate(
            status=SubmissionStatus.REWARDED,
            reward_status=SubmissionRewardStatus.REWARDED,
            reason="Reward cycle finalized with a positive protocol-points allocation.",
            reproduction_id=current.reproduction_id,
            validation_id=current.validation_id,
        ),
    )


def _reward_event_matches_cycle(event: RewardEvent, cycle: RewardCycle, allocation: RewardAllocation) -> bool:
    return (
        event.cycle_id == cycle.cycle_id
        and event.source_fingerprint == cycle.source_fingerprint
        and round(event.reward_amount, 6) == round(allocation.reward_amount, 6)
        and event.submission_id == allocation.submission_id
    )


def _load_required_submission(protocol_data_root: Path, submission_id: str) -> SubmissionRecord:
    try:
        submission = load_submission(protocol_data_root, submission_id)
    except ValueError as exc:
        if exc.__class__.__name__ == "InvalidSubmissionIdentifierError":
            raise InvalidRewardIdentifierError("Invalid reward identifier") from exc
        raise
    if submission is None:
        raise RewardSubmissionNotFoundError("Submission not found")
    return submission


def _load_required_cycle(protocol_data_root: Path, cycle_id: str) -> RewardCycle:
    cycle = load_reward_cycle(protocol_data_root, cycle_id)
    if cycle is None:
        raise RewardCycleNotFoundError("Reward cycle not found")
    return cycle


def _read_project_metadata(project_workspace: Path) -> dict[str, Any] | None:
    path = project_workspace / "metadata.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RewardInputMismatchError("Project metadata is malformed") from exc


def _load_model(path: Path, model, error_message: str):
    if not path.exists():
        return None
    if not path.is_file():
        raise RewardStorageError(error_message)
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise RewardStorageError(error_message) from exc


def _write_model_atomic(directory: Path, filename: str, model, error: Exception) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / filename
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".reward-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(_serialize(model))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output_path)
    except OSError as exc:
        raise error from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _serialize(model) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _safe_child(root: Path, identifier: str) -> Path:
    child = root / identifier
    try:
        child.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidRewardIdentifierError("Invalid reward identifier") from exc
    return child


def _validate_reward_id(identifier: str) -> None:
    if not isinstance(identifier, str) or not SAFE_REWARD_ID.fullmatch(identifier):
        raise InvalidRewardIdentifierError("Invalid reward identifier")
    if identifier in {".", ".."} or "/" in identifier or "\\" in identifier or Path(identifier).is_absolute():
        raise InvalidRewardIdentifierError("Invalid reward identifier")


def _reputation_event_id(event: ReputationEvent | None) -> str | None:
    return event.event_id if event is not None else None


def _value(source: Any | None, field: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None)


def _first_value(source: Any | None, *fields: str) -> Any:
    for field in fields:
        value = _value(source, field)
        if value is not None:
            return value
    return None


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _status(source: Any | None) -> str | None:
    value = _value(source, "status")
    value = _enum_value(value)
    return str(value).strip().lower() if value is not None else None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
