from copy import deepcopy

import pytest

from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
)
from app.schemas.validator_consensus import (
    ConsensusUnresolvedReason,
    ValidationConsensusOutcome,
    ValidationDisputeReason,
    ValidationDisputeStatus,
    ValidatorConsensusPolicyV1,
)
from app.services.node_registry_service import load_node, save_node
from app.services.finding_cluster_service import load_finding_cluster
from app.services.validation_attestation_service import create_validation_attestation
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    escalate_validation_dispute,
    finalize_validation_consensus,
    list_validation_disputes,
    quorum_required,
    supermajority_required,
    verify_validation_consensus,
    get_validation_consensus_path,
)
from app.services.validator_assignment_service import ValidatorProtocolConflictError
from tests.test_validator_reproduction_execution_service import (
    _execute,
    _fake_executor,
    _finalized_context,
)
from app.utils.protocol_serialization import atomic_write_json


@pytest.mark.parametrize(
    ("target", "quorum", "threshold"),
    [(5, 4, 4), (7, 6, 5), (9, 8, 6), (11, 10, 8)],
)
def test_integer_threshold_policy(target, quorum, threshold):
    assert quorum_required(target) == quorum
    assert supermajority_required(target) == threshold


def test_invalid_threshold_target_rejected():
    with pytest.raises(ValueError):
        quorum_required(0)
    with pytest.raises(ValueError):
        supermajority_required(-1)


def _create_evidence(
    tmp_path, monkeypatch, decisions, *, validator_count=6, high_assurance=False
):
    root, workspace, routing, cluster, committee = _finalized_context(
        tmp_path,
        validator_count=validator_count,
        high_assurance=high_assurance,
    )
    statuses = {
        seat.validator_node_id: decision.get("reproduction", ReproductionStatus.REPRODUCED)
        for seat, decision in zip(committee.authoritative_seats, decisions, strict=True)
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat, decision in zip(committee.authoritative_seats, decisions, strict=True):
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        validity = decision["validity"]
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=validity,
                root_cause_decision=decision.get(
                    "root_cause",
                    RootCauseDecision.CONFIRMED
                    if validity == ValidationStatus.ACCEPTED
                    else RootCauseDecision.MISMATCH,
                ),
                normalized_severity=decision.get(
                    "severity", "High" if validity == ValidationStatus.ACCEPTED else None
                ),
                impact_decision=decision.get(
                    "impact",
                    ImpactDecision.VALIDATED
                    if validity == ValidationStatus.ACCEPTED
                    else ImpactDecision.REJECTED,
                ),
                reason_codes=[],
                evidence_references=[],
            ),
        )
    return root, workspace, routing, cluster, committee


def test_four_of_five_full_component_supermajority_confirms(tmp_path, monkeypatch):
    decisions = [
        {"validity": ValidationStatus.ACCEPTED} for _ in range(4)
    ] + [{"validity": ValidationStatus.REJECTED, "reproduction": ReproductionStatus.FAILED}]
    root, workspace, routing, cluster, _ = _create_evidence(tmp_path, monkeypatch, decisions)
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.quorum_required == 4
    assert consensus.supermajority_required == 4
    assert consensus.consensus_outcome == ValidationConsensusOutcome.CONFIRMED
    assert consensus.final_normalized_severity.value == "High"
    assert consensus.reproduction_result.value_counts == {"failed": 1, "reproduced": 4}
    finalized = finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    assert finalized.lifecycle_status == "finalized"
    updated_cluster = load_finding_cluster(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    )
    assert updated_cluster.validation_authority == "validator_consensus"
    assert updated_cluster.final_validation_consensus_id == finalized.validation_consensus_id
    assert updated_cluster.validator_consensus_outcome == "confirmed"
    assert updated_cluster.validator_consensus_severity.value == "High"
    assert verify_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=finalized.validation_consensus_id,
    ).valid


def test_high_assurance_five_of_seven_confirms_without_weighted_votes(
    tmp_path, monkeypatch
):
    decisions = [{"validity": ValidationStatus.ACCEPTED, "severity": "Critical"}] * 5 + [
        {"validity": ValidationStatus.REJECTED},
        {"validity": ValidationStatus.REJECTED},
    ]
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path,
        monkeypatch,
        decisions,
        validator_count=8,
        high_assurance=True,
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.authoritative_target_size == 7
    assert consensus.quorum_required == 6
    assert consensus.supermajority_required == 5
    assert consensus.consensus_outcome == ValidationConsensusOutcome.CONFIRMED
    assert consensus.final_normalized_severity.value == "Critical"


@pytest.mark.parametrize(
    ("validity", "outcome"),
    [
        (ValidationStatus.REJECTED, ValidationConsensusOutcome.REJECTED),
        (ValidationStatus.OUT_OF_SCOPE, ValidationConsensusOutcome.OUT_OF_SCOPE),
        (
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE,
        ),
        (ValidationStatus.UNSAFE_POC, ValidationConsensusOutcome.UNSAFE),
        (ValidationStatus.UNSUPPORTED, ValidationConsensusOutcome.UNSUPPORTED),
    ],
)
def test_nonaccepted_validity_supermajority_is_terminal_without_severity(
    tmp_path, monkeypatch, validity, outcome
):
    decisions = [{"validity": validity}] * 4 + [
        {"validity": ValidationStatus.ACCEPTED}
    ]
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path, monkeypatch, decisions
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.consensus_outcome == outcome
    assert consensus.final_normalized_severity is None


@pytest.mark.parametrize(
    "decisions",
    [
        [ValidationStatus.ACCEPTED] * 3 + [ValidationStatus.REJECTED] * 2,
        [ValidationStatus.REJECTED] * 3 + [ValidationStatus.ACCEPTED] * 2,
    ],
)
def test_three_two_simple_majority_is_disputed(tmp_path, monkeypatch, decisions):
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path, monkeypatch, [{"validity": item} for item in decisions]
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.consensus_outcome == ValidationConsensusOutcome.DISPUTED
    assert consensus.dispute_reason_codes == [ValidationDisputeReason.VALIDITY_DISAGREEMENT]


def test_accepted_validity_with_severity_and_reproduction_disagreement_is_disputed(
    tmp_path, monkeypatch
):
    decisions = [
        {"validity": ValidationStatus.ACCEPTED, "severity": severity, "reproduction": reproduction}
        for severity, reproduction in zip(
            ["High", "High", "High", "Critical"],
            [ReproductionStatus.REPRODUCED] * 3 + [ReproductionStatus.FAILED],
            strict=True,
        )
    ] + [{"validity": ValidationStatus.REJECTED, "reproduction": ReproductionStatus.FAILED}]
    root, workspace, routing, cluster, _ = _create_evidence(tmp_path, monkeypatch, decisions)
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.validity_result.consensus_value == "accepted"
    assert consensus.consensus_outcome == ValidationConsensusOutcome.DISPUTED
    assert consensus.dispute_reason_codes == [
        ValidationDisputeReason.REPRODUCTION_DISAGREEMENT,
        ValidationDisputeReason.SEVERITY_DISAGREEMENT,
    ]


def test_shadow_attestation_never_counts_and_three_attestations_is_no_quorum(
    tmp_path, monkeypatch
):
    root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in committee.authoritative_seats[:3] + committee.shadow_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in committee.authoritative_seats[:3] + committee.shadow_seats:
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
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert consensus.valid_authoritative_attestation_count == 3
    assert consensus.consensus_outcome == ValidationConsensusOutcome.NO_QUORUM
    assert consensus.validity_result.value_counts == {"accepted": 3}


def test_finalized_round_rejects_late_reproduction_evidence(tmp_path, monkeypatch):
    root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    seat = committee.authoritative_seats[0]
    _fake_executor({seat.validator_node_id: ReproductionStatus.REPRODUCED}, monkeypatch)
    with pytest.raises(ValidatorProtocolConflictError, match="Finalized validation round"):
        _execute(root, workspace, routing, seat)


def test_dispute_escalation_uses_four_new_operators_and_is_idempotent(tmp_path, monkeypatch):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 3 + [
        {"validity": ValidationStatus.REJECTED}
    ] * 2
    root, workspace, routing, cluster, committee = _create_evidence(
        tmp_path, monkeypatch, decisions, validator_count=10
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    dispute = list_validation_disputes(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    )[0]
    result = escalate_validation_dispute(
        root, project_id="project-1", routing_id=routing.routing_id,
        validation_dispute_id=dispute.validation_dispute_id,
    )
    repeated = escalate_validation_dispute(
        root, project_id="project-1", routing_id=routing.routing_id,
        validation_dispute_id=dispute.validation_dispute_id,
    )
    assert result.dispute.status == ValidationDisputeStatus.ESCALATED
    assert repeated.validator_committee_id == result.validator_committee_id
    round_two = result.validation_round
    assert round_two is not None and round_two.round_number == 2
    from app.services.validator_committee_service import load_validator_committee
    new_committee = load_validator_committee(
        root, "project-1", routing.routing_id, result.validator_committee_id
    )
    assert new_committee is not None
    assert len(new_committee.authoritative_seats) == 4
    old_operators = {seat.operator_id for seat in committee.authoritative_seats + committee.shadow_seats}
    new_operators = {seat.operator_id for seat in new_committee.authoritative_seats}
    assert len(new_operators) == 4
    assert old_operators.isdisjoint(new_operators)

    # Round 2 uses the unchanged Day 3/Day 1 evidence paths. All legitimate
    # Round 1 evidence remains in the cumulative N=9 calculation.
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in new_committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in new_committee.authoritative_seats:
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
    cumulative = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert cumulative.authoritative_target_size == 9
    assert cumulative.quorum_required == 8
    assert cumulative.supermajority_required == 6
    assert cumulative.validity_result.value_counts == {"accepted": 7, "rejected": 2}
    assert cumulative.consensus_outcome == ValidationConsensusOutcome.CONFIRMED
    finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=cumulative.validation_consensus_id,
    )
    assert list_validation_disputes(
        root, "project-1", routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )[0].status == ValidationDisputeStatus.RESOLVED
    with pytest.raises(ValidatorProtocolConflictError, match="Maximum escalation"):
        escalate_validation_dispute(
            root, project_id="project-1", routing_id=routing.routing_id,
            validation_dispute_id=dispute.validation_dispute_id,
        )


def test_escalation_with_only_three_new_operators_fails_closed(tmp_path, monkeypatch):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 3 + [
        {"validity": ValidationStatus.REJECTED}
    ] * 2
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path, monkeypatch, decisions, validator_count=9
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalize_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    dispute = list_validation_disputes(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    )[0]
    result = escalate_validation_dispute(
        root, project_id="project-1", routing_id=routing.routing_id,
        validation_dispute_id=dispute.validation_dispute_id,
    )
    assert result.dispute.status == ValidationDisputeStatus.BLOCKED_INSUFFICIENT_VALIDATORS
    assert result.validation_round is None
    assert result.validator_committee_id is None


def test_reputation_changes_do_not_change_consensus_snapshot(tmp_path, monkeypatch):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 4 + [
        {"validity": ValidationStatus.REJECTED}
    ]
    root, workspace, routing, cluster, committee = _create_evidence(tmp_path, monkeypatch, decisions)
    first = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    node = load_node(root, committee.authoritative_seats[0].validator_node_id)
    save_node(root, node.model_copy(update={"reputation_score": 0.01}))
    repeated = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert repeated.source_fingerprint == first.source_fingerprint
    assert repeated.calculation_fingerprint == first.calculation_fingerprint
    assert repeated.consensus_outcome == first.consensus_outcome


def test_policy_change_blocks_stale_finalization(tmp_path, monkeypatch):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 4 + [
        {"validity": ValidationStatus.REJECTED}
    ]
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path, monkeypatch, decisions
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    with pytest.raises(ValidatorProtocolConflictError, match="policy changed"):
        finalize_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=consensus.validation_consensus_id,
            policy=ValidatorConsensusPolicyV1(quorum_missing_tolerance=0),
        )


def test_verification_detects_tampered_component_even_with_old_fingerprint(
    tmp_path, monkeypatch
):
    decisions = [{"validity": ValidationStatus.ACCEPTED}] * 4 + [
        {"validity": ValidationStatus.REJECTED}
    ]
    root, workspace, routing, cluster, _ = _create_evidence(
        tmp_path, monkeypatch, decisions
    )
    consensus = calculate_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    tampered_root = consensus.root_cause_result.model_copy(
        update={
            "consensus_reached": False,
            "consensus_value": None,
            "supporting_attestation_ids": [],
            "dissenting_attestation_ids": [
                item.attestation_id
                for item in []
            ],
            "unresolved_reason": ConsensusUnresolvedReason.NO_SUPERMAJORITY,
        }
    )
    tampered = consensus.model_copy(update={"root_cause_result": tampered_root})
    atomic_write_json(
        get_validation_consensus_path(
            root, routing.routing_id, consensus.validation_consensus_id
        ),
        tampered,
    )
    result = verify_validation_consensus(
        root, workspace, project_id="project-1", routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    assert result.valid is False
    assert result.checks["calculation_fingerprint"] is False
