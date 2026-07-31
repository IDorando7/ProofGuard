import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.category_performance import CategoryPerformanceRecord
from app.schemas.category_score import CategoryScoreBand, CategoryScoreRecord
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, NodeStatus, normalize_category
from app.schemas.reputation import (
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
)
from app.schemas.subnet import (
    SAFE_NODE_ID,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetRecord,
    SubnetStatus,
)
from app.schemas.subnet_membership import (
    MEMBERSHIP_POLICY_VERSION,
    MEMBERSHIP_VERSION,
    MembershipDecisionStatus,
    MembershipEvaluationResult,
    MembershipReasonCode,
    MembershipSourceSnapshot,
    SubnetMembershipDecision,
    SubnetMembershipRefreshResult,
)
from app.services.category_performance_service import (
    CategoryPerformanceStorageError,
    load_category_performance,
)
from app.services.category_scoring_service import (
    CategoryScoreServiceError,
    CategoryScoreStorageError,
    load_category_score,
    rebuild_category_score,
)
from app.services.node_registry_service import (
    InvalidNodeIdentifierError,
    list_nodes,
    load_node,
)
from app.services.reputation_service import list_reputation_events
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    SubnetMemberNotFoundError,
    SubnetNotFoundError,
    SubnetStorageError,
    get_subnet_dir,
    list_subnet_members,
    load_subnet,
    load_subnet_member,
    save_subnet_member,
)


CANDIDATE_MAXIMUM_HISTORY = 3
PROBATION_MINIMUM_SCORE = 0.40
ACTIVE_MINIMUM_ACCEPTED_UNIQUE = 2
EXPERT_MINIMUM_FINALIZED_SUBMISSIONS = 10
EXPERT_MINIMUM_ACCEPTED_UNIQUE = 6
EXPERT_MINIMUM_CATEGORY_SCORE = 0.80
RECENT_UNSAFE_LOOKBACK_DAYS = 30
ACTIVE_HYSTERESIS_SCORE_MARGIN = 0.10
EXPERT_HYSTERESIS_SCORE_MARGIN = 0.10
MEMBERSHIP_EVENTS_DIRECTORY = "membership-events"
VIRTUAL_PERFORMANCE_PREFIX = "unavailable_performance"
VIRTUAL_SCORE_PREFIX = "unavailable_category_score"


class SubnetMembershipError(ValueError):
    """Base error for subnet-membership domain failures."""


class SubnetMembershipNodeNotFoundError(SubnetMembershipError):
    pass


class SubnetMembershipSubnetNotFoundError(SubnetMembershipError):
    pass


class SubnetMembershipPerformanceNotFoundError(SubnetMembershipError):
    pass


class SubnetMembershipScoreNotFoundError(SubnetMembershipError):
    pass


class SubnetMembershipMemberNotFoundError(SubnetMembershipError):
    pass


class SubnetMembershipSourceMismatchError(SubnetMembershipError):
    pass


class InvalidSubnetMembershipIdentifierError(SubnetMembershipError):
    pass


class SubnetMembershipStorageError(SubnetMembershipError):
    pass


class SubnetMembershipCalculationError(SubnetMembershipError):
    pass


@dataclass(frozen=True)
class MembershipSourceBundle:
    node: NodeRecord
    subnet: SubnetRecord
    performance: CategoryPerformanceRecord | None
    score: CategoryScoreRecord | None
    existing_member: SubnetMemberRecord | None
    recent_unsafe_events: tuple[ReputationEvent, ...]


def get_membership_events_root(
    protocol_data_root: Path,
    subnet_id: str,
) -> Path:
    try:
        subnet_dir = get_subnet_dir(protocol_data_root, subnet_id)
    except InvalidSubnetIdentifierError as exc:
        raise InvalidSubnetMembershipIdentifierError(
            "Invalid subnet identifier"
        ) from exc
    root = subnet_dir / MEMBERSHIP_EVENTS_DIRECTORY
    _ensure_within(root, subnet_dir)
    return root


def get_node_membership_events_root(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
) -> Path:
    _validate_node_id(node_id)
    root = get_membership_events_root(protocol_data_root, subnet_id)
    node_root = root / node_id
    _ensure_within(node_root, root)
    return node_root


def collect_recent_unsafe_events(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
    evaluated_at: datetime,
    lookback_days: int = RECENT_UNSAFE_LOOKBACK_DAYS,
) -> list[ReputationEvent]:
    _validate_node_id(node_id)
    normalized = _normalize_category(category)
    evaluated_at = _as_utc(evaluated_at)
    if lookback_days < 0:
        raise SubnetMembershipCalculationError("Unsafe lookback cannot be negative")
    boundary = evaluated_at - timedelta(days=lookback_days)
    events = list_reputation_events(
        protocol_data_root,
        node_id=node_id,
        category=normalized,
        event_type=ReputationEventType.UNSAFE_SUBMISSION,
        application_status=ReputationEventApplicationStatus.APPLIED,
    )
    recent = [
        event
        for event in events
        if boundary <= (event.applied_at or event.created_at).astimezone(timezone.utc)
        <= evaluated_at
    ]
    return sorted(recent, key=lambda event: event.event_id)


def determine_node_participation_override(
    node: NodeRecord,
    subnet: SubnetRecord,
    category: str | FindingCategory,
    existing_member: SubnetMemberRecord | None,
) -> tuple[SubnetMemberStatus | None, list[MembershipReasonCode]]:
    normalized = _normalize_category(category)
    if existing_member is not None and existing_member.status == SubnetMemberStatus.REMOVED:
        code = (
            MembershipReasonCode.ADMINISTRATIVELY_REMOVED
            if existing_member.administrative_lock
            else MembershipReasonCode.CATEGORY_NOT_SUPPORTED
        )
        return SubnetMemberStatus.REMOVED, [code]
    if existing_member is not None and existing_member.administrative_lock:
        code = (
            MembershipReasonCode.ADMINISTRATIVELY_REMOVED
            if existing_member.status == SubnetMemberStatus.REMOVED
            else MembershipReasonCode.ADMINISTRATIVELY_SUSPENDED
        )
        return existing_member.status, [code]
    if subnet.status == SubnetStatus.ARCHIVED:
        return SubnetMemberStatus.REMOVED, [MembershipReasonCode.SUBNET_ARCHIVED]
    if normalized not in node.supported_categories:
        if existing_member is not None:
            return SubnetMemberStatus.REMOVED, [
                MembershipReasonCode.CATEGORY_NOT_SUPPORTED
            ]
        return None, [MembershipReasonCode.CATEGORY_NOT_SUPPORTED]
    if node.status == NodeStatus.BANNED:
        return SubnetMemberStatus.SUSPENDED, [MembershipReasonCode.NODE_BANNED]
    if node.status == NodeStatus.SUSPENDED:
        return SubnetMemberStatus.SUSPENDED, [MembershipReasonCode.NODE_SUSPENDED]
    if node.status == NodeStatus.INACTIVE:
        return SubnetMemberStatus.SUSPENDED, [MembershipReasonCode.NODE_INACTIVE]
    if subnet.status == SubnetStatus.SUSPENDED:
        return SubnetMemberStatus.SUSPENDED, [MembershipReasonCode.SUBNET_SUSPENDED]
    if subnet.status == SubnetStatus.INACTIVE:
        return SubnetMemberStatus.SUSPENDED, [MembershipReasonCode.SUBNET_INACTIVE]
    return None, []


def calculate_recommended_membership_status(
    subnet: SubnetRecord,
    performance: CategoryPerformanceRecord,
    score: CategoryScoreRecord,
    existing_member: SubnetMemberRecord | None,
    has_recent_unsafe: bool,
) -> tuple[SubnetMemberStatus, list[MembershipReasonCode]]:
    if has_recent_unsafe:
        return SubnetMemberStatus.SUSPENDED, [
            MembershipReasonCode.RECENT_UNSAFE_SUBMISSION
        ]

    finalized = performance.counts.total_finalized_submissions
    accepted = performance.counts.accepted_unique_submissions
    unsafe = performance.counts.unsafe_submissions
    category_score = score.category_score
    active_score = subnet.minimum_category_score
    active_history = subnet.minimum_finalized_submissions
    probation_score = min(PROBATION_MINIMUM_SCORE, active_score)
    expert_score = max(EXPERT_MINIMUM_CATEGORY_SCORE, active_score)

    expert_eligible = (
        category_score >= expert_score
        and finalized >= EXPERT_MINIMUM_FINALIZED_SUBMISSIONS
        and accepted >= EXPERT_MINIMUM_ACCEPTED_UNIQUE
        and unsafe == 0
    )
    active_eligible = (
        category_score >= active_score
        and finalized >= active_history
        and accepted >= ACTIVE_MINIMUM_ACCEPTED_UNIQUE
    )

    if expert_eligible:
        return SubnetMemberStatus.EXPERT, [MembershipReasonCode.ELIGIBLE_EXPERT]

    if existing_member is not None and existing_member.status == SubnetMemberStatus.EXPERT:
        if (
            category_score >= max(
                expert_score - EXPERT_HYSTERESIS_SCORE_MARGIN,
                active_score,
            )
            and finalized >= 8
            and accepted >= 5
            and unsafe == 0
        ):
            return SubnetMemberStatus.EXPERT, [
                MembershipReasonCode.ELIGIBLE_EXPERT,
                MembershipReasonCode.HYSTERESIS_PRESERVED_EXPERT,
            ]

    historical_unsafe_reason: list[MembershipReasonCode] = []
    if (
        unsafe > 0
        and category_score >= expert_score
        and finalized >= EXPERT_MINIMUM_FINALIZED_SUBMISSIONS
        and accepted >= EXPERT_MINIMUM_ACCEPTED_UNIQUE
    ):
        historical_unsafe_reason = [
            MembershipReasonCode.HISTORICAL_UNSAFE_PREVENTS_EXPERT
        ]

    if active_eligible:
        return SubnetMemberStatus.ACTIVE, [
            MembershipReasonCode.ELIGIBLE_ACTIVE,
            *historical_unsafe_reason,
        ]

    if existing_member is not None and existing_member.status == SubnetMemberStatus.ACTIVE:
        if (
            category_score >= max(
                active_score - ACTIVE_HYSTERESIS_SCORE_MARGIN,
                0.0,
            )
            and finalized >= max(active_history - 1, 1)
            and accepted >= 1
        ):
            return SubnetMemberStatus.ACTIVE, [
                MembershipReasonCode.ELIGIBLE_ACTIVE,
                MembershipReasonCode.HYSTERESIS_PRESERVED_ACTIVE,
                *historical_unsafe_reason,
            ]

    candidate_reasons: list[MembershipReasonCode] = []
    if finalized < CANDIDATE_MAXIMUM_HISTORY:
        candidate_reasons.append(MembershipReasonCode.INSUFFICIENT_HISTORY)
    if category_score < probation_score:
        candidate_reasons.append(MembershipReasonCode.INSUFFICIENT_SCORE)
    if candidate_reasons:
        return SubnetMemberStatus.CANDIDATE, [
            MembershipReasonCode.ELIGIBLE_CANDIDATE,
            *candidate_reasons,
        ]

    probation_reasons = [MembershipReasonCode.ELIGIBLE_PROBATION]
    if category_score < active_score:
        probation_reasons.append(MembershipReasonCode.INSUFFICIENT_SCORE)
    if finalized < active_history:
        probation_reasons.append(MembershipReasonCode.INSUFFICIENT_HISTORY)
    if accepted < ACTIVE_MINIMUM_ACCEPTED_UNIQUE:
        probation_reasons.append(
            MembershipReasonCode.INSUFFICIENT_ACCEPTED_FINDINGS
        )
    probation_reasons.extend(historical_unsafe_reason)
    return SubnetMemberStatus.PROBATION, _unique_codes(probation_reasons)


def build_membership_source_payload(
    node: NodeRecord,
    subnet: SubnetRecord,
    performance: CategoryPerformanceRecord,
    score: CategoryScoreRecord,
    recent_unsafe_events: list[ReputationEvent],
    existing_member: SubnetMemberRecord | None,
    policy_version: str = MEMBERSHIP_POLICY_VERSION,
) -> dict[str, Any]:
    _verify_source_identity(node, subnet, performance, score)
    return _build_source_payload(
        node=node,
        subnet=subnet,
        performance=performance,
        score=score,
        recent_unsafe_events=recent_unsafe_events,
        existing_member=existing_member,
        policy_version=policy_version,
    )


def compute_membership_source_fingerprint(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def load_membership_source_bundle(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    evaluated_at: datetime,
) -> MembershipSourceBundle:
    bundle = _load_membership_source_bundle(
        protocol_data_root,
        subnet_id,
        node_id,
        evaluated_at,
        allow_both_missing=False,
    )
    if bundle.performance is None:
        raise SubnetMembershipPerformanceNotFoundError(
            "Category-performance record not found"
        )
    if bundle.score is None:
        raise SubnetMembershipScoreNotFoundError("Category-score record not found")
    return bundle


def determine_membership_decision(
    bundle: MembershipSourceBundle,
    evaluated_at: datetime,
) -> SubnetMembershipDecision:
    evaluated_at = _as_utc(evaluated_at)
    override, reason_codes = determine_node_participation_override(
        bundle.node,
        bundle.subnet,
        bundle.subnet.category,
        bundle.existing_member,
    )
    if override is None:
        if bundle.recent_unsafe_events:
            recommended = SubnetMemberStatus.SUSPENDED
            reason_codes = [
                MembershipReasonCode.RECENT_UNSAFE_SUBMISSION
            ]
        elif bundle.performance is None and bundle.score is None:
            recommended = SubnetMemberStatus.CANDIDATE
            reason_codes = [
                MembershipReasonCode.ELIGIBLE_CANDIDATE,
                MembershipReasonCode.INSUFFICIENT_HISTORY,
            ]
        elif bundle.performance is None:
            raise SubnetMembershipPerformanceNotFoundError(
                "Category-performance record not found"
            )
        elif bundle.score is None:
            raise SubnetMembershipScoreNotFoundError(
                "Category-score record not found"
            )
        else:
            recommended, reason_codes = calculate_recommended_membership_status(
                bundle.subnet,
                bundle.performance,
                bundle.score,
                bundle.existing_member,
                bool(bundle.recent_unsafe_events),
            )
    else:
        recommended = override

    payload = _build_source_payload(
        node=bundle.node,
        subnet=bundle.subnet,
        performance=bundle.performance,
        score=bundle.score,
        recent_unsafe_events=list(bundle.recent_unsafe_events),
        existing_member=bundle.existing_member,
        policy_version=MEMBERSHIP_POLICY_VERSION,
    )
    fingerprint = compute_membership_source_fingerprint(payload)
    snapshot = _build_source_snapshot(bundle)
    return SubnetMembershipDecision(
        decision_id=_build_decision_id(fingerprint),
        policy_version=MEMBERSHIP_POLICY_VERSION,
        subnet_id=bundle.subnet.subnet_id,
        node_id=bundle.node.node_id,
        category=bundle.subnet.category,
        previous_status=(
            bundle.existing_member.status
            if bundle.existing_member is not None
            else None
        ),
        recommended_status=recommended,
        final_status=recommended,
        reason_codes=_unique_codes(reason_codes),
        reasons=_reason_messages(reason_codes),
        source_snapshot=snapshot,
        source_fingerprint=fingerprint,
        capacity_adjusted=False,
        created_at=evaluated_at,
    )


def refresh_subnet_memberships(
    protocol_data_root: Path,
    subnet_id: str,
    rebuild_dependencies: bool = False,
    evaluated_at: datetime | None = None,
) -> SubnetMembershipRefreshResult:
    evaluated_at = _as_utc(evaluated_at or datetime.now(timezone.utc))
    subnet = _load_required_subnet(protocol_data_root, subnet_id)
    nodes = {node.node_id: node for node in list_nodes(protocol_data_root)}
    existing_members = {
        member.node_id: member
        for member in list_subnet_members(protocol_data_root, subnet_id)
    }
    included_ids = sorted(
        node_id
        for node_id, node in nodes.items()
        if subnet.category.value in node.supported_categories
        or node_id in existing_members
    )
    for node_id in sorted(set(existing_members) - set(nodes)):
        included_ids.append(node_id)
    included_ids = sorted(set(included_ids))

    decisions: list[tuple[MembershipSourceBundle, SubnetMembershipDecision]] = []
    errors: list[str] = []
    for node_id in included_ids:
        node = nodes.get(node_id)
        if node is None:
            errors.append(f"{node_id}: Node not found")
            continue
        existing = existing_members.get(node_id)
        supported = subnet.category.value in node.supported_categories
        if not supported and existing is None:
            continue
        if rebuild_dependencies and supported:
            try:
                rebuild_category_score(
                    protocol_data_root,
                    node_id,
                    subnet.category,
                    rebuild_performance=True,
                )
            except CategoryScoreServiceError as exc:
                errors.append(f"{node_id}: Dependency rebuild failed: {exc}")
                continue
        try:
            bundle = _load_membership_source_bundle(
                protocol_data_root,
                subnet_id,
                node_id,
                evaluated_at,
                allow_both_missing=True,
                allow_partial_sources=True,
                verify_sources=False,
            )
            participation_override, _ = determine_node_participation_override(
                bundle.node,
                bundle.subnet,
                bundle.subnet.category,
                bundle.existing_member,
            )
            safety_override = (
                participation_override is not None
                or bool(bundle.recent_unsafe_events)
            )
            if not safety_override:
                if bundle.performance is None and bundle.score is not None:
                    raise SubnetMembershipPerformanceNotFoundError(
                        "Category-performance record not found"
                    )
                if bundle.performance is not None and bundle.score is None:
                    raise SubnetMembershipScoreNotFoundError(
                        "Category-score record not found"
                    )
                if bundle.performance is not None and bundle.score is not None:
                    _verify_source_identity(
                        bundle.node,
                        bundle.subnet,
                        bundle.performance,
                        bundle.score,
                    )
            decisions.append(
                (bundle, determine_membership_decision(bundle, evaluated_at))
            )
        except (
            SubnetMembershipError,
            CategoryPerformanceStorageError,
            CategoryScoreStorageError,
        ) as exc:
            errors.append(f"{node_id}: {exc}")

    finalized_decisions = _apply_capacity(
        decisions,
        subnet.maximum_active_nodes,
    )
    results = [
        _persist_evaluation(protocol_data_root, bundle, decision, evaluated_at)
        for bundle, decision in finalized_decisions
    ]
    results.sort(key=lambda result: result.node_id)
    counts = {
        status: sum(result.current_member.status == status for result in results)
        for status in SubnetMemberStatus
    }
    return SubnetMembershipRefreshResult(
        subnet_id=subnet.subnet_id,
        category=subnet.category,
        evaluated_nodes=len(results),
        created_members=sum(
            result.status == MembershipDecisionStatus.CREATED for result in results
        ),
        changed_members=sum(
            result.status == MembershipDecisionStatus.CHANGED for result in results
        ),
        unchanged_members=sum(
            result.status == MembershipDecisionStatus.UNCHANGED for result in results
        ),
        candidate_members=counts[SubnetMemberStatus.CANDIDATE],
        probation_members=counts[SubnetMemberStatus.PROBATION],
        active_members=counts[SubnetMemberStatus.ACTIVE],
        expert_members=counts[SubnetMemberStatus.EXPERT],
        suspended_members=counts[SubnetMemberStatus.SUSPENDED],
        removed_members=counts[SubnetMemberStatus.REMOVED],
        results=results,
        errors=errors,
        refreshed_at=evaluated_at,
    )


def evaluate_node_membership(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    rebuild_dependencies: bool = False,
    evaluated_at: datetime | None = None,
) -> MembershipEvaluationResult:
    _validate_node_id(node_id)
    if load_node(protocol_data_root, node_id) is None:
        raise SubnetMembershipNodeNotFoundError("Node not found")
    result = refresh_subnet_memberships(
        protocol_data_root,
        subnet_id,
        rebuild_dependencies=rebuild_dependencies,
        evaluated_at=evaluated_at,
    )
    for evaluation in result.results:
        if evaluation.node_id == node_id:
            return evaluation
    raise SubnetMembershipCalculationError(
        "Node is not eligible for evaluation in this subnet"
    )


def save_membership_decision_exclusively(
    protocol_data_root: Path,
    decision: SubnetMembershipDecision,
) -> SubnetMembershipDecision:
    node_root = get_node_membership_events_root(
        protocol_data_root,
        decision.subnet_id,
        decision.node_id,
    )
    node_root.mkdir(parents=True, exist_ok=True)
    for existing in list_membership_events(
        protocol_data_root,
        decision.subnet_id,
        decision.node_id,
    ):
        if existing.source_fingerprint == decision.source_fingerprint:
            return existing
    output_path = node_root / f"{decision.decision_id}.json"
    _ensure_within(output_path, node_root)
    serialized = json.dumps(
        decision.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=node_root,
            prefix=".membership-decision-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, output_path)
        except FileExistsError:
            existing = _load_membership_event_path(output_path)
            if existing.source_fingerprint != decision.source_fingerprint:
                raise SubnetMembershipStorageError(
                    "Membership decision identifier collision"
                )
            return existing
        return decision
    except OSError as exc:
        raise SubnetMembershipStorageError(
            "Unable to persist membership decision"
        ) from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def list_membership_events(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str | None = None,
    status: SubnetMemberStatus | None = None,
) -> list[SubnetMembershipDecision]:
    _load_required_subnet(protocol_data_root, subnet_id)
    root = get_membership_events_root(protocol_data_root, subnet_id)
    if node_id is not None:
        _validate_node_id(node_id)
        paths = sorted(
            get_node_membership_events_root(
                protocol_data_root, subnet_id, node_id
            ).glob("*.json")
        )
    else:
        paths = sorted(root.glob("*/*.json")) if root.exists() else []
    decisions = [_load_membership_event_path(path) for path in paths]
    if status is not None:
        decisions = [
            decision for decision in decisions if decision.final_status == status
        ]
    return sorted(
        decisions,
        key=lambda decision: (
            decision.created_at,
            decision.node_id,
            decision.decision_id,
        ),
    )


def administratively_suspend_member(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    reason: str,
) -> MembershipEvaluationResult:
    return _administrative_action(
        protocol_data_root,
        subnet_id,
        node_id,
        SubnetMemberStatus.SUSPENDED,
        MembershipReasonCode.ADMINISTRATIVELY_SUSPENDED,
        reason,
    )


def administratively_remove_member(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    reason: str,
) -> MembershipEvaluationResult:
    return _administrative_action(
        protocol_data_root,
        subnet_id,
        node_id,
        SubnetMemberStatus.REMOVED,
        MembershipReasonCode.ADMINISTRATIVELY_REMOVED,
        reason,
    )


def _load_membership_source_bundle(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    evaluated_at: datetime,
    *,
    allow_both_missing: bool,
    allow_partial_sources: bool = False,
    verify_sources: bool = True,
) -> MembershipSourceBundle:
    subnet = _load_required_subnet(protocol_data_root, subnet_id)
    _validate_node_id(node_id)
    try:
        node = load_node(protocol_data_root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise InvalidSubnetMembershipIdentifierError(
            "Invalid node identifier"
        ) from exc
    if node is None:
        raise SubnetMembershipNodeNotFoundError("Node not found")
    performance = load_category_performance(
        protocol_data_root, node_id, subnet.category
    )
    score = load_category_score(protocol_data_root, node_id, subnet.category)
    if performance is None and score is None and allow_both_missing:
        pass
    elif allow_partial_sources and (performance is None or score is None):
        pass
    elif performance is None:
        raise SubnetMembershipPerformanceNotFoundError(
            "Category-performance record not found"
        )
    elif score is None:
        raise SubnetMembershipScoreNotFoundError("Category-score record not found")
    elif verify_sources:
        _verify_source_identity(node, subnet, performance, score)
    existing = load_subnet_member(protocol_data_root, subnet_id, node_id)
    events = collect_recent_unsafe_events(
        protocol_data_root,
        node_id,
        subnet.category,
        evaluated_at,
    )
    return MembershipSourceBundle(
        node=node,
        subnet=subnet,
        performance=performance,
        score=score,
        existing_member=existing,
        recent_unsafe_events=tuple(events),
    )


def _verify_source_identity(
    node: NodeRecord,
    subnet: SubnetRecord,
    performance: CategoryPerformanceRecord,
    score: CategoryScoreRecord,
) -> None:
    if (
        performance.node_id != node.node_id
        or score.node_id != node.node_id
        or performance.category != subnet.category
        or score.category != subnet.category
    ):
        raise SubnetMembershipSourceMismatchError(
            "Membership source identity does not match node and subnet"
        )
    if score.performance_id != performance.performance_id:
        raise SubnetMembershipSourceMismatchError(
            "Category score references the wrong performance record"
        )
    if score.performance_source_fingerprint != performance.source_fingerprint:
        raise SubnetMembershipSourceMismatchError(
            "Category score references a stale performance source"
        )


def _build_source_payload(
    *,
    node: NodeRecord,
    subnet: SubnetRecord,
    performance: CategoryPerformanceRecord | None,
    score: CategoryScoreRecord | None,
    recent_unsafe_events: list[ReputationEvent],
    existing_member: SubnetMemberRecord | None,
    policy_version: str,
) -> dict[str, Any]:
    relevant_previous = _relevant_previous_status(
        node,
        subnet,
        performance,
        score,
        existing_member,
        bool(recent_unsafe_events),
    )
    return {
        "node": {
            "node_id": node.node_id,
            "status": node.status.value,
            "supported_categories": sorted(node.supported_categories),
        },
        "subnet": {
            "subnet_id": subnet.subnet_id,
            "category": subnet.category.value,
            "status": subnet.status.value,
            "minimum_category_score": subnet.minimum_category_score,
            "minimum_finalized_submissions": subnet.minimum_finalized_submissions,
            "maximum_active_nodes": subnet.maximum_active_nodes,
        },
        "performance": (
            {
                "missing": False,
                "performance_id": performance.performance_id,
                "source_fingerprint": performance.source_fingerprint,
                "finalized_submissions": (
                    performance.counts.total_finalized_submissions
                ),
                "accepted_unique_submissions": (
                    performance.counts.accepted_unique_submissions
                ),
                "unsafe_submissions": performance.counts.unsafe_submissions,
            }
            if performance is not None
            else {
                "missing": True,
                "performance_id": _virtual_performance_id(
                    node.node_id, subnet.category
                ),
                "source_fingerprint": None,
                "finalized_submissions": 0,
                "accepted_unique_submissions": 0,
                "unsafe_submissions": 0,
            }
        ),
        "score": (
            {
                "missing": False,
                "score_id": score.score_id,
                "source_fingerprint": score.source_fingerprint,
                "category_score": score.category_score,
                "score_band": score.score_band.value,
                "experience_confidence": (
                    score.components.experience_confidence
                ),
            }
            if score is not None
            else {
                "missing": True,
                "score_id": _virtual_score_id(node.node_id, subnet.category),
                "source_fingerprint": None,
                "category_score": 0.0,
                "score_band": CategoryScoreBand.INSUFFICIENT_DATA.value,
                "experience_confidence": 0.0,
            }
        ),
        "safety": {
            "recent_unsafe_events": [
                {
                    "event_id": event.event_id,
                    "source_fingerprint": event.source_fingerprint,
                }
                for event in sorted(
                    recent_unsafe_events, key=lambda item: item.event_id
                )
            ]
        },
        "previous_state": {
            "policy_relevant_status": (
                relevant_previous.value if relevant_previous is not None else None
            ),
            "administrative_lock": bool(
                existing_member and existing_member.administrative_lock
            ),
            "administrative_reason": (
                existing_member.status_reason
                if existing_member and existing_member.administrative_lock
                else None
            ),
        },
        "policy": {
            "membership_version": MEMBERSHIP_VERSION,
            "policy_version": policy_version,
            "candidate_history_threshold": CANDIDATE_MAXIMUM_HISTORY,
            "probation_minimum_score": PROBATION_MINIMUM_SCORE,
            "active_minimum_accepted_unique": ACTIVE_MINIMUM_ACCEPTED_UNIQUE,
            "expert_minimum_finalized_submissions": (
                EXPERT_MINIMUM_FINALIZED_SUBMISSIONS
            ),
            "expert_minimum_accepted_unique": EXPERT_MINIMUM_ACCEPTED_UNIQUE,
            "expert_minimum_category_score": EXPERT_MINIMUM_CATEGORY_SCORE,
            "recent_unsafe_lookback_days": RECENT_UNSAFE_LOOKBACK_DAYS,
            "active_hysteresis_score_margin": ACTIVE_HYSTERESIS_SCORE_MARGIN,
            "expert_hysteresis_score_margin": EXPERT_HYSTERESIS_SCORE_MARGIN,
        },
    }


def _relevant_previous_status(
    node: NodeRecord,
    subnet: SubnetRecord,
    performance: CategoryPerformanceRecord | None,
    score: CategoryScoreRecord | None,
    existing: SubnetMemberRecord | None,
    has_recent_unsafe: bool,
) -> SubnetMemberStatus | None:
    if existing is None:
        return None
    if existing.administrative_lock or existing.status == SubnetMemberStatus.REMOVED:
        return existing.status
    if (
        subnet.status == SubnetStatus.ARCHIVED
        or subnet.category.value not in node.supported_categories
    ):
        return SubnetMemberStatus.REMOVED
    if (
        existing.status not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT}
        or performance is None
        or score is None
        or has_recent_unsafe
    ):
        return None
    base, _ = calculate_recommended_membership_status(
        subnet, performance, score, None, False
    )
    with_existing, _ = calculate_recommended_membership_status(
        subnet, performance, score, existing, False
    )
    return existing.status if with_existing != base else None


def _build_source_snapshot(bundle: MembershipSourceBundle) -> MembershipSourceSnapshot:
    performance = bundle.performance
    score = bundle.score
    return MembershipSourceSnapshot(
        node_status=bundle.node.status,
        node_supported_categories=bundle.node.supported_categories,
        subnet_status=bundle.subnet.status,
        subnet_minimum_category_score=bundle.subnet.minimum_category_score,
        subnet_minimum_finalized_submissions=(
            bundle.subnet.minimum_finalized_submissions
        ),
        subnet_maximum_active_nodes=bundle.subnet.maximum_active_nodes,
        category_score_id=(
            score.score_id
            if score is not None
            else _virtual_score_id(bundle.node.node_id, bundle.subnet.category)
        ),
        category_score=score.category_score if score is not None else 0.0,
        score_band=(
            score.score_band
            if score is not None
            else CategoryScoreBand.INSUFFICIENT_DATA
        ),
        experience_confidence=(
            score.components.experience_confidence
            if score is not None
            else 0.0
        ),
        performance_id=(
            performance.performance_id
            if performance is not None
            else _virtual_performance_id(
                bundle.node.node_id, bundle.subnet.category
            )
        ),
        finalized_submissions=(
            performance.counts.total_finalized_submissions
            if performance is not None
            else 0
        ),
        accepted_unique_submissions=(
            performance.counts.accepted_unique_submissions
            if performance is not None
            else 0
        ),
        unsafe_submissions=(
            performance.counts.unsafe_submissions
            if performance is not None
            else 0
        ),
        recent_unsafe_event_ids=sorted(
            event.event_id for event in bundle.recent_unsafe_events
        ),
        previous_membership_status=(
            bundle.existing_member.status
            if bundle.existing_member is not None
            else None
        ),
    )


def _apply_capacity(
    decisions: list[tuple[MembershipSourceBundle, SubnetMembershipDecision]],
    maximum_active_nodes: int,
) -> list[tuple[MembershipSourceBundle, SubnetMembershipDecision]]:
    core = [
        (bundle, decision)
        for bundle, decision in decisions
        if decision.recommended_status
        in {SubnetMemberStatus.EXPERT, SubnetMemberStatus.ACTIVE}
    ]
    core.sort(key=_capacity_sort_key)
    selected = {decision.node_id for _, decision in core[:maximum_active_nodes]}
    capacity_payload = [
        {
            "node_id": decision.node_id,
            "recommended_status": decision.recommended_status.value,
            "category_score": decision.source_snapshot.category_score,
            "experience_confidence": (
                decision.source_snapshot.experience_confidence
            ),
            "accepted_unique_submissions": (
                decision.source_snapshot.accepted_unique_submissions
            ),
            "finalized_submissions": (
                decision.source_snapshot.finalized_submissions
            ),
        }
        for _, decision in core
    ]
    capacity_fingerprint = compute_membership_source_fingerprint(
        {
            "maximum_active_nodes": maximum_active_nodes,
            "ordered_core_candidates": capacity_payload,
        }
    )
    finalized: list[tuple[MembershipSourceBundle, SubnetMembershipDecision]] = []
    for bundle, decision in decisions:
        adjusted = (
            decision.recommended_status
            in {SubnetMemberStatus.EXPERT, SubnetMemberStatus.ACTIVE}
            and decision.node_id not in selected
        )
        final_status = (
            SubnetMemberStatus.PROBATION if adjusted else decision.recommended_status
        )
        codes = list(decision.reason_codes)
        if adjusted:
            codes.append(MembershipReasonCode.ACTIVE_CAPACITY_REACHED)
        payload = {
            "base_source_fingerprint": decision.source_fingerprint,
            "recommended_status": decision.recommended_status.value,
            "final_status": final_status.value,
            "capacity_adjusted": adjusted,
            "capacity_context_fingerprint": (
                capacity_fingerprint
                if decision.recommended_status
                in {SubnetMemberStatus.EXPERT, SubnetMemberStatus.ACTIVE}
                else None
            ),
        }
        fingerprint = compute_membership_source_fingerprint(payload)
        final_decision = SubnetMembershipDecision.model_validate(
            {
                **decision.model_dump(),
                "decision_id": _build_decision_id(fingerprint),
                "final_status": final_status,
                "reason_codes": _unique_codes(codes),
                "reasons": _reason_messages(codes),
                "source_fingerprint": fingerprint,
                "capacity_adjusted": adjusted,
            }
        )
        finalized.append((bundle, final_decision))
    return finalized


def _capacity_sort_key(
    item: tuple[MembershipSourceBundle, SubnetMembershipDecision],
) -> tuple[Any, ...]:
    _, decision = item
    return (
        0 if decision.recommended_status == SubnetMemberStatus.EXPERT else 1,
        -decision.source_snapshot.category_score,
        -decision.source_snapshot.experience_confidence,
        -decision.source_snapshot.accepted_unique_submissions,
        -decision.source_snapshot.finalized_submissions,
        decision.node_id,
    )


def _persist_evaluation(
    protocol_data_root: Path,
    bundle: MembershipSourceBundle,
    decision: SubnetMembershipDecision,
    evaluated_at: datetime,
) -> MembershipEvaluationResult:
    existing = bundle.existing_member
    if (
        existing is not None
        and existing.membership_source_fingerprint == decision.source_fingerprint
        and existing.status == decision.final_status
        and existing.category_score == decision.source_snapshot.category_score
        and existing.finalized_submissions
        == decision.source_snapshot.finalized_submissions
        and existing.accepted_unique_submissions
        == decision.source_snapshot.accepted_unique_submissions
    ):
        stored_decision = _load_last_decision(
            protocol_data_root, existing
        ) or decision
        return MembershipEvaluationResult(
            subnet_id=decision.subnet_id,
            node_id=decision.node_id,
            status=MembershipDecisionStatus.UNCHANGED,
            previous_member=existing,
            current_member=existing,
            decision=stored_decision,
        )

    stored_decision = save_membership_decision_exclusively(
        protocol_data_root, decision
    )
    member = _member_from_decision(bundle, stored_decision, evaluated_at)
    saved = save_subnet_member(
        protocol_data_root,
        member,
        evaluated_at=evaluated_at,
        preserve_assignment_state=True,
    )
    return MembershipEvaluationResult(
        subnet_id=decision.subnet_id,
        node_id=decision.node_id,
        status=(
            MembershipDecisionStatus.CREATED
            if existing is None
            else MembershipDecisionStatus.CHANGED
        ),
        previous_member=existing,
        current_member=saved,
        decision=stored_decision,
    )


def _member_from_decision(
    bundle: MembershipSourceBundle,
    decision: SubnetMembershipDecision,
    evaluated_at: datetime,
) -> SubnetMemberRecord:
    existing = bundle.existing_member
    status_changed = existing is None or existing.status != decision.final_status
    return SubnetMemberRecord(
        subnet_id=decision.subnet_id,
        node_id=decision.node_id,
        category=decision.category,
        status=decision.final_status,
        category_score=decision.source_snapshot.category_score,
        rank=None,
        finalized_submissions=decision.source_snapshot.finalized_submissions,
        accepted_unique_submissions=(
            decision.source_snapshot.accepted_unique_submissions
        ),
        exploration_assignments=(
            existing.exploration_assignments if existing is not None else 0
        ),
        last_assigned_at=(
            existing.last_assigned_at if existing is not None else None
        ),
        joined_at=existing.joined_at if existing is not None else evaluated_at,
        updated_at=evaluated_at,
        status_reason="; ".join(decision.reasons)[:500],
        membership_version=MEMBERSHIP_VERSION,
        policy_version=decision.policy_version,
        category_score_id=decision.source_snapshot.category_score_id,
        category_score_source_fingerprint=(
            bundle.score.source_fingerprint if bundle.score is not None else None
        ),
        performance_id=decision.source_snapshot.performance_id,
        membership_source_fingerprint=decision.source_fingerprint,
        last_decision_id=decision.decision_id,
        last_evaluated_at=evaluated_at,
        status_updated_at=(
            evaluated_at
            if status_changed
            else existing.status_updated_at or existing.updated_at
        ),
        status_reason_codes=[code.value for code in decision.reason_codes],
        administrative_lock=(
            existing.administrative_lock if existing is not None else False
        ),
    )


def _administrative_action(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
    final_status: SubnetMemberStatus,
    reason_code: MembershipReasonCode,
    reason: str,
) -> MembershipEvaluationResult:
    reason = reason.strip()
    if len(reason) < 3 or len(reason) > 500:
        raise SubnetMembershipCalculationError(
            "Administrative reason must contain 3 to 500 characters"
        )
    _load_required_subnet(protocol_data_root, subnet_id)
    _validate_node_id(node_id)
    now = datetime.now(timezone.utc)
    existing = load_subnet_member(protocol_data_root, subnet_id, node_id)
    if existing is None:
        raise SubnetMembershipMemberNotFoundError("Subnet member not found")
    if (
        existing.administrative_lock
        and existing.status == final_status
        and existing.status_reason == reason
        and existing.status_reason_codes == [reason_code.value]
    ):
        decision = _load_last_decision(protocol_data_root, existing)
        if decision is None:
            raise SubnetMembershipStorageError(
                "Administrative member is missing its decision event"
            )
        return MembershipEvaluationResult(
            subnet_id=subnet_id,
            node_id=node_id,
            status=MembershipDecisionStatus.UNCHANGED,
            previous_member=existing,
            current_member=existing,
            decision=decision,
        )

    bundle = _load_membership_source_bundle(
        protocol_data_root,
        subnet_id,
        node_id,
        now,
        allow_both_missing=True,
        allow_partial_sources=True,
        verify_sources=False,
    )
    locked = existing.model_copy(
        update={
            "status": final_status,
            "status_reason": reason,
            "status_reason_codes": [reason_code.value],
            "administrative_lock": True,
        }
    )
    locked_bundle = replace(bundle, existing_member=locked)
    payload = _build_source_payload(
        node=bundle.node,
        subnet=bundle.subnet,
        performance=bundle.performance,
        score=bundle.score,
        recent_unsafe_events=list(bundle.recent_unsafe_events),
        existing_member=locked,
        policy_version=MEMBERSHIP_POLICY_VERSION,
    )
    base_fingerprint = compute_membership_source_fingerprint(payload)
    fingerprint = compute_membership_source_fingerprint(
        {
            "base_source_fingerprint": base_fingerprint,
            "recommended_status": final_status.value,
            "final_status": final_status.value,
            "capacity_adjusted": False,
            "capacity_context_fingerprint": None,
        }
    )
    snapshot = _build_source_snapshot(bundle)
    decision = SubnetMembershipDecision(
        decision_id=_build_decision_id(fingerprint),
        policy_version=MEMBERSHIP_POLICY_VERSION,
        subnet_id=subnet_id,
        node_id=node_id,
        category=bundle.subnet.category,
        previous_status=existing.status,
        recommended_status=final_status,
        final_status=final_status,
        reason_codes=[reason_code],
        reasons=[reason],
        source_snapshot=snapshot,
        source_fingerprint=fingerprint,
        capacity_adjusted=False,
        created_at=now,
    )
    stored = save_membership_decision_exclusively(protocol_data_root, decision)
    updated_bundle = replace(locked_bundle, existing_member=existing)
    member = _member_from_decision(updated_bundle, stored, now).model_copy(
        update={
            "status_reason": reason,
            "status_reason_codes": [reason_code.value],
            "administrative_lock": True,
        }
    )
    saved = save_subnet_member(
        protocol_data_root,
        member,
        evaluated_at=now,
        preserve_assignment_state=True,
    )
    return MembershipEvaluationResult(
        subnet_id=subnet_id,
        node_id=node_id,
        status=MembershipDecisionStatus.CHANGED,
        previous_member=existing,
        current_member=saved,
        decision=stored,
    )


def _load_last_decision(
    protocol_data_root: Path,
    member: SubnetMemberRecord,
) -> SubnetMembershipDecision | None:
    if member.last_decision_id is None:
        return None
    path = (
        get_node_membership_events_root(
            protocol_data_root, member.subnet_id, member.node_id
        )
        / f"{member.last_decision_id}.json"
    )
    return _load_membership_event_path(path) if path.is_file() else None


def _load_membership_event_path(path: Path) -> SubnetMembershipDecision:
    try:
        return SubnetMembershipDecision.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise SubnetMembershipStorageError(
            "Stored membership decision is malformed"
        ) from exc


def _load_required_subnet(
    protocol_data_root: Path, subnet_id: str
) -> SubnetRecord:
    try:
        subnet = load_subnet(protocol_data_root, subnet_id)
    except (InvalidSubnetIdentifierError, SubnetStorageError) as exc:
        if isinstance(exc, InvalidSubnetIdentifierError):
            raise InvalidSubnetMembershipIdentifierError(
                "Invalid subnet identifier"
            ) from exc
        raise SubnetMembershipStorageError("Stored subnet record is malformed") from exc
    if subnet is None:
        raise SubnetMembershipSubnetNotFoundError("Subnet not found")
    return subnet


def _normalize_category(category: str | FindingCategory) -> str:
    try:
        return normalize_category(category)
    except (TypeError, ValueError) as exc:
        raise SubnetMembershipCalculationError(str(exc)) from exc


def _validate_node_id(node_id: str) -> None:
    if (
        not isinstance(node_id, str)
        or not SAFE_NODE_ID.fullmatch(node_id)
        or node_id in {".", ".."}
        or "/" in node_id
        or "\\" in node_id
        or Path(node_id).is_absolute()
    ):
        raise InvalidSubnetMembershipIdentifierError("Invalid node identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidSubnetMembershipIdentifierError(
            "Invalid membership identifier"
        ) from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SubnetMembershipCalculationError(
            "Membership evaluation time must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _virtual_performance_id(
    node_id: str, category: FindingCategory
) -> str:
    return f"{VIRTUAL_PERFORMANCE_PREFIX}_{node_id}_{category.value}"


def _virtual_score_id(node_id: str, category: FindingCategory) -> str:
    return f"{VIRTUAL_SCORE_PREFIX}_{node_id}_{category.value}"


def _build_decision_id(fingerprint: str) -> str:
    return f"membership_decision_{fingerprint[:32]}"


def _unique_codes(
    codes: list[MembershipReasonCode],
) -> list[MembershipReasonCode]:
    return list(dict.fromkeys(codes))


def _reason_messages(codes: list[MembershipReasonCode]) -> list[str]:
    messages = {
        MembershipReasonCode.ELIGIBLE_CANDIDATE: "The node qualifies for candidate membership.",
        MembershipReasonCode.ELIGIBLE_PROBATION: "The node qualifies for probation membership.",
        MembershipReasonCode.ELIGIBLE_ACTIVE: "The node meets active membership requirements.",
        MembershipReasonCode.ELIGIBLE_EXPERT: "The node meets expert membership requirements.",
        MembershipReasonCode.INSUFFICIENT_HISTORY: "The node has insufficient finalized category history.",
        MembershipReasonCode.INSUFFICIENT_SCORE: "The category score is below the required threshold.",
        MembershipReasonCode.INSUFFICIENT_ACCEPTED_FINDINGS: "The node has insufficient accepted unique findings.",
        MembershipReasonCode.NODE_INACTIVE: "The node is globally inactive.",
        MembershipReasonCode.NODE_SUSPENDED: "The node is globally suspended.",
        MembershipReasonCode.NODE_BANNED: "The node is globally banned.",
        MembershipReasonCode.SUBNET_INACTIVE: "The subnet is inactive.",
        MembershipReasonCode.SUBNET_SUSPENDED: "The subnet is suspended.",
        MembershipReasonCode.SUBNET_ARCHIVED: "The subnet is archived.",
        MembershipReasonCode.CATEGORY_NOT_SUPPORTED: "The node no longer supports the subnet category.",
        MembershipReasonCode.RECENT_UNSAFE_SUBMISSION: "A recent applied unsafe-submission event suspended participation.",
        MembershipReasonCode.HISTORICAL_UNSAFE_PREVENTS_EXPERT: "Historical unsafe submissions prevent expert membership.",
        MembershipReasonCode.ACTIVE_CAPACITY_REACHED: "Subnet active-member capacity was reached.",
        MembershipReasonCode.HYSTERESIS_PRESERVED_ACTIVE: "Active membership was preserved by the demotion threshold.",
        MembershipReasonCode.HYSTERESIS_PRESERVED_EXPERT: "Expert membership was preserved by the demotion threshold.",
        MembershipReasonCode.ADMINISTRATIVELY_SUSPENDED: "Membership is administratively suspended.",
        MembershipReasonCode.ADMINISTRATIVELY_REMOVED: "Membership is administratively removed.",
    }
    return [messages[code] for code in _unique_codes(codes)]
