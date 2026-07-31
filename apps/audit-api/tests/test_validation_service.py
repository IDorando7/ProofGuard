import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.validation import (
    ValidationDecision,
    ValidationDecisionUpdate,
    ValidationEvidence,
    ValidationStatus,
)
from app.services.validation_service import (
    create_validation_decision,
    get_validation_dir,
    list_validation_decisions,
    load_validation_decision,
    save_validation_decision,
    update_validation_decision,
)


def test_create_validation_decision_creates_folder_and_json(tmp_path: Path):
    decision = create_validation_decision(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
    )

    validation_dir = get_validation_dir(tmp_path, "finding-1")
    assert validation_dir.is_dir()
    assert (validation_dir / "validation.json").is_file()
    assert decision.status == ValidationStatus.NEEDS_REVIEW


def test_load_validation_decision_loads_saved_decision(tmp_path: Path):
    created = create_validation_decision("project-1", "finding-1", tmp_path)

    loaded = load_validation_decision(tmp_path, "finding-1")

    assert loaded == created


def test_save_validation_decision_updates_status_correctly(tmp_path: Path):
    created = create_validation_decision("project-1", "finding-1", tmp_path)
    updated = created.model_copy(update={"status": ValidationStatus.REJECTED, "reason": "False positive."})

    saved = save_validation_decision(updated, tmp_path)
    loaded = load_validation_decision(tmp_path, "finding-1")

    assert saved.status == ValidationStatus.REJECTED
    assert loaded is not None
    assert loaded.status == ValidationStatus.REJECTED


def test_update_validation_decision_changes_only_provided_fields(tmp_path: Path):
    created = create_validation_decision(
        "project-1",
        "finding-1",
        tmp_path,
        reason="Initial validation pending.",
        confidence=0.2,
    )

    updated = update_validation_decision(
        tmp_path,
        "finding-1",
        ValidationDecisionUpdate(status=ValidationStatus.ACCEPTED),
    )

    assert updated.status == ValidationStatus.ACCEPTED
    assert updated.reason == created.reason
    assert updated.confidence == created.confidence


def test_update_validation_decision_updates_updated_at(tmp_path: Path):
    created = create_validation_decision("project-1", "finding-1", tmp_path)

    updated = update_validation_decision(
        tmp_path,
        "finding-1",
        ValidationDecisionUpdate(reason="Updated reason."),
    )

    assert updated.updated_at >= created.updated_at


def test_update_validation_decision_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        update_validation_decision(
            tmp_path,
            "missing-finding",
            ValidationDecisionUpdate(reason="Updated reason."),
        )


def test_list_validation_decisions_returns_all_saved_decisions(tmp_path: Path):
    first = create_validation_decision("project-1", "finding-1", tmp_path)
    second = create_validation_decision("project-1", "finding-2", tmp_path)

    decisions = list_validation_decisions(tmp_path)

    assert [decision.validation_id for decision in decisions] == [
        first.validation_id,
        second.validation_id,
    ]


def test_list_validation_decisions_returns_decisions_sorted_by_created_at(tmp_path: Path):
    first = create_validation_decision("project-1", "finding-1", tmp_path)
    second = create_validation_decision("project-1", "finding-2", tmp_path)

    decisions = list_validation_decisions(tmp_path)

    assert decisions[0].created_at <= decisions[1].created_at
    assert [decision.finding_id for decision in decisions] == [first.finding_id, second.finding_id]


def test_validation_json_is_human_readable(tmp_path: Path):
    create_validation_decision("project-1", "finding-1", tmp_path)

    content = (get_validation_dir(tmp_path, "finding-1") / "validation.json").read_text(encoding="utf-8")
    parsed = json.loads(content)

    assert "\n  " in content
    assert parsed["project_id"] == "project-1"


def test_missing_validation_returns_none(tmp_path: Path):
    assert load_validation_decision(tmp_path, "missing-finding") is None


def test_invalid_confidence_cannot_be_saved_through_schema(tmp_path: Path):
    created = create_validation_decision("project-1", "finding-1", tmp_path)

    with pytest.raises(ValidationError):
        ValidationDecision.model_validate(
            {
                **created.model_dump(mode="json"),
                "confidence": 2.0,
            }
        )


def test_create_validation_decision_accepts_evidence(tmp_path: Path):
    decision = create_validation_decision(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
        status=ValidationStatus.DUPLICATE,
        reason="Duplicate of existing finding.",
        evidence=ValidationEvidence(is_duplicate=True, duplicate_of="finding-0"),
    )

    assert decision.evidence.is_duplicate is True
    assert decision.evidence.duplicate_of == "finding-0"

