from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.routing import (
    CategoryRoutingResult,
    ProjectRoutingRequest,
    RoutingAssignment,
    RoutingAssignmentMode,
    RoutingCandidateSnapshot,
    RoutingSelectionType,
    RoutingShortageReason,
    RoutingStatus,
)


NOW = datetime.now(timezone.utc)


def _snapshot(**updates):
    values = {
        "node_id": "node-1",
        "subnet_id": "subnet_access_control",
        "category": "access_control",
        "node_status": "active",
        "membership_status": "active",
        "category_score_id": "category_score_node-1_access_control",
        "category_score": 0.8,
        "experience_confidence": 0.7,
        "finalized_submissions": 7,
        "accepted_unique_submissions": 5,
        "membership_source_fingerprint": "a" * 64,
        "score_source_fingerprint": "b" * 64,
        "exploration_assignments_before": 0,
        "last_exploration_assignment_at": None,
    }
    values.update(updates)
    return RoutingCandidateSnapshot(**values)


def _assignment(**updates):
    snapshot = updates.pop("candidate_snapshot", _snapshot())
    values = {
        "assignment_id": "assignment-1",
        "routing_id": "routing-1",
        "project_id": "project-1",
        "subnet_id": "subnet_access_control",
        "category": "access_control",
        "node_id": "node-1",
        "selection_type": "ranked",
        "assignment_mode": "production",
        "membership_status": "active",
        "category_score": 0.8,
        "experience_confidence": 0.7,
        "position": 1,
        "selection_reasons": ["Selected deterministically."],
        "candidate_snapshot": snapshot,
        "qualification_reference": None,
        "created_at": NOW,
    }
    values.update(updates)
    return RoutingAssignment(**values)


def test_routing_enums_contain_required_values():
    assert {item.value for item in RoutingStatus} == {
        "calculated",
        "finalized",
        "superseded",
    }
    assert {item.value for item in RoutingSelectionType} == {
        "ranked",
        "exploration",
    }
    assert {item.value for item in RoutingAssignmentMode} == {
        "production",
        "shadow",
    }
    assert {item.value for item in RoutingShortageReason} == {
        "no_subnet",
        "subnet_not_active",
        "no_ranked_members",
        "insufficient_ranked_members",
        "no_exploration_members",
        "insufficient_exploration_members",
        "category_not_supported",
        "project_scope_missing",
        "project_category_missing",
        "no_eligible_nodes",
    }


def test_project_routing_request_validates_categories_and_public_fields():
    request = ProjectRoutingRequest(
        categories=["access_control", "reentrancy"],
        nodes_per_category=4,
    )
    assert [item.value for item in request.categories] == [
        "access_control",
        "reentrancy",
    ]
    for values in (
        {"categories": ["access_control", "access-control"]},
        {"categories": ["unknown"]},
        {"nodes_per_category": 0},
        {"nodes_per_category": 101},
        {"description": "x" * 501},
        {"selected_node_ids": ["node-1"]},
        {"assignment_mode": "production"},
        {"category_score": 1},
    ):
        with pytest.raises((ValidationError, ValueError)):
            ProjectRoutingRequest(**values)


def test_ranked_assignment_requires_production_and_core_membership():
    assert _assignment().qualification_reference is None
    for updates in (
        {"assignment_mode": "shadow"},
        {"membership_status": "probation"},
        {"position": 0},
        {"selection_reasons": []},
        {"qualification_reference": "benchmark-secret"},
    ):
        with pytest.raises(ValidationError):
            _assignment(**updates)


def test_exploration_assignment_requires_shadow_and_candidate_or_probation():
    snapshot = _snapshot(
        membership_status="probation",
        category_score=0.5,
        experience_confidence=0.4,
    )
    assignment = _assignment(
        selection_type="exploration",
        assignment_mode="shadow",
        membership_status="probation",
        category_score=0.5,
        experience_confidence=0.4,
        candidate_snapshot=snapshot,
    )
    assert assignment.assignment_mode.value == "shadow"
    for updates in (
        {"assignment_mode": "production"},
        {"membership_status": "active"},
    ):
        with pytest.raises(ValidationError):
            _assignment(
                selection_type="exploration",
                candidate_snapshot=snapshot,
                category_score=0.5,
                experience_confidence=0.4,
                **updates,
            )


def test_category_result_validates_totals_duplicates_and_shortages():
    assignment = _assignment()
    result = CategoryRoutingResult(
        category="access_control",
        subnet_id="subnet_access_control",
        subnet_status="active",
        requested_assignments=1,
        ranked_target=1,
        exploration_target=0,
        ranked_selected=1,
        exploration_selected=0,
        total_selected=1,
        complete=True,
        assignments=[assignment],
        shortage_reasons=[],
        warnings=[],
    )
    assert result.complete
    with pytest.raises(ValidationError):
        result.model_copy(
            update={
                "requested_assignments": 2,
                "ranked_target": 2,
                "ranked_selected": 2,
                "total_selected": 2,
                "assignments": [assignment, assignment],
                "complete": True,
            }
        ).model_validate(
            result.model_copy(
                update={
                    "requested_assignments": 2,
                    "ranked_target": 2,
                    "ranked_selected": 2,
                    "total_selected": 2,
                    "assignments": [assignment, assignment],
                }
            ).model_dump()
        )


def test_routing_schema_has_no_execution_financial_or_benchmark_payloads():
    request_fields = set(ProjectRoutingRequest.model_fields)
    assignment_fields = set(RoutingAssignment.model_fields)
    assert not {
        "selected_node_ids",
        "category_score",
        "assignment_mode",
        "rank",
    } & request_fields
    assert "qualification_reference" in assignment_fields
    assert not {
        "benchmark_answers",
        "benchmark_fixture",
        "poc",
        "token",
        "stake",
        "execution_result",
    } & assignment_fields
