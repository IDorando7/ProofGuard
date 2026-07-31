import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.node import NodeStatus, NodeType, normalize_category
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionRecord,
    SubmissionRewardStatus,
    SubmissionStatus,
    SubmissionStatusUpdate,
)
from app.services.finding_service import load_project_finding as load_existing_project_finding
from app.services.node_registry_service import (
    NodeInactiveError,
    NodeNotFoundError,
    UnsupportedNodeCategoryError,
    load_node,
    node_supports_category,
)


SUBMISSION_FILENAME = "submission.json"
SAFE_SUBMISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

ALLOWED_STATUS_TRANSITIONS: dict[SubmissionStatus, set[SubmissionStatus]] = {
    SubmissionStatus.SUBMITTED: {
        SubmissionStatus.REPRODUCTION_PENDING,
        SubmissionStatus.VALIDATION_PENDING,
    },
    SubmissionStatus.REPRODUCTION_PENDING: {SubmissionStatus.VALIDATION_PENDING},
    SubmissionStatus.VALIDATION_PENDING: {
        SubmissionStatus.ACCEPTED,
        SubmissionStatus.REJECTED,
        SubmissionStatus.DUPLICATE,
        SubmissionStatus.OUT_OF_SCOPE,
        SubmissionStatus.INSUFFICIENT_EVIDENCE,
        SubmissionStatus.NEEDS_REVIEW,
        SubmissionStatus.UNSAFE,
        SubmissionStatus.UNSUPPORTED,
    },
    SubmissionStatus.ACCEPTED: {SubmissionStatus.REWARD_PENDING},
    SubmissionStatus.REJECTED: set(),
    SubmissionStatus.DUPLICATE: set(),
    SubmissionStatus.OUT_OF_SCOPE: set(),
    SubmissionStatus.INSUFFICIENT_EVIDENCE: set(),
    SubmissionStatus.NEEDS_REVIEW: set(),
    SubmissionStatus.UNSAFE: {SubmissionStatus.PENALIZED},
    SubmissionStatus.UNSUPPORTED: set(),
    SubmissionStatus.REWARD_PENDING: {SubmissionStatus.REWARDED},
    SubmissionStatus.REWARDED: set(),
    SubmissionStatus.PENALIZED: set(),
}

REPRODUCTION_REFERENCE_STATUSES = set(SubmissionStatus) - {SubmissionStatus.SUBMITTED}
VALIDATION_REFERENCE_STATUSES = {
    SubmissionStatus.VALIDATION_PENDING,
    SubmissionStatus.ACCEPTED,
    SubmissionStatus.REJECTED,
    SubmissionStatus.DUPLICATE,
    SubmissionStatus.OUT_OF_SCOPE,
    SubmissionStatus.INSUFFICIENT_EVIDENCE,
    SubmissionStatus.NEEDS_REVIEW,
    SubmissionStatus.UNSAFE,
    SubmissionStatus.UNSUPPORTED,
    SubmissionStatus.REWARD_PENDING,
    SubmissionStatus.REWARDED,
    SubmissionStatus.PENALIZED,
}


class SubmissionServiceError(ValueError):
    """Base error for submission protocol domain failures."""


class SubmissionNotFoundError(SubmissionServiceError):
    pass


class FindingNotFoundForSubmissionError(SubmissionServiceError):
    pass


class DuplicateSubmissionError(SubmissionServiceError):
    pass


class NodeCannotSubmitError(SubmissionServiceError):
    pass


class InvalidSubmissionStatusTransitionError(SubmissionServiceError):
    pass


class InvalidSubmissionIdentifierError(SubmissionServiceError):
    pass


class SubmissionProjectMismatchError(SubmissionServiceError):
    pass


class SubmissionRoutingMismatchError(SubmissionServiceError):
    pass


class SubmissionStorageError(SubmissionServiceError):
    pass


def get_submissions_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "submissions"


def get_submission_dir(protocol_data_root: Path, submission_id: str) -> Path:
    _validate_submission_id(submission_id)
    submissions_root = get_submissions_root(protocol_data_root)
    submission_dir = submissions_root / submission_id
    try:
        submission_dir.resolve().relative_to(submissions_root.resolve())
    except ValueError as exc:
        raise InvalidSubmissionIdentifierError("Invalid submission identifier") from exc
    return submission_dir


def load_project_finding(project_workspace: Path, finding_id: str) -> Any | None:
    return load_existing_project_finding(project_workspace, finding_id)


def normalize_submission_text(value: str | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Canonical finding text must be a string or null")
    normalized_line_endings = value.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized_line_endings.strip().lower().split())


def normalize_submission_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Canonical finding paths must be strings")
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return re.sub(r"/+", "/", normalized)


def normalize_submission_category(value: Any) -> str:
    try:
        return normalize_category(value)
    except ValueError as exc:
        raise UnsupportedNodeCategoryError(str(exc)) from exc


def build_canonical_finding_payload(project_id: str, finding: Any) -> dict[str, Any]:
    normalized_project_id = _required_text(project_id, "project_id")
    category = normalize_submission_category(_finding_value(finding, "category"))
    contracts = _normalized_unique_values(
        _finding_value(finding, "contracts", "contract", "affected_contracts"),
        normalize_submission_path,
    )
    functions = _normalized_unique_values(
        _finding_value(finding, "functions", "function", "affected_functions"),
        _normalize_function,
    )
    return {
        "project_id": normalized_project_id,
        "category": category,
        "contracts": contracts,
        "functions": functions,
        "root_cause": normalize_submission_text(_finding_value(finding, "root_cause")),
        "attack_path": normalize_submission_text(_finding_value(finding, "attack_path")),
        "impact": normalize_submission_text(_finding_value(finding, "impact")),
    }


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_finding_hash(project_id: str, finding: Any) -> str:
    payload = build_canonical_finding_payload(project_id, finding)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def create_submission(
    protocol_data_root: Path,
    project_workspace: Path,
    payload: SubmissionCreate,
) -> SubmissionRecord:
    finding = load_project_finding(project_workspace, payload.finding_id)
    if finding is None:
        raise FindingNotFoundForSubmissionError("Finding not found")

    stored_project_id = _finding_value(finding, "project_id")
    if stored_project_id is not None and str(stored_project_id).strip() != payload.project_id:
        raise SubmissionProjectMismatchError("Finding does not belong to the requested project")

    category = normalize_submission_category(_finding_value(finding, "category"))
    node = load_node(protocol_data_root, payload.node_id)
    if node is None:
        raise NodeNotFoundError("Node not found")
    if node.status != NodeStatus.ACTIVE:
        raise NodeInactiveError(f"Node is not active (status: {node.status.value})")
    if node.node_type not in {NodeType.AGENT, NodeType.HYBRID}:
        raise NodeCannotSubmitError("Validator-only nodes cannot submit agent findings")
    if not node_supports_category(node, category):
        raise UnsupportedNodeCategoryError("Node does not support the finding category")
    _validate_routing_linkage(
        protocol_data_root,
        payload.project_id,
        payload.node_id,
        category,
        payload.routing_id,
        payload.routing_assignment_id,
    )

    finding_hash = compute_finding_hash(payload.project_id, finding)
    duplicate = find_submission_by_node_and_hash(
        protocol_data_root,
        payload.project_id,
        payload.node_id,
        finding_hash,
    )
    if duplicate is not None:
        raise DuplicateSubmissionError("Node already submitted identical finding content for this project")

    now = _utc_now()
    finding_agent_name = _finding_value(finding, "agent_name")
    submission = SubmissionRecord(
        submission_id=str(uuid.uuid4()),
        project_id=payload.project_id,
        finding_id=payload.finding_id,
        node_id=payload.node_id,
        node_type=node.node_type.value,
        agent_name=payload.agent_name if payload.agent_name is not None else finding_agent_name,
        agent_version=payload.agent_version,
        category=category,
        finding_hash=finding_hash,
        status=SubmissionStatus.SUBMITTED,
        reproduction_id=None,
        validation_id=None,
        routing_id=payload.routing_id,
        routing_assignment_id=payload.routing_assignment_id,
        reward_status=SubmissionRewardStatus.PENDING,
        metadata=copy.deepcopy(payload.metadata),
        submitted_at=now,
        created_at=now,
        updated_at=now,
        status_updated_at=now,
        status_reason="Finding submission created.",
    )
    _write_submission(protocol_data_root, submission)
    return submission


def save_submission(protocol_data_root: Path, submission: SubmissionRecord) -> SubmissionRecord:
    saved = SubmissionRecord.model_validate(
        {
            **submission.model_dump(),
            "submitted_at": submission.submitted_at,
            "created_at": submission.created_at,
            "updated_at": _utc_now(),
        }
    )
    _write_submission(protocol_data_root, saved)
    return saved


def load_submission(protocol_data_root: Path, submission_id: str) -> SubmissionRecord | None:
    path = get_submission_dir(protocol_data_root, submission_id) / SUBMISSION_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise SubmissionStorageError("Stored submission record is not a regular file")
    try:
        return SubmissionRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise SubmissionStorageError(
            f"Stored submission record is malformed for submission '{submission_id}'"
        ) from exc


def list_submissions(
    protocol_data_root: Path,
    project_id: str | None = None,
    node_id: str | None = None,
    status: SubmissionStatus | None = None,
    category: str | None = None,
    reward_status: SubmissionRewardStatus | None = None,
) -> list[SubmissionRecord]:
    normalized_category = normalize_submission_category(category) if category is not None else None
    submissions_root = get_submissions_root(protocol_data_root)
    if not submissions_root.exists():
        return []

    submissions: list[SubmissionRecord] = []
    for path in sorted(submissions_root.glob(f"*/{SUBMISSION_FILENAME}")):
        submission = load_submission(protocol_data_root, path.parent.name)
        if submission is None:
            continue
        if project_id is not None and submission.project_id != project_id:
            continue
        if node_id is not None and submission.node_id != node_id:
            continue
        if status is not None and submission.status != status:
            continue
        if normalized_category is not None and submission.category != normalized_category:
            continue
        if reward_status is not None and submission.reward_status != reward_status:
            continue
        submissions.append(submission)
    return sorted(submissions, key=lambda item: (item.submitted_at, item.submission_id))


def find_submission_by_node_and_hash(
    protocol_data_root: Path,
    project_id: str,
    node_id: str,
    finding_hash: str,
) -> SubmissionRecord | None:
    if not SHA256_HEX.fullmatch(finding_hash):
        raise SubmissionServiceError("Finding hash must be a lowercase SHA-256 hexadecimal digest")
    for submission in list_submissions(
        protocol_data_root,
        project_id=project_id,
        node_id=node_id,
    ):
        if submission.finding_hash == finding_hash:
            return submission
    return None


def list_project_submissions(protocol_data_root: Path, project_id: str) -> list[SubmissionRecord]:
    return list_submissions(protocol_data_root, project_id=project_id)


def list_node_submissions(protocol_data_root: Path, node_id: str) -> list[SubmissionRecord]:
    return list_submissions(protocol_data_root, node_id=node_id)


def update_submission_status(
    protocol_data_root: Path,
    submission_id: str,
    update: SubmissionStatusUpdate,
) -> SubmissionRecord:
    existing = load_submission(protocol_data_root, submission_id)
    if existing is None:
        raise SubmissionNotFoundError("Submission not found")

    target_status = update.status
    if target_status != existing.status and target_status not in ALLOWED_STATUS_TRANSITIONS[existing.status]:
        raise InvalidSubmissionStatusTransitionError(
            f"Invalid submission status transition: {existing.status.value} -> {target_status.value}"
        )
    if update.reproduction_id is not None and target_status not in REPRODUCTION_REFERENCE_STATUSES:
        raise InvalidSubmissionStatusTransitionError(
            "A reproduction reference requires reproduction_pending or a later status"
        )
    if update.validation_id is not None and target_status not in VALIDATION_REFERENCE_STATUSES:
        raise InvalidSubmissionStatusTransitionError(
            "A validation reference requires validation_pending or a later status"
        )
    if update.reward_status == SubmissionRewardStatus.REWARDED and target_status != SubmissionStatus.REWARDED:
        raise InvalidSubmissionStatusTransitionError("Reward status rewarded requires submission status rewarded")
    if update.reward_status == SubmissionRewardStatus.PENALIZED and target_status != SubmissionStatus.PENALIZED:
        raise InvalidSubmissionStatusTransitionError("Reward status penalized requires submission status penalized")

    update_data: dict[str, Any] = {
        "status": target_status,
        "status_reason": update.reason,
        "status_updated_at": _utc_now(),
    }
    if update.reproduction_id is not None:
        update_data["reproduction_id"] = update.reproduction_id
    if update.validation_id is not None:
        update_data["validation_id"] = update.validation_id
    if update.reward_status is not None:
        update_data["reward_status"] = update.reward_status

    changed = SubmissionRecord.model_validate({**existing.model_dump(), **update_data})
    return save_submission(protocol_data_root, changed)


def attach_reproduction_reference(
    protocol_data_root: Path,
    submission_id: str,
    reproduction_id: str,
) -> SubmissionRecord:
    submission = load_submission(protocol_data_root, submission_id)
    if submission is None:
        raise SubmissionNotFoundError("Submission not found")
    return update_submission_status(
        protocol_data_root,
        submission_id,
        SubmissionStatusUpdate(
            status=submission.status,
            reason="Reproduction reference attached.",
            reproduction_id=reproduction_id,
        ),
    )


def attach_validation_reference(
    protocol_data_root: Path,
    submission_id: str,
    validation_id: str,
) -> SubmissionRecord:
    submission = load_submission(protocol_data_root, submission_id)
    if submission is None:
        raise SubmissionNotFoundError("Submission not found")
    return update_submission_status(
        protocol_data_root,
        submission_id,
        SubmissionStatusUpdate(
            status=submission.status,
            reason="Validation reference attached.",
            validation_id=validation_id,
        ),
    )


def _finding_value(finding: Any, *names: str) -> Any:
    for name in names:
        if isinstance(finding, dict) and name in finding:
            return finding[name]
        if hasattr(finding, name):
            return getattr(finding, name)
    return None


def _validate_routing_linkage(
    protocol_data_root: Path,
    project_id: str,
    node_id: str,
    category: str,
    routing_id: str | None,
    routing_assignment_id: str | None,
) -> None:
    if routing_id is None and routing_assignment_id is None:
        return
    if routing_id is None or routing_assignment_id is None:
        raise SubmissionRoutingMismatchError(
            "Routing identifiers must be provided together"
        )
    from app.schemas.routing import RoutingStatus
    from app.services.subnet_router_service import load_routing_record

    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise SubmissionRoutingMismatchError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise SubmissionRoutingMismatchError(
            "Submissions may link only to finalized routing"
        )
    assignment = next(
        (
            item
            for result in routing.results
            for item in result.assignments
            if item.assignment_id == routing_assignment_id
        ),
        None,
    )
    if assignment is None:
        raise SubmissionRoutingMismatchError(
            "Routing assignment does not belong to the routing record"
        )
    if (
        assignment.project_id != project_id
        or assignment.node_id != node_id
        or assignment.category.value != category
    ):
        raise SubmissionRoutingMismatchError(
            "Routing assignment does not match submission project, node, and category"
        )


def _normalized_unique_values(value: Any, normalizer) -> list[str]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        raise ValueError("Canonical finding collection fields must be strings or lists")
    normalized = {normalizer(item) for item in values}
    normalized.discard("")
    return sorted(normalized)


def _normalize_function(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Canonical finding functions must be strings")
    return " ".join(value.strip().split())


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value.strip()


def _validate_submission_id(submission_id: str) -> None:
    if not isinstance(submission_id, str) or not SAFE_SUBMISSION_ID.fullmatch(submission_id):
        raise InvalidSubmissionIdentifierError("Invalid submission identifier")
    if (
        submission_id in {".", ".."}
        or "/" in submission_id
        or "\\" in submission_id
        or Path(submission_id).is_absolute()
    ):
        raise InvalidSubmissionIdentifierError("Invalid submission identifier")


def _write_submission(protocol_data_root: Path, submission: SubmissionRecord) -> None:
    submission_dir = get_submission_dir(protocol_data_root, submission.submission_id)
    submission_dir.mkdir(parents=True, exist_ok=True)
    output_path = submission_dir / SUBMISSION_FILENAME
    serialized = json.dumps(submission.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=submission_dir,
            prefix=".submission-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        Path(temporary_name).replace(output_path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise SubmissionStorageError("Unable to persist submission record") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
