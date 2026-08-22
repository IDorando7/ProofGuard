import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from app.schemas.reproduction import (
    ReproductionResult,
    ReproductionStatus,
)
from app.schemas.poc import ReproductionRunRequest
from app.schemas.sandbox import SandboxRunStatus
from app.services import safety_preflight_service, sandbox_runner
from app.utils.protocol_serialization import atomic_write_json, atomic_write_text


REPRODUCTION_FILENAME = "reproduction.json"
ATTRIBUTED_REPRODUCTION_NAMESPACE = "validator"


def get_reproduction_dir(project_workspace: Path, finding_id: str) -> Path:
    return project_workspace / "reproductions" / finding_id


def create_initial_reproduction_result(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
) -> ReproductionResult:
    now = _utc_now()
    result = ReproductionResult(
        reproduction_id=str(uuid.uuid4()),
        finding_id=finding_id,
        project_id=project_id,
        status=ReproductionStatus.NOT_ATTEMPTED,
        stdout=None,
        stderr=None,
        safety_notes=[],
        created_at=now,
        updated_at=now,
    )
    _write_result(result, project_workspace)
    return result


def save_reproduction_result(
    result: ReproductionResult,
    project_workspace: Path,
) -> ReproductionResult:
    saved = result.model_copy(update={"updated_at": _utc_now()})
    _write_result(saved, project_workspace)
    return saved


def load_reproduction_result(
    project_workspace: Path,
    finding_id: str,
) -> Optional[ReproductionResult]:
    path = get_reproduction_dir(project_workspace, finding_id) / REPRODUCTION_FILENAME
    if not path.exists():
        return None

    return ReproductionResult.model_validate_json(path.read_text(encoding="utf-8"))


def list_reproduction_results(project_workspace: Path) -> List[ReproductionResult]:
    reproductions_dir = project_workspace / "reproductions"
    if not reproductions_dir.exists():
        return []

    results: list[ReproductionResult] = []
    for path in sorted(reproductions_dir.glob(f"*/{REPRODUCTION_FILENAME}")):
        try:
            results.append(ReproductionResult.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"Invalid reproduction result file: {path}") from exc

    return sorted(results, key=lambda result: result.created_at)


def write_reproduction_output_files(
    project_workspace: Path,
    finding_id: str,
    stdout: Optional[str],
    stderr: Optional[str],
) -> None:
    reproduction_dir = get_reproduction_dir(project_workspace, finding_id)
    reproduction_dir.mkdir(parents=True, exist_ok=True)

    if stdout is not None:
        (reproduction_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    if stderr is not None:
        (reproduction_dir / "stderr.txt").write_text(stderr, encoding="utf-8")


def get_attributed_reproduction_dir(
    project_workspace: Path,
    reproduction_request_id: str,
) -> Path:
    if not _safe_execution_identifier(reproduction_request_id):
        raise ValueError("Invalid attributed reproduction request identifier")
    root = project_workspace / "reproductions" / ATTRIBUTED_REPRODUCTION_NAMESPACE
    path = root / reproduction_request_id
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Invalid attributed reproduction storage path") from exc
    return path


def attributed_reproduction_storage_ref(reproduction_request_id: str) -> str:
    get_attributed_reproduction_dir(Path("."), reproduction_request_id)
    return (
        f"reproductions/{ATTRIBUTED_REPRODUCTION_NAMESPACE}/"
        f"{reproduction_request_id}/{REPRODUCTION_FILENAME}"
    )


def save_attributed_reproduction_result(
    result: ReproductionResult,
    project_workspace: Path,
    reproduction_request_id: str,
) -> ReproductionResult:
    saved = result.model_copy(update={"updated_at": _utc_now()})
    path = (
        get_attributed_reproduction_dir(project_workspace, reproduction_request_id)
        / REPRODUCTION_FILENAME
    )
    atomic_write_json(path, saved, temporary_prefix=".validator-week3-result-")
    return saved


def load_attributed_reproduction_result(
    project_workspace: Path,
    storage_ref: str,
) -> ReproductionResult | None:
    parts = Path(storage_ref).parts
    if (
        len(parts) != 4
        or parts[0] != "reproductions"
        or parts[1] != ATTRIBUTED_REPRODUCTION_NAMESPACE
        or parts[3] != REPRODUCTION_FILENAME
        or not _safe_execution_identifier(parts[2])
    ):
        raise ValueError("Invalid attributed reproduction storage reference")
    path = project_workspace.joinpath(*parts)
    root = project_workspace / "reproductions" / ATTRIBUTED_REPRODUCTION_NAMESPACE
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Attributed reproduction storage escapes its namespace") from exc
    if not path.exists():
        return None
    return ReproductionResult.model_validate_json(path.read_text(encoding="utf-8"))


def write_attributed_reproduction_output_files(
    project_workspace: Path,
    reproduction_request_id: str,
    stdout: str | None,
    stderr: str | None,
) -> None:
    directory = get_attributed_reproduction_dir(
        project_workspace, reproduction_request_id
    )
    if stdout is not None:
        atomic_write_text(directory / "stdout.txt", stdout)
    if stderr is not None:
        atomic_write_text(directory / "stderr.txt", stderr)


def execute_reproduction_safely(
    *,
    project_id: str,
    finding_id: str,
    project_workspace: Path,
    request: ReproductionRunRequest,
    reproduction_id: str | None = None,
) -> ReproductionResult:
    """Run the shared Week 3 preflight + isolated Docker path without persisting it."""
    from app.services.poc_service import poc_exists

    if not poc_exists(project_workspace, request.poc_file):
        raise FileNotFoundError("PoC file not found")
    now = _utc_now()
    command = ["forge", "test", "--match-test", request.test_name]
    base = ReproductionResult(
        reproduction_id=reproduction_id or str(uuid.uuid4()),
        finding_id=finding_id,
        project_id=project_id,
        status=ReproductionStatus.RUNNING,
        poc_file=request.poc_file,
        test_name=request.test_name,
        command=command,
        stdout=None,
        stderr=None,
        duration_ms=None,
        error_message=None,
        safety_notes=[],
        created_at=now,
        updated_at=now,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="proofguard-reproduction-") as temporary:
            isolated_repo = Path(temporary) / "repo"
            shutil.copytree(project_workspace / "repo", isolated_repo, symlinks=True)
            safety_result = safety_preflight_service.run_safety_preflight(
                repo_path=isolated_repo,
                poc_file=request.poc_file,
                test_name=request.test_name,
                command=command,
            )
            notes = [
                f"{issue.code}: {issue.message}" for issue in safety_result.issues
            ]
            if not safety_result.passed:
                return base.model_copy(
                    update={
                        "status": ReproductionStatus.REJECTED_UNSAFE,
                        "error_message": "Safety preflight failed",
                        "safety_notes": notes,
                        "updated_at": _utc_now(),
                    }
                )
            sandbox_result = sandbox_runner.run_in_sandbox(
                repo_path=isolated_repo,
                command=command,
                timeout_seconds=request.timeout_seconds,
            )
            return base.model_copy(
                update={
                    "status": map_sandbox_status(sandbox_result.status),
                    "stdout": sandbox_result.stdout,
                    "stderr": sandbox_result.stderr,
                    "duration_ms": sandbox_result.duration_ms,
                    "error_message": sandbox_result.error_message,
                    "safety_notes": notes,
                    "updated_at": _utc_now(),
                }
            )
    except OSError:
        return base.model_copy(
            update={
                "status": ReproductionStatus.ERROR,
                "error_message": "Isolated reproduction workspace could not be prepared.",
                "updated_at": _utc_now(),
            }
        )


def map_sandbox_status(status: SandboxRunStatus) -> ReproductionStatus:
    return {
        SandboxRunStatus.COMPLETED: ReproductionStatus.REPRODUCED,
        SandboxRunStatus.FAILED: ReproductionStatus.FAILED,
        SandboxRunStatus.TIMEOUT: ReproductionStatus.TIMEOUT,
        SandboxRunStatus.REJECTED: ReproductionStatus.REJECTED_UNSAFE,
        SandboxRunStatus.SANDBOX_ERROR: ReproductionStatus.SANDBOX_ERROR,
    }[status]


def _write_result(result: ReproductionResult, project_workspace: Path) -> None:
    reproduction_dir = get_reproduction_dir(project_workspace, result.finding_id)
    reproduction_dir.mkdir(parents=True, exist_ok=True)
    output_path = reproduction_dir / REPRODUCTION_FILENAME
    output_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_execution_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value not in {".", ".."}
        and all(character.isalnum() or character in "_-" for character in value)
    )
