from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.poc import ReproductionRunRequest
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.validator_attestation import (
    VALIDATOR_REPRODUCTION_VERSION,
    ValidatorAssignmentStatus,
    ValidatorReproductionMode,
    ValidatorReproductionRecord,
)
from app.schemas.validator_committee import ValidatorCommitteeStatus
from app.schemas.validator_reproduction import (
    CommitteeReproductionReadiness,
    CommitteeReproductionStageStatus,
    IndependentReproductionArtifact,
    VALIDATOR_REPRODUCTION_EXECUTION_VERSION,
    VALIDATOR_SANDBOX_POLICY_VERSION,
    ValidatorReproductionExecutionRequest,
    ValidatorReproductionJob,
    ValidatorReproductionJobStatus,
)
from app.services.poc_service import get_poc_path
from app.services.reproduction_service import (
    attributed_reproduction_storage_ref,
    execute_reproduction_safely,
    save_attributed_reproduction_result,
    write_attributed_reproduction_output_files,
)
from app.services.scope_service import read_parsed_scope
from app.services.subnet_router_service import load_routing_record
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
    load_validator_assignment,
    update_assignment_status,
    validate_assignment_for_action,
)
from app.services.validator_committee_service import load_validator_committee
from app.services.validator_reproduction_service import (
    build_underlying_reproduction_fingerprint,
    build_validator_reproduction_source_payload,
    get_validator_reproduction_path,
    list_validator_reproduction_records,
    load_validator_reproduction_record,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


DEFAULT_TIMEOUT_SECONDS = 60
SANDBOX_IMAGE = "foundry-sandbox:latest"


def execute_validator_reproduction(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_assignment_id: str,
    request: ValidatorReproductionExecutionRequest,
    independent_artifact: IndependentReproductionArtifact | None = None,
) -> ValidatorReproductionRecord:
    """Execute one independently attributable Week 3 job for a committee seat."""
    assignment = load_validator_assignment(
        protocol_data_root, project_id, routing_id, validator_assignment_id
    )
    if assignment is None:
        raise ValidatorProtocolNotFoundError("Validator assignment not found")
    if assignment.validator_node_id != request.validator_node_id:
        raise ValidatorProtocolRelationshipError(
            "Validator node does not match the authorized assignment"
        )
    if assignment.validator_committee_id is None:
        raise ValidatorProtocolRelationshipError(
            "Validator execution requires a finalized committee assignment"
        )

    request_id = build_validator_reproduction_request_id(assignment.validator_assignment_id)
    existing_job = load_validator_reproduction_job(
        protocol_data_root, project_id, routing_id, request_id
    )
    if existing_job is not None:
        if existing_job.reproduction_mode != request.reproduction_mode:
            raise ValidatorProtocolConflictError(
                "A different reproduction mode already owns this assignment execution"
            )
        if existing_job.status == ValidatorReproductionJobStatus.RUNNING:
            raise ValidatorProtocolConflictError(
                "Validator reproduction is already in progress"
            )
        record = load_validator_reproduction_record(
            protocol_data_root,
            project_id,
            routing_id,
            existing_job.validator_reproduction_id or "",
        )
        if record is None:
            raise ValidatorProtocolStorageError(
                "Finalized validator reproduction job has no record"
            )
        return record

    cluster, _ = validate_assignment_for_action(
        protocol_data_root,
        assignment,
        validator_node_id=request.validator_node_id,
        allow_reproduction_recorded=False,
    )
    committee = load_validator_committee(
        protocol_data_root,
        project_id,
        routing_id,
        assignment.validator_committee_id,
    )
    if committee is None:
        raise ValidatorProtocolNotFoundError("Validator committee not found")
    if committee.status != ValidatorCommitteeStatus.FINALIZED:
        raise ValidatorProtocolConflictError(
            "Validator reproduction requires a finalized committee"
        )
    if (
        committee.finding_cluster_id != assignment.finding_cluster_id
        or committee.validation_round != assignment.validation_round
    ):
        raise ValidatorProtocolRelationshipError(
            "Assignment does not belong to the committee validation round"
        )
    seats = committee.authoritative_seats + committee.shadow_seats
    seat = next(
        (
            item
            for item in seats
            if item.validator_assignment_id == assignment.validator_assignment_id
        ),
        None,
    )
    if (
        seat is None
        or seat.validator_node_id != assignment.validator_node_id
        or seat.operator_id != assignment.validator_operator_id
        or seat.assignment_role != assignment.assignment_role
    ):
        raise ValidatorProtocolRelationshipError(
            "Assignment attribution does not match finalized committee membership"
        )

    artifact = _resolve_artifact(
        project_workspace,
        cluster,
        request.reproduction_mode,
        independent_artifact,
    )
    source_revision, source_snapshot_fingerprint = build_source_snapshot(
        protocol_data_root,
        project_workspace,
        project_id=project_id,
        routing_id=routing_id,
    )
    environment_fingerprint = build_environment_fingerprint()
    result_storage_ref = attributed_reproduction_storage_ref(request_id)
    request_payload = {
        "execution_protocol_version": VALIDATOR_REPRODUCTION_EXECUTION_VERSION,
        "validator_assignment_id": assignment.validator_assignment_id,
        "validator_committee_id": committee.validator_committee_id,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": cluster.finding_cluster_id,
        "validator_node_id": assignment.validator_node_id,
        "validator_operator_id": assignment.validator_operator_id,
        "assignment_role": assignment.assignment_role.value,
        "reproduction_mode": request.reproduction_mode.value,
        "artifact_reference": artifact[0],
        "artifact_fingerprint": artifact[1],
        "execution_poc_file": artifact[2],
        "execution_test_name": artifact[3],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "source_revision": source_revision,
        "source_snapshot_fingerprint": source_snapshot_fingerprint,
        "environment_fingerprint": environment_fingerprint,
        "result_storage_ref": result_storage_ref,
    }
    request_fingerprint = protocol_fingerprint(request_payload)
    now = _utc_now()
    running_job = ValidatorReproductionJob(
        reproduction_request_id=request_id,
        validator_assignment_id=assignment.validator_assignment_id,
        validator_committee_id=committee.validator_committee_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        assignment_role=assignment.assignment_role,
        reproduction_mode=request.reproduction_mode,
        artifact_reference=artifact[0],
        artifact_fingerprint=artifact[1],
        source_revision=source_revision,
        source_snapshot_fingerprint=source_snapshot_fingerprint,
        environment_fingerprint=environment_fingerprint,
        request_fingerprint=request_fingerprint,
        result_storage_ref=result_storage_ref,
        status=ValidatorReproductionJobStatus.RUNNING,
        created_at=now,
    )
    if not atomic_create_json(
        get_validator_reproduction_job_path(protocol_data_root, routing_id, request_id),
        running_job,
        temporary_prefix=".validator-reproduction-job-create-",
    ):
        raced = load_validator_reproduction_job(
            protocol_data_root, project_id, routing_id, request_id
        )
        if raced is not None and raced.status == ValidatorReproductionJobStatus.FINALIZED:
            record = load_validator_reproduction_record(
                protocol_data_root,
                project_id,
                routing_id,
                raced.validator_reproduction_id or "",
            )
            if record is not None:
                return record
        raise ValidatorProtocolConflictError(
            "Validator reproduction is already in progress"
        )

    try:
        underlying_result = _execute_or_mark_unsupported(
            project_workspace=project_workspace,
            project_id=project_id,
            finding_id=artifact[4],
            request_id=request_id,
            poc_file=artifact[2],
            test_name=artifact[3],
        )
    except Exception:
        underlying_result = _infrastructure_error_result(
            project_id=project_id,
            finding_id=artifact[4],
            request_id=request_id,
            poc_file=artifact[2],
            test_name=artifact[3],
        )
    underlying_result = save_attributed_reproduction_result(
        underlying_result, project_workspace, request_id
    )
    write_attributed_reproduction_output_files(
        project_workspace,
        request_id,
        underlying_result.stdout,
        underlying_result.stderr,
    )
    record = _finalize_record(
        protocol_data_root,
        assignment=assignment,
        job=running_job,
        result=underlying_result,
        poc_artifact_fingerprint=(artifact[1] if artifact[2] is not None else None),
    )
    finalized_job = running_job.model_copy(
        update={
            "status": ValidatorReproductionJobStatus.FINALIZED,
            "validator_reproduction_id": record.validator_reproduction_id,
            "finalized_at": record.finalized_at,
        }
    )
    atomic_write_json(
        get_validator_reproduction_job_path(protocol_data_root, routing_id, request_id),
        finalized_job,
        temporary_prefix=".validator-reproduction-job-finalize-",
    )
    if assignment.status == ValidatorAssignmentStatus.ASSIGNED:
        update_assignment_status(
            protocol_data_root,
            assignment,
            ValidatorAssignmentStatus.REPRODUCTION_RECORDED,
        )
    return record


def build_validator_reproduction_request_id(validator_assignment_id: str) -> str:
    digest = protocol_fingerprint(
        {
            "execution_protocol_version": VALIDATOR_REPRODUCTION_EXECUTION_VERSION,
            "validator_assignment_id": validator_assignment_id,
        }
    )
    return f"validator_reproduction_request_{digest}"


def build_environment_fingerprint() -> str:
    return protocol_fingerprint(
        {
            "sandbox_policy_version": VALIDATOR_SANDBOX_POLICY_VERSION,
            "sandbox_image": SANDBOX_IMAGE,
            "toolchain": "forge",
            "network": "none",
            "container_user": "1000:1000",
            "capabilities": "drop_all",
            "no_new_privileges": True,
            "cpu_limit": "1",
            "memory_limit": "1g",
            "pids_limit": 256,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "output_limit_bytes": 1_048_576,
        }
    )


def build_source_snapshot(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
) -> tuple[str, str]:
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise ValidatorProtocolNotFoundError("Routing record not found")
    scope = read_parsed_scope(project_workspace)
    raw_revision = scope.commit_hash or routing.project_scope_fingerprint
    revision = (
        raw_revision
        if _safe_revision(raw_revision)
        else f"source_revision_{protocol_fingerprint(raw_revision)}"
    )
    repo_root = (project_workspace / "repo").resolve()
    files: list[dict[str, str | None]] = []
    for relative_text in sorted(set(scope.contracts_in_scope)):
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidatorProtocolRelationshipError(
                "In-scope source path is unsafe"
            )
        resolved = (repo_root / relative).resolve()
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise ValidatorProtocolRelationshipError(
                "In-scope source escapes the project repository"
            ) from exc
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": (
                    hashlib.sha256(resolved.read_bytes()).hexdigest()
                    if resolved.is_file()
                    else None
                ),
            }
        )
    return revision, protocol_fingerprint(
        {
            "project_id": project_id,
            "routing_id": routing_id,
            "project_scope_fingerprint": routing.project_scope_fingerprint,
            "source_revision": revision,
            "in_scope_files": files,
        }
    )


def get_committee_reproduction_readiness(
    protocol_data_root: Path,
    *,
    project_id: str,
    routing_id: str,
    validator_committee_id: str,
) -> CommitteeReproductionReadiness:
    committee = load_validator_committee(
        protocol_data_root, project_id, routing_id, validator_committee_id
    )
    if committee is None:
        raise ValidatorProtocolNotFoundError("Validator committee not found")
    records = list_validator_reproduction_records(
        protocol_data_root,
        project_id,
        routing_id,
        validator_committee_id=validator_committee_id,
    )
    by_assignment = {item.validator_assignment_id: item for item in records}
    authoritative_ids = [
        item.validator_assignment_id for item in committee.authoritative_seats
    ]
    shadow_ids = [item.validator_assignment_id for item in committee.shadow_seats]
    pending_authoritative = [
        item for item in authoritative_ids if item not in by_assignment
    ]
    pending_shadow = [item for item in shadow_ids if item not in by_assignment]
    authoritative_records = [
        by_assignment[item] for item in authoritative_ids if item in by_assignment
    ]
    shadow_records = [by_assignment[item] for item in shadow_ids if item in by_assignment]
    ready = not pending_authoritative
    if ready:
        stage = CommitteeReproductionStageStatus.READY
    elif authoritative_records:
        stage = CommitteeReproductionStageStatus.IN_PROGRESS
    else:
        stage = CommitteeReproductionStageStatus.PENDING
    return CommitteeReproductionReadiness(
        validator_committee_id=committee.validator_committee_id,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=committee.finding_cluster_id,
        authoritative_target=len(authoritative_ids),
        authoritative_terminal_count=len(authoritative_records),
        shadow_target=len(shadow_ids),
        shadow_terminal_count=len(shadow_records),
        pending_authoritative_assignment_ids=pending_authoritative,
        pending_shadow_assignment_ids=pending_shadow,
        terminal_status_counts=_status_counts(authoritative_records),
        shadow_terminal_status_counts=_status_counts(shadow_records),
        status=stage,
        ready_for_attestation_or_consensus_stage=ready,
    )


def get_validator_reproduction_job_path(
    protocol_data_root: Path,
    routing_id: str,
    reproduction_request_id: str,
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(reproduction_request_id, "reproduction request")
    root = get_validator_protocol_root(protocol_data_root) / "execution-jobs" / routing_id
    path = root / f"{reproduction_request_id}.json"
    _ensure_within(path, root)
    return path


def load_validator_reproduction_job(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    reproduction_request_id: str,
) -> ValidatorReproductionJob | None:
    path = get_validator_reproduction_job_path(
        protocol_data_root, routing_id, reproduction_request_id
    )
    if not path.exists():
        return None
    try:
        job = ValidatorReproductionJob.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError(
            "Stored validator reproduction job is malformed"
        ) from exc
    if job.project_id != project_id or job.routing_id != routing_id:
        raise ValidatorProtocolRelationshipError(
            "Validator reproduction job belongs to another project or routing"
        )
    return job


def _resolve_artifact(
    project_workspace: Path,
    cluster,
    mode: ValidatorReproductionMode,
    independent_artifact: IndependentReproductionArtifact | None,
) -> tuple[str, str, str | None, str | None, str]:
    if mode == ValidatorReproductionMode.INDEPENDENT_REPRODUCTION:
        if independent_artifact is None:
            raise ValidatorProtocolConflictError(
                "Independent artifact upload is not public in v1; use a trusted pre-ingested artifact"
            )
        path = get_poc_path(project_workspace, independent_artifact.poc_file)
        if not path.is_file():
            raise ValidatorProtocolNotFoundError(
                "Independent reproduction artifact not found"
            )
        return (
            independent_artifact.artifact_id,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            independent_artifact.poc_file,
            independent_artifact.test_name,
            cluster.canonical_finding_id,
        )

    from app.services.reproduction_service import load_reproduction_result

    canonical_first = sorted(
        cluster.members,
        key=lambda item: (
            item.finding_id != cluster.canonical_finding_id,
            item.submitted_at,
            item.submission_id,
        ),
    )
    for member in canonical_first:
        result = load_reproduction_result(project_workspace, member.finding_id)
        if result is None or result.poc_file is None:
            continue
        if member.reproduction_id is not None and result.reproduction_id != member.reproduction_id:
            continue
        try:
            path = get_poc_path(project_workspace, result.poc_file)
        except ValueError:
            continue
        if not path.is_file():
            continue
        return (
            f"cluster_member_{member.submission_id}",
            hashlib.sha256(path.read_bytes()).hexdigest(),
            result.poc_file,
            result.test_name,
            member.finding_id,
        )
    marker = protocol_fingerprint(
        {
            "finding_cluster_id": cluster.finding_cluster_id,
            "reason": "approved_submitted_poc_missing",
        }
    )
    return (
        "approved_submitted_poc_missing",
        marker,
        None,
        None,
        cluster.canonical_finding_id,
    )


def _execute_or_mark_unsupported(
    *,
    project_workspace: Path,
    project_id: str,
    finding_id: str,
    request_id: str,
    poc_file: str | None,
    test_name: str | None,
) -> ReproductionResult:
    result_id = "validator_week3_result_" + protocol_fingerprint(
        {
            "execution_protocol_version": VALIDATOR_REPRODUCTION_EXECUTION_VERSION,
            "reproduction_request_id": request_id,
        }
    )
    if poc_file is None or test_name is None:
        now = _utc_now()
        return ReproductionResult(
            reproduction_id=result_id,
            project_id=project_id,
            finding_id=finding_id,
            status=ReproductionStatus.UNSUPPORTED,
            poc_file=poc_file,
            test_name=test_name,
            command=None,
            stdout=None,
            stderr=None,
            duration_ms=None,
            error_message="Approved submitted PoC or test identifier is unavailable.",
            safety_notes=[],
            created_at=now,
            updated_at=now,
        )
    return execute_reproduction_safely(
        project_id=project_id,
        finding_id=finding_id,
        project_workspace=project_workspace,
        request=ReproductionRunRequest(
            poc_file=poc_file,
            test_name=test_name,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        ),
        reproduction_id=result_id,
    )


def _infrastructure_error_result(
    *,
    project_id: str,
    finding_id: str,
    request_id: str,
    poc_file: str | None,
    test_name: str | None,
) -> ReproductionResult:
    now = _utc_now()
    return ReproductionResult(
        reproduction_id="validator_week3_result_"
        + protocol_fingerprint(
            {
                "execution_protocol_version": VALIDATOR_REPRODUCTION_EXECUTION_VERSION,
                "reproduction_request_id": request_id,
            }
        ),
        project_id=project_id,
        finding_id=finding_id,
        status=ReproductionStatus.ERROR,
        poc_file=poc_file,
        test_name=test_name,
        command=(
            ["forge", "test", "--match-test", test_name]
            if test_name is not None
            else None
        ),
        stdout=None,
        stderr=None,
        duration_ms=None,
        error_message="Validator reproduction infrastructure failed.",
        safety_notes=[],
        created_at=now,
        updated_at=now,
    )


def _finalize_record(
    protocol_data_root: Path,
    *,
    assignment,
    job: ValidatorReproductionJob,
    result: ReproductionResult,
    poc_artifact_fingerprint: str | None,
) -> ValidatorReproductionRecord:
    underlying_fingerprint = build_underlying_reproduction_fingerprint(
        result, poc_artifact_fingerprint=poc_artifact_fingerprint
    )
    payload = build_validator_reproduction_source_payload(
        assignment_source_fingerprint=assignment.source_fingerprint,
        assignment_id=assignment.validator_assignment_id,
        project_id=assignment.project_id,
        routing_id=assignment.routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        reproduction_mode=job.reproduction_mode.value,
        underlying_result=result,
        underlying_result_fingerprint=underlying_fingerprint,
        poc_artifact_fingerprint=poc_artifact_fingerprint,
        environment_fingerprint=job.environment_fingerprint,
        validator_committee_id=job.validator_committee_id,
        assignment_role=job.assignment_role.value,
        underlying_request_id=job.reproduction_request_id,
        underlying_request_fingerprint=job.request_fingerprint,
        underlying_result_storage_ref=job.result_storage_ref,
        source_revision=job.source_revision,
        source_snapshot_fingerprint=job.source_snapshot_fingerprint,
    )
    identity = protocol_fingerprint(
        {
            "reproduction_protocol_version": VALIDATOR_REPRODUCTION_VERSION,
            "validator_assignment_id": assignment.validator_assignment_id,
        }
    )
    now = _utc_now()
    record = ValidatorReproductionRecord(
        validator_reproduction_id=f"validator_reproduction_{identity}",
        validator_assignment_id=assignment.validator_assignment_id,
        validator_committee_id=job.validator_committee_id,
        project_id=assignment.project_id,
        routing_id=assignment.routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        assignment_role=assignment.assignment_role,
        reproduction_mode=job.reproduction_mode,
        underlying_reproduction_request_id=job.reproduction_request_id,
        underlying_reproduction_request_fingerprint=job.request_fingerprint,
        underlying_reproduction_result_id=result.reproduction_id,
        underlying_reproduction_result_storage_ref=job.result_storage_ref,
        underlying_reproduction_result_fingerprint=underlying_fingerprint,
        reproduction_status=result.status,
        poc_artifact_fingerprint=poc_artifact_fingerprint,
        environment_fingerprint=job.environment_fingerprint,
        source_revision=job.source_revision,
        source_snapshot_fingerprint=job.source_snapshot_fingerprint,
        source_fingerprint=protocol_fingerprint(payload),
        created_at=job.created_at,
        finalized_at=now,
    )
    path = get_validator_reproduction_path(
        protocol_data_root, assignment.routing_id, record.validator_reproduction_id
    )
    if not atomic_create_json(
        path, record, temporary_prefix=".validator-reproduction-execution-finalize-"
    ):
        existing = load_validator_reproduction_record(
            protocol_data_root,
            assignment.project_id,
            assignment.routing_id,
            record.validator_reproduction_id,
        )
        if existing is None or existing.source_fingerprint != record.source_fingerprint:
            raise ValidatorProtocolConflictError(
                "Conflicting final reproduction already exists for assignment"
            )
        return existing
    return record


def _status_counts(records: list[ValidatorReproductionRecord]) -> dict:
    counts: dict[ReproductionStatus, int] = {}
    for record in records:
        counts[record.reproduction_status] = counts.get(record.reproduction_status, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0].value))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_revision(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value not in {".", ".."}
        and all(character.isalnum() or character in "_-" for character in value)
    )
