import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.category_performance import (
    CATEGORY_PERFORMANCE_VERSION,
    CategoryPerformanceContributionStats,
    CategoryPerformanceCounts,
    CategoryPerformanceOutcome,
    CategoryPerformanceRecord,
)
from app.schemas.contribution import ContributionEligibilityStatus, ContributionScoreRecord
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, normalize_category
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.reputation import (
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
)
from app.schemas.reward import RewardEvent, RewardEventStatus
from app.schemas.submission import SubmissionRecord
from app.schemas.validation import ValidationDecision
from app.services.contribution_scoring_service import load_contribution_score
from app.services.node_registry_service import (
    InvalidNodeIdentifierError,
    list_nodes,
    load_node,
)
from app.services.reproduction_service import load_reproduction_result
from app.services.reputation_service import (
    compute_reputation_source_fingerprint,
    list_reputation_events,
)
from app.services.reward_service import load_reward_event_by_submission
from app.services.submission_service import load_submission
from app.services.subnet_registry_service import load_subnet
from app.services.validation_service import load_validation_decision


CATEGORY_PERFORMANCE_DIRECTORY = "category-performance"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
FINALIZED_REPRODUCTION_STATUSES = {
    ReproductionStatus.REPRODUCED.value,
    ReproductionStatus.FAILED.value,
    ReproductionStatus.ERROR.value,
    ReproductionStatus.TIMEOUT.value,
    ReproductionStatus.UNSUPPORTED.value,
    ReproductionStatus.REJECTED_UNSAFE.value,
    ReproductionStatus.SANDBOX_ERROR.value,
}


class CategoryPerformanceServiceError(ValueError):
    """Base error for per-category historical aggregation failures."""


class CategoryPerformanceNodeNotFoundError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceNotFoundError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceUnsupportedCategoryError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceInputMismatchError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceCategoryMismatchError(CategoryPerformanceInputMismatchError):
    pass


class CategoryPerformanceSourceNotFoundError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceUnknownEventTypeError(CategoryPerformanceServiceError):
    pass


class InvalidCategoryPerformanceIdentifierError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceRebuildError(CategoryPerformanceServiceError):
    pass


class CategoryPerformanceStorageError(CategoryPerformanceServiceError):
    pass


@dataclass(frozen=True)
class CategoryPerformanceSourceBundle:
    reputation_event: ReputationEvent
    submission: SubmissionRecord
    contribution: ContributionScoreRecord
    validation: ValidationDecision
    reproduction: ReproductionResult | None
    reward_event: RewardEvent | None


def get_category_performance_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / CATEGORY_PERFORMANCE_DIRECTORY


def get_node_category_performance_root(
    protocol_data_root: Path,
    node_id: str,
) -> Path:
    _validate_identifier(node_id, "node")
    root = get_category_performance_root(protocol_data_root)
    node_root = root / node_id
    _ensure_path_within(node_root, root)
    return node_root


def get_category_performance_path(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> Path:
    normalized = _normalize_service_category(category)
    node_root = get_node_category_performance_root(protocol_data_root, node_id)
    path = node_root / f"{normalized}.json"
    _ensure_path_within(path, node_root)
    return path


def build_category_performance_id(
    node_id: str,
    category: str | FindingCategory,
) -> str:
    _validate_identifier(node_id, "node")
    normalized = _normalize_service_category(category)
    return f"performance_{node_id}_{normalized}"


def collect_node_category_events(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> list[ReputationEvent]:
    _load_required_node(protocol_data_root, node_id)
    normalized = _normalize_service_category(category)
    events = list_reputation_events(
        protocol_data_root,
        node_id=node_id,
        category=normalized,
        application_status=ReputationEventApplicationStatus.APPLIED,
    )
    return sorted(
        events,
        key=lambda event: (
            event.applied_at or event.created_at,
            event.submission_id,
            event.event_id,
        ),
    )


def determine_category_performance_outcome(
    reputation_event: ReputationEvent,
) -> CategoryPerformanceOutcome:
    raw_event_type = reputation_event.event_type
    if isinstance(raw_event_type, Enum):
        raw_event_type = raw_event_type.value
    mapping = {
        ReputationEventType.ACCEPTED_CONTRIBUTION.value:
            CategoryPerformanceOutcome.ACCEPTED_UNIQUE,
        ReputationEventType.REJECTED_FINDING.value:
            CategoryPerformanceOutcome.REJECTED,
        ReputationEventType.DUPLICATE_FINDING.value:
            CategoryPerformanceOutcome.DUPLICATE,
        ReputationEventType.OUT_OF_SCOPE_FINDING.value:
            CategoryPerformanceOutcome.OUT_OF_SCOPE,
        ReputationEventType.INSUFFICIENT_EVIDENCE.value:
            CategoryPerformanceOutcome.INSUFFICIENT_EVIDENCE,
        ReputationEventType.UNSAFE_SUBMISSION.value:
            CategoryPerformanceOutcome.UNSAFE,
        ReputationEventType.UNSUPPORTED_SUBMISSION.value:
            CategoryPerformanceOutcome.UNSUPPORTED,
    }
    try:
        return mapping[str(raw_event_type)]
    except KeyError as exc:
        raise CategoryPerformanceUnknownEventTypeError(
            f"Unsupported finalized reputation event type '{raw_event_type}'"
        ) from exc


def validate_category_event_sources(
    protocol_data_root: Path,
    event: ReputationEvent,
) -> CategoryPerformanceSourceBundle:
    if event.application_status != ReputationEventApplicationStatus.APPLIED:
        raise CategoryPerformanceRebuildError("Only applied reputation events can be aggregated")

    node = _load_required_node(protocol_data_root, event.node_id)
    submission = load_submission(protocol_data_root, event.submission_id)
    if submission is None:
        raise CategoryPerformanceSourceNotFoundError("Submission source record not found")
    _require_equal(event.node_id, node.node_id, "Reputation event node")
    _require_equal(submission.submission_id, event.submission_id, "Submission identifier")
    _require_equal(submission.node_id, event.node_id, "Submission node")
    _require_equal(submission.project_id, event.project_id, "Submission project")
    _require_equal(submission.finding_id, event.finding_id, "Submission finding")
    if submission.category != event.category:
        raise CategoryPerformanceCategoryMismatchError(
            "Submission category does not match the reputation event"
        )

    contribution = load_contribution_score(protocol_data_root, event.submission_id)
    if contribution is None:
        raise CategoryPerformanceSourceNotFoundError("Contribution source record not found")
    _require_equal(contribution.score_id, event.contribution_score_id, "Contribution score identifier")
    _require_equal(contribution.submission_id, event.submission_id, "Contribution submission")
    _require_equal(contribution.node_id, event.node_id, "Contribution node")
    _require_equal(contribution.project_id, event.project_id, "Contribution project")
    _require_equal(contribution.finding_id, event.finding_id, "Contribution finding")
    if round(contribution.total_score, 6) != round(event.contribution_score, 6):
        raise CategoryPerformanceInputMismatchError(
            "Contribution score does not match the reputation event snapshot"
        )
    if contribution.eligibility_status.value != event.contribution_eligibility_status:
        raise CategoryPerformanceInputMismatchError(
            "Contribution eligibility does not match the reputation event snapshot"
        )

    workspace = _resolve_project_workspace(protocol_data_root, event.project_id)
    validation = load_validation_decision(workspace, event.finding_id)
    if validation is None:
        raise CategoryPerformanceSourceNotFoundError("Validation source record not found")
    _require_equal(validation.validation_id, event.validation_id, "Validation identifier")
    _require_equal(validation.project_id, event.project_id, "Validation project")
    _require_equal(validation.finding_id, event.finding_id, "Validation finding")
    _require_equal(validation.status.value, event.validation_status, "Validation status")
    _require_equal(submission.validation_id, event.validation_id, "Submission validation reference")
    _require_equal(contribution.validation_id, event.validation_id, "Contribution validation reference")

    reproduction = load_reproduction_result(workspace, event.finding_id)
    if reproduction is None:
        if (
            event.reproduction_id is not None
            or event.reproduction_status is not None
            or submission.reproduction_id is not None
            or contribution.reproduction_id is not None
        ):
            raise CategoryPerformanceSourceNotFoundError("Reproduction source record not found")
    else:
        _require_equal(reproduction.reproduction_id, event.reproduction_id, "Reproduction identifier")
        _require_equal(reproduction.project_id, event.project_id, "Reproduction project")
        _require_equal(reproduction.finding_id, event.finding_id, "Reproduction finding")
        _require_equal(reproduction.status.value, event.reproduction_status, "Reproduction status")
        _require_equal(submission.reproduction_id, event.reproduction_id, "Submission reproduction reference")
        _require_equal(
            contribution.reproduction_id,
            event.reproduction_id,
            "Contribution reproduction reference",
        )
    evidence_reproduction_status = validation.evidence.reproduction_status
    if (
        evidence_reproduction_status is not None
        and evidence_reproduction_status != event.reproduction_status
    ):
        raise CategoryPerformanceInputMismatchError(
            "Validation reproduction status does not match the reputation event"
        )
    if (
        validation.evidence.normalized_severity is not None
        and validation.evidence.normalized_severity != event.normalized_severity
    ):
        raise CategoryPerformanceInputMismatchError(
            "Validation severity does not match the reputation event snapshot"
        )

    current_reputation_source = {
        "reputation_version": event.reputation_version,
        "submission_id": submission.submission_id,
        "node_id": submission.node_id,
        "project_id": submission.project_id,
        "finding_id": submission.finding_id,
        "category": submission.category,
        "validation_id": validation.validation_id,
        "validation_status": validation.status.value,
        "reproduction_id": reproduction.reproduction_id if reproduction is not None else None,
        "reproduction_status": (
            reproduction.status.value if reproduction is not None else evidence_reproduction_status
        ),
        "contribution_score_id": contribution.score_id,
        "contribution_score": contribution.total_score,
        "contribution_eligibility_status": contribution.eligibility_status.value,
        "eligible_for_reward": contribution.eligible_for_reward,
        "normalized_severity": event.normalized_severity,
    }
    if compute_reputation_source_fingerprint(current_reputation_source) != event.source_fingerprint:
        raise CategoryPerformanceInputMismatchError(
            "Finalized reputation source fingerprint no longer matches its source records"
        )

    reward_event = load_reward_event_by_submission(protocol_data_root, event.submission_id)
    if reward_event is not None:
        _validate_reward_event(reward_event, event, contribution)

    return CategoryPerformanceSourceBundle(
        reputation_event=event,
        submission=submission,
        contribution=contribution,
        validation=validation,
        reproduction=reproduction,
        reward_event=reward_event,
    )


def build_category_performance_source_payload(
    node_id: str,
    category: str | FindingCategory,
    source_events: list[ReputationEvent],
    contributions: list[ContributionScoreRecord],
    reward_event_ids: list[str],
) -> dict[str, Any]:
    normalized = _normalize_service_category(category)
    contributions_by_submission = {
        contribution.submission_id: contribution for contribution in contributions
    }
    entries: list[dict[str, Any]] = []
    for event in sorted(
        source_events,
        key=lambda item: (item.submission_id, item.event_id),
    ):
        contribution = contributions_by_submission.get(event.submission_id)
        if contribution is None:
            raise CategoryPerformanceSourceNotFoundError(
                "Contribution source missing while building performance fingerprint"
            )
        entries.append(
            {
                "event_id": event.event_id,
                "reputation_source_fingerprint": event.source_fingerprint,
                "submission_id": event.submission_id,
                "project_id": event.project_id,
                "finding_id": event.finding_id,
                "node_id": event.node_id,
                "category": event.category,
                "event_type": event.event_type.value,
                "validation_status": event.validation_status,
                "reproduction_status": event.reproduction_status,
                "contribution_score_id": contribution.score_id,
                "contribution_score": round(contribution.total_score, 6),
                "contribution_eligibility_status": contribution.eligibility_status.value,
                "eligible_for_reward": contribution.eligible_for_reward,
                "applied_at": (
                    event.applied_at or event.created_at
                ).isoformat(),
            }
        )
    return {
        "performance_version": CATEGORY_PERFORMANCE_VERSION,
        "node_id": node_id,
        "category": normalized,
        "sources": entries,
        "reward_event_ids": sorted(reward_event_ids),
    }


def compute_category_performance_source_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def aggregate_category_performance(
    node: NodeRecord,
    category: str | FindingCategory,
    source_bundles: list[CategoryPerformanceSourceBundle],
) -> CategoryPerformanceRecord:
    normalized = _normalize_service_category(category)
    unique_bundles = _deduplicate_source_bundles(source_bundles)
    for bundle in unique_bundles:
        event = bundle.reputation_event
        if event.node_id != node.node_id:
            raise CategoryPerformanceInputMismatchError(
                "Source event node does not match the aggregate node"
            )
        if event.category != normalized:
            raise CategoryPerformanceCategoryMismatchError(
                "Source event category does not match the aggregate category"
            )
        if event.application_status != ReputationEventApplicationStatus.APPLIED:
            raise CategoryPerformanceRebuildError(
                "Only applied reputation events can be aggregated"
            )

    count_values = {field: 0 for field in CategoryPerformanceCounts.model_fields}
    scores: list[float] = []
    accepted_scores: list[float] = []
    activity_times: list[datetime] = []
    reward_event_ids: list[str] = []
    outcome_field = {
        CategoryPerformanceOutcome.ACCEPTED_UNIQUE: "accepted_unique_submissions",
        CategoryPerformanceOutcome.REJECTED: "rejected_submissions",
        CategoryPerformanceOutcome.DUPLICATE: "duplicate_submissions",
        CategoryPerformanceOutcome.OUT_OF_SCOPE: "out_of_scope_submissions",
        CategoryPerformanceOutcome.INSUFFICIENT_EVIDENCE:
            "insufficient_evidence_submissions",
        CategoryPerformanceOutcome.UNSAFE: "unsafe_submissions",
        CategoryPerformanceOutcome.UNSUPPORTED: "unsupported_submissions",
    }

    for bundle in unique_bundles:
        event = bundle.reputation_event
        outcome = determine_category_performance_outcome(event)
        count_values["total_finalized_submissions"] += 1
        count_values[outcome_field[outcome]] += 1

        reproduction_status = event.reproduction_status
        if reproduction_status in FINALIZED_REPRODUCTION_STATUSES:
            count_values["reproduction_attempts"] += 1
        if reproduction_status == ReproductionStatus.REPRODUCED.value:
            count_values["reproduced_submissions"] += 1

        contribution = bundle.contribution
        if (
            contribution.eligibility_status == ContributionEligibilityStatus.ELIGIBLE
            and contribution.eligible_for_reward
        ):
            count_values["reward_eligible_submissions"] += 1
        if (
            bundle.reward_event is not None
            and bundle.reward_event.event_status == RewardEventStatus.FINALIZED
        ):
            if not contribution.eligible_for_reward:
                raise CategoryPerformanceInputMismatchError(
                    "A finalized reward event references an ineligible contribution"
                )
            count_values["rewarded_submissions"] += 1
            reward_event_ids.append(bundle.reward_event.reward_event_id)

        score = round(contribution.total_score, 6)
        scores.append(score)
        if outcome == CategoryPerformanceOutcome.ACCEPTED_UNIQUE:
            accepted_scores.append(score)
        activity_times.append(event.applied_at or event.created_at)

    total_score = round(sum(scores), 6)
    accepted_total = round(sum(accepted_scores), 6)
    contribution_stats = CategoryPerformanceContributionStats(
        total_contribution_score=total_score,
        average_contribution_score=(
            round(total_score / len(scores), 6) if scores else 0
        ),
        minimum_contribution_score=min(scores) if scores else None,
        maximum_contribution_score=max(scores) if scores else None,
        accepted_contribution_score_total=accepted_total,
        accepted_average_contribution_score=(
            round(accepted_total / len(accepted_scores), 6)
            if accepted_scores
            else 0
        ),
        accepted_minimum_contribution_score=(
            min(accepted_scores) if accepted_scores else None
        ),
        accepted_maximum_contribution_score=(
            max(accepted_scores) if accepted_scores else None
        ),
    )
    counts = CategoryPerformanceCounts(**count_values)
    source_events = [bundle.reputation_event for bundle in unique_bundles]
    contributions = [bundle.contribution for bundle in unique_bundles]
    source_payload = build_category_performance_source_payload(
        node.node_id,
        normalized,
        source_events,
        contributions,
        reward_event_ids,
    )
    now = _utc_now()
    return CategoryPerformanceRecord(
        performance_id=build_category_performance_id(node.node_id, normalized),
        performance_version=CATEGORY_PERFORMANCE_VERSION,
        node_id=node.node_id,
        category=normalized,
        counts=counts,
        contribution_stats=contribution_stats,
        source_event_ids=sorted(event.event_id for event in source_events),
        source_submission_ids=sorted(event.submission_id for event in source_events),
        source_fingerprint=compute_category_performance_source_fingerprint(source_payload),
        first_activity_at=min(activity_times) if activity_times else None,
        last_activity_at=max(activity_times) if activity_times else None,
        rebuilt_at=now,
        created_at=now,
        updated_at=now,
    )


def rebuild_category_performance(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> CategoryPerformanceRecord:
    node = _load_required_node(protocol_data_root, node_id)
    normalized = _normalize_service_category(category)
    events = collect_node_category_events(protocol_data_root, node_id, normalized)
    bundles = [
        validate_category_event_sources(protocol_data_root, event)
        for event in events
    ]
    rebuilt = aggregate_category_performance(node, normalized, bundles)
    existing = load_category_performance(protocol_data_root, node_id, normalized)
    if existing is not None and existing.source_fingerprint == rebuilt.source_fingerprint:
        return existing

    now = _utc_now()
    candidate = CategoryPerformanceRecord.model_validate(
        {
            **rebuilt.model_dump(),
            "performance_id": (
                existing.performance_id if existing is not None else rebuilt.performance_id
            ),
            "created_at": existing.created_at if existing is not None else now,
            "rebuilt_at": now,
            "updated_at": now,
        }
    )
    return save_category_performance(protocol_data_root, candidate)


def rebuild_node_category_performance(
    protocol_data_root: Path,
    node_id: str,
) -> list[CategoryPerformanceRecord]:
    _load_required_node(protocol_data_root, node_id)
    events = list_reputation_events(
        protocol_data_root,
        node_id=node_id,
        application_status=ReputationEventApplicationStatus.APPLIED,
    )
    categories = sorted({_normalize_service_category(event.category) for event in events})
    return [
        rebuild_category_performance(protocol_data_root, node_id, category)
        for category in categories
    ]


def rebuild_all_category_performance(
    protocol_data_root: Path,
) -> list[CategoryPerformanceRecord]:
    records: list[CategoryPerformanceRecord] = []
    for node in sorted(list_nodes(protocol_data_root), key=lambda item: item.node_id):
        records.extend(rebuild_node_category_performance(protocol_data_root, node.node_id))
    return sorted(records, key=lambda record: (record.node_id, record.category.value))


def save_category_performance(
    protocol_data_root: Path,
    record: CategoryPerformanceRecord,
) -> CategoryPerformanceRecord:
    existing = load_category_performance(
        protocol_data_root,
        record.node_id,
        record.category,
    )
    values = record.model_dump()
    if existing is not None:
        values.update(
            {
                "performance_id": existing.performance_id,
                "node_id": existing.node_id,
                "category": existing.category,
                "created_at": existing.created_at,
            }
        )
    values["updated_at"] = _utc_now()
    saved = CategoryPerformanceRecord.model_validate(values)
    _write_category_performance(protocol_data_root, saved)
    return saved


def load_category_performance(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> CategoryPerformanceRecord | None:
    normalized = _normalize_service_category(category)
    path = get_category_performance_path(protocol_data_root, node_id, normalized)
    if not path.exists():
        return None
    if not path.is_file():
        raise CategoryPerformanceStorageError(
            "Stored category-performance record is not a regular file"
        )
    try:
        record = CategoryPerformanceRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise CategoryPerformanceStorageError(
            "Stored category-performance record is malformed"
        ) from exc
    if record.node_id != node_id or record.category.value != normalized:
        raise CategoryPerformanceStorageError(
            "Stored category-performance identity does not match its path"
        )
    return record


def list_category_performance(
    protocol_data_root: Path,
    node_id: str | None = None,
    category: str | None = None,
    minimum_finalized_submissions: int | None = None,
) -> list[CategoryPerformanceRecord]:
    if node_id is not None:
        _validate_identifier(node_id, "node")
    normalized = _normalize_service_category(category) if category is not None else None
    if minimum_finalized_submissions is not None and minimum_finalized_submissions < 0:
        raise CategoryPerformanceServiceError(
            "Minimum finalized submissions cannot be negative"
        )
    root = get_category_performance_root(protocol_data_root)
    if not root.exists():
        return []

    records: list[CategoryPerformanceRecord] = []
    for path in sorted(root.glob("*/*.json")):
        path_node_id = path.parent.name
        path_category = path.stem
        record = load_category_performance(protocol_data_root, path_node_id, path_category)
        if record is None:
            continue
        if node_id is not None and record.node_id != node_id:
            continue
        if normalized is not None and record.category.value != normalized:
            continue
        if (
            minimum_finalized_submissions is not None
            and record.counts.total_finalized_submissions
            < minimum_finalized_submissions
        ):
            continue
        records.append(record)
    return sorted(records, key=lambda record: (record.node_id, record.category.value))


def get_node_category_performance(
    protocol_data_root: Path,
    node_id: str,
) -> list[CategoryPerformanceRecord]:
    _load_required_node(protocol_data_root, node_id)
    return list_category_performance(protocol_data_root, node_id=node_id)


def get_subnet_category_performance(
    protocol_data_root: Path,
    subnet_id: str,
) -> list[CategoryPerformanceRecord]:
    subnet = load_subnet(protocol_data_root, subnet_id)
    if subnet is None:
        from app.services.subnet_registry_service import SubnetNotFoundError

        raise SubnetNotFoundError("Subnet not found")
    return list_category_performance(
        protocol_data_root,
        category=subnet.category.value,
    )


def _deduplicate_source_bundles(
    bundles: list[CategoryPerformanceSourceBundle],
) -> list[CategoryPerformanceSourceBundle]:
    selected: list[CategoryPerformanceSourceBundle] = []
    by_event_id: dict[str, ReputationEvent] = {}
    by_submission_id: dict[str, ReputationEvent] = {}
    for bundle in sorted(
        bundles,
        key=lambda item: (
            item.reputation_event.submission_id,
            item.reputation_event.event_id,
        ),
    ):
        event = bundle.reputation_event
        prior_event = by_event_id.get(event.event_id)
        prior_submission = by_submission_id.get(event.submission_id)
        if prior_event is not None:
            if _event_identity(prior_event) != _event_identity(event):
                raise CategoryPerformanceInputMismatchError(
                    "Duplicate reputation event identifier has conflicting source data"
                )
            continue
        if prior_submission is not None:
            if prior_submission.source_fingerprint != event.source_fingerprint:
                raise CategoryPerformanceInputMismatchError(
                    "A submission has conflicting applied reputation events"
                )
            continue
        by_event_id[event.event_id] = event
        by_submission_id[event.submission_id] = event
        selected.append(bundle)
    return selected


def _event_identity(event: ReputationEvent) -> tuple[str, str, str, str, str]:
    return (
        event.event_id,
        event.submission_id,
        event.node_id,
        event.category,
        event.source_fingerprint,
    )


def _validate_reward_event(
    reward_event: RewardEvent,
    reputation_event: ReputationEvent,
    contribution: ContributionScoreRecord,
) -> None:
    _require_equal(reward_event.submission_id, reputation_event.submission_id, "Reward submission")
    _require_equal(reward_event.node_id, reputation_event.node_id, "Reward node")
    _require_equal(reward_event.project_id, reputation_event.project_id, "Reward project")
    _require_equal(reward_event.finding_id, reputation_event.finding_id, "Reward finding")
    if reward_event.category != reputation_event.category:
        raise CategoryPerformanceCategoryMismatchError(
            "Reward category does not match the reputation event"
        )
    _require_equal(
        reward_event.contribution_score_id,
        contribution.score_id,
        "Reward contribution reference",
    )
    if (
        reward_event.reputation_event_id is not None
        and reward_event.reputation_event_id != reputation_event.event_id
    ):
        raise CategoryPerformanceInputMismatchError(
            "Reward reputation reference does not match the source event"
        )
    if round(reward_event.contribution_score, 6) != round(contribution.total_score, 6):
        raise CategoryPerformanceInputMismatchError(
            "Reward contribution score does not match the source contribution"
        )


def _load_required_node(protocol_data_root: Path, node_id: str) -> NodeRecord:
    _validate_identifier(node_id, "node")
    try:
        node = load_node(protocol_data_root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise InvalidCategoryPerformanceIdentifierError("Invalid node identifier") from exc
    if node is None:
        raise CategoryPerformanceNodeNotFoundError("Node not found")
    return node


def _normalize_service_category(category: str | FindingCategory) -> str:
    try:
        return normalize_category(category)
    except (TypeError, ValueError) as exc:
        raise CategoryPerformanceUnsupportedCategoryError(str(exc)) from exc


def _validate_identifier(identifier: str, label: str) -> None:
    if (
        not isinstance(identifier, str)
        or not SAFE_IDENTIFIER.fullmatch(identifier)
        or identifier in {".", ".."}
        or "/" in identifier
        or "\\" in identifier
        or Path(identifier).is_absolute()
    ):
        raise InvalidCategoryPerformanceIdentifierError(
            f"Invalid {label} identifier"
        )


def _resolve_project_workspace(protocol_data_root: Path, project_id: str) -> Path:
    _validate_identifier(project_id, "project")
    sibling_workspace = protocol_data_root.parent / "audits" / project_id
    if sibling_workspace.exists():
        return sibling_workspace
    return get_settings().absolute_data_dir / project_id


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise CategoryPerformanceInputMismatchError(f"{label} does not match")


def _ensure_path_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidCategoryPerformanceIdentifierError(
            "Invalid category-performance storage path"
        ) from exc


def _write_category_performance(
    protocol_data_root: Path,
    record: CategoryPerformanceRecord,
) -> None:
    output_path = get_category_performance_path(
        protocol_data_root,
        record.node_id,
        record.category,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        record.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=".category-performance-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(output_path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise CategoryPerformanceStorageError(
            "Unable to persist category-performance record"
        ) from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
