from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.poc import (
    PocUploadRequest,
    PocUploadResponse,
    ReproductionRunRequest,
    ReproductionRunResponse,
)
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.sandbox import SandboxCommandResult, SandboxRunStatus
from app.services import safety_preflight_service, sandbox_runner
from app.services.poc_service import poc_exists, store_poc_for_finding
from app.services.project_service import get_project_or_404, project_workspace
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    list_reproduction_results,
    load_reproduction_result,
    save_reproduction_result,
    write_reproduction_output_files,
)


router = APIRouter(prefix="/projects", tags=["reproduction"])


@router.post(
    "/{project_id}/findings/{finding_id}/poc",
    response_model=PocUploadResponse,
)
def upload_poc(
    project_id: str,
    finding_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> PocUploadResponse:
    get_project_or_404(db, project_id)
    request = _validate_upload_request(payload)
    workspace = project_workspace(project_id)

    try:
        poc_file = store_poc_for_finding(
            project_id=project_id,
            finding_id=finding_id,
            project_workspace=workspace,
            poc_filename=request.poc_filename,
            poc_content=request.poc_content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = _load_or_create_reproduction(project_id, finding_id, workspace)
    save_reproduction_result(
        result.model_copy(
            update={
                "status": ReproductionStatus.GENERATED,
                "poc_file": poc_file,
                "safety_notes": [],
                "error_message": None,
            }
        ),
        workspace,
    )

    return PocUploadResponse(
        project_id=project_id,
        finding_id=finding_id,
        poc_file=poc_file,
        stored_path=f"repo/{poc_file}",
        status=ReproductionStatus.GENERATED.value,
        message="PoC stored for reproduction.",
    )


@router.post(
    "/{project_id}/findings/{finding_id}/reproduction/run",
    response_model=ReproductionRunResponse,
)
def run_reproduction(
    project_id: str,
    finding_id: str,
    payload: dict,
    db: Session = Depends(get_db),
) -> ReproductionRunResponse:
    get_project_or_404(db, project_id)
    request = _validate_run_request(payload)
    workspace = project_workspace(project_id)
    repo_path = workspace / "repo"

    if not poc_exists(workspace, request.poc_file):
        raise HTTPException(status_code=404, detail="PoC file not found")

    command = ["forge", "test", "--match-test", request.test_name]
    safety_result = safety_preflight_service.run_safety_preflight(
        repo_path=repo_path,
        poc_file=request.poc_file,
        test_name=request.test_name,
        command=command,
    )
    safety_notes = _safety_notes(safety_result.issues)

    if not safety_result.passed:
        result = _save_reproduction_result(
            project_id=project_id,
            finding_id=finding_id,
            workspace=workspace,
            status=ReproductionStatus.REJECTED_UNSAFE,
            poc_file=request.poc_file,
            test_name=request.test_name,
            command=command,
            duration_ms=None,
            error_message="Safety preflight failed",
            safety_notes=safety_notes,
        )
        return _run_response(result)

    sandbox_result = sandbox_runner.run_in_sandbox(
        repo_path=repo_path,
        command=command,
        timeout_seconds=request.timeout_seconds,
    )
    write_reproduction_output_files(
        project_workspace=workspace,
        finding_id=finding_id,
        stdout=sandbox_result.stdout,
        stderr=sandbox_result.stderr,
    )
    result = _save_reproduction_result(
        project_id=project_id,
        finding_id=finding_id,
        workspace=workspace,
        status=_map_sandbox_status(sandbox_result.status),
        poc_file=request.poc_file,
        test_name=request.test_name,
        command=command,
        duration_ms=sandbox_result.duration_ms,
        error_message=sandbox_result.error_message,
        safety_notes=safety_notes,
    )
    return _run_response(result)


@router.get(
    "/{project_id}/findings/{finding_id}/reproduction",
    response_model=ReproductionRunResponse,
)
def get_reproduction(
    project_id: str,
    finding_id: str,
    db: Session = Depends(get_db),
) -> ReproductionRunResponse:
    get_project_or_404(db, project_id)
    result = load_reproduction_result(project_workspace(project_id), finding_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Reproduction result not found")
    return _run_response(result)


@router.get(
    "/{project_id}/reproductions",
    response_model=list[ReproductionRunResponse],
)
def list_project_reproductions(
    project_id: str,
    db: Session = Depends(get_db),
) -> list[ReproductionRunResponse]:
    get_project_or_404(db, project_id)
    return [_run_response(result) for result in list_reproduction_results(project_workspace(project_id))]


def _validate_upload_request(payload: dict) -> PocUploadRequest:
    try:
        return PocUploadRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_errors(exc)) from exc


def _validate_run_request(payload: dict) -> ReproductionRunRequest:
    try:
        return ReproductionRunRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_errors(exc)) from exc


def _validation_errors(exc: ValidationError) -> list[dict]:
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "loc": list(error.get("loc", [])),
                "msg": error.get("msg", "Invalid request"),
                "type": error.get("type", "value_error"),
            }
        )
    return errors


def _load_or_create_reproduction(project_id: str, finding_id: str, workspace) -> ReproductionResult:
    existing = load_reproduction_result(workspace, finding_id)
    if existing is not None:
        return existing
    return create_initial_reproduction_result(project_id, finding_id, workspace)


def _save_reproduction_result(
    project_id: str,
    finding_id: str,
    workspace,
    status: ReproductionStatus,
    poc_file: str | None,
    test_name: str | None,
    command: list[str] | None,
    duration_ms: int | None,
    error_message: str | None,
    safety_notes: list[str],
) -> ReproductionResult:
    result = _load_or_create_reproduction(project_id, finding_id, workspace)
    return save_reproduction_result(
        result.model_copy(
            update={
                "status": status,
                "poc_file": poc_file,
                "test_name": test_name,
                "command": command,
                "duration_ms": duration_ms,
                "error_message": error_message,
                "safety_notes": safety_notes,
            }
        ),
        workspace,
    )


def _map_sandbox_status(status: SandboxRunStatus) -> ReproductionStatus:
    return {
        SandboxRunStatus.COMPLETED: ReproductionStatus.REPRODUCED,
        SandboxRunStatus.FAILED: ReproductionStatus.FAILED,
        SandboxRunStatus.TIMEOUT: ReproductionStatus.TIMEOUT,
        SandboxRunStatus.REJECTED: ReproductionStatus.REJECTED_UNSAFE,
        SandboxRunStatus.SANDBOX_ERROR: ReproductionStatus.SANDBOX_ERROR,
    }[status]


def _safety_notes(issues) -> list[str]:
    return [f"{issue.code}: {issue.message}" for issue in issues]


def _run_response(result: ReproductionResult) -> ReproductionRunResponse:
    return ReproductionRunResponse(
        project_id=result.project_id,
        finding_id=result.finding_id,
        reproduction_id=result.reproduction_id,
        status=result.status.value,
        poc_file=result.poc_file,
        test_name=result.test_name,
        command=result.command,
        duration_ms=result.duration_ms,
        error_message=result.error_message,
        safety_notes=result.safety_notes,
    )
