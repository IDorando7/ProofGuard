import json

import pytest

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.node_registry_service import create_node, load_node
from app.services.penalty_service import (
    InvalidPenaltyIdentifierError,
    PenaltyNotApplicableError,
    determine_penalty_eligibility,
    list_penalty_events,
    process_submission_penalty,
)
from app.services.reproduction_service import create_initial_reproduction_result, save_reproduction_result
from app.services.submission_service import create_submission, load_submission, update_submission_status
from app.services.validation_service import create_validation_decision


def _prepare(tmp_path, *, unsafe=True):
    root = tmp_path / "protocol"
    workspace = tmp_path / "audits" / "project-1"
    finding = Finding(
        finding_id="finding-1", project_id="project-1", title="Unsafe access control payload",
        category="access_control", severity="High", confidence=.8, contracts=["src/T.sol"], functions=["set"],
        root_cause="Missing authorization control", attack_path="Attacker invokes privileged setter",
        impact="Unauthorized protocol state mutation", recommended_fix="Add authorization checks.", agent_name="agent",
    )
    path = workspace / "findings"
    path.mkdir(parents=True)
    (path / "findings.json").write_text(json.dumps([finding.model_dump(mode="json")], indent=2), encoding="utf-8")
    node = create_node(root, NodeCreate(node_type="agent", display_name="unsafe-agent", operator_id="operator", supported_categories=["access_control"]))
    submission = create_submission(root, workspace, SubmissionCreate(project_id="project-1", finding_id="finding-1", node_id=node.node_id))
    reproduction = create_initial_reproduction_result("project-1", "finding-1", workspace)
    reproduction_status = ReproductionStatus.REJECTED_UNSAFE if unsafe else ReproductionStatus.FAILED
    reproduction = save_reproduction_result(reproduction.model_copy(update={"status": reproduction_status}), workspace)
    validation_status = ValidationStatus.UNSAFE_POC if unsafe else ValidationStatus.REJECTED
    validation = create_validation_decision(
        "project-1", "finding-1", workspace, status=validation_status, reason="Final result.",
        evidence=ValidationEvidence(has_finding=True, has_reproduction=True, reproduction_status=reproduction_status.value, in_scope=True, is_duplicate=False, normalized_severity="High"),
    )
    update_submission_status(root, submission.submission_id, SubmissionStatusUpdate(status="validation_pending", reason="Pending.", reproduction_id=reproduction.reproduction_id))
    submission = update_submission_status(root, submission.submission_id, SubmissionStatusUpdate(status="unsafe" if unsafe else "rejected", reason="Finalized.", validation_id=validation.validation_id))
    return root, workspace, node, submission, validation, reproduction


def test_unsafe_penalty_is_persistent_separate_and_idempotent(tmp_path):
    root, workspace, node, submission, validation, reproduction = _prepare(tmp_path)
    eligible, code, reasons = determine_penalty_eligibility(submission, validation, reproduction, None)
    assert eligible and code.value == "unsafe_submission" and reasons
    before = load_node(root, node.node_id)
    response = process_submission_penalty(root, workspace, submission.submission_id)
    assert response.created is True
    assert response.event.protocol_penalty_points == 25
    assert response.event.reward_denied is True
    assert response.event.simulated_stake_loss == 0
    assert response.event.executed_onchain is False
    assert response.event.requires_human_review is True
    assert load_submission(root, submission.submission_id).status.value == "penalized"
    repeated = process_submission_penalty(root, workspace, submission.submission_id)
    assert repeated.created is False
    assert repeated.event == response.event
    assert len(list_penalty_events(root, node_id=node.node_id, project_id="project-1", category="access_control")) == 1
    assert load_node(root, node.node_id).model_dump() == before.model_dump()


def test_ordinary_rejection_is_not_automatically_penalized(tmp_path):
    root, workspace, _, submission, validation, reproduction = _prepare(tmp_path, unsafe=False)
    assert determine_penalty_eligibility(submission, validation, reproduction, None)[0] is False
    with pytest.raises(PenaltyNotApplicableError):
        process_submission_penalty(root, workspace, submission.submission_id)
    assert list_penalty_events(root) == []


def test_penalty_paths_reject_traversal(tmp_path):
    from app.services.penalty_service import load_penalty_event_by_submission
    with pytest.raises(InvalidPenaltyIdentifierError):
        load_penalty_event_by_submission(tmp_path, "../escape")
