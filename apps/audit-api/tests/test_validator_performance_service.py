from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
    ValidatorAssignmentRole,
)
from app.schemas.validator_consensus import ValidationConsensusOutcome
from app.schemas.validator_performance import (
    ValidationQualityComponent,
    ValidationQualityPolicyV1,
    ValidatorCategoryScorePolicyV1,
    ValidatorMembershipStatus,
    ValidatorMembershipPolicyV1,
    ValidatorProtocolViolationCode,
)
from app.services.validation_attestation_service import create_validation_attestation
from app.services.validation_quality_assessment_service import (
    ValidationQualityNotEvaluableError,
    ValidationQualityStaleError,
    calculate_validation_quality_assessment,
    calculate_validation_quality_score,
    finalize_validation_quality_assessment,
    _component_scores,
)
from app.services.validator_category_performance_service import (
    rebuild_validator_category_performance,
)
from app.services.validator_category_score_service import (
    apply_experience_shrinkage,
    calculate_validator_category_score_values,
    metric_rate,
)
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    escalate_validation_dispute,
    finalize_validation_consensus,
    list_validation_disputes,
)
from app.services.validator_performance_service import (
    evaluate_resolved_consensus_performance,
)
from app.services.validator_membership_service import _performance_membership
from app.services.validator_protocol_compliance_service import (
    record_adjudicated_validator_protocol_violation,
)
from app.services.validation_attestation_service import get_attestation_path
from app.utils.protocol_serialization import atomic_write_json
from tests.test_validator_consensus_service import _create_evidence
from tests.test_validator_reproduction_execution_service import (
    _execute,
    _fake_executor,
    _finalized_context,
)


def _finalized_confirmed(tmp_path, monkeypatch, *, include_shadow=False):
    decisions = [
        {"validity": ValidationStatus.ACCEPTED},
        {"validity": ValidationStatus.ACCEPTED, "severity": "Critical"},
        {
            "validity": ValidationStatus.ACCEPTED,
            "reproduction": ReproductionStatus.FAILED,
        },
        {"validity": ValidationStatus.ACCEPTED},
        {"validity": ValidationStatus.ACCEPTED},
    ]
    root, workspace, routing, cluster, committee = _create_evidence(
        tmp_path, monkeypatch, decisions
    )
    if include_shadow:
        seat = committee.shadow_seats[0]
        current = _fake_executor(
            {seat.validator_node_id: ReproductionStatus.REPRODUCED}, monkeypatch
        )
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=ValidationStatus.ACCEPTED,
                root_cause_decision=RootCauseDecision.CONFIRMED,
                normalized_severity="High",
                impact_decision=ImpactDecision.VALIDATED,
            ),
        )
    consensus = calculate_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.consensus_outcome == ValidationConsensusOutcome.CONFIRMED
    consensus = finalize_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    return root, workspace, routing, cluster, committee, consensus


def test_quality_policy_decimal_weights_and_renormalization():
    policy = ValidationQualityPolicyV1()
    assert sum(policy.weight_map().values(), Decimal("0")) == Decimal("1.00")
    scores = {
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.ROOT_CAUSE: Decimal("1.000000"),
        ValidationQualityComponent.SEVERITY: Decimal("0.000000"),
        ValidationQualityComponent.IMPACT: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    }
    assert calculate_validation_quality_score(scores, policy) == Decimal("0.850000")
    applicable = {
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    }
    assert calculate_validation_quality_score(applicable, policy) == Decimal("1.000000")
    with pytest.raises(ValidationError):
        ValidationQualityPolicyV1(validity_weight=Decimal("0.34"))
    with pytest.raises(ValidationError):
        ValidationQualityPolicyV1(validity_weight=0.35)


@pytest.mark.parametrize(
    ("outcome", "validity", "reproduction_status"),
    [
        (ValidationConsensusOutcome.REJECTED, ValidationStatus.REJECTED, ReproductionStatus.FAILED),
        (ValidationConsensusOutcome.OUT_OF_SCOPE, ValidationStatus.OUT_OF_SCOPE, ReproductionStatus.UNSUPPORTED),
        (
            ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ReproductionStatus.TIMEOUT,
        ),
        (
            ValidationConsensusOutcome.UNSAFE,
            ValidationStatus.UNSAFE_POC,
            ReproductionStatus.REJECTED_UNSAFE,
        ),
        (ValidationConsensusOutcome.UNSUPPORTED, ValidationStatus.UNSUPPORTED, ReproductionStatus.UNSUPPORTED),
    ],
)
def test_terminal_nonaccepted_truth_mapping_and_unsafe_artifact_compliance(
    outcome, validity, reproduction_status
):
    consensus = SimpleNamespace(
        consensus_outcome=outcome,
        reproduction_result=SimpleNamespace(
            consensus_reached=True, consensus_value=reproduction_status.value
        ),
    )
    scores = _component_scores(
        consensus,
        SimpleNamespace(validity_decision=validity),
        SimpleNamespace(reproduction_status=reproduction_status),
    )
    assert scores == {
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    }
    assert calculate_validation_quality_score(scores) == Decimal("1.000000")


def test_confirmed_pipeline_scores_each_validator_and_is_idempotent(tmp_path, monkeypatch):
    root, workspace, routing, _, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch
    )
    outside_before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "validator-protocol" not in path.parts
    }
    first = evaluate_resolved_consensus_performance(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    second = evaluate_resolved_consensus_performance(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    by_assignment = {
        item.validator_assignment_id: item for item in first.assessments
    }
    assert len(by_assignment) == 5
    expected = [
        Decimal("1.000000"),
        Decimal("0.850000"),
        Decimal("0.800000"),
        Decimal("1.000000"),
        Decimal("1.000000"),
    ]
    assert [
        by_assignment[seat.validator_assignment_id].validation_quality_score
        for seat in committee.authoritative_seats
    ] == expected
    assert {item.validation_quality_assessment_id for item in first.assessments} == {
        item.validation_quality_assessment_id for item in second.assessments
    }
    assert {item.validator_performance_event_id for item in first.events} == {
        item.validator_performance_event_id for item in second.events
    }
    assert all(item.resolved_validations == 1 for item in first.category_performance)
    assert all(item.experience_confidence == Decimal("0.100000") for item in first.category_scores)
    assert all(item.status == ValidatorMembershipStatus.CANDIDATE for item in first.memberships)
    outside_after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "validator-protocol" not in path.parts
    }
    assert outside_after == outside_before


def test_shadow_builds_skill_without_changing_consensus(tmp_path, monkeypatch):
    root, workspace, routing, _, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch, include_shadow=True
    )
    before = consensus.calculation_fingerprint
    result = evaluate_resolved_consensus_performance(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    shadow_id = committee.shadow_seats[0].validator_assignment_id
    shadow = next(item for item in result.assessments if item.validator_assignment_id == shadow_id)
    shadow_event = next(
        item for item in result.events if item.validation_quality_assessment_id == shadow.validation_quality_assessment_id
    )
    assert shadow.assignment_role == ValidatorAssignmentRole.SHADOW
    assert shadow.validation_quality_score == Decimal("1.000000")
    assert shadow_event.assignment_role == ValidatorAssignmentRole.SHADOW
    assert consensus.calculation_fingerprint == before
    assert consensus.valid_authoritative_attestation_count == 5


@pytest.mark.parametrize(
    "decisions",
    [
        [ValidationStatus.ACCEPTED] * 3 + [ValidationStatus.REJECTED] * 2,
        [ValidationStatus.ACCEPTED] * 3,
    ],
)
def test_unresolved_consensus_creates_no_performance_truth(tmp_path, monkeypatch, decisions):
    if len(decisions) == 5:
        root, workspace, routing, cluster, _ = _create_evidence(
            tmp_path, monkeypatch, [{"validity": value} for value in decisions]
        )
    else:
        root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
        statuses = {
            seat.validator_node_id: ReproductionStatus.REPRODUCED
            for seat in committee.authoritative_seats[:3]
        }
        current = _fake_executor(statuses, monkeypatch)
        for seat in committee.authoritative_seats[:3]:
            current["value"] = seat.validator_node_id
            reproduction = _execute(root, workspace, routing, seat)
            create_validation_attestation(
                root, workspace, project_id="project-1", routing_id=routing.routing_id,
                validator_assignment_id=seat.validator_assignment_id,
                request=ValidationAttestationRequest(
                    validator_node_id=seat.validator_node_id,
                    validator_reproduction_id=reproduction.validator_reproduction_id,
                    validity_decision=ValidationStatus.ACCEPTED,
                    root_cause_decision=RootCauseDecision.CONFIRMED,
                    normalized_severity="High",
                    impact_decision=ImpactDecision.VALIDATED,
                ),
            )
    consensus = calculate_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    consensus = finalize_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    assert consensus.consensus_outcome in {
        ValidationConsensusOutcome.DISPUTED,
        ValidationConsensusOutcome.NO_QUORUM,
    }
    with pytest.raises(ValidationQualityNotEvaluableError):
        evaluate_resolved_consensus_performance(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            final_validation_consensus_id=consensus.validation_consensus_id,
        )
    assert not (root / "validator-protocol" / "performance-events").exists()


def test_round_one_minority_is_scored_against_final_cumulative_truth(tmp_path, monkeypatch):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 3 + [
        {"validity": ValidationStatus.REJECTED}
    ] * 2
    root, workspace, routing, cluster, round_one_committee = _create_evidence(
        tmp_path, monkeypatch, decisions, validator_count=10
    )
    round_one = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    round_one = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=round_one.validation_consensus_id,
    )
    with pytest.raises(ValidationQualityNotEvaluableError):
        evaluate_resolved_consensus_performance(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            final_validation_consensus_id=round_one.validation_consensus_id,
        )
    dispute = list_validation_disputes(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    )[0]
    escalation = escalate_validation_dispute(
        root, project_id="project-1", routing_id=routing.routing_id,
        validation_dispute_id=dispute.validation_dispute_id,
    )
    from app.services.validator_committee_service import load_validator_committee

    round_two_committee = load_validator_committee(
        root, "project-1", routing.routing_id, escalation.validator_committee_id
    )
    statuses = {
        seat.validator_node_id: ReproductionStatus.FAILED
        for seat in round_two_committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in round_two_committee.authoritative_seats:
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        create_validation_attestation(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=ValidationStatus.REJECTED,
                root_cause_decision=RootCauseDecision.MISMATCH,
                normalized_severity=None,
                impact_decision=ImpactDecision.REJECTED,
            ),
        )
    cumulative = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert cumulative.consensus_outcome == ValidationConsensusOutcome.REJECTED
    cumulative = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=cumulative.validation_consensus_id,
    )
    result = evaluate_resolved_consensus_performance(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        final_validation_consensus_id=cumulative.validation_consensus_id,
    )
    assessments = {item.validator_assignment_id: item for item in result.assessments}
    round_one_scores = [
        assessments[seat.validator_assignment_id].validity_accuracy
        for seat in round_one_committee.authoritative_seats
    ]
    assert round_one_scores == [Decimal("0.000000")] * 3 + [Decimal("1.000000")] * 2


def test_historical_score_neutral_defaults_and_exact_shrinkage():
    policy = ValidatorCategoryScorePolicyV1()

    def state(resolved, correct):
        return SimpleNamespace(
            resolved_validations=resolved,
            completed_assignments=resolved,
            validity_evaluated=resolved,
            validity_correct=correct,
            reproduction_evaluated=0,
            reproduction_correct=0,
            root_cause_evaluated=resolved,
            root_cause_correct=correct,
            severity_evaluated=resolved,
            severity_correct=correct,
            impact_evaluated=resolved,
            impact_correct=correct,
            protocol_compliance_evaluated=resolved,
            protocol_compliant_count=resolved,
        )

    metrics, _, confidence, _ = calculate_validator_category_score_values(state(0, 0), policy)
    assert confidence == Decimal("0.000000")
    assert metrics["reproduction_accuracy"] == Decimal("0.500000")
    assert metric_rate(0, 0) == Decimal("0.500000")

    confidence, final = apply_experience_shrinkage(Decimal("0.900000"), 1, policy)
    assert confidence == Decimal("0.100000")
    assert final == Decimal("0.540000")
    assert apply_experience_shrinkage(Decimal("0.900000"), 5, policy) == (
        Decimal("0.500000"), Decimal("0.700000")
    )
    assert apply_experience_shrinkage(Decimal("0.900000"), 20, policy) == (
        Decimal("1.000000"), Decimal("0.900000")
    )


def test_rebuild_is_deterministic_and_role_counts_are_separate(tmp_path, monkeypatch):
    root, workspace, routing, _, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch, include_shadow=True
    )
    result = evaluate_resolved_consensus_performance(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    shadow_node = committee.shadow_seats[0].validator_node_id
    category = result.assessments[0].category.value
    first = rebuild_validator_category_performance(
        root, validator_node_id=shadow_node, category=category
    )
    second = rebuild_validator_category_performance(
        root, validator_node_id=shadow_node, category=category
    )
    assert first.source_fingerprint == second.source_fingerprint
    assert first.shadow_resolved_validations == 1
    assert first.authoritative_resolved_validations == 0
    assert first.quality_score_sum == Decimal("1.000000")


def test_validator_membership_policy_has_hysteresis_and_dissent_is_not_suspension():
    policy = ValidatorMembershipPolicyV1()
    performance = SimpleNamespace(
        resolved_validations=5,
        validity_evaluated=5,
        reproduction_evaluated=5,
        protocol_violation_count=0,
    )
    score = SimpleNamespace(
        final_score=Decimal("0.550000"),
        validity_accuracy=Decimal("0.800000"),
        reproduction_accuracy=Decimal("0.800000"),
    )
    fresh, _ = _performance_membership(score, performance, None, policy)
    retained, _ = _performance_membership(
        score, performance, ValidatorMembershipStatus.ACTIVE, policy
    )
    assert fresh == ValidatorMembershipStatus.PROBATION
    assert retained == ValidatorMembershipStatus.ACTIVE

    score.final_score = Decimal("0.850000")
    performance.resolved_validations = 10
    assert _performance_membership(score, performance, None, policy)[0] == ValidatorMembershipStatus.EXPERT
    # There is no dissent/minority input in membership derivation; only resolved
    # validator-specific evidence and attributable compliance state are used.


def test_assessment_finalization_revalidates_attestation_source(tmp_path, monkeypatch):
    root, workspace, routing, _, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch
    )
    seat = committee.authoritative_seats[0]
    assessment = calculate_validation_quality_assessment(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
        validator_assignment_id=seat.validator_assignment_id,
    )
    from app.services.validation_attestation_service import list_validation_attestations

    attestation = next(
        item
        for item in list_validation_attestations(
            root,
            "project-1",
            routing.routing_id,
            finding_cluster_id=consensus.finding_cluster_id,
        )
        if item.validator_assignment_id == seat.validator_assignment_id
    )
    atomic_write_json(
        get_attestation_path(root, routing.routing_id, attestation.attestation_id),
        attestation.model_copy(update={"source_fingerprint": "0" * 64}),
    )
    with pytest.raises(ValidationQualityStaleError):
        finalize_validation_quality_assessment(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            assessment_id=assessment.validation_quality_assessment_id,
        )


def test_only_adjudicated_validator_misconduct_reduces_compliance(tmp_path, monkeypatch):
    root, workspace, routing, _, committee, consensus = _finalized_confirmed(
        tmp_path, monkeypatch
    )
    seat = committee.authoritative_seats[0]
    violation = record_adjudicated_validator_protocol_violation(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=seat.validator_assignment_id,
        violation_code=ValidatorProtocolViolationCode.FORGED_REPRODUCTION_LINK,
        adjudication_reference="adjudication-1",
    )
    repeated = record_adjudicated_validator_protocol_violation(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=seat.validator_assignment_id,
        violation_code=ValidatorProtocolViolationCode.FORGED_REPRODUCTION_LINK,
        adjudication_reference="adjudication-1",
    )
    assert repeated.validator_protocol_violation_event_id == violation.validator_protocol_violation_event_id
    result = evaluate_resolved_consensus_performance(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
    )
    assessment = next(
        item for item in result.assessments if item.validator_assignment_id == seat.validator_assignment_id
    )
    event = next(
        item
        for item in result.events
        if item.validation_quality_assessment_id == assessment.validation_quality_assessment_id
    )
    performance = next(
        item for item in result.category_performance if item.validator_node_id == seat.validator_node_id
    )
    membership = next(
        item for item in result.memberships if item.validator_node_id == seat.validator_node_id
    )
    assert assessment.protocol_compliance == Decimal("0.000000")
    assert assessment.validation_quality_score == Decimal("0.950000")
    assert event.protocol_compliant is False
    assert performance.protocol_violation_count == 1
    assert membership.status == ValidatorMembershipStatus.SUSPENDED
