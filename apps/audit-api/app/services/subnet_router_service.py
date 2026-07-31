import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.category_score import CategoryScoreRecord
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, NodeStatus, normalize_category
from app.schemas.routing import (
    ROUTING_VERSION,
    CategoryRoutingResult,
    ProjectRoutingRecord,
    ProjectRoutingRequest,
    RoutingAssignment,
    RoutingAssignmentMode,
    RoutingCalculationResponse,
    RoutingCandidateSnapshot,
    RoutingFinalizationResponse,
    RoutingProcessingStatus,
    RoutingSelectionType,
    RoutingShortageReason,
    RoutingStatus,
    RoutingUsageEvent,
    RoutingUsageSummary,
)
from app.schemas.scope import ScopeManifest
from app.schemas.subnet import (
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetRecord,
    SubnetStatus,
)
from app.services.category_scoring_service import (
    CategoryScoreStorageError,
    load_category_score,
)
from app.services.node_registry_service import (
    InvalidNodeIdentifierError,
    NodeStorageError,
    load_node,
)
from app.services.scope_service import read_parsed_scope
from app.services.subnet_registry_service import (
    InvalidSubnetIdentifierError,
    SubnetStorageError,
    find_subnet_by_category,
    list_subnet_members,
)


ROUTING_PROJECTS_DIRECTORY = "projects"
ROUTING_USAGE_DIRECTORY = "usage"
ROUTING_FILENAME = "routing.json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


class SubnetRouterError(ValueError):
    """Base error for deterministic subnet-routing failures."""


class RoutingProjectNotFoundError(SubnetRouterError):
    pass


class RoutingProjectScopeMissingError(SubnetRouterError):
    pass


class RoutingProjectCategoryMissingError(SubnetRouterError):
    pass


class RoutingCategoryNotInScopeError(SubnetRouterError):
    pass


class RoutingSubnetNotFoundError(SubnetRouterError):
    pass


class RoutingNoEligibleNodesError(SubnetRouterError):
    pass


class RoutingIncompleteError(SubnetRouterError):
    pass


class RoutingRecordNotFoundError(SubnetRouterError):
    pass


class RoutingAlreadyFinalizedError(SubnetRouterError):
    pass


class RoutingSupersededError(SubnetRouterError):
    pass


class RoutingSourceChangedError(SubnetRouterError):
    pass


class RoutingUsageConflictError(SubnetRouterError):
    pass


class RoutingInputMismatchError(SubnetRouterError):
    pass


class InvalidRoutingIdentifierError(SubnetRouterError):
    pass


class RoutingStorageError(SubnetRouterError):
    pass


@dataclass(frozen=True)
class RoutingCandidateInput:
    node: NodeRecord | None
    member: SubnetMemberRecord
    score: CategoryScoreRecord | None
    usage: RoutingUsageSummary
    usage_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoutingCategoryContext:
    category: FindingCategory
    subnet: SubnetRecord | None
    candidates: tuple[RoutingCandidateInput, ...]


def get_routing_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "routing"


def get_project_routing_root(
    protocol_data_root: Path,
    project_id: str,
) -> Path:
    _validate_identifier(project_id, "project")
    projects_root = get_routing_root(protocol_data_root) / ROUTING_PROJECTS_DIRECTORY
    project_root = projects_root / project_id
    _ensure_within(project_root, projects_root, "Invalid project identifier")
    return project_root


def get_routing_record_path(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    project_root = get_project_routing_root(protocol_data_root, project_id)
    routing_root = project_root / routing_id
    _ensure_within(routing_root, project_root, "Invalid routing identifier")
    return routing_root / ROUTING_FILENAME


def get_routing_usage_root(protocol_data_root: Path) -> Path:
    return get_routing_root(protocol_data_root) / ROUTING_USAGE_DIRECTORY


def resolve_project_routing_categories(
    project_workspace: Path,
    requested_categories: list[str | FindingCategory] | None,
) -> list[str]:
    scope = _load_project_scope(project_workspace)
    scope_categories = sorted(
        {_normalize_category(category) for category in scope.attack_categories}
    )
    if not scope_categories:
        raise RoutingProjectCategoryMissingError(
            "Project scope has no attack categories"
        )
    if requested_categories is None:
        return scope_categories
    normalized = [_normalize_category(category) for category in requested_categories]
    if len(normalized) != len(set(normalized)):
        raise RoutingInputMismatchError("Requested routing categories must be unique")
    outside_scope = sorted(set(normalized) - set(scope_categories))
    if outside_scope:
        raise RoutingCategoryNotInScopeError(
            "Requested categories are outside project scope: "
            + ", ".join(outside_scope)
        )
    if not normalized:
        raise RoutingProjectCategoryMissingError(
            "At least one project attack category is required"
        )
    return sorted(normalized)


def calculate_routing_slot_targets(
    nodes_per_category: int,
    exploration_ratio: float,
    include_exploration: bool,
) -> tuple[int, int]:
    if nodes_per_category < 1 or nodes_per_category > 100:
        raise RoutingInputMismatchError(
            "nodes_per_category must be between 1 and 100"
        )
    if exploration_ratio < 0 or exploration_ratio > 1:
        raise RoutingInputMismatchError(
            "Subnet exploration ratio must be between 0 and 1"
        )
    if (
        not include_exploration
        or nodes_per_category == 1
        or exploration_ratio == 0
    ):
        return nodes_per_category, 0
    exploration = min(
        nodes_per_category - 1,
        math.ceil(nodes_per_category * exploration_ratio),
    )
    return nodes_per_category - exploration, exploration


def is_ranked_routing_candidate(
    node: NodeRecord,
    subnet: SubnetRecord,
    member: SubnetMemberRecord,
    score: CategoryScoreRecord | None,
) -> tuple[bool, list[str]]:
    failures = _common_candidate_failures(node, subnet, member)
    if member.status not in {
        SubnetMemberStatus.ACTIVE,
        SubnetMemberStatus.EXPERT,
    }:
        failures.append("Membership is not active or expert.")
    if score is None:
        failures.append("A current category-score record is required.")
    elif not _score_matches_member(score, member, subnet):
        failures.append("Category-score source does not match membership snapshot.")
    if failures:
        return False, failures
    return True, [
        "Node and subnet are active and source snapshots are consistent.",
        (
            f"Eligible as {member.status.value} member with category score "
            f"{score.category_score:.6f}."
        ),
    ]


def is_exploration_routing_candidate(
    node: NodeRecord,
    subnet: SubnetRecord,
    member: SubnetMemberRecord,
    score: CategoryScoreRecord | None,
) -> tuple[bool, list[str]]:
    failures = _common_candidate_failures(node, subnet, member)
    if member.status not in {
        SubnetMemberStatus.CANDIDATE,
        SubnetMemberStatus.PROBATION,
    }:
        failures.append("Membership is not candidate or probation.")
    if score is None:
        missing_score_allowed = (
            member.status == SubnetMemberStatus.CANDIDATE
            and member.finalized_submissions < 3
            and member.category_score_source_fingerprint is None
        )
        if not missing_score_allowed:
            failures.append(
                "Only insufficient-history candidates may explore without a score."
            )
    elif not _score_matches_member(score, member, subnet):
        failures.append("Category-score source does not match membership snapshot.")
    if failures:
        return False, failures
    return True, [
        f"Eligible for shadow exploration as a {member.status.value} member.",
        "Shadow assignment is non-authoritative during Router v0.",
    ]


def collect_node_routing_usage(
    protocol_data_root: Path,
    node_id: str,
    subnet_id: str,
    category: str | FindingCategory,
) -> RoutingUsageSummary:
    normalized = _normalize_category(category)
    events = [
        event
        for event in list_routing_usage_events(
            protocol_data_root,
            node_id=node_id,
            subnet_id=subnet_id,
            category=normalized,
        )
        if _usage_has_finalized_route(protocol_data_root, event)
    ]
    exploration = [
        event
        for event in events
        if event.selection_type == RoutingSelectionType.EXPLORATION
    ]
    return RoutingUsageSummary(
        total_assignments=len(events),
        ranked_assignments=sum(
            event.selection_type == RoutingSelectionType.RANKED
            for event in events
        ),
        exploration_assignments=len(exploration),
        last_assignment_at=max(
            (event.applied_at for event in events), default=None
        ),
        last_exploration_assignment_at=max(
            (event.applied_at for event in exploration), default=None
        ),
    )


def calculate_category_routing(
    protocol_data_root: Path,
    project_id: str,
    subnet: SubnetRecord,
    nodes_per_category: int,
    include_exploration: bool,
    calculated_at: datetime,
    *,
    routing_id: str | None = None,
) -> CategoryRoutingResult:
    calculated_at = _as_utc(calculated_at)
    context = _collect_category_context(
        protocol_data_root,
        subnet.category,
        subnet=subnet,
    )
    return _calculate_category_from_context(
        context,
        project_id,
        routing_id or _preview_routing_id(project_id, subnet.category),
        nodes_per_category,
        include_exploration,
        calculated_at,
    )


def build_routing_source_payload(
    project: Any,
    project_scope_fingerprint: str,
    request: ProjectRoutingRequest,
    category_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    project_id = _value(project, "project_id", "id")
    if not isinstance(project_id, str) or not project_id:
        raise RoutingInputMismatchError("Project identity is required")
    return {
        "routing_version": ROUTING_VERSION,
        "project": {
            "project_id": project_id,
            "scope_fingerprint": project_scope_fingerprint,
            "requested_categories": sorted(
                item["category"] for item in category_inputs
            ),
        },
        "request": {
            "nodes_per_category": request.nodes_per_category,
            "include_exploration": request.include_exploration,
            "allow_partial": request.allow_partial,
        },
        "category_inputs": sorted(
            category_inputs, key=lambda item: item["category"]
        ),
    }


def compute_routing_source_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def build_routing_request_fingerprint(
    project_id: str,
    request: ProjectRoutingRequest,
    resolved_categories: list[str | FindingCategory],
) -> str:
    categories = sorted(_normalize_category(item) for item in resolved_categories)
    payload = {
        "routing_version": ROUTING_VERSION,
        "project_id": project_id,
        "categories": categories,
        "nodes_per_category": request.nodes_per_category,
        "include_exploration": request.include_exploration,
        "allow_partial": request.allow_partial,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def calculate_project_routing(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    request: ProjectRoutingRequest,
) -> RoutingCalculationResponse:
    project = _load_project_metadata(project_workspace, project_id)
    scope = _load_project_scope(project_workspace)
    categories = resolve_project_routing_categories(
        project_workspace, request.categories
    )
    scope_fingerprint = _fingerprint_scope(scope)
    contexts = [
        _collect_category_context(protocol_data_root, category)
        for category in categories
    ]
    category_inputs = [
        _category_context_source_input(context) for context in contexts
    ]
    source_payload = build_routing_source_payload(
        project,
        scope_fingerprint,
        request,
        category_inputs,
    )
    source_fingerprint = compute_routing_source_fingerprint(source_payload)
    request_fingerprint = build_routing_request_fingerprint(
        project_id, request, categories
    )
    records = list_project_routing_records(protocol_data_root, project_id)
    for existing in records:
        if (
            existing.request_fingerprint == request_fingerprint
            and existing.source_fingerprint == source_fingerprint
            and existing.status
            in {RoutingStatus.CALCULATED, RoutingStatus.FINALIZED}
        ):
            return RoutingCalculationResponse(
                status=RoutingProcessingStatus.UNCHANGED,
                record=existing,
                message="Routing inputs are unchanged; the existing plan was reused.",
            )

    now = datetime.now(timezone.utc)
    routing_id = str(uuid.uuid4())
    results = [
        _calculate_category_from_context(
            context,
            project_id,
            routing_id,
            request.nodes_per_category,
            request.include_exploration,
            now,
        )
        for context in contexts
    ]
    incomplete = [result for result in results if not result.complete]
    if incomplete and not request.allow_partial:
        categories_text = ", ".join(result.category.value for result in incomplete)
        raise RoutingIncompleteError(
            f"Routing is incomplete for categories: {categories_text}"
        )

    prior = next(
        (
            record
            for record in records
            if record.request_fingerprint == request_fingerprint
            and record.status == RoutingStatus.CALCULATED
        ),
        None,
    )
    assignments = [
        assignment for result in results for assignment in result.assignments
    ]
    record = ProjectRoutingRecord(
        routing_id=routing_id,
        routing_version=ROUTING_VERSION,
        project_id=project_id,
        project_scope_fingerprint=scope_fingerprint,
        requested_categories=categories,
        nodes_per_category=request.nodes_per_category,
        include_exploration=request.include_exploration,
        allow_partial=request.allow_partial,
        status=RoutingStatus.CALCULATED,
        results=results,
        total_assignments=len(assignments),
        production_assignments=sum(
            assignment.assignment_mode == RoutingAssignmentMode.PRODUCTION
            for assignment in assignments
        ),
        shadow_assignments=sum(
            assignment.assignment_mode == RoutingAssignmentMode.SHADOW
            for assignment in assignments
        ),
        complete_categories=sum(result.complete for result in results),
        partial_categories=sum(
            not result.complete and result.total_selected > 0
            for result in results
        ),
        failed_categories=sum(
            not result.complete and result.total_selected == 0
            for result in results
        ),
        source_fingerprint=source_fingerprint,
        request_fingerprint=request_fingerprint,
        supersedes_routing_id=prior.routing_id if prior is not None else None,
        superseded_by_routing_id=None,
        description=request.description,
        calculated_at=now,
        finalized_at=None,
        created_at=now,
        updated_at=now,
    )
    saved = save_routing_record(protocol_data_root, record)
    if prior is not None:
        supersede_routing(
            protocol_data_root,
            project_id,
            prior.routing_id,
            saved.routing_id,
        )
    return RoutingCalculationResponse(
        status=RoutingProcessingStatus.CREATED,
        record=saved,
        message=(
            "Routing plan calculated and stored."
            if not incomplete
            else "Partial routing plan calculated and stored with explicit shortages."
        ),
    )


def finalize_project_routing(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
) -> RoutingFinalizationResponse:
    _load_project_metadata(project_workspace, project_id)
    record = load_routing_record(protocol_data_root, project_id, routing_id)
    if record is None:
        raise RoutingRecordNotFoundError("Routing record not found")
    if record.status == RoutingStatus.FINALIZED:
        return RoutingFinalizationResponse(
            status=RoutingProcessingStatus.ALREADY_FINALIZED,
            record=record,
            usage_events_created=0,
            usage_events_existing=record.total_assignments,
            message="Routing was already finalized; no usage was duplicated.",
        )
    if record.status == RoutingStatus.SUPERSEDED:
        raise RoutingSupersededError("Superseded routing cannot be finalized")

    request = ProjectRoutingRequest(
        categories=record.requested_categories,
        nodes_per_category=record.nodes_per_category,
        include_exploration=record.include_exploration,
        allow_partial=record.allow_partial,
        description=record.description,
    )
    try:
        scope = _load_project_scope(project_workspace)
        categories = resolve_project_routing_categories(
            project_workspace, record.requested_categories
        )
        contexts = [
            _collect_category_context(protocol_data_root, category)
            for category in categories
        ]
        current_payload = build_routing_source_payload(
            {"project_id": project_id},
            _fingerprint_scope(scope),
            request,
            [
                _category_context_source_input(context)
                for context in contexts
            ],
        )
        current_fingerprint = compute_routing_source_fingerprint(current_payload)
    except SubnetRouterError as exc:
        raise RoutingSourceChangedError(
            "Current routing sources no longer match the calculated plan"
        ) from exc
    if current_fingerprint != record.source_fingerprint:
        raise RoutingSourceChangedError(
            "Routing sources changed after calculation"
        )

    now = datetime.now(timezone.utc)
    created = 0
    existing = 0
    for assignment in (
        assignment for result in record.results for assignment in result.assignments
    ):
        event = _build_usage_event(record, assignment, now)
        stored, was_created = save_routing_usage_event_exclusively(
            protocol_data_root, event
        )
        if stored.assignment_id != assignment.assignment_id:
            raise RoutingUsageConflictError("Routing usage assignment mismatch")
        if was_created:
            created += 1
        else:
            existing += 1

    finalized = ProjectRoutingRecord.model_validate(
        {
            **record.model_dump(),
            "status": RoutingStatus.FINALIZED,
            "finalized_at": now,
            "updated_at": now,
        }
    )
    saved = save_routing_record(protocol_data_root, finalized)
    return RoutingFinalizationResponse(
        status=RoutingProcessingStatus.FINALIZED,
        record=saved,
        usage_events_created=created,
        usage_events_existing=existing,
        message="Routing finalized and assignment usage recorded.",
    )


def supersede_routing(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    replacement_routing_id: str,
) -> ProjectRoutingRecord:
    record = load_routing_record(protocol_data_root, project_id, routing_id)
    if record is None:
        raise RoutingRecordNotFoundError("Routing record not found")
    replacement = load_routing_record(
        protocol_data_root, project_id, replacement_routing_id
    )
    if replacement is None:
        raise RoutingRecordNotFoundError("Replacement routing record not found")
    if record.status == RoutingStatus.FINALIZED:
        raise RoutingAlreadyFinalizedError("Finalized routing cannot be superseded")
    if record.status == RoutingStatus.SUPERSEDED:
        if record.superseded_by_routing_id == replacement_routing_id:
            return record
        raise RoutingSupersededError("Routing was already superseded")
    if routing_id == replacement_routing_id:
        raise RoutingInputMismatchError("Routing cannot supersede itself")
    now = datetime.now(timezone.utc)
    superseded = ProjectRoutingRecord.model_validate(
        {
            **record.model_dump(),
            "status": RoutingStatus.SUPERSEDED,
            "superseded_by_routing_id": replacement_routing_id,
            "updated_at": now,
        }
    )
    return save_routing_record(protocol_data_root, superseded)


def save_routing_record(
    protocol_data_root: Path,
    record: ProjectRoutingRecord,
) -> ProjectRoutingRecord:
    validated = ProjectRoutingRecord.model_validate(record.model_dump())
    existing = load_routing_record(
        protocol_data_root, validated.project_id, validated.routing_id
    )
    if existing is not None:
        if existing.status == RoutingStatus.FINALIZED:
            if existing == validated:
                return existing
            raise RoutingAlreadyFinalizedError(
                "Finalized routing records are immutable"
            )
        if existing.status == RoutingStatus.SUPERSEDED:
            if existing == validated:
                return existing
            raise RoutingSupersededError("Superseded routing records are immutable")
        if validated.status not in {
            RoutingStatus.FINALIZED,
            RoutingStatus.SUPERSEDED,
        }:
            if existing == validated:
                return existing
            raise RoutingInputMismatchError(
                "Calculated routing records cannot be edited"
            )
        if _immutable_routing_payload(existing) != _immutable_routing_payload(
            validated
        ):
            raise RoutingInputMismatchError(
                "Routing transition attempted to modify immutable plan data"
            )
        values = validated.model_dump()
        values["created_at"] = existing.created_at
        values["calculated_at"] = existing.calculated_at
        validated = ProjectRoutingRecord.model_validate(values)
    _write_routing_record(protocol_data_root, validated)
    return validated


def load_routing_record(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
) -> ProjectRoutingRecord | None:
    path = get_routing_record_path(
        protocol_data_root, project_id, routing_id
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise RoutingStorageError("Stored routing record is not a regular file")
    try:
        record = ProjectRoutingRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise RoutingStorageError("Stored routing record is malformed") from exc
    if record.project_id != project_id or record.routing_id != routing_id:
        raise RoutingStorageError("Stored routing identity does not match its path")
    return record


def load_routing_record_by_id(
    protocol_data_root: Path,
    routing_id: str,
) -> ProjectRoutingRecord | None:
    _validate_identifier(routing_id, "routing")
    projects_root = get_routing_root(protocol_data_root) / ROUTING_PROJECTS_DIRECTORY
    if not projects_root.exists():
        return None
    matches = sorted(projects_root.glob(f"*/{routing_id}/{ROUTING_FILENAME}"))
    if not matches:
        return None
    if len(matches) > 1:
        raise RoutingStorageError("Routing identifier is not globally unique")
    return load_routing_record(
        protocol_data_root, matches[0].parents[1].name, routing_id
    )


def list_project_routing_records(
    protocol_data_root: Path,
    project_id: str,
    status: RoutingStatus | None = None,
) -> list[ProjectRoutingRecord]:
    root = get_project_routing_root(protocol_data_root, project_id)
    if not root.exists():
        return []
    records: list[ProjectRoutingRecord] = []
    for path in sorted(root.glob(f"*/{ROUTING_FILENAME}")):
        record = load_routing_record(
            protocol_data_root, project_id, path.parent.name
        )
        if record is None:
            continue
        if status is not None and record.status != status:
            continue
        records.append(record)
    return sorted(
        records,
        key=lambda record: (
            -record.created_at.timestamp(),
            record.routing_id,
        ),
    )


def save_routing_usage_event_exclusively(
    protocol_data_root: Path,
    event: RoutingUsageEvent,
) -> tuple[RoutingUsageEvent, bool]:
    path = _routing_usage_path(
        protocol_data_root, event.node_id, event.assignment_id
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_usage_path(path) if path.exists() else None
    if existing is not None:
        if _usage_identity(existing) != _usage_identity(event):
            raise RoutingUsageConflictError(
                "Conflicting routing usage exists for assignment"
            )
        return existing, False
    serialized = json.dumps(
        event.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".routing-usage-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            claimed = _load_usage_path(path)
            if _usage_identity(claimed) != _usage_identity(event):
                raise RoutingUsageConflictError(
                    "Conflicting routing usage exists for assignment"
                )
            return claimed, False
        return event, True
    except OSError as exc:
        raise RoutingStorageError("Unable to persist routing usage") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def list_routing_usage_events(
    protocol_data_root: Path,
    node_id: str | None = None,
    subnet_id: str | None = None,
    category: str | FindingCategory | None = None,
    selection_type: RoutingSelectionType | None = None,
) -> list[RoutingUsageEvent]:
    if node_id is not None:
        _validate_identifier(node_id, "node")
    if subnet_id is not None:
        _validate_identifier(subnet_id, "subnet")
    normalized = _normalize_category(category) if category is not None else None
    root = get_routing_usage_root(protocol_data_root)
    if not root.exists():
        return []
    paths = (
        sorted((root / node_id).glob("*.json"))
        if node_id is not None
        else sorted(root.glob("*/*.json"))
    )
    events: list[RoutingUsageEvent] = []
    for path in paths:
        event = _load_usage_path(path)
        if node_id is not None and event.node_id != node_id:
            continue
        if subnet_id is not None and event.subnet_id != subnet_id:
            continue
        if normalized is not None and event.category.value != normalized:
            continue
        if selection_type is not None and event.selection_type != selection_type:
            continue
        events.append(event)
    return sorted(
        events,
        key=lambda event: (
            event.applied_at,
            event.node_id,
            event.assignment_id,
        ),
    )


def _collect_category_context(
    protocol_data_root: Path,
    category: str | FindingCategory,
    *,
    subnet: SubnetRecord | None = None,
) -> RoutingCategoryContext:
    normalized = _normalize_category(category)
    finding_category = FindingCategory(normalized)
    if subnet is None:
        try:
            subnet = find_subnet_by_category(protocol_data_root, finding_category)
        except (InvalidSubnetIdentifierError, SubnetStorageError) as exc:
            raise RoutingStorageError("Stored subnet data is malformed") from exc
    if subnet is None:
        return RoutingCategoryContext(
            category=finding_category,
            subnet=None,
            candidates=(),
        )
    if subnet.category != finding_category:
        raise RoutingInputMismatchError(
            "Subnet category does not match routing category"
        )
    try:
        members = list_subnet_members(protocol_data_root, subnet.subnet_id)
    except (InvalidSubnetIdentifierError, SubnetStorageError) as exc:
        raise RoutingStorageError("Stored subnet-member data is malformed") from exc
    candidates: list[RoutingCandidateInput] = []
    for member in sorted(members, key=lambda item: item.node_id):
        try:
            node = load_node(protocol_data_root, member.node_id)
        except (InvalidNodeIdentifierError, NodeStorageError):
            node = None
        try:
            score = load_category_score(
                protocol_data_root, member.node_id, subnet.category
            )
        except CategoryScoreStorageError:
            score = None
        finalized_usage = [
            event
            for event in list_routing_usage_events(
                protocol_data_root,
                node_id=member.node_id,
                subnet_id=subnet.subnet_id,
                category=subnet.category,
            )
            if _usage_has_finalized_route(protocol_data_root, event)
        ]
        exploration = [
            event
            for event in finalized_usage
            if event.selection_type == RoutingSelectionType.EXPLORATION
        ]
        usage = RoutingUsageSummary(
            total_assignments=len(finalized_usage),
            ranked_assignments=sum(
                event.selection_type == RoutingSelectionType.RANKED
                for event in finalized_usage
            ),
            exploration_assignments=len(exploration),
            last_assignment_at=max(
                (event.applied_at for event in finalized_usage), default=None
            ),
            last_exploration_assignment_at=max(
                (event.applied_at for event in exploration), default=None
            ),
        )
        candidates.append(
            RoutingCandidateInput(
                node=node,
                member=member,
                score=score,
                usage=usage,
                usage_event_ids=tuple(
                    sorted(event.usage_event_id for event in finalized_usage)
                ),
            )
        )
    return RoutingCategoryContext(
        category=finding_category,
        subnet=subnet,
        candidates=tuple(candidates),
    )


def _calculate_category_from_context(
    context: RoutingCategoryContext,
    project_id: str,
    routing_id: str,
    nodes_per_category: int,
    include_exploration: bool,
    calculated_at: datetime,
) -> CategoryRoutingResult:
    subnet = context.subnet
    if subnet is None:
        return CategoryRoutingResult(
            category=context.category,
            subnet_id=None,
            subnet_status=None,
            requested_assignments=nodes_per_category,
            ranked_target=nodes_per_category,
            exploration_target=0,
            ranked_selected=0,
            exploration_selected=0,
            total_selected=0,
            complete=False,
            assignments=[],
            shortage_reasons=[RoutingShortageReason.NO_SUBNET],
            warnings=[
                "No registered subnet exists for the requested category."
            ],
        )
    ranked_target, exploration_target = calculate_routing_slot_targets(
        nodes_per_category,
        subnet.exploration_ratio,
        include_exploration,
    )
    if subnet.status != SubnetStatus.ACTIVE:
        return CategoryRoutingResult(
            category=context.category,
            subnet_id=subnet.subnet_id,
            subnet_status=subnet.status,
            requested_assignments=nodes_per_category,
            ranked_target=ranked_target,
            exploration_target=exploration_target,
            ranked_selected=0,
            exploration_selected=0,
            total_selected=0,
            complete=False,
            assignments=[],
            shortage_reasons=[RoutingShortageReason.SUBNET_NOT_ACTIVE],
            warnings=[
                f"Subnet status {subnet.status.value} prevents routing."
            ],
        )

    ranked_candidates: list[RoutingCandidateInput] = []
    exploration_candidates: list[RoutingCandidateInput] = []
    for candidate in context.candidates:
        if candidate.node is None:
            continue
        ranked, _ = is_ranked_routing_candidate(
            candidate.node, subnet, candidate.member, candidate.score
        )
        if ranked:
            ranked_candidates.append(candidate)
            continue
        exploration, _ = is_exploration_routing_candidate(
            candidate.node, subnet, candidate.member, candidate.score
        )
        if exploration:
            exploration_candidates.append(candidate)
    ranked_candidates.sort(key=_ranked_sort_key)
    exploration_candidates.sort(key=_exploration_sort_key)
    selected_ranked = ranked_candidates[:ranked_target]
    ranked_node_ids = {candidate.member.node_id for candidate in selected_ranked}
    selected_exploration = [
        candidate
        for candidate in exploration_candidates
        if candidate.member.node_id not in ranked_node_ids
    ][:exploration_target]

    assignments: list[RoutingAssignment] = []
    for candidate in selected_ranked:
        assignments.append(
            _build_assignment(
                candidate,
                project_id,
                routing_id,
                subnet,
                RoutingSelectionType.RANKED,
                len(assignments) + 1,
                calculated_at,
            )
        )
    for candidate in selected_exploration:
        assignments.append(
            _build_assignment(
                candidate,
                project_id,
                routing_id,
                subnet,
                RoutingSelectionType.EXPLORATION,
                len(assignments) + 1,
                calculated_at,
            )
        )

    shortage: list[RoutingShortageReason] = []
    warnings: list[str] = []
    if len(selected_ranked) < ranked_target:
        shortage.append(
            RoutingShortageReason.NO_RANKED_MEMBERS
            if not selected_ranked
            else RoutingShortageReason.INSUFFICIENT_RANKED_MEMBERS
        )
        warnings.append(
            f"Selected {len(selected_ranked)} of {ranked_target} ranked production nodes."
        )
    if len(selected_exploration) < exploration_target:
        shortage.append(
            RoutingShortageReason.NO_EXPLORATION_MEMBERS
            if not selected_exploration
            else RoutingShortageReason.INSUFFICIENT_EXPLORATION_MEMBERS
        )
        warnings.append(
            f"Selected {len(selected_exploration)} of {exploration_target} shadow exploration nodes."
        )
    if not assignments:
        shortage.append(RoutingShortageReason.NO_ELIGIBLE_NODES)
        warnings.append("No eligible nodes were selected; no unsafe fallback was used.")
    shortage = list(dict.fromkeys(shortage))
    total = len(assignments)
    return CategoryRoutingResult(
        category=context.category,
        subnet_id=subnet.subnet_id,
        subnet_status=subnet.status,
        requested_assignments=nodes_per_category,
        ranked_target=ranked_target,
        exploration_target=exploration_target,
        ranked_selected=len(selected_ranked),
        exploration_selected=len(selected_exploration),
        total_selected=total,
        complete=total == nodes_per_category,
        assignments=assignments,
        shortage_reasons=[] if total == nodes_per_category else shortage,
        warnings=[] if total == nodes_per_category else warnings,
    )


def _build_assignment(
    candidate: RoutingCandidateInput,
    project_id: str,
    routing_id: str,
    subnet: SubnetRecord,
    selection_type: RoutingSelectionType,
    position: int,
    created_at: datetime,
) -> RoutingAssignment:
    member = candidate.member
    score = candidate.score
    confidence = (
        score.components.experience_confidence if score is not None else 0.0
    )
    category_score = score.category_score if score is not None else member.category_score
    snapshot = RoutingCandidateSnapshot(
        node_id=member.node_id,
        subnet_id=subnet.subnet_id,
        category=subnet.category,
        node_status=candidate.node.status,
        membership_status=member.status,
        category_score_id=score.score_id if score is not None else member.category_score_id,
        category_score=category_score,
        experience_confidence=confidence,
        finalized_submissions=member.finalized_submissions,
        accepted_unique_submissions=member.accepted_unique_submissions,
        membership_source_fingerprint=member.membership_source_fingerprint,
        score_source_fingerprint=score.source_fingerprint if score is not None else None,
        exploration_assignments_before=candidate.usage.exploration_assignments,
        last_exploration_assignment_at=(
            candidate.usage.last_exploration_assignment_at
        ),
    )
    if selection_type == RoutingSelectionType.RANKED:
        mode = RoutingAssignmentMode.PRODUCTION
        reasons = [
            (
                f"Selected as {member.status.value} member with category score "
                f"{category_score:.6f}."
            ),
            "Ranked deterministically by membership tier, category score, confidence, and finalized history.",
            "Node and subnet are active and source snapshots are consistent.",
        ]
    else:
        mode = RoutingAssignmentMode.SHADOW
        reasons = [
            f"Selected for shadow exploration as a {member.status.value} member.",
            (
                "Selected using finalized exploration usage: "
                f"{candidate.usage.exploration_assignments} prior assignments."
            ),
            "Shadow output is not authoritative during Router v0.",
        ]
    assignment_id = _assignment_id(
        routing_id, subnet.category, member.node_id, selection_type
    )
    return RoutingAssignment(
        assignment_id=assignment_id,
        routing_id=routing_id,
        project_id=project_id,
        subnet_id=subnet.subnet_id,
        category=subnet.category,
        node_id=member.node_id,
        selection_type=selection_type,
        assignment_mode=mode,
        membership_status=member.status,
        category_score=category_score,
        experience_confidence=confidence,
        position=position,
        selection_reasons=reasons,
        candidate_snapshot=snapshot,
        qualification_reference=None,
        created_at=created_at,
    )


def _common_candidate_failures(
    node: NodeRecord,
    subnet: SubnetRecord,
    member: SubnetMemberRecord,
) -> list[str]:
    failures: list[str] = []
    if node.status != NodeStatus.ACTIVE:
        failures.append(f"Node status is {node.status.value}, not active.")
    if subnet.status != SubnetStatus.ACTIVE:
        failures.append(f"Subnet status is {subnet.status.value}, not active.")
    if subnet.category.value not in node.supported_categories:
        failures.append("Node does not support the subnet category.")
    if (
        member.node_id != node.node_id
        or member.subnet_id != subnet.subnet_id
        or member.category != subnet.category
    ):
        failures.append("Membership identity does not match node and subnet.")
    if member.membership_source_fingerprint is None:
        failures.append("Membership lacks a Day 4 source fingerprint.")
    if member.administrative_lock:
        failures.append("Membership has an administrative safety lock.")
    if member.status in {
        SubnetMemberStatus.SUSPENDED,
        SubnetMemberStatus.REMOVED,
    }:
        failures.append(f"Membership status {member.status.value} is ineligible.")
    return failures


def _score_matches_member(
    score: CategoryScoreRecord,
    member: SubnetMemberRecord,
    subnet: SubnetRecord,
) -> bool:
    return (
        score.node_id == member.node_id
        and score.category == member.category == subnet.category
        and member.category_score_id == score.score_id
        and member.category_score_source_fingerprint == score.source_fingerprint
        and member.category_score == score.category_score
        and member.finalized_submissions == score.finalized_submissions
        and member.accepted_unique_submissions
        == score.accepted_unique_submissions
    )


def _ranked_sort_key(candidate: RoutingCandidateInput) -> tuple[Any, ...]:
    score = candidate.score
    return (
        0 if candidate.member.status == SubnetMemberStatus.EXPERT else 1,
        -(score.category_score if score is not None else 0.0),
        -(
            score.components.experience_confidence
            if score is not None
            else 0.0
        ),
        -candidate.member.accepted_unique_submissions,
        -candidate.member.finalized_submissions,
        candidate.member.node_id,
    )


def _exploration_sort_key(candidate: RoutingCandidateInput) -> tuple[Any, ...]:
    last = candidate.usage.last_exploration_assignment_at
    score = candidate.score
    return (
        candidate.usage.exploration_assignments,
        last is not None,
        last or datetime.min.replace(tzinfo=timezone.utc),
        0 if candidate.member.status == SubnetMemberStatus.PROBATION else 1,
        -(score.category_score if score is not None else candidate.member.category_score),
        -(
            score.components.experience_confidence
            if score is not None
            else 0.0
        ),
        candidate.member.node_id,
    )


def _category_context_source_input(
    context: RoutingCategoryContext,
) -> dict[str, Any]:
    subnet = context.subnet
    return {
        "category": context.category.value,
        "subnet": (
            None
            if subnet is None
            else {
                "subnet_id": subnet.subnet_id,
                "registry_version": subnet.registry_version,
                "category": subnet.category.value,
                "status": subnet.status.value,
                "exploration_ratio": subnet.exploration_ratio,
                "minimum_category_score": subnet.minimum_category_score,
                "minimum_finalized_submissions": (
                    subnet.minimum_finalized_submissions
                ),
                "maximum_active_nodes": subnet.maximum_active_nodes,
                "updated_at": subnet.updated_at.isoformat(),
            }
        ),
        "members": [
            {
                "node_id": candidate.member.node_id,
                "node_status": (
                    candidate.node.status.value
                    if candidate.node is not None
                    else None
                ),
                "node_supported_categories": (
                    sorted(candidate.node.supported_categories)
                    if candidate.node is not None
                    else []
                ),
                "membership_status": candidate.member.status.value,
                "membership_source_fingerprint": (
                    candidate.member.membership_source_fingerprint
                ),
                "category_score_id": candidate.member.category_score_id,
                "category_score": candidate.member.category_score,
                "category_score_source_fingerprint": (
                    candidate.member.category_score_source_fingerprint
                ),
                "loaded_score_id": (
                    candidate.score.score_id
                    if candidate.score is not None
                    else None
                ),
                "loaded_score_source_fingerprint": (
                    candidate.score.source_fingerprint
                    if candidate.score is not None
                    else None
                ),
                "loaded_score": (
                    candidate.score.category_score
                    if candidate.score is not None
                    else None
                ),
                "experience_confidence": (
                    candidate.score.components.experience_confidence
                    if candidate.score is not None
                    else 0.0
                ),
                "finalized_submissions": candidate.member.finalized_submissions,
                "accepted_unique_submissions": (
                    candidate.member.accepted_unique_submissions
                ),
                "administrative_lock": candidate.member.administrative_lock,
                "routing_usage": {
                    **candidate.usage.model_dump(mode="json"),
                    "usage_event_ids": list(candidate.usage_event_ids),
                },
            }
            for candidate in sorted(
                context.candidates, key=lambda item: item.member.node_id
            )
        ],
    }


def _build_usage_event(
    record: ProjectRoutingRecord,
    assignment: RoutingAssignment,
    applied_at: datetime,
) -> RoutingUsageEvent:
    payload = {
        "routing_version": record.routing_version,
        "routing_id": record.routing_id,
        "routing_source_fingerprint": record.source_fingerprint,
        "assignment_id": assignment.assignment_id,
        "project_id": assignment.project_id,
        "subnet_id": assignment.subnet_id,
        "category": assignment.category.value,
        "node_id": assignment.node_id,
        "selection_type": assignment.selection_type.value,
        "assignment_mode": assignment.assignment_mode.value,
    }
    fingerprint = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return RoutingUsageEvent(
        usage_event_id=f"routing_usage_{assignment.assignment_id}",
        routing_id=record.routing_id,
        assignment_id=assignment.assignment_id,
        project_id=assignment.project_id,
        subnet_id=assignment.subnet_id,
        category=assignment.category,
        node_id=assignment.node_id,
        selection_type=assignment.selection_type,
        assignment_mode=assignment.assignment_mode,
        source_fingerprint=fingerprint,
        applied_at=applied_at,
    )


def _usage_has_finalized_route(
    protocol_data_root: Path,
    event: RoutingUsageEvent,
) -> bool:
    route = load_routing_record_by_id(protocol_data_root, event.routing_id)
    return route is not None and route.status == RoutingStatus.FINALIZED


def _routing_usage_path(
    protocol_data_root: Path,
    node_id: str,
    assignment_id: str,
) -> Path:
    _validate_identifier(node_id, "node")
    _validate_identifier(assignment_id, "assignment")
    root = get_routing_usage_root(protocol_data_root)
    node_root = root / node_id
    _ensure_within(node_root, root, "Invalid node identifier")
    path = node_root / f"{assignment_id}.json"
    _ensure_within(path, node_root, "Invalid assignment identifier")
    return path


def _load_usage_path(path: Path) -> RoutingUsageEvent:
    try:
        event = RoutingUsageEvent.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise RoutingStorageError("Stored routing usage is malformed") from exc
    if path.stem != event.assignment_id or path.parent.name != event.node_id:
        raise RoutingStorageError("Stored routing usage identity does not match its path")
    return event


def _write_routing_record(
    protocol_data_root: Path,
    record: ProjectRoutingRecord,
) -> None:
    path = get_routing_record_path(
        protocol_data_root, record.project_id, record.routing_id
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(path, record.model_dump(mode="json"))


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    except OSError as exc:
        raise RoutingStorageError("Unable to persist routing record") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _load_project_metadata(
    project_workspace: Path,
    project_id: str,
) -> dict[str, Any]:
    _validate_identifier(project_id, "project")
    metadata_path = project_workspace / "metadata.json"
    if not project_workspace.is_dir() or not metadata_path.is_file():
        raise RoutingProjectNotFoundError("Project not found")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RoutingProjectNotFoundError("Project metadata is unavailable") from exc
    if not isinstance(metadata, dict) or metadata.get("project_id") != project_id:
        raise RoutingInputMismatchError(
            "Project workspace does not match requested project"
        )
    return metadata


def _load_project_scope(project_workspace: Path) -> ScopeManifest:
    parsed_path = project_workspace / "scope" / "parsed_scope.json"
    if not parsed_path.is_file():
        raise RoutingProjectScopeMissingError("Project parsed scope is missing")
    try:
        return read_parsed_scope(project_workspace)
    except HTTPException as exc:
        raise RoutingProjectScopeMissingError("Project parsed scope is missing") from exc
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise RoutingProjectCategoryMissingError(
            "Project scope has no valid attack categories"
        ) from exc


def _fingerprint_scope(scope: ScopeManifest) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(scope.model_dump(mode="json"))
    ).hexdigest()


def _immutable_routing_payload(record: ProjectRoutingRecord) -> dict[str, Any]:
    return record.model_dump(
        exclude={
            "status",
            "finalized_at",
            "updated_at",
            "superseded_by_routing_id",
        }
    )


def _usage_identity(event: RoutingUsageEvent) -> dict[str, Any]:
    return event.model_dump(exclude={"applied_at"})


def _assignment_id(
    routing_id: str,
    category: FindingCategory,
    node_id: str,
    selection_type: RoutingSelectionType,
) -> str:
    digest = hashlib.sha256(
        _canonical_json_bytes(
            {
                "routing_id": routing_id,
                "category": category.value,
                "node_id": node_id,
                "selection_type": selection_type.value,
            }
        )
    ).hexdigest()
    return f"assignment_{digest[:32]}"


def _preview_routing_id(project_id: str, category: FindingCategory) -> str:
    digest = hashlib.sha256(
        f"{project_id}:{category.value}".encode("utf-8")
    ).hexdigest()
    return f"routing_preview_{digest[:32]}"


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _normalize_category(category: str | FindingCategory) -> str:
    try:
        return normalize_category(category)
    except (TypeError, ValueError) as exc:
        raise RoutingInputMismatchError(str(exc)) from exc


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise InvalidRoutingIdentifierError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path, message: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidRoutingIdentifierError(message) from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RoutingInputMismatchError("Routing timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None
