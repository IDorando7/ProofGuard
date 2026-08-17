from copy import deepcopy
from decimal import Decimal

import pytest

from app.schemas.report_quality import (
    ReportQualityAssessmentRequest,
    ReportQualityAssessmentStatus,
    ReportQualityConfig,
)
from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.services.finding_cluster_service import rebuild_finding_clusters_for_task
from app.services.finding_service import load_project_finding
from app.services.node_registry_service import load_node, save_node
from app.services.report_quality_assessment_service import (
    ReportQualityConflictError,
    ReportQualityEligibilityError,
    ReportQualityLinkageError,
    assess_report_quality,
    evaluate_fix_quality,
    evaluate_impact_quality,
    evaluate_poc_quality,
    evaluate_root_cause_quality,
    get_report_quality_assessment_path,
    list_cluster_report_quality_assessments,
    load_active_report_quality_assessment,
    rebuild_task_report_quality_assessments,
)
from app.services.reproduction_service import (
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.validation_service import (
    load_validation_decision,
    save_validation_decision,
)
from tests.test_finding_cluster_service import _mark_independent
from tests.test_subnet_reward_allocation_service import _setup


FULL_INPUT = ReportQualityAssessmentRequest(
    correctness_score="1.000000",
    poc_quality_score="0.800000",
    root_cause_quality_score="0.900000",
    impact_quality_score="0.700000",
    fix_quality_score="0.600000",
    reason_codes={
        "correctness": ["accepted_validation"],
        "poc_quality": ["reproduced_targeted_test"],
        "root_cause_quality": ["canonical_root_cause_match"],
        "impact_quality": ["accepted_severity_consistent"],
        "fix_quality": ["actionable_root_cause_fix"],
    },
)


def _sources(tmp_path, *, include_candidate=False):
    root, workspace, _, routing, submissions = _setup(
        tmp_path, include_candidate=include_candidate
    )
    result = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    return root, workspace, routing, submissions, result


def _assess(root, workspace, routing, cluster, submission, request=None, **kwargs):
    return assess_report_quality(
        root,
        workspace,
        "project-1",
        routing.routing_id,
        cluster.finding_cluster_id,
        submission.submission_id,
        request,
        **kwargs,
    )


def _file_snapshot(root, excluded="report-quality"):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and excluded not in path.parts
    }


def test_deterministic_assessment_is_honest_draft_until_fix_is_reviewed(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    submission = submissions[0][0]
    assessment, operation = _assess(
        root, workspace, routing, result.clusters[0], submission
    )
    assert operation == "created"
    assert assessment.assessment_status == ReportQualityAssessmentStatus.DRAFT
    assert assessment.quality_score is None
    assert assessment.components.correctness.score == Decimal("1.000000")
    assert assessment.components.poc_quality.score == Decimal("0.750000")
    assert assessment.components.root_cause_quality.score == Decimal("1.000000")
    assert assessment.components.impact_quality.score == Decimal("0.500000")
    assert assessment.components.fix_quality.score is None
    assert "validator_fix_assessment_required" in assessment.components.fix_quality.reason_codes
    assert assessment.operator_id == load_node(root, submission.node_id).operator_id
    assert assessment.submitted_at == submission.submitted_at


def test_validator_components_finalize_exact_q_and_are_idempotent(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    first, operation = _assess(
        root, workspace, routing, cluster, submission, FULL_INPUT
    )
    second, repeated = _assess(
        root, workspace, routing, cluster, submission, FULL_INPUT
    )
    assert operation == "created"
    assert repeated == "unchanged"
    assert first == second
    assert first.assessment_status == ReportQualityAssessmentStatus.FINALIZED
    assert first.assessment_method.value == "validator_supplied"
    assert first.quality_score == Decimal("0.860000")
    assert first.finalized_at is not None
    assert load_active_report_quality_assessment(
        root, routing.routing_id, submission.submission_id
    ) == first


def test_storage_is_atomic_human_readable_decimal_safe_and_isolated(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    protocol_before = _file_snapshot(root)
    workspace_before = _file_snapshot(workspace, excluded="never")
    assessment, _ = _assess(root, workspace, routing, cluster, submission, FULL_INPUT)
    path = get_report_quality_assessment_path(
        root,
        routing.routing_id,
        submission.submission_id,
        assessment.assessment_version,
    )
    text = path.read_text(encoding="utf-8")
    assert path == (
        root
        / "report-quality"
        / "tasks"
        / routing.routing_id
        / "submissions"
        / submission.submission_id
        / "assessment-000001.json"
    )
    assert '"quality_score": "0.860000"' in text
    assert "/home/" not in text
    assert "private_key" not in text
    assert not list(path.parent.glob("*.tmp"))
    assert _file_snapshot(root) == protocol_before
    assert _file_snapshot(workspace, excluded="never") == workspace_before
    assert not (root / "rewards" / "events").exists()


def test_finalized_conflict_requires_explicit_supersession_and_history_survives(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    original, _ = _assess(root, workspace, routing, cluster, submission, FULL_INPUT)
    changed = FULL_INPUT.model_copy(update={"fix_quality_score": Decimal("0.900000")})
    with pytest.raises(ReportQualityConflictError):
        _assess(root, workspace, routing, cluster, submission, changed)
    replacement, operation = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        changed.model_copy(update={"supersede_existing": True}),
    )
    assert operation == "created"
    assert replacement.assessment_version == 2
    assert replacement.supersedes_assessment_id == original.report_quality_assessment_id
    history = list_cluster_report_quality_assessments(
        root,
        "project-1",
        routing.routing_id,
        cluster.finding_cluster_id,
        include_superseded=True,
    )
    assert [item.assessment_status.value for item in history] == ["superseded", "finalized"]
    assert history[0].superseded_by_assessment_id == replacement.report_quality_assessment_id


def test_source_fingerprint_tracks_policy_components_and_sources_not_timestamps(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    first, _ = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        ReportQualityAssessmentRequest(finalize=False),
    )
    repeated, operation = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        ReportQualityAssessmentRequest(finalize=False),
    )
    assert operation == "unchanged"
    assert first.source_fingerprint == repeated.source_fingerprint

    validation = load_validation_decision(workspace, submission.finding_id)
    save_validation_decision(deepcopy(validation), workspace)
    timestamp_only, operation = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        ReportQualityAssessmentRequest(finalize=False),
    )
    assert operation == "unchanged"
    assert timestamp_only.source_fingerprint == first.source_fingerprint

    changed_validation = validation.model_copy(update={"confidence": 0.77})
    save_validation_decision(changed_validation, workspace)
    validation_changed, _ = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        ReportQualityAssessmentRequest(finalize=False),
    )
    assert validation_changed.validation_source_fingerprint != first.validation_source_fingerprint
    assert validation_changed.source_fingerprint != first.source_fingerprint

    changed_weights = ReportQualityConfig(
        correctness="0.34",
        poc_quality="0.26",
        root_cause="0.20",
        impact="0.10",
        fix="0.10",
    )
    policy_changed, _ = _assess(
        root,
        workspace,
        routing,
        cluster,
        submission,
        ReportQualityAssessmentRequest(finalize=False),
        configured_quality=changed_weights,
    )
    assert policy_changed.source_fingerprint != validation_changed.source_fingerprint


def test_reproduction_source_change_changes_fingerprint(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    first, _ = _assess(
        root, workspace, routing, cluster, submission, ReportQualityAssessmentRequest(finalize=False)
    )
    reproduction = load_reproduction_result(workspace, submission.finding_id)
    save_reproduction_result(
        reproduction.model_copy(update={"stdout": "deterministic output"}), workspace
    )
    changed, _ = _assess(
        root, workspace, routing, cluster, submission, ReportQualityAssessmentRequest(finalize=False)
    )
    assert changed.reproduction_source_fingerprint != first.reproduction_source_fingerprint
    assert changed.source_fingerprint != first.source_fingerprint
    assert changed.components.poc_quality.score == Decimal("0.750000")


def test_ineligible_or_mismatched_sources_cannot_finalize(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    validation = load_validation_decision(workspace, submission.finding_id)
    save_validation_decision(
        validation.model_copy(update={"status": ValidationStatus.REJECTED}), workspace
    )
    with pytest.raises(ReportQualityEligibilityError):
        _assess(root, workspace, routing, cluster, submission, FULL_INPUT)

    save_validation_decision(validation, workspace)
    node = load_node(root, submission.node_id)
    save_node(root, node.model_copy(update={"operator_id": "spoofed-operator"}))
    with pytest.raises(ReportQualityLinkageError):
        _assess(root, workspace, routing, cluster, submission, FULL_INPUT)


def test_independent_duplicate_and_multiple_nodes_for_one_operator_are_each_assessed(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path, include_candidate=True)
    first = submissions[0][0]
    second = submissions[1][0]
    _mark_independent(workspace, second.finding_id, first.finding_id)
    second_node = load_node(root, second.node_id)
    first_operator = load_node(root, first.node_id).operator_id
    save_node(root, second_node.model_copy(update={"operator_id": first_operator}))
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    for submission in (first, second):
        assessment, _ = _assess(
            root, workspace, routing, cluster, submission, FULL_INPUT
        )
        assert assessment.components.correctness.score == Decimal("1.000000")
        assert assessment.operator_id == first_operator
    listed = list_cluster_report_quality_assessments(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    )
    assert len(listed) == 2
    assert len({item.submission_id for item in listed}) == 2
    assert len({item.operator_id for item in listed}) == 1


def test_component_evaluators_are_conservative_and_status_specific(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    finding = load_project_finding(workspace, submission.finding_id)
    validation = load_validation_decision(workspace, submission.finding_id)
    reproduction = load_reproduction_result(workspace, submission.finding_id)
    targeted = reproduction.model_copy(
        update={
            "poc_file": "test/Proof.t.sol",
            "test_name": "testExploit",
            "command": ["forge", "test"],
            "stdout": "1 passed",
        }
    )
    assert evaluate_poc_quality(targeted, finding)[0] == Decimal("1.000000")
    assert evaluate_poc_quality(reproduction, finding)[0] == Decimal("0.750000")
    for status in (
        ReproductionStatus.FAILED,
        ReproductionStatus.TIMEOUT,
        ReproductionStatus.REJECTED_UNSAFE,
        ReproductionStatus.UNSUPPORTED,
    ):
        score, _, reasons, _ = evaluate_poc_quality(
            reproduction.model_copy(update={"status": status}), finding
        )
        assert score == Decimal("0.000000")
        assert reasons == [f"reproduction_{status.value}"]
    assert evaluate_poc_quality(None, finding)[0] == Decimal("0.000000")
    assert evaluate_root_cause_quality(finding, cluster)[0] == Decimal("1.000000")
    assert evaluate_root_cause_quality(
        finding.model_copy(update={"contracts": [], "functions": []}), cluster
    )[0] == Decimal("0.500000")
    assert evaluate_root_cause_quality(
        finding.model_copy(update={"root_cause": "placeholder", "contracts": [], "functions": []}),
        cluster,
    )[0] == Decimal("0.000000")
    assert evaluate_impact_quality(finding, validation, cluster)[0] == Decimal("0.500000")
    assert evaluate_fix_quality(finding)[0] is None


def test_rebuild_preserves_validator_input_and_does_not_create_rewards(tmp_path):
    root, workspace, routing, submissions, result = _sources(tmp_path)
    cluster = result.clusters[0]
    submission = submissions[0][0]
    _assess(root, workspace, routing, cluster, submission, FULL_INPUT)
    before = _file_snapshot(root)
    rebuilt = rebuild_task_report_quality_assessments(
        root, workspace, "project-1", routing.routing_id
    )
    assert rebuilt.total_assessments == 1
    assert rebuilt.unchanged_assessments == 1
    assert rebuilt.finalized_assessments == 1
    assert rebuilt.assessments[0].quality_score == Decimal("0.860000")
    assert _file_snapshot(root) == before
    assert not (root / "rewards" / "events").exists()
