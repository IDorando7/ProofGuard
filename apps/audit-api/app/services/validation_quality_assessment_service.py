from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestation,
    ValidatorAssignment,
    ValidatorAssignmentRole,
    ValidatorReproductionRecord,
)
from app.schemas.validator_committee import ValidatorCommitteeStatus
from app.schemas.validator_consensus import (
    ValidationConsensus,
    ValidationConsensusLifecycle,
    ValidationConsensusOutcome,
)
from app.schemas.validator_performance import (
    VALIDATION_QUALITY_ASSESSMENT_VERSION,
    QUALITY_COMPONENT_ORDER,
    ValidationQualityAssessment,
    ValidationQualityAssessmentStatus,
    ValidationQualityComponent,
    ValidationQualityPolicyV1,
    quantize_score,
)
from app.services.finding_cluster_service import load_finding_cluster
from app.services.node_registry_service import load_node
from app.services.validation_attestation_service import (
    build_attestation_source_payload,
    list_validation_attestations,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
    load_validator_assignment,
)
from app.services.validator_committee_service import load_validator_committee
from app.services.validator_consensus_service import (
    build_validation_round_id,
    load_validation_consensus,
    verify_validation_consensus,
)
from app.services.validator_reproduction_service import (
    load_validator_reproduction_record,
    validate_validator_reproduction_current,
)
from app.services.validator_protocol_compliance_service import (
    list_validator_protocol_violations,
)
from app.utils.protocol_serialization import atomic_create_json, atomic_write_json, protocol_fingerprint


RESOLVED_OUTCOMES = {
    ValidationConsensusOutcome.CONFIRMED,
    ValidationConsensusOutcome.REJECTED,
    ValidationConsensusOutcome.OUT_OF_SCOPE,
    ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE,
    ValidationConsensusOutcome.UNSAFE,
    ValidationConsensusOutcome.UNSUPPORTED,
}


class ValidationQualityError(ValidatorProtocolConflictError):
    pass


class ValidationQualityNotEvaluableError(ValidationQualityError):
    pass


class ValidationQualityStaleError(ValidationQualityError):
    pass


def get_quality_assessment_path(
    protocol_data_root: Path, routing_id: str, assessment_id: str
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(assessment_id, "validation quality assessment")
    root = get_validator_protocol_root(protocol_data_root) / "quality-assessments" / routing_id
    path = root / f"{assessment_id}.json"
    _ensure_within(path, root)
    return path


def build_quality_assessment_id(
    assignment_id: str,
    final_consensus_id: str,
    policy_version: str,
) -> str:
    return "validation_quality_assessment_" + protocol_fingerprint(
        {
            "assessment_version": VALIDATION_QUALITY_ASSESSMENT_VERSION,
            "validator_assignment_id": assignment_id,
            "final_validation_consensus_id": final_consensus_id,
            "quality_policy_version": policy_version,
        }
    )


def calculate_validation_quality_score(
    scores: dict[ValidationQualityComponent, Decimal],
    policy: ValidationQualityPolicyV1 | None = None,
) -> Decimal:
    policy = policy or ValidationQualityPolicyV1()
    if not scores:
        raise ValueError("At least one quality component must be applicable")
    weights = policy.weight_map()
    denominator = sum((weights[item] for item in scores), Decimal("0"))
    if denominator <= 0:
        raise ValueError("Applicable quality weight must be positive")
    return quantize_score(
        sum((weights[item] * score for item, score in scores.items()), Decimal("0"))
        / denominator
    )


def list_consensus_work_items(
    protocol_data_root: Path,
    consensus: ValidationConsensus,
) -> list[tuple[ValidatorAssignment, ValidationAttestation, ValidatorReproductionRecord, str]]:
    attestation_values = list_validation_attestations(
        protocol_data_root,
        consensus.project_id,
        consensus.routing_id,
        finding_cluster_id=consensus.finding_cluster_id,
    )
    attestations = {item.validator_assignment_id: item for item in attestation_values}
    if len(attestations) != len(attestation_values):
        raise ValidationQualityError("Duplicate finalized attestation exists for an assignment")
    items = []
    for committee_id in consensus.included_committee_ids:
        committee = load_validator_committee(
            protocol_data_root,
            consensus.project_id,
            consensus.routing_id,
            committee_id,
        )
        if committee is None or committee.status != ValidatorCommitteeStatus.FINALIZED:
            raise ValidationQualityError("Included validator committee is not finalized")
        round_id = build_validation_round_id(
            consensus.project_id,
            consensus.routing_id,
            consensus.finding_cluster_id,
            committee.validation_round,
        )
        if round_id not in consensus.included_round_ids:
            raise ValidationQualityError("Committee validation round is absent from final consensus")
        for seat in committee.authoritative_seats + committee.shadow_seats:
            attestation = attestations.get(seat.validator_assignment_id)
            if attestation is None:
                continue
            assignment = load_validator_assignment(
                protocol_data_root,
                consensus.project_id,
                consensus.routing_id,
                seat.validator_assignment_id,
            )
            if assignment is None:
                raise ValidationQualityError("Validator assignment is missing")
            reproduction = load_validator_reproduction_record(
                protocol_data_root,
                consensus.project_id,
                consensus.routing_id,
                attestation.validator_reproduction_id,
            )
            if reproduction is None:
                raise ValidationQualityError("Validator reproduction is missing")
            items.append((assignment, attestation, reproduction, round_id))
    return sorted(
        items,
        key=lambda item: (
            item[0].validation_round,
            0 if item[0].assignment_role == ValidatorAssignmentRole.AUTHORITATIVE else 1,
            item[0].seat_index or 0,
            item[0].validator_assignment_id,
        ),
    )


def calculate_validation_quality_assessment(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    final_validation_consensus_id: str,
    validator_assignment_id: str,
    policy: ValidationQualityPolicyV1 | None = None,
    persist: bool = True,
) -> ValidationQualityAssessment:
    policy = policy or ValidationQualityPolicyV1()
    consensus = _load_resolved_consensus(
        protocol_data_root,
        project_workspace,
        project_id,
        routing_id,
        final_validation_consensus_id,
    )
    matching = [
        item
        for item in list_consensus_work_items(protocol_data_root, consensus)
        if item[0].validator_assignment_id == validator_assignment_id
    ]
    if len(matching) != 1:
        raise ValidatorProtocolNotFoundError(
            "Assignment has no finalized attested work item in the resolved consensus rounds"
        )
    assignment, attestation, reproduction, round_id = matching[0]
    _validate_work_item(
        protocol_data_root,
        project_workspace,
        consensus,
        assignment,
        attestation,
        reproduction,
    )
    violations = list_validator_protocol_violations(
        protocol_data_root,
        project_id,
        routing_id,
        validator_assignment_id=assignment.validator_assignment_id,
    )
    scores = _component_scores(
        consensus,
        attestation,
        reproduction,
        protocol_compliant=not violations,
    )
    quality_score = calculate_validation_quality_score(scores, policy)
    assessment_id = build_quality_assessment_id(
        assignment.validator_assignment_id,
        consensus.validation_consensus_id,
        policy.policy_version,
    )
    source_payload = {
        "assessment_version": VALIDATION_QUALITY_ASSESSMENT_VERSION,
        "quality_policy": policy,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": consensus.finding_cluster_id,
        "final_consensus": {
            "id": consensus.validation_consensus_id,
            "source_fingerprint": consensus.source_fingerprint,
            "calculation_fingerprint": consensus.calculation_fingerprint,
            "outcome": consensus.consensus_outcome.value,
            "reproduction": consensus.reproduction_result.consensus_value,
            "root_cause": consensus.root_cause_result.consensus_value,
            "severity": consensus.final_normalized_severity.value if consensus.final_normalized_severity else None,
            "impact": consensus.impact_result.consensus_value,
        },
        "validation_round_id": round_id,
        "validator_committee_id": assignment.validator_committee_id,
        "assignment": {
            "id": assignment.validator_assignment_id,
            "source_fingerprint": assignment.source_fingerprint,
            "role": assignment.assignment_role.value,
            "node_id": assignment.validator_node_id,
            "operator_id": assignment.validator_operator_id,
        },
        "attestation": {
            "id": attestation.attestation_id,
            "source_fingerprint": attestation.source_fingerprint,
        },
        "reproduction": {
            "id": reproduction.validator_reproduction_id,
            "source_fingerprint": reproduction.source_fingerprint,
        },
        "adjudicated_protocol_violations": [
            {
                "id": item.validator_protocol_violation_event_id,
                "code": item.violation_code.value,
                "source_fingerprint": item.source_fingerprint,
            }
            for item in violations
        ],
        "applicable_components": [
            item.value for item in QUALITY_COMPONENT_ORDER if item in scores
        ],
        "component_scores": scores,
        "validation_quality_score": quality_score,
    }
    now = _utc_now()
    assessment = ValidationQualityAssessment(
        validation_quality_assessment_id=assessment_id,
        quality_policy=policy,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=consensus.finding_cluster_id,
        final_validation_consensus_id=consensus.validation_consensus_id,
        validation_round_id=round_id,
        validator_committee_id=assignment.validator_committee_id,
        validator_assignment_id=assignment.validator_assignment_id,
        validator_node_id=assignment.validator_node_id,
        validator_operator_id=assignment.validator_operator_id,
        assignment_role=assignment.assignment_role,
        category=assignment.category,
        attestation_id=attestation.attestation_id,
        validator_reproduction_id=reproduction.validator_reproduction_id,
        validity_accuracy=scores[ValidationQualityComponent.VALIDITY],
        reproduction_accuracy=scores.get(ValidationQualityComponent.REPRODUCTION),
        root_cause_accuracy=scores.get(ValidationQualityComponent.ROOT_CAUSE),
        severity_accuracy=scores.get(ValidationQualityComponent.SEVERITY),
        impact_accuracy=scores.get(ValidationQualityComponent.IMPACT),
        protocol_compliance=scores[ValidationQualityComponent.PROTOCOL_COMPLIANCE],
        applicable_components=[item for item in QUALITY_COMPONENT_ORDER if item in scores],
        validation_quality_score=quality_score,
        source_fingerprint=protocol_fingerprint(source_payload),
        status=ValidationQualityAssessmentStatus.CALCULATED,
        created_at=now,
        calculated_at=now,
    )
    if not persist:
        return assessment
    path = get_quality_assessment_path(protocol_data_root, routing_id, assessment_id)
    existing = load_validation_quality_assessment(
        protocol_data_root, project_id, routing_id, assessment_id
    )
    if existing is not None:
        if existing.source_fingerprint != assessment.source_fingerprint:
            raise ValidationQualityError("Conflicting quality assessment exists")
        return existing
    if not atomic_create_json(path, assessment, temporary_prefix=".validation-quality-create-"):
        raced = load_validation_quality_assessment(
            protocol_data_root, project_id, routing_id, assessment_id
        )
        if raced is None or raced.source_fingerprint != assessment.source_fingerprint:
            raise ValidationQualityError("Conflicting quality assessment exists")
        return raced
    return assessment


def finalize_validation_quality_assessment(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    assessment_id: str,
    policy: ValidationQualityPolicyV1 | None = None,
) -> ValidationQualityAssessment:
    stored = load_validation_quality_assessment(
        protocol_data_root, project_id, routing_id, assessment_id
    )
    if stored is None:
        raise ValidatorProtocolNotFoundError("Validation quality assessment not found")
    policy = policy or ValidationQualityPolicyV1()
    if stored.quality_policy != policy:
        raise ValidationQualityStaleError("Validation quality policy changed")
    current = calculate_validation_quality_assessment(
        protocol_data_root,
        project_workspace,
        project_id=project_id,
        routing_id=routing_id,
        final_validation_consensus_id=stored.final_validation_consensus_id,
        validator_assignment_id=stored.validator_assignment_id,
        policy=policy,
        persist=False,
    )
    if current.source_fingerprint != stored.source_fingerprint:
        raise ValidationQualityStaleError("Validation quality source changed")
    if stored.status == ValidationQualityAssessmentStatus.FINALIZED:
        return stored
    finalized = stored.model_copy(
        update={
            "status": ValidationQualityAssessmentStatus.FINALIZED,
            "finalized_at": _utc_now(),
        }
    )
    atomic_write_json(
        get_quality_assessment_path(protocol_data_root, routing_id, assessment_id),
        finalized,
        temporary_prefix=".validation-quality-finalize-",
    )
    return finalized


def load_validation_quality_assessment(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    assessment_id: str,
) -> ValidationQualityAssessment | None:
    path = get_quality_assessment_path(protocol_data_root, routing_id, assessment_id)
    if not path.exists():
        return None
    try:
        value = ValidationQualityAssessment.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError("Stored quality assessment is malformed") from exc
    if value.project_id != project_id or value.routing_id != routing_id:
        raise ValidatorProtocolRelationshipError("Quality assessment belongs to another project or routing")
    return value


def list_validation_quality_assessments(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    *,
    finding_cluster_id: str | None = None,
) -> list[ValidationQualityAssessment]:
    root = get_validator_protocol_root(protocol_data_root) / "quality-assessments" / routing_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        value = load_validation_quality_assessment(
            protocol_data_root, project_id, routing_id, path.stem
        )
        if value and (finding_cluster_id is None or value.finding_cluster_id == finding_cluster_id):
            values.append(value)
    return sorted(
        values,
        key=lambda item: (
            item.finding_cluster_id,
            item.validation_round_id,
            item.assignment_role.value,
            item.validator_operator_id,
            item.validation_quality_assessment_id,
        ),
    )


def _load_resolved_consensus(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
    consensus_id: str,
) -> ValidationConsensus:
    consensus = load_validation_consensus(
        protocol_data_root, project_id, routing_id, consensus_id
    )
    if consensus is None:
        raise ValidatorProtocolNotFoundError("Validation consensus not found")
    if consensus.lifecycle_status != ValidationConsensusLifecycle.FINALIZED:
        raise ValidationQualityNotEvaluableError("Consensus is not finalized")
    if consensus.consensus_outcome not in RESOLVED_OUTCOMES:
        raise ValidationQualityNotEvaluableError(
            "DISPUTED and NO_QUORUM consensus cannot establish performance truth"
        )
    cluster = load_finding_cluster(
        protocol_data_root, project_id, routing_id, consensus.finding_cluster_id
    )
    if (
        cluster is None
        or cluster.final_validation_consensus_id != consensus.validation_consensus_id
        or cluster.validator_consensus_outcome != consensus.consensus_outcome.value
    ):
        raise ValidationQualityNotEvaluableError(
            "Consensus is not the cluster's stable final validator resolution"
        )
    verification = verify_validation_consensus(
        protocol_data_root,
        project_workspace,
        project_id=project_id,
        routing_id=routing_id,
        validation_consensus_id=consensus_id,
    )
    if not verification.valid:
        raise ValidationQualityStaleError("Final validation consensus verification failed")
    return consensus


def _component_scores(
    consensus: ValidationConsensus,
    attestation: ValidationAttestation,
    reproduction: ValidatorReproductionRecord,
    *,
    protocol_compliant: bool = True,
) -> dict[ValidationQualityComponent, Decimal]:
    expected_validity = {
        ValidationConsensusOutcome.CONFIRMED: ValidationStatus.ACCEPTED,
        ValidationConsensusOutcome.REJECTED: ValidationStatus.REJECTED,
        ValidationConsensusOutcome.OUT_OF_SCOPE: ValidationStatus.OUT_OF_SCOPE,
        ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE: ValidationStatus.INSUFFICIENT_EVIDENCE,
        ValidationConsensusOutcome.UNSAFE: ValidationStatus.UNSAFE_POC,
        ValidationConsensusOutcome.UNSUPPORTED: ValidationStatus.UNSUPPORTED,
    }[consensus.consensus_outcome]
    scores = {
        ValidationQualityComponent.VALIDITY: Decimal("1.000000") if attestation.validity_decision == expected_validity else Decimal("0.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: (
            Decimal("1.000000") if protocol_compliant else Decimal("0.000000")
        ),
    }
    if consensus.reproduction_result.consensus_reached and consensus.reproduction_result.consensus_value is not None:
        scores[ValidationQualityComponent.REPRODUCTION] = (
            Decimal("1.000000")
            if reproduction.reproduction_status.value == consensus.reproduction_result.consensus_value
            else Decimal("0.000000")
        )
    if consensus.consensus_outcome == ValidationConsensusOutcome.CONFIRMED:
        scores[ValidationQualityComponent.ROOT_CAUSE] = (
            Decimal("1.000000")
            if attestation.root_cause_decision == RootCauseDecision.CONFIRMED
            else Decimal("0.000000")
        )
        scores[ValidationQualityComponent.SEVERITY] = (
            Decimal("1.000000")
            if attestation.normalized_severity == consensus.final_normalized_severity
            else Decimal("0.000000")
        )
        scores[ValidationQualityComponent.IMPACT] = (
            Decimal("1.000000")
            if attestation.impact_decision == ImpactDecision.VALIDATED
            else Decimal("0.000000")
        )
    return scores


def _validate_work_item(
    protocol_data_root: Path,
    project_workspace: Path,
    consensus: ValidationConsensus,
    assignment: ValidatorAssignment,
    attestation: ValidationAttestation,
    reproduction: ValidatorReproductionRecord,
) -> None:
    if not assignment.validator_committee_id:
        raise ValidationQualityError("Performance requires committee assignment provenance")
    if not (
        assignment.project_id == consensus.project_id
        and assignment.routing_id == consensus.routing_id
        and assignment.finding_cluster_id == consensus.finding_cluster_id
        and attestation.validator_assignment_id == assignment.validator_assignment_id
        and attestation.validator_node_id == assignment.validator_node_id
        and attestation.validator_operator_id == assignment.validator_operator_id
        and attestation.assignment_role == assignment.assignment_role
        and reproduction.validator_assignment_id == assignment.validator_assignment_id
        and reproduction.validator_reproduction_id == attestation.validator_reproduction_id
        and reproduction.reproduction_status == attestation.reproduction_decision
    ):
        raise ValidationQualityError("Validator work-item attribution is inconsistent")
    node = load_node(protocol_data_root, assignment.validator_node_id)
    if node is None or node.operator_id != assignment.validator_operator_id:
        raise ValidationQualityError("Validator operator attribution changed")
    validate_validator_reproduction_current(
        project_workspace,
        reproduction,
        assignment_source_fingerprint=assignment.source_fingerprint,
    )
    payload = build_attestation_source_payload(
        assignment_source_fingerprint=assignment.source_fingerprint,
        reproduction_source_fingerprint=reproduction.source_fingerprint,
        validator_assignment_id=assignment.validator_assignment_id,
        project_id=assignment.project_id,
        routing_id=assignment.routing_id,
        finding_cluster_id=assignment.finding_cluster_id,
        validator_node_id=attestation.validator_node_id,
        validator_operator_id=attestation.validator_operator_id,
        assignment_role=attestation.assignment_role.value,
        validity_decision=attestation.validity_decision.value,
        root_cause_decision=attestation.root_cause_decision.value,
        reproduction_decision=attestation.reproduction_decision.value,
        normalized_severity=attestation.normalized_severity.value if attestation.normalized_severity else None,
        impact_decision=attestation.impact_decision.value,
        reason_codes=[item.value for item in attestation.reason_codes],
        evidence_references=attestation.evidence_references,
        validator_reproduction_id=attestation.validator_reproduction_id,
    )
    if protocol_fingerprint(payload) != attestation.source_fingerprint:
        raise ValidationQualityError("Validation attestation fingerprint changed")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
