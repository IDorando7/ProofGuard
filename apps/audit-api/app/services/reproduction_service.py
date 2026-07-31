import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from app.schemas.reproduction import (
    ReproductionResult,
    ReproductionStatus,
)


REPRODUCTION_FILENAME = "reproduction.json"


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

