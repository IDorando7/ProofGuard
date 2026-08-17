import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.contribution import ContributionEligibilityStatus, ContributionScoreRecord
from app.schemas.node import NodeRecord, NodeStatistics, normalize_category
from app.schemas.reputation import (
    NodeReputationResponse,
    NodeStatisticsDelta,
    ReputationDeltaComponents,
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
    ReputationProcessResponse,
    ReputationProcessingStatus,
    ReputationSignal,
)
from app.services.contribution_scoring_service import (
    is_valid_independent_duplicate,
    load_contribution_score,
)
from app.services.finding_service import load_project_finding
from app.services.node_registry_service import load_node, save_node
from app.services.reproduction_service import load_reproduction_result
from app.services.severity_service import normalize_severity_value
from app.services.submission_service import InvalidSubmissionIdentifierError, load_submission
from app.services.validation_service import load_validation_decision


REPUTATION_VERSION = "reputation_v0"
REPUTATION_EVENT_FILENAME = "event.json"
SAFE_REPUTATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
FINALIZED_VALIDATION_STATUSES = {
    "accepted",
    "duplicate",
    "rejected",
    "out_of_scope",
    "insufficient_evidence",
    "unsafe",
    "unsafe_poc",
    "unsupported",
}
PENDING_VALIDATION_STATUSES = {None, "needs_review", "pending", "running", "validation_pending"}
PENDING_REPRODUCTION_STATUSES = {
    None,
    "not_attempted",
    "generated",
    "running",
    "timeout",
    "sandbox_error",
    "error",
}


class ReputationServiceError(ValueError):
    """Base error for deterministic reputation processing failures."""


class ReputationSubmissionNotFoundError(ReputationServiceError):
    pass


class ReputationNodeNotFoundError(ReputationServiceError):
    pass


class ReputationFindingNotFoundError(ReputationServiceError):
    pass


class ReputationContributionNotFoundError(ReputationServiceError):
    pass


class ReputationInputMismatchError(ReputationServiceError):
    pass


class ReputationEventNotFoundError(ReputationServiceError):
    pass


class ReputationSourceChangedError(ReputationServiceError):
    pass


class InvalidReputationIdentifierError(ReputationServiceError):
    pass


class ReputationProcessingError(ReputationServiceError):
    pass


class ReputationStorageError(ReputationServiceError):
    pass


def get_reputation_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "reputation"


def get_reputation_events_root(protocol_data_root: Path) -> Path:
    return get_reputation_root(protocol_data_root) / "events"


def get_reputation_event_dir(protocol_data_root: Path, submission_id: str) -> Path:
    _validate_reputation_id(submission_id)
    events_root = get_reputation_events_root(protocol_data_root)
    event_dir = events_root / submission_id
    try:
        event_dir.resolve().relative_to(events_root.resolve())
    except ValueError as exc:
        raise InvalidReputationIdentifierError("Invalid reputation identifier") from exc
    return event_dir


def extract_validation_status(validation: Any | None) -> str | None:
    return _normalize_status(_value(validation, "status"))


def extract_reproduction_status(
    reproduction: Any | None,
    validation: Any | None,
) -> str | None:
    actual = _normalize_status(_value(reproduction, "status"))
    if actual is not None:
        return actual
    evidence = _value(validation, "evidence")
    return _normalize_status(_value(evidence, "reproduction_status"))


def extract_normalized_severity(finding: Any, validation: Any | None) -> str:
    evidence = _value(validation, "evidence")
    severity = _value(evidence, "normalized_severity")
    if severity is None:
        severity = _value(validation, "normalized_severity")
    if severity is None:
        severity = _value(finding, "severity")
    if isinstance(severity, Enum):
        severity = severity.value
    return normalize_severity_value(severity).value


def extract_category(submission: Any, finding: Any) -> str:
    submission_category = _value(submission, "category")
    finding_category = _value(finding, "category")
    normalized_submission = normalize_category(submission_category) if submission_category is not None else None
    normalized_finding = normalize_category(finding_category) if finding_category is not None else None
    if normalized_submission is not None and normalized_finding is not None:
        if normalized_submission != normalized_finding:
            raise ReputationInputMismatchError("Submission category does not match the finding")
    category = normalized_submission or normalized_finding
    if category is None:
        raise ReputationInputMismatchError("A vulnerability category is required")
    return category


def normalize_reputation_value(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)


def determine_reputation_processing_readiness(
    validation_status: str | None,
    reproduction_status: str | None,
    contribution: ContributionScoreRecord | None,
) -> tuple[ReputationProcessingStatus, list[str]]:
    validation = _normalize_status(validation_status)
    reproduction = _normalize_status(reproduction_status)
    reasons: list[str] = []

    if validation in PENDING_VALIDATION_STATUSES:
        if validation is None:
            reasons.append("Validation decision is missing.")
        elif validation == "needs_review":
            reasons.append("Validation still needs review.")
        else:
            reasons.append(f"Validation status {validation} is not finalized.")
    elif validation not in FINALIZED_VALIDATION_STATUSES:
        reasons.append(f"Validation status {validation} is not processable.")

    if contribution is None:
        reasons.append("Contribution score is missing.")
    elif contribution.eligibility_status == ContributionEligibilityStatus.PENDING:
        reasons.append("Contribution eligibility is pending.")

    if validation == "accepted" and reproduction != "reproduced":
        if reproduction in PENDING_REPRODUCTION_STATUSES:
            reasons.append("Accepted contribution is awaiting reproduced evidence.")
        else:
            reasons.append(f"Accepted contribution has reproduction status {reproduction}, not reproduced.")

    if reasons:
        return ReputationProcessingStatus.PENDING, reasons
    return ReputationProcessingStatus.APPLIED, ["Validation and contribution inputs are finalized."]


def determine_reputation_event_type(
    validation_status: str,
    reproduction_status: str | None,
    contribution: ContributionScoreRecord,
    independent_duplicate: bool = False,
) -> ReputationEventType:
    validation = _normalize_status(validation_status)
    reproduction = _normalize_status(reproduction_status)
    if validation == "accepted":
        if contribution.eligibility_status == ContributionEligibilityStatus.PENDING:
            raise ReputationProcessingError("Accepted contribution eligibility is still pending")
        if independent_duplicate:
            if reproduction != "reproduced":
                raise ReputationInputMismatchError(
                    "Accepted independent duplicate requires reproduced evidence"
                )
            return ReputationEventType.ACCEPTED_CONTRIBUTION
        if (
            contribution.eligibility_status != ContributionEligibilityStatus.ELIGIBLE
            or not contribution.eligible_for_reward
            or reproduction != "reproduced"
        ):
            raise ReputationInputMismatchError(
                "Accepted reputation processing requires an eligible contribution and reproduced evidence"
            )
        return ReputationEventType.ACCEPTED_CONTRIBUTION

    if (
        contribution.eligibility_status == ContributionEligibilityStatus.ELIGIBLE
        or contribution.eligible_for_reward
    ):
        raise ReputationInputMismatchError(
            "A non-accepted validation cannot have an eligible contribution"
        )

    mapping = {
        "duplicate": ReputationEventType.DUPLICATE_FINDING,
        "rejected": ReputationEventType.REJECTED_FINDING,
        "out_of_scope": ReputationEventType.OUT_OF_SCOPE_FINDING,
        "insufficient_evidence": ReputationEventType.INSUFFICIENT_EVIDENCE,
        "unsafe": ReputationEventType.UNSAFE_SUBMISSION,
        "unsafe_poc": ReputationEventType.UNSAFE_SUBMISSION,
        "unsupported": ReputationEventType.UNSUPPORTED_SUBMISSION,
    }
    try:
        return mapping[validation]
    except KeyError as exc:
        raise ReputationProcessingError("Validation status is not finalized for reputation") from exc


def calculate_reputation_delta(
    event_type: ReputationEventType,
    contribution_score: float,
    normalized_severity: str,
) -> tuple[ReputationDeltaComponents, list[ReputationSignal]]:
    base_delta = 0.0
    contribution_bonus = 0.0
    severity_bonus = 0.0
    penalty = 0.0
    signals: list[ReputationSignal] = []

    if event_type == ReputationEventType.ACCEPTED_CONTRIBUTION:
        base_delta = 0.03
        signals.append(_signal("ACCEPTED_BASE", 0.03, "The submission was accepted and reproduced.", "validation"))
        if contribution_score >= 90:
            contribution_bonus = 0.02
            signals.append(_signal("CONTRIBUTION_SCORE_90_PLUS", 0.02, "Contribution score is at least 90.", "contribution"))
        elif contribution_score >= 75:
            contribution_bonus = 0.01
            signals.append(_signal("CONTRIBUTION_SCORE_75_PLUS", 0.01, "Contribution score is at least 75.", "contribution"))
        else:
            signals.append(_signal("NO_CONTRIBUTION_BONUS", 0, "Contribution score is below 75.", "contribution"))

        severity = normalize_severity_value(
            normalized_severity.value if isinstance(normalized_severity, Enum) else normalized_severity
        ).value
        if severity == "Critical":
            severity_bonus = 0.02
            signals.append(_signal("CRITICAL_SEVERITY_BONUS", 0.02, "The normalized severity is Critical.", "validation"))
        elif severity == "High":
            severity_bonus = 0.01
            signals.append(_signal("HIGH_SEVERITY_BONUS", 0.01, "The normalized severity is High.", "validation"))
        else:
            signals.append(_signal("NO_SEVERITY_BONUS", 0, f"The normalized severity is {severity}.", "validation"))
    else:
        penalties = {
            ReputationEventType.DUPLICATE_FINDING: (-0.01, "DUPLICATE_PENALTY", "The submission was finalized as a duplicate."),
            ReputationEventType.OUT_OF_SCOPE_FINDING: (-0.02, "OUT_OF_SCOPE_PENALTY", "The submission was finalized as out of scope."),
            ReputationEventType.INSUFFICIENT_EVIDENCE: (-0.02, "INSUFFICIENT_EVIDENCE_PENALTY", "The submission had insufficient evidence."),
            ReputationEventType.REJECTED_FINDING: (-0.03, "REJECTED_FINDING_PENALTY", "The submission was rejected."),
            ReputationEventType.UNSAFE_SUBMISSION: (-0.08, "UNSAFE_SUBMISSION_PENALTY", "The submission was finalized as unsafe."),
            ReputationEventType.UNSUPPORTED_SUBMISSION: (0.0, "UNSUPPORTED_NO_REPUTATION_CHANGE", "The submission is unsupported by the current system."),
        }
        penalty, code, message = penalties[event_type]
        signals.append(_signal(code, penalty, message, "validation"))

    total_delta = round(base_delta + contribution_bonus + severity_bonus + penalty, 6)
    return (
        ReputationDeltaComponents(
            base_delta=round(base_delta, 6),
            contribution_bonus=round(contribution_bonus, 6),
            severity_bonus=round(severity_bonus, 6),
            penalty=round(penalty, 6),
            total_delta=total_delta,
        ),
        signals,
    )


def determine_statistics_delta(event_type: ReputationEventType) -> NodeStatisticsDelta:
    values: dict[str, int] = {"total_submissions": 1}
    outcome_field = {
        ReputationEventType.ACCEPTED_CONTRIBUTION: "accepted_submissions",
        ReputationEventType.DUPLICATE_FINDING: "duplicate_submissions",
        ReputationEventType.REJECTED_FINDING: "rejected_submissions",
        ReputationEventType.INSUFFICIENT_EVIDENCE: "rejected_submissions",
        ReputationEventType.OUT_OF_SCOPE_FINDING: "out_of_scope_submissions",
        ReputationEventType.UNSAFE_SUBMISSION: "unsafe_submissions",
    }.get(event_type)
    if outcome_field is not None:
        values[outcome_field] = 1
    return NodeStatisticsDelta(**values)


def apply_statistics_delta(
    current_statistics: NodeStatistics,
    delta: NodeStatisticsDelta,
) -> NodeStatistics:
    current = current_statistics.model_dump()
    changes = delta.model_dump()
    return NodeStatistics.model_validate(
        {field: current[field] + changes[field] for field in NodeStatistics.model_fields}
    )


def build_reputation_source_payload(
    submission: Any,
    validation: Any,
    reproduction: Any | None,
    contribution: ContributionScoreRecord,
    normalized_severity: str,
    category: str,
) -> dict[str, Any]:
    payload = {
        "reputation_version": REPUTATION_VERSION,
        "submission_id": _value(submission, "submission_id"),
        "node_id": _value(submission, "node_id"),
        "project_id": _value(submission, "project_id"),
        "finding_id": _value(submission, "finding_id"),
        "category": category,
        "validation_id": _value(validation, "validation_id"),
        "validation_status": extract_validation_status(validation),
        "reproduction_id": _value(reproduction, "reproduction_id"),
        "reproduction_status": extract_reproduction_status(reproduction, validation),
        "contribution_score_id": contribution.score_id,
        "contribution_score": contribution.total_score,
        "contribution_eligibility_status": contribution.eligibility_status.value,
        "eligible_for_reward": contribution.eligible_for_reward,
        "normalized_severity": normalized_severity,
    }
    if is_valid_independent_duplicate(validation):
        payload["duplicate_relation"] = "independent_root_cause"
        payload["canonical_finding_id"] = (
            _value(_value(validation, "evidence"), "canonical_finding_id")
            or _value(_value(validation, "evidence"), "duplicate_of")
        )
    return payload


def compute_reputation_source_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_reputation_reason(
    event_type: ReputationEventType,
    previous_reputation: float,
    new_reputation: float,
    delta_components: ReputationDeltaComponents,
    contribution_score: float,
    normalized_severity: str,
) -> str:
    del delta_components
    if event_type == ReputationEventType.ACCEPTED_CONTRIBUTION:
        return (
            f"Reputation increased from {previous_reputation:.6f} to {new_reputation:.6f}. "
            f"The submission was accepted, reproduced, scored {contribution_score:.2f}, "
            f"and normalized as {normalized_severity} severity."
        )
    if event_type == ReputationEventType.UNSUPPORTED_SUBMISSION:
        return (
            f"Reputation remained at {new_reputation:.6f} because the submission is "
            "unsupported by the current system."
        )
    labels = {
        ReputationEventType.DUPLICATE_FINDING: "a duplicate",
        ReputationEventType.REJECTED_FINDING: "rejected",
        ReputationEventType.OUT_OF_SCOPE_FINDING: "out of scope",
        ReputationEventType.INSUFFICIENT_EVIDENCE: "insufficiently evidenced",
        ReputationEventType.UNSAFE_SUBMISSION: "unsafe",
    }
    return (
        f"Reputation decreased from {previous_reputation:.6f} to {new_reputation:.6f} "
        f"because the submission was finalized as {labels[event_type]}."
    )


def build_reputation_event(
    node: NodeRecord,
    submission: Any,
    finding: Any,
    validation: Any,
    reproduction: Any | None,
    contribution: ContributionScoreRecord,
) -> ReputationEvent:
    _verify_input_consistency(node, submission, finding, validation, reproduction, contribution)
    validation_status = extract_validation_status(validation)
    reproduction_status = extract_reproduction_status(reproduction, validation)
    if validation_status is None:
        raise ReputationProcessingError("Validation is required to build a reputation event")
    normalized_severity = extract_normalized_severity(finding, validation)
    category = extract_category(submission, finding)
    independent_duplicate = is_valid_independent_duplicate(validation)
    event_type = determine_reputation_event_type(
        validation_status,
        reproduction_status,
        contribution,
        independent_duplicate=independent_duplicate,
    )
    delta_components, signals = calculate_reputation_delta(
        event_type,
        contribution.total_score,
        normalized_severity,
    )
    if independent_duplicate:
        signals.append(
            _signal(
                "ACCEPTED_INDEPENDENT_DUPLICATE",
                0,
                "The valid report independently confirmed an existing root cause.",
                "validation",
            )
        )
    statistics_delta = determine_statistics_delta(event_type)
    previous_statistics = node.statistics.model_dump()
    new_statistics_model = apply_statistics_delta(node.statistics, statistics_delta)
    new_statistics = new_statistics_model.model_dump()
    previous_reputation = normalize_reputation_value(node.reputation_score)
    new_reputation = normalize_reputation_value(
        previous_reputation + delta_components.total_delta
    )
    source_payload = build_reputation_source_payload(
        submission,
        validation,
        reproduction,
        contribution,
        normalized_severity,
        category,
    )
    now = _utc_now()
    return ReputationEvent(
        event_id=str(uuid.uuid4()),
        reputation_version=REPUTATION_VERSION,
        application_status=ReputationEventApplicationStatus.PREPARED,
        node_id=node.node_id,
        submission_id=_value(submission, "submission_id"),
        project_id=_value(submission, "project_id"),
        finding_id=_value(submission, "finding_id"),
        category=category,
        validation_id=_value(validation, "validation_id"),
        reproduction_id=_value(reproduction, "reproduction_id"),
        contribution_score_id=contribution.score_id,
        validation_status=validation_status,
        reproduction_status=reproduction_status,
        contribution_score=contribution.total_score,
        contribution_eligibility_status=contribution.eligibility_status.value,
        normalized_severity=normalized_severity,
        event_type=event_type,
        previous_reputation=previous_reputation,
        delta_components=delta_components,
        new_reputation=new_reputation,
        statistics_delta=statistics_delta,
        previous_statistics=previous_statistics,
        new_statistics=new_statistics,
        signals=signals,
        reason=build_reputation_reason(
            event_type,
            previous_reputation,
            new_reputation,
            delta_components,
            contribution.total_score,
            normalized_severity,
        ),
        source_fingerprint=compute_reputation_source_fingerprint(source_payload),
        created_at=now,
        applied_at=None,
        updated_at=now,
    )


def load_reputation_event_by_submission(
    protocol_data_root: Path,
    submission_id: str,
) -> ReputationEvent | None:
    path = get_reputation_event_dir(protocol_data_root, submission_id) / REPUTATION_EVENT_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise ReputationStorageError("Stored reputation event is not a regular file")
    try:
        return ReputationEvent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ReputationStorageError("Stored reputation event is malformed") from exc


def load_reputation_event_by_id(
    protocol_data_root: Path,
    event_id: str,
) -> ReputationEvent | None:
    _validate_reputation_id(event_id)
    for event in list_reputation_events(protocol_data_root):
        if event.event_id == event_id:
            return event
    return None


def list_reputation_events(
    protocol_data_root: Path,
    node_id: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    event_type: ReputationEventType | None = None,
    application_status: ReputationEventApplicationStatus | None = None,
) -> list[ReputationEvent]:
    normalized_category = normalize_category(category) if category is not None else None
    events_root = get_reputation_events_root(protocol_data_root)
    if not events_root.exists():
        return []
    events: list[ReputationEvent] = []
    for path in sorted(events_root.glob(f"*/{REPUTATION_EVENT_FILENAME}")):
        event = load_reputation_event_by_submission(protocol_data_root, path.parent.name)
        if event is None:
            continue
        if node_id is not None and event.node_id != node_id:
            continue
        if project_id is not None and event.project_id != project_id:
            continue
        if normalized_category is not None and event.category != normalized_category:
            continue
        if event_type is not None and event.event_type != event_type:
            continue
        if application_status is not None and event.application_status != application_status:
            continue
        events.append(event)
    return sorted(events, key=lambda item: (item.created_at, item.event_id))


def save_prepared_event_exclusively(
    protocol_data_root: Path,
    event: ReputationEvent,
) -> ReputationEvent:
    if event.application_status != ReputationEventApplicationStatus.PREPARED:
        raise ReputationProcessingError("Only prepared reputation events may be created exclusively")
    event_dir = get_reputation_event_dir(protocol_data_root, event.submission_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    output_path = event_dir / REPUTATION_EVENT_FILENAME
    payload = _serialize_event(event)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=event_dir,
            prefix=".reputation-prepared-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, output_path)
        except FileExistsError:
            existing = load_reputation_event_by_submission(protocol_data_root, event.submission_id)
            if existing is None:
                raise ReputationStorageError("Existing reputation event could not be loaded")
            return existing
        return event
    except OSError as exc:
        raise ReputationStorageError("Unable to create prepared reputation event") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def save_reputation_event(
    protocol_data_root: Path,
    event: ReputationEvent,
) -> ReputationEvent:
    existing = load_reputation_event_by_submission(protocol_data_root, event.submission_id)
    if existing is None:
        raise ReputationEventNotFoundError("Reputation event not found")
    if existing.event_id != event.event_id:
        raise ReputationInputMismatchError("Reputation event identity cannot change")
    if existing.source_fingerprint != event.source_fingerprint:
        raise ReputationSourceChangedError("Reputation event source fingerprint cannot change")
    if existing.application_status == ReputationEventApplicationStatus.APPLIED:
        if event.application_status != ReputationEventApplicationStatus.APPLIED:
            raise ReputationInputMismatchError("Applied reputation events cannot return to prepared")
        if event.applied_at != existing.applied_at:
            raise ReputationInputMismatchError("Applied reputation event timestamp cannot change")
    immutable_exclusions = {"application_status", "applied_at", "updated_at"}
    if existing.model_dump(exclude=immutable_exclusions) != event.model_dump(
        exclude=immutable_exclusions
    ):
        raise ReputationInputMismatchError("Immutable reputation event fields cannot change")
    saved = ReputationEvent.model_validate(
        {
            **event.model_dump(),
            "event_id": existing.event_id,
            "submission_id": existing.submission_id,
            "created_at": existing.created_at,
            "updated_at": _utc_now(),
        }
    )
    _write_event_atomic(protocol_data_root, saved)
    return saved


def process_submission_reputation(
    protocol_data_root: Path,
    project_workspace: Path,
    submission_id: str,
) -> ReputationProcessResponse:
    try:
        submission = load_submission(protocol_data_root, submission_id)
    except InvalidSubmissionIdentifierError as exc:
        raise InvalidReputationIdentifierError("Invalid reputation identifier") from exc
    if submission is None:
        raise ReputationSubmissionNotFoundError("Submission not found")

    node = load_node(protocol_data_root, submission.node_id)
    if node is None:
        raise ReputationNodeNotFoundError("Node not found")
    finding = load_project_finding(project_workspace, submission.finding_id)
    if finding is None:
        raise ReputationFindingNotFoundError("Finding not found for reputation processing")
    validation = load_validation_decision(project_workspace, submission.finding_id)
    reproduction = load_reproduction_result(project_workspace, submission.finding_id)
    contribution = load_contribution_score(protocol_data_root, submission.submission_id)
    if contribution is None:
        raise ReputationContributionNotFoundError("Contribution score is required before reputation processing")

    _verify_input_consistency(node, submission, finding, validation, reproduction, contribution)
    validation_status = extract_validation_status(validation)
    reproduction_status = extract_reproduction_status(reproduction, validation)
    existing = load_reputation_event_by_submission(protocol_data_root, submission.submission_id)
    source_fingerprint: str | None = None
    if validation is not None:
        normalized_severity = extract_normalized_severity(finding, validation)
        category = extract_category(submission, finding)
        source_fingerprint = compute_reputation_source_fingerprint(
            build_reputation_source_payload(
                submission,
                validation,
                reproduction,
                contribution,
                normalized_severity,
                category,
            )
        )
        if existing is not None and existing.source_fingerprint != source_fingerprint:
            raise ReputationSourceChangedError(
                "Reputation source data changed after the submission event was created"
            )

    if (
        validation_status == "accepted"
        and contribution.eligibility_status == ContributionEligibilityStatus.INELIGIBLE
        and not is_valid_independent_duplicate(validation)
    ):
        raise ReputationInputMismatchError(
            "Accepted validation cannot use an ineligible contribution score"
        )

    readiness_reproduction_status = (
        None if validation_status == "accepted" and reproduction is None else reproduction_status
    )
    readiness, readiness_reasons = determine_reputation_processing_readiness(
        validation_status,
        readiness_reproduction_status,
        contribution,
    )
    if readiness == ReputationProcessingStatus.PENDING:
        return ReputationProcessResponse(
            processing_status=ReputationProcessingStatus.PENDING,
            submission_id=submission.submission_id,
            node_id=node.node_id,
            event_id=None,
            previous_reputation=node.reputation_score,
            reputation_delta=0,
            current_reputation=node.reputation_score,
            message=" ".join(readiness_reasons),
            event=None,
        )

    if validation is None:
        raise ReputationProcessingError("Finalized validation input is required")
    if source_fingerprint is None:
        raise ReputationProcessingError("Unable to fingerprint finalized reputation inputs")
    if existing is not None:
        return _handle_existing_event(protocol_data_root, node, existing, source_fingerprint)

    prepared = build_reputation_event(node, submission, finding, validation, reproduction, contribution)
    claimed = save_prepared_event_exclusively(protocol_data_root, prepared)
    if claimed.event_id != prepared.event_id:
        return _handle_existing_event(protocol_data_root, node, claimed, source_fingerprint)
    return _apply_prepared_event(protocol_data_root, node, claimed)


def get_node_reputation(
    protocol_data_root: Path,
    node_id: str,
) -> NodeReputationResponse:
    node = load_node(protocol_data_root, node_id)
    if node is None:
        raise ReputationNodeNotFoundError("Node not found")
    events = list_reputation_events(
        protocol_data_root,
        node_id=node_id,
        application_status=ReputationEventApplicationStatus.APPLIED,
    )
    applied_times = [event.applied_at for event in events if event.applied_at is not None]
    return NodeReputationResponse(
        node_id=node.node_id,
        current_reputation=node.reputation_score,
        statistics=node.statistics,
        total_reputation_events=len(events),
        last_event_at=max(applied_times) if applied_times else None,
        reputation_version=REPUTATION_VERSION,
    )


def get_node_reputation_history(
    protocol_data_root: Path,
    node_id: str,
    category: str | None = None,
    event_type: ReputationEventType | None = None,
    application_status: ReputationEventApplicationStatus | None = None,
) -> list[ReputationEvent]:
    node = load_node(protocol_data_root, node_id)
    if node is None:
        raise ReputationNodeNotFoundError("Node not found")
    return list_reputation_events(
        protocol_data_root,
        node_id=node_id,
        category=category,
        event_type=event_type,
        application_status=application_status,
    )


def _verify_input_consistency(
    node: NodeRecord,
    submission: Any,
    finding: Any,
    validation: Any | None,
    reproduction: Any | None,
    contribution: ContributionScoreRecord,
) -> None:
    submission_id = _value(submission, "submission_id")
    node_id = _value(submission, "node_id")
    project_id = _value(submission, "project_id")
    finding_id = _value(submission, "finding_id")
    if node_id != node.node_id:
        raise ReputationInputMismatchError("Submission node does not match the loaded node")
    _require_reference(finding, "finding", project_id, finding_id)
    extract_category(submission, finding)

    checks = {
        "submission_id": submission_id,
        "node_id": node_id,
        "project_id": project_id,
        "finding_id": finding_id,
    }
    for field, expected in checks.items():
        if _value(contribution, field) != expected:
            raise ReputationInputMismatchError(f"Contribution {field} does not match the submission")

    if validation is not None:
        _require_reference(validation, "validation", project_id, finding_id)
        validation_id = _value(validation, "validation_id")
        if contribution.validation_id is not None and contribution.validation_id != validation_id:
            raise ReputationInputMismatchError("Contribution validation reference does not match")
        submission_validation_id = _value(submission, "validation_id")
        if submission_validation_id is not None and submission_validation_id != validation_id:
            raise ReputationInputMismatchError("Submission validation reference does not match")
    if reproduction is not None:
        _require_reference(reproduction, "reproduction", project_id, finding_id)
        reproduction_id = _value(reproduction, "reproduction_id")
        if contribution.reproduction_id is not None and contribution.reproduction_id != reproduction_id:
            raise ReputationInputMismatchError("Contribution reproduction reference does not match")
        submission_reproduction_id = _value(submission, "reproduction_id")
        if submission_reproduction_id is not None and submission_reproduction_id != reproduction_id:
            raise ReputationInputMismatchError("Submission reproduction reference does not match")

def _require_reference(record: Any, name: str, project_id: str, finding_id: str) -> None:
    if _value(record, "project_id") != project_id:
        raise ReputationInputMismatchError(f"{name.capitalize()} project does not match the submission")
    record_finding_id = _first_value(record, "finding_id", "id")
    if record_finding_id != finding_id:
        raise ReputationInputMismatchError(f"{name.capitalize()} finding does not match the submission")


def _handle_existing_event(
    protocol_data_root: Path,
    node: NodeRecord,
    event: ReputationEvent,
    source_fingerprint: str,
) -> ReputationProcessResponse:
    if event.source_fingerprint != source_fingerprint:
        raise ReputationSourceChangedError(
            "Reputation source data changed after the submission event was created"
        )
    if event.application_status == ReputationEventApplicationStatus.PREPARED:
        return _apply_prepared_event(protocol_data_root, node, event)
    return ReputationProcessResponse(
        processing_status=ReputationProcessingStatus.ALREADY_APPLIED,
        submission_id=event.submission_id,
        node_id=event.node_id,
        event_id=event.event_id,
        previous_reputation=event.previous_reputation,
        reputation_delta=event.delta_components.total_delta,
        current_reputation=node.reputation_score,
        message="Reputation was already applied for this submission.",
        event=event,
    )


def _apply_prepared_event(
    protocol_data_root: Path,
    node: NodeRecord,
    event: ReputationEvent,
) -> ReputationProcessResponse:
    updated_node = NodeRecord.model_validate(
        {
            **node.model_dump(),
            "reputation_score": event.new_reputation,
            "statistics": event.new_statistics,
        }
    )
    saved_node = save_node(protocol_data_root, updated_node)
    now = _utc_now()
    applied = ReputationEvent.model_validate(
        {
            **event.model_dump(),
            "application_status": ReputationEventApplicationStatus.APPLIED,
            "applied_at": event.applied_at or now,
            "updated_at": now,
        }
    )
    applied = save_reputation_event(protocol_data_root, applied)
    return ReputationProcessResponse(
        processing_status=ReputationProcessingStatus.APPLIED,
        submission_id=applied.submission_id,
        node_id=applied.node_id,
        event_id=applied.event_id,
        previous_reputation=applied.previous_reputation,
        reputation_delta=applied.delta_components.total_delta,
        current_reputation=saved_node.reputation_score,
        message="Reputation event was applied successfully.",
        event=applied,
    )


def _write_event_atomic(protocol_data_root: Path, event: ReputationEvent) -> None:
    event_dir = get_reputation_event_dir(protocol_data_root, event.submission_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    output_path = event_dir / REPUTATION_EVENT_FILENAME
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=event_dir,
            prefix=".reputation-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(_serialize_event(event))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output_path)
    except OSError as exc:
        raise ReputationStorageError("Unable to save reputation event") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _serialize_event(event: ReputationEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def _validate_reputation_id(identifier: str) -> None:
    if not isinstance(identifier, str) or not SAFE_REPUTATION_ID.fullmatch(identifier):
        raise InvalidReputationIdentifierError("Invalid reputation identifier")
    if identifier in {".", ".."} or "/" in identifier or "\\" in identifier or Path(identifier).is_absolute():
        raise InvalidReputationIdentifierError("Invalid reputation identifier")


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


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower()


def _signal(code: str, delta: float, message: str, source: str | None) -> ReputationSignal:
    return ReputationSignal(code=code, delta=round(delta, 6), message=message, source=source)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
