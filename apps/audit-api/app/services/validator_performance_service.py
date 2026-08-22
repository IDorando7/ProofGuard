from __future__ import annotations

from pathlib import Path

from app.schemas.validator_performance import ValidatorPerformanceEvaluationResult
from app.services.validation_quality_assessment_service import (
    _load_resolved_consensus,
    calculate_validation_quality_assessment,
    finalize_validation_quality_assessment,
    list_consensus_work_items,
)
from app.services.validator_category_performance_service import (
    rebuild_validator_category_performance,
)
from app.services.validator_category_score_service import calculate_validator_category_score
from app.services.validator_membership_service import derive_validator_membership
from app.services.validator_performance_event_service import create_validator_performance_event


def evaluate_resolved_consensus_performance(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    final_validation_consensus_id: str,
) -> ValidatorPerformanceEvaluationResult:
    consensus = _load_resolved_consensus(
        protocol_data_root,
        project_workspace,
        project_id,
        routing_id,
        final_validation_consensus_id,
    )
    work_items = list_consensus_work_items(protocol_data_root, consensus)
    assessments = []
    events = []
    for assignment, _, _, _ in work_items:
        calculated = calculate_validation_quality_assessment(
            protocol_data_root,
            project_workspace,
            project_id=project_id,
            routing_id=routing_id,
            final_validation_consensus_id=final_validation_consensus_id,
            validator_assignment_id=assignment.validator_assignment_id,
        )
        finalized = finalize_validation_quality_assessment(
            protocol_data_root,
            project_workspace,
            project_id=project_id,
            routing_id=routing_id,
            assessment_id=calculated.validation_quality_assessment_id,
        )
        assessments.append(finalized)
        events.append(
            create_validator_performance_event(
                protocol_data_root,
                project_id=project_id,
                routing_id=routing_id,
                assessment_id=finalized.validation_quality_assessment_id,
            )
        )

    affected = sorted({(item.validator_node_id, item.category.value) for item in events})
    performance = [
        rebuild_validator_category_performance(
            protocol_data_root, validator_node_id=node_id, category=category
        )
        for node_id, category in affected
    ]
    scores = [
        calculate_validator_category_score(
            protocol_data_root,
            validator_node_id=item.validator_node_id,
            category=item.category.value,
        )
        for item in performance
    ]
    memberships = [
        derive_validator_membership(
            protocol_data_root,
            validator_node_id=item.validator_node_id,
            category=item.category.value,
        )
        for item in scores
    ]
    return ValidatorPerformanceEvaluationResult(
        final_validation_consensus_id=final_validation_consensus_id,
        assessments=assessments,
        events=events,
        category_performance=performance,
        category_scores=scores,
        memberships=memberships,
    )


def rebuild_validator_performance_state(
    protocol_data_root: Path,
    *,
    validator_node_id: str,
    category: str,
):
    performance = rebuild_validator_category_performance(
        protocol_data_root, validator_node_id=validator_node_id, category=category
    )
    score = calculate_validator_category_score(
        protocol_data_root, validator_node_id=validator_node_id, category=category
    )
    membership = derive_validator_membership(
        protocol_data_root, validator_node_id=validator_node_id, category=category
    )
    return performance, score, membership
