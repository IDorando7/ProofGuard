from copy import deepcopy

import pytest

from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.reproduction import ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
    ValidatorAssignmentRole,
    ValidatorAssignmentStatus,
    ValidatorReproductionMode,
    ValidatorReproductionRequest,
)
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    rebuild_finding_clusters_for_task,
)
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    load_node,
    save_node,
)
from app.services.reproduction_service import (
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.validation_attestation_service import (
    create_validation_attestation,
    list_validation_attestations,
    load_validation_attestation,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolEligibilityError,
    ValidatorProtocolRelationshipError,
    create_validator_assignment,
    list_validator_assignments,
    load_validator_assignment,
)
from app.services.validator_reproduction_service import (
    create_validator_reproduction_record,
    load_validator_reproduction_record,
)
from tests.test_subnet_reward_allocation_service import _setup


def _validator(root, name, *, node_type="validator", operator_id=None):
    return create_node(
        root,
        NodeCreate(
            node_type=node_type,
            display_name=f"Validator {name}",
            operator_id=operator_id or f"validator-operator-{name}",
            supported_categories=["access_control"] if node_type == "hybrid" else [],
        ),
    )


def _context(tmp_path, *, validator_type="validator"):
    root, workspace, _, routing, submissions = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(
        root, "project-1", routing.routing_id
    )[0]
    validator = _validator(root, "one", node_type=validator_type)
    assignment = create_validator_assignment(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        validator_node_id=validator.node_id,
    )
    result = load_reproduction_result(workspace, cluster.canonical_finding_id)
    assert result is not None
    return root, workspace, routing, cluster, validator, assignment, result, submissions


def _reproduction(root, workspace, routing, validator, assignment, result, **updates):
    request = ValidatorReproductionRequest(
        validator_node_id=validator.node_id,
        reproduction_mode=updates.pop(
            "reproduction_mode", ValidatorReproductionMode.SUBMITTED_POC
        ),
        underlying_reproduction_result_id=result.reproduction_id,
        **updates,
    )
    return create_validator_reproduction_record(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=request,
    )


def _attestation_request(validator, reproduction, **updates):
    values = {
        "validator_node_id": validator.node_id,
        "validator_reproduction_id": reproduction.validator_reproduction_id,
        "validity_decision": ValidationStatus.ACCEPTED,
        "root_cause_decision": RootCauseDecision.CONFIRMED,
        "normalized_severity": "High",
        "impact_decision": ImpactDecision.VALIDATED,
        "reason_codes": [
            "root_cause_confirmed",
            "reproduction_confirmed",
            "impact_confirmed",
        ],
        "evidence_references": [],
    }
    values.update(updates)
    return ValidationAttestationRequest(**values)


def test_active_validator_and_hybrid_assignments_resolve_authoritative_operator(tmp_path):
    for index, node_type in enumerate(("validator", "hybrid"), start=1):
        case = tmp_path / f"case-{index}"
        root, _, routing, cluster, validator, assignment, _, _ = _context(
            case, validator_type=node_type
        )
        assert assignment.validator_operator_id == load_node(root, validator.node_id).operator_id
        assert assignment.category == cluster.category
        assert assignment.status == ValidatorAssignmentStatus.ASSIGNED
        loaded = load_validator_assignment(
            root, "project-1", routing.routing_id, assignment.validator_assignment_id
        )
        assert loaded == assignment


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_non_active_validator_cannot_receive_assignment(tmp_path, status):
    root, workspace, _, routing, _ = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)[0]
    validator = _validator(root, status)
    change_node_status(
        root,
        validator.node_id,
        NodeStatusChangeRequest(status=status, reason="Eligibility test."),
    )
    with pytest.raises(ValidatorProtocolEligibilityError):
        create_validator_assignment(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
            validator_node_id=validator.node_id,
        )


def test_agent_only_node_cannot_receive_validator_assignment(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)[0]
    with pytest.raises(ValidatorProtocolEligibilityError):
        create_validator_assignment(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
            validator_node_id=submissions[0][0].node_id,
        )


def test_reporting_operator_conflict_blocks_validator_hybrid_and_shadow(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)[0]
    reporter_operator = cluster.members[0].operator_id
    for index, role in enumerate(
        (ValidatorAssignmentRole.AUTHORITATIVE, ValidatorAssignmentRole.SHADOW), start=1
    ):
        validator = _validator(
            root,
            f"conflict-{index}",
            node_type="hybrid",
            operator_id=reporter_operator,
        )
        with pytest.raises(ValidatorProtocolEligibilityError):
            create_validator_assignment(
                root,
                project_id="project-1",
                routing_id=routing.routing_id,
                finding_cluster_id=cluster.finding_cluster_id,
                validator_node_id=validator.node_id,
                assignment_role=role,
            )


def test_same_operator_multiple_nodes_cannot_create_multiple_authoritative_seats(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    cluster = finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)[0]
    first = _validator(root, "seat-one", operator_id="shared-validator-operator")
    second = _validator(root, "seat-two", operator_id="shared-validator-operator")
    assignment = create_validator_assignment(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        validator_node_id=first.node_id,
    )
    with pytest.raises(ValidatorProtocolConflictError):
        create_validator_assignment(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
            validator_node_id=second.node_id,
        )
    assert list_validator_assignments(
        root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id
    ) == [assignment]


@pytest.mark.parametrize(
    "status",
    [
        ReproductionStatus.REPRODUCED,
        ReproductionStatus.FAILED,
        ReproductionStatus.TIMEOUT,
        ReproductionStatus.UNSUPPORTED,
        ReproductionStatus.REJECTED_UNSAFE,
        ReproductionStatus.SANDBOX_ERROR,
    ],
)
def test_validator_reproduction_preserves_authoritative_week3_status(tmp_path, status):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    result = save_reproduction_result(result.model_copy(update={"status": status}), workspace)
    record = _reproduction(root, workspace, routing, validator, assignment, result)
    assert record.reproduction_status == status
    assert record.validator_operator_id == validator.operator_id
    assert load_validator_reproduction_record(
        root, "project-1", routing.routing_id, record.validator_reproduction_id
    ) == record
    assert "/home/" not in record.model_dump_json()


def test_reproduction_modes_and_idempotency_are_distinct(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    submitted = _reproduction(root, workspace, routing, validator, assignment, result)
    repeated = _reproduction(root, workspace, routing, validator, assignment, result)
    independent = _reproduction(
        root,
        workspace,
        routing,
        validator,
        assignment,
        result,
        reproduction_mode=ValidatorReproductionMode.INDEPENDENT_REPRODUCTION,
    )
    assert submitted == repeated
    assert submitted.validator_reproduction_id != independent.validator_reproduction_id
    assert submitted.source_fingerprint != independent.source_fingerprint


def test_wrong_validator_and_cross_cluster_reproduction_are_rejected(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    other = _validator(root, "other")
    with pytest.raises(ValidatorProtocolRelationshipError):
        _reproduction(root, workspace, routing, other, assignment, result)
    unrelated = save_reproduction_result(
        result.model_copy(
            update={"reproduction_id": "unrelated-reproduction", "finding_id": "unrelated"}
        ),
        workspace,
    )
    with pytest.raises(ValidatorProtocolRelationshipError):
        _reproduction(root, workspace, routing, validator, assignment, unrelated)


def test_structured_attestation_is_final_immutable_and_server_derives_reproduction(tmp_path):
    root, workspace, routing, cluster, validator, assignment, result, _ = _context(tmp_path)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    request = _attestation_request(validator, reproduction)
    attestation = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=request,
    )
    assert attestation.validity_decision == ValidationStatus.ACCEPTED
    assert attestation.root_cause_decision == RootCauseDecision.CONFIRMED
    assert attestation.reproduction_decision == ReproductionStatus.REPRODUCED
    assert attestation.normalized_severity.value == "High"
    assert attestation.impact_decision == ImpactDecision.VALIDATED
    assert attestation.status == "finalized"
    assert cluster.finding_cluster_id in attestation.evidence_references
    assert load_validation_attestation(
        root, "project-1", routing.routing_id, attestation.attestation_id
    ) == attestation
    stored_assignment = load_validator_assignment(
        root, "project-1", routing.routing_id, assignment.validator_assignment_id
    )
    assert stored_assignment.status == ValidatorAssignmentStatus.ATTESTED


def test_failed_reproduction_does_not_automatically_reject_finding(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    failed = save_reproduction_result(
        result.model_copy(update={"status": ReproductionStatus.FAILED}), workspace
    )
    reproduction = _reproduction(root, workspace, routing, validator, assignment, failed)
    request = _attestation_request(
        validator,
        reproduction,
        validity_decision=ValidationStatus.INSUFFICIENT_EVIDENCE,
        root_cause_decision=RootCauseDecision.INSUFFICIENT_EVIDENCE,
        normalized_severity=None,
        impact_decision=ImpactDecision.INSUFFICIENT_EVIDENCE,
        reason_codes=["reproduction_failed", "insufficient_evidence"],
    )
    attestation = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=request,
    )
    assert attestation.reproduction_decision == ReproductionStatus.FAILED
    assert attestation.validity_decision == ValidationStatus.INSUFFICIENT_EVIDENCE


def test_attestation_idempotency_canonical_order_and_conflicting_final_rejection(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    first_request = _attestation_request(
        validator,
        reproduction,
        reason_codes=["reproduction_confirmed", "root_cause_confirmed"],
        evidence_references=[assignment.validator_assignment_id, result.reproduction_id],
    )
    second_request = _attestation_request(
        validator,
        reproduction,
        reason_codes=["root_cause_confirmed", "reproduction_confirmed"],
        evidence_references=[result.reproduction_id, assignment.validator_assignment_id],
    )
    first = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=first_request,
    )
    repeated = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=second_request,
    )
    assert repeated == first
    with pytest.raises(ValidatorProtocolConflictError):
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=assignment.validator_assignment_id,
            request=_attestation_request(
                validator,
                reproduction,
                validity_decision=ValidationStatus.INSUFFICIENT_EVIDENCE,
                root_cause_decision=RootCauseDecision.INSUFFICIENT_EVIDENCE,
                normalized_severity=None,
                impact_decision=ImpactDecision.INSUFFICIENT_EVIDENCE,
            ),
        )


def test_attestation_has_no_consensus_reward_or_performance_side_effect(tmp_path):
    root, workspace, routing, cluster, validator, assignment, result, _ = _context(tmp_path)
    node_before = deepcopy(load_node(root, validator.node_id))
    cluster_before = deepcopy(cluster)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=_attestation_request(validator, reproduction),
    )
    assert load_node(root, validator.node_id) == node_before
    assert finalize_finding_clusters_for_task(root, "project-1", routing.routing_id)[0] == cluster_before
    assert len(list_validation_attestations(root, "project-1", routing.routing_id)) == 1
    assert not (root / "validator-consensus").exists()
    assert not (root / "validator-rewards").exists()
    assert not (root / "rewards" / "events").exists()


def test_node_suspension_after_assignment_blocks_new_attestation(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    change_node_status(
        root,
        validator.node_id,
        NodeStatusChangeRequest(status="suspended", reason="Suspended before attestation."),
    )
    with pytest.raises(ValidatorProtocolEligibilityError):
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=assignment.validator_assignment_id,
            request=_attestation_request(validator, reproduction),
        )


def test_reputation_change_does_not_change_assignment_or_attestation_semantics(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    save_node(
        root,
        validator.model_copy(update={"reputation_score": 0.99}),
    )
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    attestation = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=_attestation_request(validator, reproduction),
    )
    assert attestation.validator_operator_id == validator.operator_id
    assert "reputation" not in attestation.model_dump_json().lower()


def test_operator_mapping_change_after_assignment_blocks_evidence(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    save_node(
        root,
        validator.model_copy(update={"operator_id": "changed-validator-operator"}),
    )
    with pytest.raises(ValidatorProtocolConflictError):
        _reproduction(root, workspace, routing, validator, assignment, result)


def test_underlying_result_mutation_after_wrapper_blocks_attestation(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    save_reproduction_result(
        result.model_copy(update={"status": ReproductionStatus.FAILED}), workspace
    )
    with pytest.raises(ValidatorProtocolConflictError):
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=assignment.validator_assignment_id,
            request=_attestation_request(validator, reproduction),
        )


def test_pending_week3_result_cannot_be_final_validator_evidence(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    pending = save_reproduction_result(
        result.model_copy(update={"status": ReproductionStatus.RUNNING}), workspace
    )
    with pytest.raises(ValidatorProtocolConflictError):
        _reproduction(root, workspace, routing, validator, assignment, pending)


def test_unsafe_result_is_preserved_in_structured_unsafe_attestation(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    unsafe = save_reproduction_result(
        result.model_copy(update={"status": ReproductionStatus.REJECTED_UNSAFE}),
        workspace,
    )
    reproduction = _reproduction(root, workspace, routing, validator, assignment, unsafe)
    request = _attestation_request(
        validator,
        reproduction,
        validity_decision=ValidationStatus.UNSAFE_POC,
        root_cause_decision=RootCauseDecision.INSUFFICIENT_EVIDENCE,
        normalized_severity=None,
        impact_decision=ImpactDecision.INSUFFICIENT_EVIDENCE,
        reason_codes=["unsafe_reproduction"],
    )
    attestation = create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
        request=request,
    )
    assert attestation.reproduction_decision == ReproductionStatus.REJECTED_UNSAFE
    assert attestation.validity_decision == ValidationStatus.UNSAFE_POC


def test_arbitrary_evidence_reference_is_rejected(tmp_path):
    root, workspace, routing, _, validator, assignment, result, _ = _context(tmp_path)
    reproduction = _reproduction(root, workspace, routing, validator, assignment, result)
    with pytest.raises(ValidatorProtocolRelationshipError):
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=assignment.validator_assignment_id,
            request=_attestation_request(
                validator,
                reproduction,
                evidence_references=["caller-controlled-evidence"],
            ),
        )


def test_three_independent_attestations_persist_without_consensus(tmp_path):
    root, workspace, routing, cluster, first_validator, first_assignment, result, _ = _context(
        tmp_path
    )
    validators_and_assignments = [(first_validator, first_assignment)]
    for name in ("two", "three"):
        validator = _validator(root, name)
        assignment = create_validator_assignment(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
            validator_node_id=validator.node_id,
        )
        validators_and_assignments.append((validator, assignment))

    for validator, assignment in validators_and_assignments[:2]:
        reproduction = _reproduction(
            root, workspace, routing, validator, assignment, result
        )
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=assignment.validator_assignment_id,
            request=_attestation_request(validator, reproduction),
        )

    failed_result = save_reproduction_result(
        result.model_copy(update={"status": ReproductionStatus.FAILED}), workspace
    )
    third_validator, third_assignment = validators_and_assignments[2]
    third_reproduction = _reproduction(
        root,
        workspace,
        routing,
        third_validator,
        third_assignment,
        failed_result,
    )
    create_validation_attestation(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=third_assignment.validator_assignment_id,
        request=_attestation_request(
            third_validator,
            third_reproduction,
            validity_decision=ValidationStatus.INSUFFICIENT_EVIDENCE,
            root_cause_decision=RootCauseDecision.INSUFFICIENT_EVIDENCE,
            normalized_severity="High",
            impact_decision=ImpactDecision.INSUFFICIENT_EVIDENCE,
            reason_codes=["reproduction_failed", "insufficient_evidence"],
        ),
    )

    attestations = list_validation_attestations(
        root,
        "project-1",
        routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    assert len(attestations) == 3
    assert [item.validity_decision for item in attestations].count(
        ValidationStatus.ACCEPTED
    ) == 2
    assert [item.validity_decision for item in attestations].count(
        ValidationStatus.INSUFFICIENT_EVIDENCE
    ) == 1
    assert finalize_finding_clusters_for_task(
        root, "project-1", routing.routing_id
    )[0] == cluster
    assert not (root / "validator-consensus").exists()
    assert not (root / "validator-rewards").exists()


def test_path_traversal_identifiers_are_rejected(tmp_path):
    root = tmp_path / "protocol"
    with pytest.raises(ValidatorProtocolRelationshipError):
        load_validator_assignment(root, "project-1", "../routing", "assignment")
    with pytest.raises(ValidatorProtocolRelationshipError):
        load_validation_attestation(root, "project-1", "routing", "../attestation")
