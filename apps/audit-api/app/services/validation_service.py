import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import ValidationError

from app.schemas.validation import (
    ValidationDecision,
    ValidationDecisionUpdate,
    ValidationEvidence,
    ValidationStatus,
)


VALIDATION_FILENAME = "validation.json"


def get_validation_dir(project_workspace: Path, finding_id: str) -> Path:
    return project_workspace / "validations" / finding_id


def create_validation_decision(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
    status: ValidationStatus = ValidationStatus.NEEDS_REVIEW,
    reason: str = "Validation has not been completed yet.",
    confidence: float = 0.0,
    evidence: ValidationEvidence | None = None,
    validator_name: str = "validator_v0",
) -> ValidationDecision:
    now = _utc_now()
    decision = ValidationDecision(
        validation_id=str(uuid.uuid4()),
        project_id=project_id,
        finding_id=finding_id,
        status=status,
        confidence=confidence,
        reason=reason,
        evidence=evidence or ValidationEvidence(),
        validator_name=validator_name,
        created_at=now,
        updated_at=now,
    )
    _write_decision(decision, project_workspace)
    return decision


def save_validation_decision(
    decision: ValidationDecision,
    project_workspace: Path,
) -> ValidationDecision:
    saved = decision.model_copy(update={"updated_at": _utc_now()})
    _write_decision(saved, project_workspace)
    return saved


def load_validation_decision(
    project_workspace: Path,
    finding_id: str,
) -> Optional[ValidationDecision]:
    path = get_validation_dir(project_workspace, finding_id) / VALIDATION_FILENAME
    if not path.exists():
        return None
    return ValidationDecision.model_validate_json(path.read_text(encoding="utf-8"))


def list_validation_decisions(project_workspace: Path) -> List[ValidationDecision]:
    validations_dir = project_workspace / "validations"
    if not validations_dir.exists():
        return []

    decisions: list[ValidationDecision] = []
    for path in sorted(validations_dir.glob(f"*/{VALIDATION_FILENAME}")):
        try:
            decisions.append(ValidationDecision.model_validate_json(path.read_text(encoding="utf-8")))
        except ValidationError as exc:
            raise ValueError(f"Invalid validation decision file: {path}") from exc

    return sorted(decisions, key=lambda decision: decision.created_at)


def update_validation_decision(
    project_workspace: Path,
    finding_id: str,
    update: ValidationDecisionUpdate,
) -> ValidationDecision:
    existing = load_validation_decision(project_workspace, finding_id)
    if existing is None:
        raise FileNotFoundError(f"Validation decision not found for finding_id={finding_id}")

    update_data = update.model_dump(exclude_unset=True)
    updated = existing.model_copy(update=update_data)
    return save_validation_decision(updated, project_workspace)


def _write_decision(decision: ValidationDecision, project_workspace: Path) -> None:
    validation_dir = get_validation_dir(project_workspace, decision.finding_id)
    validation_dir.mkdir(parents=True, exist_ok=True)
    output_path = validation_dir / VALIDATION_FILENAME
    output_path.write_text(
        json.dumps(decision.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

