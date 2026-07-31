import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.schemas.contribution import ContributionEligibilityStatus
from app.schemas.finding import Finding
from app.schemas.node import NodeCreate, NodeRecord, NodeStatistics
from app.schemas.reproduction import ReproductionStatus
from app.schemas.reputation import (
    ReputationEventApplicationStatus,
    ReputationEventType,
    ReputationProcessingStatus,
)
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    load_contribution_score,
    save_contribution_score,
)
from app.services.node_registry_service import create_node, load_node, save_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.reputation_service import (
    InvalidReputationIdentifierError,
    ReputationContributionNotFoundError,
    ReputationFindingNotFoundError,
    ReputationInputMismatchError,
    ReputationNodeNotFoundError,
    ReputationSourceChangedError,
    ReputationSubmissionNotFoundError,
    apply_statistics_delta,
    build_reputation_event,
    build_reputation_source_payload,
    calculate_reputation_delta,
    compute_reputation_source_fingerprint,
    determine_reputation_event_type,
    determine_reputation_processing_readiness,
    determine_statistics_delta,
    extract_category,
    get_node_reputation,
    get_reputation_event_dir,
    list_reputation_events,
    load_reputation_event_by_id,
    load_reputation_event_by_submission,
    normalize_reputation_value,
    process_submission_reputation,
    save_prepared_event_exclusively,
    save_reputation_event,
)
from app.services.submission_service import create_submission, load_submission, update_submission_status
from app.services.validation_service import create_validation_decision, load_validation_decision


def _finding(project_id="project-1", finding_id="finding-1", severity="High", **updates):
    values = {
        "finding_id": finding_id,
        "project_id": project_id,
        "title": "Missing access control on treasury setter",
        "category": "access_control",
        "severity": severity,
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


def _setup_case(
    tmp_path,
    validation_status="accepted",
    reproduction_status="reproduced",
    severity="High",
    create_contribution=True,
    create_validation=True,
):
    root = tmp_path / "protocol"
    workspace = tmp_path / "audits" / "project-1"
    finding = _finding(severity=severity)
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
    if reproduction_status is not None:
        reproduction = create_initial_reproduction_result("project-1", "finding-1", workspace)
        reproduction = save_reproduction_result(
            reproduction.model_copy(update={"status": ReproductionStatus(reproduction_status)}),
            workspace,
        )
    validation = None
    if create_validation:
        validation = create_validation_decision(
            "project-1",
            "finding-1",
            workspace,
            status=ValidationStatus(validation_status),
            reason="Deterministic validation result.",
            evidence=ValidationEvidence(
                has_finding=True,
                has_reproduction=reproduction is not None,
                reproduction_status=reproduction_status,
                in_scope=validation_status != "out_of_scope",
                is_duplicate=validation_status == "duplicate",
                normalized_severity=severity,
            ),
        )
        status_map = {"unsafe_poc": "unsafe"}
        submission = update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="validation_pending",
                reason="Validation pending.",
                reproduction_id=reproduction.reproduction_id if reproduction else None,
            ),
        )
        submission = update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status=status_map.get(validation_status, validation_status),
                reason="Validation finalized.",
                validation_id=validation.validation_id,
            ),
        )
    contribution = None
    if create_contribution:
        contribution = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    return root, workspace, finding, node, submission, validation, reproduction, contribution


@pytest.mark.parametrize(
    ("score", "severity", "expected"),
    [(92, "High", 0.06), (92, "Critical", 0.07), (80, "High", 0.05), (80, "Medium", 0.04), (60, "Medium", 0.03)],
)
def test_accepted_delta_rules(score, severity, expected):
    components, signals = calculate_reputation_delta(
        ReputationEventType.ACCEPTED_CONTRIBUTION,
        score,
        severity,
    )
    assert components.total_delta == expected
    assert components.total_delta == round(
        components.base_delta
        + components.contribution_bonus
        + components.severity_bonus
        + components.penalty,
        6,
    )
    assert signals


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("duplicate_finding", -0.01),
        ("out_of_scope_finding", -0.02),
        ("insufficient_evidence", -0.02),
        ("rejected_finding", -0.03),
        ("unsafe_submission", -0.08),
        ("unsupported_submission", 0),
    ],
)
def test_negative_and_unsupported_delta_rules(event_type, expected):
    components, signals = calculate_reputation_delta(ReputationEventType(event_type), 100, "Critical")
    assert components.total_delta == expected
    assert components.contribution_bonus == 0
    assert components.severity_bonus == 0
    assert len(signals) == 1


def test_unsafe_is_single_penalty_and_reputation_clamps_deterministically():
    components, signals = calculate_reputation_delta(ReputationEventType.UNSAFE_SUBMISSION, 100, "Critical")
    assert components.penalty == -0.08
    assert len(signals) == 1
    assert normalize_reputation_value(0.98 + 0.06) == 1.0
    assert normalize_reputation_value(0.03 - 0.08) == 0.0
    assert normalize_reputation_value(0.123456789) == 0.123457


@pytest.mark.parametrize(
    ("event_type", "expected_field"),
    [
        ("accepted_contribution", "accepted_submissions"),
        ("duplicate_finding", "duplicate_submissions"),
        ("rejected_finding", "rejected_submissions"),
        ("insufficient_evidence", "rejected_submissions"),
        ("out_of_scope_finding", "out_of_scope_submissions"),
        ("unsafe_submission", "unsafe_submissions"),
        ("unsupported_submission", None),
    ],
)
def test_statistics_delta_rules(event_type, expected_field):
    delta = determine_statistics_delta(ReputationEventType(event_type))
    assert delta.total_submissions == 1
    outcome_values = delta.model_dump(exclude={"total_submissions"})
    assert sum(outcome_values.values()) == (1 if expected_field else 0)
    if expected_field:
        assert outcome_values[expected_field] == 1


def test_statistics_application_preserves_existing_counts_and_input():
    current = NodeStatistics(total_submissions=5, accepted_submissions=3, rejected_submissions=2)
    before = current.model_dump()
    updated = apply_statistics_delta(
        current,
        determine_statistics_delta(ReputationEventType.DUPLICATE_FINDING),
    )
    assert updated.total_submissions == 6
    assert updated.duplicate_submissions == 1
    assert updated.accepted_submissions == 3
    assert current.model_dump() == before


@pytest.mark.parametrize(
    ("validation", "reproduction", "eligibility", "expected"),
    [
        (None, None, "ineligible", "pending"),
        ("needs_review", None, "pending", "pending"),
        ("accepted", None, "eligible", "pending"),
        ("accepted", "running", "eligible", "pending"),
        ("accepted", "reproduced", "eligible", "applied"),
        ("duplicate", None, "ineligible", "applied"),
        ("out_of_scope", None, "ineligible", "applied"),
        ("insufficient_evidence", "failed", "ineligible", "applied"),
        ("unsafe_poc", "rejected_unsafe", "ineligible", "applied"),
        ("unsupported", None, "ineligible", "applied"),
    ],
)
def test_readiness_rules(validation, reproduction, eligibility, expected):
    from app.schemas.contribution import ContributionComponents, ContributionScoreRecord

    now = datetime.now(timezone.utc)
    contribution = ContributionScoreRecord(
        score_id="score-1",
        scoring_version="contribution_v0",
        submission_id="submission-1",
        project_id="project-1",
        finding_id="finding-1",
        node_id="node-1",
        total_score=0,
        eligibility_status=eligibility,
        eligible_for_reward=eligibility == "eligible",
        eligibility_reasons=["Test state."],
        components=ContributionComponents(),
        signals=[],
        reason="Test contribution.",
        created_at=now,
        updated_at=now,
    )
    status, reasons = determine_reputation_processing_readiness(validation, reproduction, contribution)
    assert status.value == expected
    assert reasons


def test_missing_contribution_readiness_is_pending():
    status, reasons = determine_reputation_processing_readiness("accepted", "reproduced", None)
    assert status == ReputationProcessingStatus.PENDING
    assert "Contribution score is missing." in reasons


def test_accepted_ineligible_contribution_is_rejected_as_inconsistent(tmp_path):
    root, workspace, _, _, submission, _, _, contribution = _setup_case(tmp_path)
    changed = contribution.model_copy(
        update={"eligibility_status": ContributionEligibilityStatus.INELIGIBLE, "eligible_for_reward": False}
    )
    save_contribution_score(root, changed)
    with pytest.raises(ReputationInputMismatchError):
        process_submission_reputation(root, workspace, submission.submission_id)


def test_fingerprint_is_canonical_and_excludes_mutable_or_full_content(tmp_path):
    root, _, finding, _, submission, validation, reproduction, contribution = _setup_case(tmp_path)
    severity = "High"
    payload = build_reputation_source_payload(
        submission,
        validation,
        reproduction,
        contribution,
        severity,
        "access_control",
    )
    fingerprint = compute_reputation_source_fingerprint(payload)
    assert len(fingerprint) == 64
    assert fingerprint == fingerprint.lower()
    assert fingerprint == compute_reputation_source_fingerprint(dict(reversed(list(payload.items()))))
    assert not set(payload).intersection(
        {"created_at", "updated_at", "current_reputation", "root_cause", "attack_path", "impact", "poc_file"}
    )
    for field, value in (
        ("validation_status", "rejected"),
        ("contribution_score", 91),
        ("contribution_score_id", "other-score"),
        ("reproduction_status", "failed"),
        ("normalized_severity", "Critical"),
        ("category", "reentrancy"),
        ("node_id", "other-node"),
    ):
        assert fingerprint != compute_reputation_source_fingerprint({**payload, field: value})
    assert finding.root_cause not in json.dumps(payload)
    assert load_contribution_score(root, submission.submission_id) == contribution


def test_category_mismatch_fails():
    with pytest.raises(ReputationInputMismatchError):
        extract_category({"category": "access_control"}, {"category": "reentrancy"})


def test_process_accepted_event_updates_only_allowed_node_fields(tmp_path):
    root, workspace, finding, node, submission, validation, reproduction, contribution = _setup_case(tmp_path)
    source_snapshots = {
        "submission": deepcopy(load_submission(root, submission.submission_id)),
        "finding": deepcopy(finding.model_dump()),
        "validation": deepcopy(load_validation_decision(workspace, finding.finding_id)),
        "reproduction": deepcopy(load_reproduction_result(workspace, finding.finding_id)),
        "contribution": deepcopy(load_contribution_score(root, submission.submission_id)),
    }
    response = process_submission_reputation(root, workspace, submission.submission_id)
    assert response.processing_status == ReputationProcessingStatus.APPLIED
    assert response.previous_reputation == 0.5
    assert response.reputation_delta == 0.06
    assert response.current_reputation == 0.56
    event = response.event
    assert event.application_status == ReputationEventApplicationStatus.APPLIED
    assert event.applied_at is not None
    assert event.category == "access_control"
    assert event.contribution_score == contribution.total_score
    assert event.normalized_severity == "High"
    assert event.source_fingerprint
    assert event.delta_components.total_delta == 0.06
    assert event.signals
    assert event.previous_statistics["total_submissions"] == 0
    assert event.new_statistics["total_submissions"] == 1
    path = root / "reputation" / "events" / submission.submission_id / "event.json"
    stored = path.read_text(encoding="utf-8")
    assert path.is_file()
    assert "\n  \"event_id\"" in stored
    assert "/home/" not in stored
    assert "poc_file" not in stored
    assert "private_key" not in stored
    saved_node = load_node(root, node.node_id)
    assert saved_node.reputation_score == 0.56
    assert saved_node.statistics.total_submissions == 1
    assert saved_node.statistics.accepted_submissions == 1
    assert saved_node.status == node.status
    assert load_submission(root, submission.submission_id) == source_snapshots["submission"]
    assert finding.model_dump() == source_snapshots["finding"]
    assert load_validation_decision(workspace, finding.finding_id) == source_snapshots["validation"]
    assert load_reproduction_result(workspace, finding.finding_id) == source_snapshots["reproduction"]
    assert load_contribution_score(root, submission.submission_id) == source_snapshots["contribution"]


@pytest.mark.parametrize(
    ("validation_status", "reproduction_status", "event_type", "delta", "counter"),
    [
        ("duplicate", None, "duplicate_finding", -0.01, "duplicate_submissions"),
        ("rejected", None, "rejected_finding", -0.03, "rejected_submissions"),
        ("out_of_scope", None, "out_of_scope_finding", -0.02, "out_of_scope_submissions"),
        ("insufficient_evidence", "failed", "insufficient_evidence", -0.02, "rejected_submissions"),
        ("unsafe_poc", "rejected_unsafe", "unsafe_submission", -0.08, "unsafe_submissions"),
        ("unsupported", None, "unsupported_submission", 0, None),
    ],
)
def test_finalized_negative_and_unsupported_processing(
    tmp_path, validation_status, reproduction_status, event_type, delta, counter
):
    root, workspace, _, node, submission, _, _, _ = _setup_case(
        tmp_path,
        validation_status=validation_status,
        reproduction_status=reproduction_status,
    )
    response = process_submission_reputation(root, workspace, submission.submission_id)
    assert response.event.event_type.value == event_type
    assert response.reputation_delta == delta
    stats = load_node(root, node.node_id).statistics
    assert stats.total_submissions == 1
    outcome = stats.model_dump()
    outcome.pop("total_submissions")
    assert sum(outcome.values()) == (1 if counter else 0)
    if counter:
        assert outcome[counter] == 1


def test_pending_processing_creates_no_event_and_changes_no_node(tmp_path):
    root, workspace, _, node, submission, _, _, _ = _setup_case(
        tmp_path,
        validation_status="needs_review",
        reproduction_status=None,
    )
    before = load_node(root, node.node_id)
    response = process_submission_reputation(root, workspace, submission.submission_id)
    assert response.processing_status == ReputationProcessingStatus.PENDING
    assert response.reputation_delta == 0
    assert response.event_id is None
    assert load_reputation_event_by_submission(root, submission.submission_id) is None
    assert load_node(root, node.node_id) == before


def test_missing_required_records_fail_without_node_change(tmp_path):
    root, workspace, _, node, submission, _, _, _ = _setup_case(
        tmp_path / "missing-contribution",
        create_contribution=False,
    )
    before = load_node(root, node.node_id)
    with pytest.raises(ReputationContributionNotFoundError):
        process_submission_reputation(root, workspace, submission.submission_id)
    assert load_node(root, node.node_id) == before

    with pytest.raises(ReputationSubmissionNotFoundError):
        process_submission_reputation(tmp_path / "empty", tmp_path / "workspace", "missing")

    root2, workspace2, _, node2, submission2, _, _, _ = _setup_case(tmp_path / "missing-finding")
    (workspace2 / "findings" / "findings.json").unlink()
    with pytest.raises(ReputationFindingNotFoundError):
        process_submission_reputation(root2, workspace2, submission2.submission_id)
    assert load_node(root2, node2.node_id).reputation_score == 0.5


def test_missing_node_fails(tmp_path):
    root, workspace, _, node, submission, _, _, _ = _setup_case(tmp_path)
    (root / "nodes" / node.node_id / "node.json").unlink()
    with pytest.raises(ReputationNodeNotFoundError):
        process_submission_reputation(root, workspace, submission.submission_id)


def test_same_source_processing_is_idempotent(tmp_path):
    root, workspace, _, node, submission, _, _, _ = _setup_case(tmp_path)
    first = process_submission_reputation(root, workspace, submission.submission_id)
    after_first = load_node(root, node.node_id)
    second = process_submission_reputation(root, workspace, submission.submission_id)
    assert second.processing_status == ReputationProcessingStatus.ALREADY_APPLIED
    assert second.event_id == first.event_id
    assert load_node(root, node.node_id) == after_first
    assert after_first.statistics.total_submissions == 1


def test_prepared_event_recovery_assigns_exact_state_without_double_delta(tmp_path):
    root, workspace, finding, node, submission, validation, reproduction, contribution = _setup_case(tmp_path)
    prepared = build_reputation_event(
        node,
        submission,
        finding,
        validation,
        reproduction,
        contribution,
    )
    save_prepared_event_exclusively(root, prepared)
    already_updated = NodeRecord.model_validate(
        {
            **node.model_dump(),
            "reputation_score": prepared.new_reputation,
            "statistics": prepared.new_statistics,
        }
    )
    save_node(root, already_updated)
    recovered = process_submission_reputation(root, workspace, submission.submission_id)
    assert recovered.processing_status == ReputationProcessingStatus.APPLIED
    assert recovered.event_id == prepared.event_id
    saved_node = load_node(root, node.node_id)
    assert saved_node.reputation_score == prepared.new_reputation
    assert saved_node.statistics.model_dump() == prepared.new_statistics
    assert saved_node.statistics.total_submissions == 1
    assert load_reputation_event_by_submission(root, submission.submission_id).application_status.value == "applied"


def test_changed_source_conflicts_without_overwriting_or_updating_node(tmp_path):
    root, workspace, _, node, submission, _, _, contribution = _setup_case(tmp_path)
    first = process_submission_reputation(root, workspace, submission.submission_id)
    before_node = load_node(root, node.node_id)
    changed = contribution.model_copy(update={"total_score": contribution.total_score - 1})
    save_contribution_score(root, changed)
    with pytest.raises(ReputationSourceChangedError):
        process_submission_reputation(root, workspace, submission.submission_id)
    assert load_node(root, node.node_id) == before_node
    stored = load_reputation_event_by_submission(root, submission.submission_id)
    assert stored.event_id == first.event_id
    assert stored.source_fingerprint == first.event.source_fingerprint


def test_reference_mismatch_fails_before_event(tmp_path):
    root, workspace, _, node, submission, _, _, contribution = _setup_case(tmp_path)
    changed = contribution.model_copy(update={"node_id": "other-node"})
    save_contribution_score(root, changed)
    with pytest.raises(ReputationInputMismatchError):
        process_submission_reputation(root, workspace, submission.submission_id)
    assert load_node(root, node.node_id).reputation_score == 0.5
    assert load_reputation_event_by_submission(root, submission.submission_id) is None


def test_event_loading_listing_filters_and_node_summary(tmp_path):
    root, workspace, _, node, submission, _, _, _ = _setup_case(tmp_path)
    response = process_submission_reputation(root, workspace, submission.submission_id)
    event = response.event
    assert load_reputation_event_by_submission(root, submission.submission_id) == event
    assert load_reputation_event_by_id(root, event.event_id) == event
    assert load_reputation_event_by_id(root, "missing") is None
    assert list_reputation_events(root, node_id=node.node_id) == [event]
    assert list_reputation_events(root, project_id="project-1") == [event]
    assert list_reputation_events(root, category="Access Control") == [event]
    assert list_reputation_events(root, event_type="accepted_contribution") == [event]
    assert list_reputation_events(root, application_status="applied") == [event]
    assert list_reputation_events(root, node_id="other") == []
    summary = get_node_reputation(root, node.node_id)
    assert summary.current_reputation == 0.56
    assert summary.statistics.total_submissions == 1
    assert summary.total_reputation_events == 1
    assert summary.last_event_at == event.applied_at


@pytest.mark.parametrize("unsafe_id", ["../escape", "a/b", "a\\b", "/absolute", ".."])
def test_unsafe_identifiers_cannot_escape_root(tmp_path, unsafe_id):
    with pytest.raises(InvalidReputationIdentifierError):
        get_reputation_event_dir(tmp_path, unsafe_id)
    assert not (tmp_path.parent / "escape").exists()
