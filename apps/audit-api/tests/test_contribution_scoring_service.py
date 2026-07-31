import json
from copy import deepcopy

import pytest

from app.schemas.contribution import ContributionComponents, ContributionEligibilityStatus
from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import (
    ContributionFindingNotFoundError,
    ContributionInputMismatchError,
    ContributionSubmissionNotFoundError,
    InvalidContributionIdentifierError,
    build_contribution_reason,
    calculate_contribution_for_submission,
    calculate_final_score,
    calculate_penalties,
    calculate_quality_component,
    calculate_reproducibility_component,
    calculate_severity_component,
    calculate_uniqueness_component,
    calculate_validity_component,
    determine_reward_eligibility,
    get_contribution_dir,
    list_contribution_scores,
    load_contribution_score,
    save_contribution_score,
)
from app.services.node_registry_service import create_node, load_node
from app.services.reproduction_service import create_initial_reproduction_result, load_reproduction_result, save_reproduction_result
from app.services.submission_service import create_submission, load_submission, update_submission_status
from app.services.validation_service import create_validation_decision, load_validation_decision


def _finding(project_id="project-1", finding_id="finding-1", **updates):
    values = {
        "finding_id": finding_id,
        "project_id": project_id,
        "title": "Missing access control on treasury setter",
        "category": "access_control",
        "severity": "High",
        "confidence": 0.8,
        "contracts": ["src/Treasury.sol"],
        "functions": ["setTreasury"],
        "root_cause": "Missing onlyOwner authorization check",
        "attack_path": "Attacker calls setTreasury directly",
        "impact": "Unauthorized treasury modification",
        "recommended_fix": "Add onlyOwner authorization.",
        "agent_name": "access_control_agent",
    }
    values.update(updates)
    return Finding(**values)


def _write_findings(workspace, findings):
    findings_dir = workspace / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json") for finding in findings], indent=2),
        encoding="utf-8",
    )


def _setup(tmp_path, with_validation=True, with_reproduction=True, validation_status="accepted", reproduction_status="reproduced"):
    root = tmp_path / "protocol"
    workspace = tmp_path / "audits" / "project-1"
    finding = _finding()
    _write_findings(workspace, [finding])
    node = create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name="agent-one",
            operator_id="operator-one",
            supported_categories=["access_control"],
        ),
    )
    submission = create_submission(
        root,
        workspace,
        SubmissionCreate(project_id="project-1", finding_id="finding-1", node_id=node.node_id),
    )
    reproduction = None
    if with_reproduction:
        reproduction = create_initial_reproduction_result("project-1", "finding-1", workspace)
        reproduction = save_reproduction_result(
            reproduction.model_copy(update={"status": ReproductionStatus(reproduction_status)}),
            workspace,
        )
    validation = None
    if with_validation:
        validation = create_validation_decision(
            "project-1",
            "finding-1",
            workspace,
            status=ValidationStatus(validation_status),
            reason="Deterministic validation result.",
            evidence=ValidationEvidence(
                has_finding=True,
                has_reproduction=reproduction is not None,
                reproduction_status=reproduction_status if reproduction is not None else None,
                in_scope=True,
                is_duplicate=False,
                normalized_severity="High",
            ),
        )
    if validation_status == "accepted" and validation is not None:
        submission = update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="validation_pending",
                reason="Validation is pending.",
                reproduction_id=reproduction.reproduction_id if reproduction else None,
            ),
        )
        submission = update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="accepted",
                reason="Validation accepted the finding.",
                validation_id=validation.validation_id,
            ),
        )
    return root, workspace, finding, node, submission, validation, reproduction


@pytest.mark.parametrize(
    ("status", "expected"),
    [("accepted", 30), ("needs_review", 5), ("duplicate", 0), ("out_of_scope", 0), ("rejected", 0), (None, 0)],
)
def test_validity_component(status, expected):
    points, signals = calculate_validity_component(status)
    assert points == expected
    assert signals


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("Critical", 25), ("high", 20), ("MEDIUM", 12), ("Low", 5), ("informational", 1)],
)
def test_severity_component(severity, expected):
    assert calculate_severity_component(severity)[0] == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [("reproduced", 25), ("timeout", 5), ("failed", 0), ("rejected_unsafe", 0), ("unsupported", 0), (None, 0)],
)
def test_reproducibility_component(status, expected):
    assert calculate_reproducibility_component(status)[0] == expected


def test_uniqueness_component_outcomes():
    assert calculate_uniqueness_component("accepted", {"is_duplicate": False})[0] == 15
    assert calculate_uniqueness_component("duplicate", {"is_duplicate": False})[0] == 0
    assert calculate_uniqueness_component("accepted", {"is_duplicate": True})[0] == 0
    assert calculate_uniqueness_component("accepted", {"is_duplicate": None})[0] == 5
    assert calculate_uniqueness_component(None, None)[0] == 0


def test_quality_component_is_deterministic_and_capped():
    complete = _finding()
    points, signals = calculate_quality_component(complete, None)
    assert points == 10
    assert len(signals) == 5
    placeholder = _finding(
        root_cause="placeholder",
        attack_path="unknown",
        impact="not applicable",
        recommended_fix="placeholder",
        contracts=[],
        functions=[],
    )
    assert calculate_quality_component(placeholder, None)[0] == 0


@pytest.mark.parametrize(
    ("status", "reproduction", "expected"),
    [
        (None, None, -20),
        ("rejected", None, -15),
        ("duplicate", None, -10),
        ("out_of_scope", None, -20),
        ("insufficient_evidence", None, -10),
        ("unsafe_poc", None, -25),
        ("unsupported", None, -5),
    ],
)
def test_required_penalties(status, reproduction, expected):
    assert calculate_penalties({}, status, reproduction, None)[0] == expected


def test_unsafe_penalty_is_not_double_counted():
    points, signals = calculate_penalties({}, "unsafe_poc", "rejected_unsafe", None)
    assert points == -25
    assert [signal.code for signal in signals].count("PENALTY_UNSAFE") == 1


def test_eligibility_hard_gates_and_all_reasons():
    submission = {"status": "accepted", "reward_status": "pending"}
    validation = {"status": "accepted", "evidence": {"is_duplicate": False, "in_scope": True}}
    reproduction = {"status": "reproduced"}
    status, eligible, reasons = determine_reward_eligibility(submission, validation, reproduction)
    assert status == ContributionEligibilityStatus.ELIGIBLE
    assert eligible is True
    assert len(reasons) == 4

    pending = determine_reward_eligibility(submission, validation, None)
    assert pending[:2] == (ContributionEligibilityStatus.PENDING, False)
    duplicate = determine_reward_eligibility(
        submission,
        {"status": "duplicate", "evidence": {"is_duplicate": True, "in_scope": True}},
        reproduction,
    )
    assert duplicate[:2] == (ContributionEligibilityStatus.INELIGIBLE, False)
    assert any("duplicate" in reason for reason in duplicate[2])


@pytest.mark.parametrize("status", ["duplicate", "out_of_scope", "rejected", "unsafe_poc", "unsupported"])
def test_forced_zero_final_scores(status):
    components = ContributionComponents(validity=30, severity=25, reproducibility=25, uniqueness=15, quality=10, raw_total=105)
    assert calculate_final_score(components, ContributionEligibilityStatus.INELIGIBLE, status) == 0


def test_final_score_clamps_and_caps():
    full = ContributionComponents(validity=30, severity=25, reproducibility=25, uniqueness=15, quality=10, raw_total=105)
    assert calculate_final_score(full, ContributionEligibilityStatus.ELIGIBLE, "accepted") == 100
    assert calculate_final_score(full, ContributionEligibilityStatus.PENDING, "accepted") == 25
    insufficient = full.model_copy(update={"raw_total": 90})
    assert calculate_final_score(insufficient, ContributionEligibilityStatus.INELIGIBLE, "insufficient_evidence") == 20
    negative = ContributionComponents(penalties=-100, raw_total=-100)
    assert calculate_final_score(negative, ContributionEligibilityStatus.PENDING, None) == 0
    unsafe_reproduction = full.model_copy(update={"penalties": -25, "raw_total": 80})
    assert calculate_final_score(
        unsafe_reproduction,
        ContributionEligibilityStatus.INELIGIBLE,
        "accepted",
        "rejected_unsafe",
    ) == 0


def test_reason_is_deterministic_and_reflects_state():
    components = ContributionComponents(raw_total=0)
    assert "marked duplicate" in build_contribution_reason(0, ContributionEligibilityStatus.INELIGIBLE, "duplicate", "reproduced", "High", components)
    assert "Validation has not been finalized" in build_contribution_reason(10, ContributionEligibilityStatus.PENDING, None, None, "High", components)


def test_calculation_loads_inputs_saves_explainable_eligible_score(tmp_path):
    root, workspace, finding, node, submission, validation, reproduction = _setup(tmp_path)
    before = {
        "finding": deepcopy(finding.model_dump()),
        "submission": load_submission(root, submission.submission_id),
        "node": load_node(root, node.node_id),
        "validation": load_validation_decision(workspace, finding.finding_id),
        "reproduction": load_reproduction_result(workspace, finding.finding_id),
    }
    record = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    path = root / "contributions" / submission.submission_id / "contribution.json"
    assert path.is_file()
    assert record.scoring_version == "contribution_v0"
    assert record.validation_id == validation.validation_id
    assert record.reproduction_id == reproduction.reproduction_id
    assert record.total_score == 100
    assert record.eligibility_status.value == "eligible"
    assert record.eligible_for_reward is True
    assert record.components.raw_total == sum(
        [
            record.components.validity,
            record.components.severity,
            record.components.reproducibility,
            record.components.uniqueness,
            record.components.quality,
            record.components.penalties,
        ]
    )
    assert record.signals
    stored = path.read_text(encoding="utf-8")
    assert "\n  \"score_id\"" in stored
    assert "/home/" not in stored
    assert "poc_file" not in stored
    assert "private_key" not in stored
    assert load_contribution_score(root, submission.submission_id) == record
    assert load_submission(root, submission.submission_id) == before["submission"]
    assert load_node(root, node.node_id) == before["node"]
    assert load_validation_decision(workspace, finding.finding_id) == before["validation"]
    assert load_reproduction_result(workspace, finding.finding_id) == before["reproduction"]
    assert finding.model_dump() == before["finding"]


def test_missing_validation_is_pending_and_missing_reproduction_prevents_eligibility(tmp_path):
    root, workspace, _, _, submission, _, _ = _setup(tmp_path, with_validation=False, with_reproduction=False)
    record = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    assert record.eligibility_status.value == "pending"
    assert record.eligible_for_reward is False
    assert record.total_score <= 25
    assert "PENALTY_VALIDATION_MISSING" in {signal.code for signal in record.signals}


def test_missing_submission_and_finding_fail_without_writing_score(tmp_path):
    with pytest.raises(ContributionSubmissionNotFoundError):
        calculate_contribution_for_submission(tmp_path / "protocol", tmp_path / "workspace", "missing")
    root, workspace, _, _, submission, _, _ = _setup(tmp_path / "second", with_validation=False, with_reproduction=False)
    (workspace / "findings" / "findings.json").unlink()
    with pytest.raises(ContributionFindingNotFoundError):
        calculate_contribution_for_submission(root, workspace, submission.submission_id)
    assert load_contribution_score(root, submission.submission_id) is None


@pytest.mark.parametrize(("record_type", "field"), [("validation", "finding_id"), ("validation", "project_id"), ("reproduction", "finding_id"), ("reproduction", "project_id")])
def test_mismatched_optional_inputs_fail(tmp_path, record_type, field):
    root, workspace, _, _, submission, validation, reproduction = _setup(tmp_path)
    if record_type == "validation":
        path = workspace / "validations" / "finding-1" / "validation.json"
        payload = validation.model_dump(mode="json")
    else:
        path = workspace / "reproductions" / "finding-1" / "reproduction.json"
        payload = reproduction.model_dump(mode="json")
    payload[field] = "other"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(ContributionInputMismatchError):
        calculate_contribution_for_submission(root, workspace, submission.submission_id)


def test_recalculation_is_idempotent_and_listing_filters(tmp_path):
    root, workspace, _, _, submission, _, _ = _setup(tmp_path)
    first = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    second = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    assert second.score_id == first.score_id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert len(list((root / "contributions" / submission.submission_id).glob("*.json"))) == 1
    assert list_contribution_scores(root, project_id="project-1") == [second]
    assert list_contribution_scores(root, project_id="other") == []
    assert list_contribution_scores(root, node_id=submission.node_id) == [second]
    assert list_contribution_scores(root, eligibility_status="eligible") == [second]
    assert list_contribution_scores(root, eligible_for_reward=True) == [second]
    assert list_contribution_scores(root, eligible_for_reward=False) == []


def test_save_preserves_existing_identity(tmp_path):
    root, workspace, _, _, submission, _, _ = _setup(tmp_path)
    first = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    changed = first.model_copy(update={"reason": "Still deterministic."})
    saved = save_contribution_score(root, changed)
    assert saved.score_id == first.score_id
    assert saved.created_at == first.created_at
    assert saved.reason == "Still deterministic."


@pytest.mark.parametrize("unsafe_id", ["../escape", "a/b", "a\\b", "/absolute", ".."])
def test_unsafe_identifiers_cannot_escape_root(tmp_path, unsafe_id):
    with pytest.raises(InvalidContributionIdentifierError):
        get_contribution_dir(tmp_path, unsafe_id)
    assert not (tmp_path.parent / "escape").exists()
