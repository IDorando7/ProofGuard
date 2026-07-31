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

from app.schemas.reputation import ReputationEvent, ReputationEventType
from app.schemas.reward import (
    PenaltyEvent,
    PenaltyEventStatus,
    PenaltyProcessResponse,
    PenaltyReasonCode,
)
from app.schemas.submission import (
    SubmissionRecord,
    SubmissionRewardStatus,
    SubmissionStatus,
    SubmissionStatusUpdate,
)
from app.services.finding_service import load_project_finding
from app.services.node_registry_service import load_node
from app.services.reproduction_service import load_reproduction_result
from app.services.reputation_service import load_reputation_event_by_submission
from app.services.submission_service import load_submission, update_submission_status
from app.services.validation_service import load_validation_decision


PENALTY_VERSION = "penalty_v0"
PENALTY_EVENT_FILENAME = "penalty_event.json"
SAFE_PENALTY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class PenaltyServiceError(ValueError):
    """Base error for off-chain penalty-record failures."""


class PenaltyNotApplicableError(PenaltyServiceError):
    pass


class PenaltyEventNotFoundError(PenaltyServiceError):
    pass


class PenaltyInputMismatchError(PenaltyServiceError):
    pass


class PenaltySourceChangedError(PenaltyServiceError):
    pass


class InvalidPenaltyIdentifierError(PenaltyServiceError):
    pass


class PenaltyStorageError(PenaltyServiceError):
    pass


def get_penalties_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "penalties"


def get_penalty_events_root(protocol_data_root: Path) -> Path:
    return get_penalties_root(protocol_data_root) / "events"


def get_penalty_event_dir(protocol_data_root: Path, submission_id: str) -> Path:
    _validate_penalty_id(submission_id)
    root = get_penalty_events_root(protocol_data_root)
    event_dir = root / submission_id
    try:
        event_dir.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidPenaltyIdentifierError("Invalid penalty identifier") from exc
    return event_dir


def determine_penalty_eligibility(
    submission: SubmissionRecord,
    validation: Any | None,
    reproduction: Any | None,
    reputation_event: ReputationEvent | None,
) -> tuple[bool, PenaltyReasonCode | None, list[str]]:
    reasons: list[str] = []
    validation_status = _status(validation)
    reproduction_status = _status(reproduction)
    reputation_type = _enum_value(_value(reputation_event, "event_type"))
    if validation_status in {"unsafe", "unsafe_poc"}:
        reasons.append("Validation marked the submission unsafe.")
    if reproduction_status == "rejected_unsafe":
        reasons.append("Safety preflight rejected the reproduction as unsafe.")
    if reputation_type == ReputationEventType.UNSAFE_SUBMISSION.value:
        reasons.append("The applied ReputationEvent classifies this as an unsafe submission.")
    if reasons:
        return True, PenaltyReasonCode.UNSAFE_SUBMISSION, reasons
    return False, None, ["No finalized unsafe signal makes a Day 6 PenaltyEvent applicable."]


def build_penalty_source_fingerprint(
    submission: SubmissionRecord,
    validation: Any | None,
    reproduction: Any | None,
    reputation_event: ReputationEvent | None,
    reason_code: PenaltyReasonCode,
) -> str:
    payload = {
        "submission_id": submission.submission_id,
        "node_id": submission.node_id,
        "finding_id": submission.finding_id,
        "project_id": submission.project_id,
        "category": submission.category,
        "validation_id": _value(validation, "validation_id"),
        "validation_status": _status(validation),
        "reproduction_id": _value(reproduction, "reproduction_id"),
        "reproduction_status": _status(reproduction),
        "reputation_event_id": _value(reputation_event, "event_id"),
        "reputation_event_type": _enum_value(_value(reputation_event, "event_type")),
        "penalty_version": PENALTY_VERSION,
        "penalty_reason_code": reason_code.value,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def process_submission_penalty(
    protocol_data_root: Path,
    project_workspace: Path,
    submission_id: str,
) -> PenaltyProcessResponse:
    try:
        submission = load_submission(protocol_data_root, submission_id)
    except ValueError as exc:
        if exc.__class__.__name__ == "InvalidSubmissionIdentifierError":
            raise InvalidPenaltyIdentifierError("Invalid penalty identifier") from exc
        raise
    if submission is None:
        raise PenaltyEventNotFoundError("Submission not found")
    node = load_node(protocol_data_root, submission.node_id)
    if node is None:
        raise PenaltyInputMismatchError("Submission node is missing")
    finding = load_project_finding(project_workspace, submission.finding_id)
    if finding is None:
        raise PenaltyInputMismatchError("Submission finding is missing")
    validation = load_validation_decision(project_workspace, submission.finding_id)
    reproduction = load_reproduction_result(project_workspace, submission.finding_id)
    reputation_event = load_reputation_event_by_submission(protocol_data_root, submission.submission_id)
    _verify_consistency(submission, node, finding, validation, reproduction, reputation_event)

    eligible, reason_code, reasons = determine_penalty_eligibility(
        submission,
        validation,
        reproduction,
        reputation_event,
    )
    if not eligible or reason_code is None:
        raise PenaltyNotApplicableError(reasons[0])
    fingerprint = build_penalty_source_fingerprint(
        submission,
        validation,
        reproduction,
        reputation_event,
        reason_code,
    )
    existing = load_penalty_event_by_submission(protocol_data_root, submission.submission_id)
    if existing is not None:
        if existing.source_fingerprint != fingerprint:
            raise PenaltySourceChangedError("Penalty source data changed after the event was recorded")
        _mark_submission_penalized(protocol_data_root, submission)
        return PenaltyProcessResponse(
            submission_id=existing.submission_id,
            node_id=existing.node_id,
            created=False,
            event=existing,
            message="PenaltyEvent was already recorded for this unsafe submission.",
        )

    now = _utc_now()
    if submission.status not in {SubmissionStatus.UNSAFE, SubmissionStatus.PENALIZED}:
        raise PenaltyInputMismatchError("Only a finalized unsafe submission may be penalized")
    event = PenaltyEvent(
        penalty_event_id=str(uuid.uuid4()),
        penalty_version=PENALTY_VERSION,
        status=PenaltyEventStatus.RECORDED,
        node_id=submission.node_id,
        submission_id=submission.submission_id,
        project_id=submission.project_id,
        finding_id=submission.finding_id,
        category=submission.category,
        reason_code=PenaltyReasonCode.UNSAFE_SUBMISSION,
        reward_denied=True,
        protocol_penalty_points=25,
        reputation_event_id=_value(reputation_event, "event_id"),
        simulated_stake_loss=0,
        executed_onchain=False,
        requires_human_review=True,
        source_fingerprint=fingerprint,
        reason=(
            "Unsafe submission recorded. Reward denied and 25 simulated protocol penalty "
            "points assigned for review."
        ),
        created_at=now,
        updated_at=now,
    )
    event = save_penalty_event_exclusively(protocol_data_root, event)
    _mark_submission_penalized(protocol_data_root, submission)
    return PenaltyProcessResponse(
        submission_id=event.submission_id,
        node_id=event.node_id,
        created=True,
        event=event,
        message="Unsafe PenaltyEvent recorded; no stake or financial value was changed.",
    )


def save_penalty_event_exclusively(
    protocol_data_root: Path,
    event: PenaltyEvent,
) -> PenaltyEvent:
    event_dir = get_penalty_event_dir(protocol_data_root, event.submission_id)
    event_dir.mkdir(parents=True, exist_ok=True)
    output_path = event_dir / PENALTY_EVENT_FILENAME
    serialized = json.dumps(event.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=event_dir, prefix=".penalty-event-", suffix=".tmp", delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_name, output_path)
    except FileExistsError:
        existing = load_penalty_event_by_submission(protocol_data_root, event.submission_id)
        if existing is not None and existing.source_fingerprint == event.source_fingerprint:
            return existing
        raise PenaltySourceChangedError("A conflicting PenaltyEvent already exists")
    except OSError as exc:
        raise PenaltyStorageError("Unable to persist penalty event") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return event


def load_penalty_event_by_submission(
    protocol_data_root: Path,
    submission_id: str,
) -> PenaltyEvent | None:
    path = get_penalty_event_dir(protocol_data_root, submission_id) / PENALTY_EVENT_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise PenaltyStorageError("Stored penalty event is malformed")
    try:
        return PenaltyEvent.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise PenaltyStorageError("Stored penalty event is malformed") from exc


def load_penalty_event_by_id(
    protocol_data_root: Path,
    penalty_event_id: str,
) -> PenaltyEvent | None:
    _validate_penalty_id(penalty_event_id)
    for event in list_penalty_events(protocol_data_root):
        if event.penalty_event_id == penalty_event_id:
            return event
    return None


def list_penalty_events(
    protocol_data_root: Path,
    node_id: str | None = None,
    project_id: str | None = None,
    category: str | None = None,
    reason_code: PenaltyReasonCode | None = None,
) -> list[PenaltyEvent]:
    if category is not None:
        from app.schemas.node import normalize_category

        category = normalize_category(category)
    root = get_penalty_events_root(protocol_data_root)
    if not root.exists():
        return []
    events = []
    for path in sorted(root.glob(f"*/{PENALTY_EVENT_FILENAME}")):
        event = load_penalty_event_by_submission(protocol_data_root, path.parent.name)
        if event is None:
            continue
        if node_id is not None and event.node_id != node_id:
            continue
        if project_id is not None and event.project_id != project_id:
            continue
        if category is not None and event.category != category:
            continue
        if reason_code is not None and event.reason_code != reason_code:
            continue
        events.append(event)
    return sorted(events, key=lambda item: (item.created_at, item.penalty_event_id))


def _verify_consistency(submission, node, finding, validation, reproduction, reputation_event) -> None:
    if node.node_id != submission.node_id:
        raise PenaltyInputMismatchError("Node does not match the submission")
    if _value(finding, "project_id") != submission.project_id or _first_value(finding, "finding_id", "id") != submission.finding_id:
        raise PenaltyInputMismatchError("Finding does not match the submission")
    for name, record, identifier_field, expected_identifier in (
        ("Validation", validation, "validation_id", submission.validation_id),
        ("Reproduction", reproduction, "reproduction_id", submission.reproduction_id),
    ):
        if record is None:
            continue
        if _value(record, "project_id") != submission.project_id or _value(record, "finding_id") != submission.finding_id:
            raise PenaltyInputMismatchError(f"{name} does not match the submission")
        if expected_identifier is not None and _value(record, identifier_field) != expected_identifier:
            raise PenaltyInputMismatchError(f"{name} identifier does not match the submission")
    if reputation_event is not None:
        for field in ("submission_id", "node_id", "project_id", "finding_id"):
            if _value(reputation_event, field) != _value(submission, field):
                raise PenaltyInputMismatchError(f"ReputationEvent {field} does not match the submission")


def _mark_submission_penalized(protocol_data_root: Path, submission: SubmissionRecord) -> None:
    current = load_submission(protocol_data_root, submission.submission_id)
    if current is None:
        raise PenaltyEventNotFoundError("Submission not found")
    if current.status == SubmissionStatus.PENALIZED and current.reward_status == SubmissionRewardStatus.PENALIZED:
        return
    if current.status != SubmissionStatus.UNSAFE:
        raise PenaltyInputMismatchError("Submission lifecycle does not permit penalization")
    update_submission_status(
        protocol_data_root,
        current.submission_id,
        SubmissionStatusUpdate(
            status=SubmissionStatus.PENALIZED,
            reward_status=SubmissionRewardStatus.PENALIZED,
            reason="Unsafe PenaltyEvent recorded for required human review.",
            reproduction_id=current.reproduction_id,
            validation_id=current.validation_id,
        ),
    )


def _validate_penalty_id(identifier: str) -> None:
    if not isinstance(identifier, str) or not SAFE_PENALTY_ID.fullmatch(identifier):
        raise InvalidPenaltyIdentifierError("Invalid penalty identifier")
    if identifier in {".", ".."} or "/" in identifier or "\\" in identifier or Path(identifier).is_absolute():
        raise InvalidPenaltyIdentifierError("Invalid penalty identifier")


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
    value = _enum_value(_value(source, "status"))
    return str(value).strip().lower() if value is not None else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
