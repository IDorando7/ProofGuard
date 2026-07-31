from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from app.schemas.contribution import (
    ContributionEligibilityStatus,
    ContributionScoreRecord,
)
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, NodeStatus, normalize_category
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.reward import RewardEvent, RewardEventStatus
from app.schemas.routing import (
    CategoryRoutingResult,
    ProjectRoutingRecord,
    RoutingAssignment,
    RoutingAssignmentMode,
    RoutingSelectionType,
    RoutingStatus,
)
from app.schemas.submission import (
    SubmissionRecord,
    SubmissionRewardStatus,
    SubmissionStatus,
)
from app.schemas.subnet import SubnetMemberStatus
from app.schemas.subnet_reward import (
    CATEGORY_MULTIPLIER_POLICY_VERSION,
    MEMBERSHIP_MULTIPLIER_POLICY_VERSION,
    PROTOCOL_POINTS_QUANTUM,
    SUBNET_REWARD_EVENT_VERSION,
    SUBNET_REWARD_POLICY_VERSION,
    SUBNET_REWARD_VERSION,
    CategoryPoolAllocation,
    NodeSubnetRewardSummary,
    SubnetRewardCalculationResponse,
    SubnetRewardCycle,
    SubnetRewardCycleCreateRequest,
    SubnetRewardCycleStatus,
    SubnetRewardEligibilityResult,
    SubnetRewardEligibilityStatus,
    SubnetRewardEvent,
    SubnetRewardEventStatus,
    SubnetRewardExclusion,
    SubnetRewardExclusionReason,
    SubnetRewardFinalizationResponse,
    SubnetRewardProcessingStatus,
    SubnetRewardSourceSnapshot,
    SubnetSubmissionRewardAllocation,
)
from app.schemas.validation import ValidationDecision, ValidationStatus
from app.services.contribution_scoring_service import load_contribution_score
from app.services.node_registry_service import load_node
from app.services.reproduction_service import load_reproduction_result
from app.services.reward_service import load_reward_event_by_submission
from app.services.submission_service import list_submissions
from app.services.subnet_router_service import load_routing_record
from app.services.validation_service import load_validation_decision


SUBNET_REWARD_CYCLE_FILENAME = "cycle.json"
SUBNET_REWARD_PROJECTS_DIRECTORY = "projects"
SUBNET_REWARD_EVENTS_DIRECTORY = "events"
SUBNET_REWARD_SUBMISSION_EVENTS_DIRECTORY = "submissions"
NORMALIZED_SHARE_QUANTUM = Decimal("0.000000000001")


class SubnetRewardServiceError(ValueError):
    """Base error for deterministic subnet reward allocation."""


class SubnetRewardProjectNotFoundError(SubnetRewardServiceError):
    pass


class SubnetRewardRoutingNotFoundError(SubnetRewardServiceError):
    pass


class SubnetRewardRoutingNotFinalizedError(SubnetRewardServiceError):
    pass


class SubnetRewardCycleNotFoundError(SubnetRewardServiceError):
    pass


class SubnetRewardCycleConflictError(SubnetRewardServiceError):
    pass


class SubnetRewardAlreadyFinalizedError(SubnetRewardServiceError):
    pass


class SubnetRewardInvalidCategoryWeightsError(SubnetRewardServiceError):
    pass


class SubnetRewardSubmissionNotFoundError(SubnetRewardServiceError):
    pass


class SubnetRewardSourceMismatchError(SubnetRewardServiceError):
    pass


class SubnetRewardSourceChangedError(SubnetRewardServiceError):
    pass


class SubnetRewardDuplicateEventError(SubnetRewardServiceError):
    pass


class SubnetRewardAlreadyPaidError(SubnetRewardServiceError):
    pass


class SubnetRewardConservationError(SubnetRewardServiceError):
    pass


class InvalidSubnetRewardIdentifierError(SubnetRewardServiceError):
    pass


class SubnetRewardStorageError(SubnetRewardServiceError):
    pass


@dataclass(frozen=True)
class WeightedRewardItem:
    allocation_id: str
    node_id: str
    submission_id: str
    raw_weight: Decimal
    contribution_score: Decimal


@dataclass(frozen=True)
class SubnetRewardSourceBundle:
    routing_record: ProjectRoutingRecord
    routing_assignment: RoutingAssignment | None
    submission: SubmissionRecord
    contribution_score_record: ContributionScoreRecord | None
    validation_decision: ValidationDecision | None
    reproduction_result: ReproductionResult | None
    existing_week_5_reward_events: tuple[RewardEvent, ...]
    existing_subnet_reward_events: tuple[SubnetRewardEvent, ...]
    current_node_record: NodeRecord | None
    reward_cycle_id: str | None = None


def get_subnet_rewards_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "subnet-rewards"


def get_project_subnet_rewards_root(
    protocol_data_root: Path,
    project_id: str,
) -> Path:
    _validate_identifier(project_id, "project")
    return _safe_child(
        get_subnet_rewards_root(protocol_data_root)
        / SUBNET_REWARD_PROJECTS_DIRECTORY,
        project_id,
    )


def get_subnet_reward_cycle_path(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
) -> Path:
    _validate_identifier(reward_cycle_id, "reward cycle")
    return (
        _safe_child(
            get_project_subnet_rewards_root(protocol_data_root, project_id),
            reward_cycle_id,
        )
        / SUBNET_REWARD_CYCLE_FILENAME
    )


def get_submission_reward_events_root(
    protocol_data_root: Path,
    submission_id: str,
) -> Path:
    _validate_identifier(submission_id, "submission")
    root = (
        get_subnet_rewards_root(protocol_data_root)
        / SUBNET_REWARD_EVENTS_DIRECTORY
        / SUBNET_REWARD_SUBMISSION_EVENTS_DIRECTORY
    )
    return _safe_child(root, submission_id)


def get_membership_reward_multiplier(
    membership_status: SubnetMemberStatus,
) -> Decimal | None:
    membership_status = SubnetMemberStatus(membership_status)
    return {
        SubnetMemberStatus.CANDIDATE: None,
        SubnetMemberStatus.PROBATION: Decimal("0.90"),
        SubnetMemberStatus.ACTIVE: Decimal("1.00"),
        SubnetMemberStatus.EXPERT: Decimal("1.10"),
        SubnetMemberStatus.SUSPENDED: None,
        SubnetMemberStatus.REMOVED: None,
    }[membership_status]


def get_category_reward_multiplier(category_score: Decimal) -> Decimal:
    score = _decimal(category_score)
    if not score.is_finite() or score < 0 or score > 1:
        raise ValueError("Category score must be between 0 and 1")
    if score < Decimal("0.40"):
        return Decimal("0.80")
    if score < Decimal("0.60"):
        return Decimal("0.95")
    if score < Decimal("0.80"):
        return Decimal("1.05")
    return Decimal("1.15")


def allocate_category_pools(
    total_pool_points: Decimal,
    categories: list[str | FindingCategory],
    category_weights: dict[str | FindingCategory, Decimal] | None,
) -> dict[str, Decimal]:
    pool = _points(total_pool_points)
    normalized_categories = sorted(
        {normalize_category(item) for item in categories}
    )
    if not normalized_categories:
        raise SubnetRewardInvalidCategoryWeightsError(
            "At least one routed category is required"
        )
    if len(normalized_categories) != len(categories):
        raise SubnetRewardInvalidCategoryWeightsError(
            "Routed categories must be unique"
        )
    if category_weights is None:
        weights = {
            category: Decimal("1") for category in normalized_categories
        }
    else:
        weights: dict[str, Decimal] = {}
        for category, value in category_weights.items():
            normalized = normalize_category(category)
            if normalized in weights:
                raise SubnetRewardInvalidCategoryWeightsError(
                    "Category weights must be unique"
                )
            decimal_value = _decimal(value)
            if decimal_value <= 0:
                raise SubnetRewardInvalidCategoryWeightsError(
                    "Category weights must be positive"
                )
            weights[normalized] = decimal_value
        if set(weights) != set(normalized_categories):
            raise SubnetRewardInvalidCategoryWeightsError(
                "Category weights must exactly match routed categories"
            )
    total_weight = sum(weights.values(), Decimal("0"))
    ideals = {
        category: pool * weights[category] / total_weight
        for category in normalized_categories
    }
    allocations = {
        category: ideal.quantize(PROTOCOL_POINTS_QUANTUM, rounding=ROUND_DOWN)
        for category, ideal in ideals.items()
    }
    residual_units = int(
        (pool - sum(allocations.values(), Decimal("0")))
        / PROTOCOL_POINTS_QUANTUM
    )
    remainder_order = sorted(
        normalized_categories,
        key=lambda category: (
            -(ideals[category] - allocations[category]),
            category,
        ),
    )
    for category in remainder_order[:residual_units]:
        allocations[category] += PROTOCOL_POINTS_QUANTUM
    if sum(allocations.values(), Decimal("0")) != pool:
        raise SubnetRewardConservationError(
            "Category pool allocation failed to conserve protocol points"
        )
    return allocations


def allocate_proportional_rewards(
    pool_points: Decimal,
    weighted_items: list[WeightedRewardItem],
) -> dict[str, Decimal]:
    pool = _points(pool_points)
    ordered = sorted(weighted_items, key=lambda item: item.allocation_id)
    if not ordered:
        return {}
    if len({item.allocation_id for item in ordered}) != len(ordered):
        raise SubnetRewardConservationError(
            "Weighted allocation identifiers must be unique"
        )
    if any(item.raw_weight <= 0 for item in ordered):
        raise SubnetRewardConservationError(
            "Weighted allocation values must be positive"
        )
    total_weight = sum((item.raw_weight for item in ordered), Decimal("0"))
    ideals = {
        item.allocation_id: pool * item.raw_weight / total_weight
        for item in ordered
    }
    rewards = {
        item.allocation_id: ideals[item.allocation_id].quantize(
            PROTOCOL_POINTS_QUANTUM,
            rounding=ROUND_DOWN,
        )
        for item in ordered
    }
    residual_units = int(
        (pool - sum(rewards.values(), Decimal("0")))
        / PROTOCOL_POINTS_QUANTUM
    )
    remainder_order = sorted(
        ordered,
        key=lambda item: (
            -(ideals[item.allocation_id] - rewards[item.allocation_id]),
            -item.raw_weight,
            -item.contribution_score,
            item.node_id,
            item.submission_id,
        ),
    )
    for item in remainder_order[:residual_units]:
        rewards[item.allocation_id] += PROTOCOL_POINTS_QUANTUM
    if sum(rewards.values(), Decimal("0")) != pool:
        raise SubnetRewardConservationError(
            "Proportional allocation failed to conserve category points"
        )
    return rewards


def collect_routed_submissions(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_record: ProjectRoutingRecord,
) -> list[SubmissionRecord]:
    _verify_project_workspace(project_workspace, project_id)
    if routing_record.project_id != project_id:
        raise SubnetRewardSourceMismatchError(
            "Routing record does not belong to the reward project"
        )
    assignment_ids = {
        assignment.assignment_id
        for result in routing_record.results
        for assignment in result.assignments
    }
    submissions = [
        submission
        for submission in list_submissions(
            protocol_data_root,
            project_id=project_id,
        )
        if (
            submission.routing_id == routing_record.routing_id
            and submission.routing_assignment_id in assignment_ids
        )
    ]
    return sorted(
        submissions,
        key=lambda item: (item.category, item.node_id, item.submission_id),
    )


def evaluate_subnet_reward_eligibility(
    source_bundle: SubnetRewardSourceBundle,
) -> SubnetRewardEligibilityResult:
    routing = source_bundle.routing_record
    assignment = source_bundle.routing_assignment
    submission = source_bundle.submission
    contribution = source_bundle.contribution_score_record
    validation = source_bundle.validation_decision
    reproduction = source_bundle.reproduction_result
    reasons: list[SubnetRewardExclusionReason] = []
    explanations: list[str] = []

    def exclude(reason: SubnetRewardExclusionReason, explanation: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
            explanations.append(explanation)

    if routing.status != RoutingStatus.FINALIZED:
        exclude(
            SubnetRewardExclusionReason.ROUTING_NOT_FINALIZED,
            "The routing record is not finalized.",
        )
    if assignment is None:
        exclude(
            SubnetRewardExclusionReason.ROUTING_ASSIGNMENT_MISSING,
            "The referenced routing assignment does not exist.",
        )
    if submission.routing_id is None or submission.routing_assignment_id is None:
        exclude(
            SubnetRewardExclusionReason.SUBMISSION_NOT_LINKED_TO_ROUTING,
            "The submission is not linked to a routing assignment.",
        )
    elif submission.routing_id != routing.routing_id:
        exclude(
            SubnetRewardExclusionReason.SUBMISSION_NOT_LINKED_TO_ROUTING,
            "The submission links to a different routing record.",
        )
    if submission.project_id != routing.project_id:
        exclude(
            SubnetRewardExclusionReason.SUBMISSION_PROJECT_MISMATCH,
            "The submission project does not match the routing project.",
        )
    if assignment is not None:
        if submission.routing_assignment_id != assignment.assignment_id:
            exclude(
                SubnetRewardExclusionReason.ROUTING_ASSIGNMENT_MISSING,
                "The submission links to a different routing assignment.",
            )
        if submission.project_id != assignment.project_id:
            exclude(
                SubnetRewardExclusionReason.SUBMISSION_PROJECT_MISMATCH,
                "The submission project does not match the assignment.",
            )
        if submission.category != assignment.category.value:
            exclude(
                SubnetRewardExclusionReason.SUBMISSION_CATEGORY_MISMATCH,
                "The submission category does not match the assignment.",
            )
        if submission.node_id != assignment.node_id:
            exclude(
                SubnetRewardExclusionReason.SUBMISSION_NODE_MISMATCH,
                "The submission node does not match the assignment.",
            )
        if assignment.candidate_snapshot.category_score_id is None:
            exclude(
                SubnetRewardExclusionReason.CATEGORY_SCORE_MISSING,
                "The routing assignment has no category-score snapshot.",
            )
        if assignment.selection_type == RoutingSelectionType.EXPLORATION:
            if assignment.membership_status == SubnetMemberStatus.CANDIDATE:
                exclude(
                    SubnetRewardExclusionReason.CANDIDATE_SHADOW_INELIGIBLE,
                    "Candidate shadow assignments are ineligible in Subnet Reward v0.",
                )
            elif (
                assignment.membership_status != SubnetMemberStatus.PROBATION
                or assignment.assignment_mode != RoutingAssignmentMode.SHADOW
            ):
                exclude(
                    SubnetRewardExclusionReason.MEMBERSHIP_SNAPSHOT_INELIGIBLE,
                    "The exploration assignment snapshot is not probation shadow.",
                )
        elif (
            assignment.assignment_mode != RoutingAssignmentMode.PRODUCTION
            or assignment.membership_status
            not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT}
        ):
            exclude(
                SubnetRewardExclusionReason.MEMBERSHIP_SNAPSHOT_INELIGIBLE,
                "The ranked assignment snapshot is not active/expert production.",
            )

    finalized_submission_statuses = {
        SubmissionStatus.ACCEPTED,
        SubmissionStatus.REWARD_PENDING,
    }
    submission_reason_map = {
        SubmissionStatus.DUPLICATE: (
            SubnetRewardExclusionReason.FINDING_DUPLICATE,
            "The submission outcome is duplicate.",
        ),
        SubmissionStatus.OUT_OF_SCOPE: (
            SubnetRewardExclusionReason.FINDING_OUT_OF_SCOPE,
            "The submission outcome is out of scope.",
        ),
        SubmissionStatus.INSUFFICIENT_EVIDENCE: (
            SubnetRewardExclusionReason.INSUFFICIENT_EVIDENCE,
            "The submission outcome has insufficient evidence.",
        ),
        SubmissionStatus.REJECTED: (
            SubnetRewardExclusionReason.FINDING_REJECTED,
            "The submission outcome is rejected.",
        ),
        SubmissionStatus.UNSAFE: (
            SubnetRewardExclusionReason.UNSAFE_SUBMISSION,
            "The submission outcome is unsafe.",
        ),
        SubmissionStatus.PENALIZED: (
            SubnetRewardExclusionReason.UNSAFE_SUBMISSION,
            "The penalized submission is not reward eligible.",
        ),
        SubmissionStatus.UNSUPPORTED: (
            SubnetRewardExclusionReason.UNSUPPORTED_SUBMISSION,
            "The submission outcome is unsupported.",
        ),
    }
    if submission.status in submission_reason_map:
        exclude(*submission_reason_map[submission.status])
    if submission.status not in finalized_submission_statuses:
        if (
            submission.status == SubmissionStatus.REWARDED
            or submission.reward_status == SubmissionRewardStatus.REWARDED
        ):
            exclude(
                SubnetRewardExclusionReason.ALREADY_REWARDED,
                "The submission is already marked rewarded.",
            )
        else:
            exclude(
                SubnetRewardExclusionReason.SUBMISSION_NOT_FINALIZED,
                "The submission does not have an accepted finalized outcome.",
            )

    validation_status = validation.status if validation is not None else None
    validation_reason_map = {
        ValidationStatus.DUPLICATE: (
            SubnetRewardExclusionReason.FINDING_DUPLICATE,
            "Validation marked the finding duplicate.",
        ),
        ValidationStatus.OUT_OF_SCOPE: (
            SubnetRewardExclusionReason.FINDING_OUT_OF_SCOPE,
            "Validation marked the finding out of scope.",
        ),
        ValidationStatus.INSUFFICIENT_EVIDENCE: (
            SubnetRewardExclusionReason.INSUFFICIENT_EVIDENCE,
            "Validation found insufficient evidence.",
        ),
        ValidationStatus.REJECTED: (
            SubnetRewardExclusionReason.FINDING_REJECTED,
            "Validation rejected the finding.",
        ),
        ValidationStatus.UNSAFE_POC: (
            SubnetRewardExclusionReason.UNSAFE_SUBMISSION,
            "Validation marked the submission unsafe.",
        ),
        ValidationStatus.UNSUPPORTED: (
            SubnetRewardExclusionReason.UNSUPPORTED_SUBMISSION,
            "Validation marked the submission unsupported.",
        ),
    }
    if validation_status != ValidationStatus.ACCEPTED:
        if validation_status in validation_reason_map:
            exclude(*validation_reason_map[validation_status])
        else:
            exclude(
                SubnetRewardExclusionReason.VALIDATION_NOT_ACCEPTED,
                "An accepted validation decision is required.",
            )
    if validation is not None:
        if validation.evidence.is_duplicate is True:
            exclude(
                SubnetRewardExclusionReason.FINDING_DUPLICATE,
                "Validation evidence identifies a duplicate finding.",
            )
        if validation.evidence.in_scope is False:
            exclude(
                SubnetRewardExclusionReason.FINDING_OUT_OF_SCOPE,
                "Validation evidence identifies an out-of-scope finding.",
            )
        if (
            validation.status == ValidationStatus.ACCEPTED
            and (
                validation.evidence.in_scope is not True
                or validation.evidence.is_duplicate is not False
            )
        ):
            exclude(
                SubnetRewardExclusionReason.VALIDATION_NOT_ACCEPTED,
                "Accepted validation requires affirmative in-scope and unique evidence.",
            )

    if (
        reproduction is None
        or reproduction.status != ReproductionStatus.REPRODUCED
    ):
        if (
            reproduction is not None
            and reproduction.status == ReproductionStatus.REJECTED_UNSAFE
        ):
            exclude(
                SubnetRewardExclusionReason.UNSAFE_SUBMISSION,
                "Safety preflight rejected the reproduction as unsafe.",
            )
        if (
            reproduction is not None
            and reproduction.status == ReproductionStatus.UNSUPPORTED
        ):
            exclude(
                SubnetRewardExclusionReason.UNSUPPORTED_SUBMISSION,
                "The reproduction result is unsupported.",
            )
        exclude(
            SubnetRewardExclusionReason.REPRODUCTION_NOT_REPRODUCED,
            "A successfully reproduced result is required.",
        )

    if contribution is None:
        exclude(
            SubnetRewardExclusionReason.CONTRIBUTION_RECORD_MISSING,
            "ContributionScoreRecord is missing.",
        )
    else:
        if (
            contribution.eligibility_status
            != ContributionEligibilityStatus.ELIGIBLE
            or not contribution.eligible_for_reward
        ):
            exclude(
                SubnetRewardExclusionReason.CONTRIBUTION_NOT_ELIGIBLE,
                "The contribution record is not reward eligible.",
            )
        if contribution.total_score <= 0:
            exclude(
                SubnetRewardExclusionReason.CONTRIBUTION_SCORE_ZERO,
                "The contribution score must be greater than zero.",
            )

    if (
        source_bundle.current_node_record is None
        or source_bundle.current_node_record.status != NodeStatus.ACTIVE
    ):
        exclude(
            SubnetRewardExclusionReason.NODE_NOT_ACTIVE,
            "The node is not currently active.",
        )

    finalized_week_5 = [
        event
        for event in source_bundle.existing_week_5_reward_events
        if event.event_status == RewardEventStatus.FINALIZED
    ]
    external_subnet_events = [
        event
        for event in source_bundle.existing_subnet_reward_events
        if (
            event.status == SubnetRewardEventStatus.APPLIED
            and event.reward_cycle_id != source_bundle.reward_cycle_id
        )
    ]
    if finalized_week_5 or external_subnet_events:
        exclude(
            SubnetRewardExclusionReason.ALREADY_REWARDED,
            "A finalized contribution reward already exists for the submission.",
        )

    if reasons:
        return SubnetRewardEligibilityResult(
            status=SubnetRewardEligibilityStatus.INELIGIBLE,
            reasons=reasons,
            explanations=explanations,
        )
    return SubnetRewardEligibilityResult(
        status=SubnetRewardEligibilityStatus.ELIGIBLE,
        reasons=[],
        explanations=[
            "The routed submission is accepted, reproduced, unique, and reward eligible."
        ],
    )


def calculate_category_reward_allocation(
    reward_cycle_id: str,
    project_id: str,
    routing_record: ProjectRoutingRecord,
    category_result: CategoryRoutingResult,
    allocated_pool_points: Decimal,
    source_bundles: list[SubnetRewardSourceBundle],
    calculated_at: datetime,
    *,
    requested_weight: Decimal = Decimal("1"),
    normalized_weight: Decimal = Decimal("1"),
) -> CategoryPoolAllocation:
    category = category_result.category
    subnet_id = category_result.subnet_id or f"subnet_{category.value}"
    allocations: list[SubnetSubmissionRewardAllocation] = []
    exclusions: list[SubnetRewardExclusion] = []
    weighted_items: list[WeightedRewardItem] = []
    allocation_inputs: list[tuple[SubnetRewardSourceBundle, Decimal, Decimal, Decimal, str]] = []

    for bundle in sorted(
        source_bundles,
        key=lambda item: (
            item.submission.node_id,
            item.submission.submission_id,
        ),
    ):
        _verify_structural_source_consistency(bundle)
        eligibility = evaluate_subnet_reward_eligibility(bundle)
        assignment = bundle.routing_assignment
        if eligibility.status == SubnetRewardEligibilityStatus.INELIGIBLE:
            exclusions.append(
                SubnetRewardExclusion(
                    submission_id=bundle.submission.submission_id,
                    assignment_id=(
                        assignment.assignment_id if assignment is not None else None
                    ),
                    node_id=bundle.submission.node_id,
                    category=category,
                    reasons=eligibility.reasons,
                    explanation=eligibility.explanations,
                )
            )
            continue
        if assignment is None or bundle.contribution_score_record is None:
            raise SubnetRewardSourceMismatchError(
                "Eligible reward input is missing assignment or contribution"
            )
        contribution_score = _decimal(
            bundle.contribution_score_record.total_score
        )
        category_score = _decimal(assignment.category_score)
        category_multiplier = get_category_reward_multiplier(category_score)
        membership_multiplier = get_membership_reward_multiplier(
            assignment.membership_status
        )
        if membership_multiplier is None:
            raise SubnetRewardSourceMismatchError(
                "Eligible assignment has no membership multiplier"
            )
        raw_weight = _points(
            contribution_score
            * category_multiplier
            * membership_multiplier
        )
        allocation_id = _allocation_id(
            reward_cycle_id,
            bundle.submission.submission_id,
        )
        weighted_items.append(
            WeightedRewardItem(
                allocation_id=allocation_id,
                node_id=bundle.submission.node_id,
                submission_id=bundle.submission.submission_id,
                raw_weight=raw_weight,
                contribution_score=contribution_score,
            )
        )
        allocation_inputs.append(
            (
                bundle,
                category_score,
                category_multiplier,
                membership_multiplier,
                allocation_id,
            )
        )

    rewards = allocate_proportional_rewards(
        allocated_pool_points,
        weighted_items,
    )
    total_weight = sum(
        (item.raw_weight for item in weighted_items),
        Decimal("0"),
    )
    item_by_id = {item.allocation_id: item for item in weighted_items}
    for (
        bundle,
        category_score,
        category_multiplier,
        membership_multiplier,
        allocation_id,
    ) in allocation_inputs:
        assignment = bundle.routing_assignment
        contribution = bundle.contribution_score_record
        assert assignment is not None and contribution is not None
        item = item_by_id[allocation_id]
        snapshot = _build_source_snapshot(bundle)
        allocation_source_fingerprint = _fingerprint(
            {
                "reward_cycle_id": reward_cycle_id,
                "project_id": project_id,
                "subnet_id": subnet_id,
                "category": category.value,
                "allocation_id": allocation_id,
                "source_snapshot": snapshot,
                "raw_weight": item.raw_weight,
            }
        )
        normalized_share = (
            item.raw_weight / total_weight
        ).quantize(NORMALIZED_SHARE_QUANTUM, rounding=ROUND_HALF_UP)
        allocations.append(
            SubnetSubmissionRewardAllocation(
                allocation_id=allocation_id,
                reward_cycle_id=reward_cycle_id,
                project_id=project_id,
                routing_id=routing_record.routing_id,
                routing_assignment_id=assignment.assignment_id,
                submission_id=bundle.submission.submission_id,
                finding_id=bundle.submission.finding_id,
                node_id=bundle.submission.node_id,
                subnet_id=subnet_id,
                category=category,
                selection_type=assignment.selection_type,
                assignment_mode=assignment.assignment_mode,
                membership_status=assignment.membership_status,
                contribution_score=_decimal(contribution.total_score),
                category_score=category_score,
                category_multiplier=category_multiplier,
                membership_multiplier=membership_multiplier,
                raw_weight=item.raw_weight,
                normalized_share=normalized_share,
                reward_points=rewards[allocation_id],
                eligibility_status=SubnetRewardEligibilityStatus.ELIGIBLE,
                eligibility_reasons=[
                    "Eligible routed contribution received proportional protocol points."
                ],
                source_snapshot=snapshot,
                source_fingerprint=allocation_source_fingerprint,
                created_at=calculated_at,
            )
        )
    allocations.sort(key=lambda item: item.allocation_id)
    pool = _points(allocated_pool_points)
    distributed = (
        pool if allocations and total_weight > 0 else Decimal("0.000000")
    )
    undistributed = _points(pool - distributed)
    warnings = (
        ["No eligible contribution existed; the category pool remains undistributed."]
        if not allocations
        else []
    )
    return CategoryPoolAllocation(
        category=category,
        subnet_id=subnet_id,
        requested_weight=requested_weight,
        normalized_weight=normalized_weight,
        allocated_pool_points=pool,
        eligible_submissions=len(allocations),
        ineligible_submissions=len(exclusions),
        total_raw_weight=_points(total_weight),
        distributed_points=distributed,
        undistributed_points=undistributed,
        allocations=allocations,
        exclusions=exclusions,
        warnings=warnings,
    )


def build_subnet_reward_request_fingerprint(
    project_id: str,
    routing_id: str,
    total_pool_points: Decimal,
    category_weights: dict[str | FindingCategory, Decimal],
    reward_version: str = SUBNET_REWARD_VERSION,
    policy_version: str = SUBNET_REWARD_POLICY_VERSION,
) -> str:
    weights = {
        normalize_category(category): _decimal(value)
        for category, value in category_weights.items()
    }
    return _fingerprint(
        {
            "project_id": project_id,
            "routing_id": routing_id,
            "total_pool_points": _points(total_pool_points),
            "category_weights": {
                category: weights[category] for category in sorted(weights)
            },
            "reward_version": reward_version,
            "policy_version": policy_version,
        }
    )


def build_subnet_reward_source_payload(
    cycle: SubnetRewardCycle,
    routing_record: ProjectRoutingRecord,
    category_source_bundles: list[SubnetRewardSourceBundle],
    existing_reward_events: list[Any],
) -> dict[str, Any]:
    assignment_inputs = []
    for result in routing_record.results:
        for assignment in result.assignments:
            assignment_inputs.append(
                {
                    "assignment_id": assignment.assignment_id,
                    "node_id": assignment.node_id,
                    "category": assignment.category.value,
                    "subnet_id": assignment.subnet_id,
                    "selection_type": assignment.selection_type.value,
                    "assignment_mode": assignment.assignment_mode.value,
                    "membership_status": assignment.membership_status.value,
                    "category_score": _decimal(assignment.category_score),
                    "assignment_source_fingerprint": _assignment_fingerprint(
                        assignment
                    ),
                }
            )
    submission_inputs = [
        _bundle_source_input(bundle)
        for bundle in sorted(
            category_source_bundles,
            key=lambda item: item.submission.submission_id,
        )
    ]
    external_events = sorted(
        (
            _existing_event_source_input(event)
            for event in existing_reward_events
            if not (
                isinstance(event, SubnetRewardEvent)
                and event.reward_cycle_id == cycle.reward_cycle_id
            )
        ),
        key=lambda item: item["event_id"],
    )
    return {
        "reward_policy": {
            "reward_version": SUBNET_REWARD_VERSION,
            "policy_version": SUBNET_REWARD_POLICY_VERSION,
            "category_multiplier_policy_version": (
                CATEGORY_MULTIPLIER_POLICY_VERSION
            ),
            "membership_multiplier_policy_version": (
                MEMBERSHIP_MULTIPLIER_POLICY_VERSION
            ),
            "protocol_points_quantum": PROTOCOL_POINTS_QUANTUM,
        },
        "cycle_input": {
            "project_id": cycle.project_id,
            "routing_id": cycle.routing_id,
            "total_pool_points": cycle.total_pool_points,
            "category_weights": {
                pool.category.value: pool.normalized_weight
                for pool in cycle.category_pools
            },
            "category_pools": {
                pool.category.value: pool.allocated_pool_points
                for pool in cycle.category_pools
            },
        },
        "routing": {
            "routing_id": routing_record.routing_id,
            "status": routing_record.status.value,
            "source_fingerprint": routing_record.source_fingerprint,
            "finalized_at": routing_record.finalized_at,
            "assignments": sorted(
                assignment_inputs,
                key=lambda item: item["assignment_id"],
            ),
        },
        "submissions": submission_inputs,
        "existing_rewards": external_events,
    }


def compute_subnet_reward_source_fingerprint(payload: dict[str, Any]) -> str:
    return _fingerprint(payload)


def create_subnet_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    request: SubnetRewardCycleCreateRequest,
) -> SubnetRewardCycle:
    _verify_project_workspace(project_workspace, project_id)
    routing = load_routing_record(
        protocol_data_root,
        project_id,
        request.routing_id,
    )
    if routing is None:
        raise SubnetRewardRoutingNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise SubnetRewardSourceMismatchError(
            "Routing does not belong to the requested project"
        )
    if routing.status != RoutingStatus.FINALIZED:
        raise SubnetRewardRoutingNotFinalizedError(
            "Subnet rewards require finalized routing"
        )
    categories = [result.category.value for result in routing.results]
    supplied_weights = (
        None
        if request.category_weights is None
        else {
            category.value: value
            for category, value in request.category_weights.items()
        }
    )
    category_pools = allocate_category_pools(
        request.total_pool_points,
        categories,
        supplied_weights,
    )
    requested_weights = (
        {category: Decimal("1") for category in sorted(categories)}
        if supplied_weights is None
        else supplied_weights
    )
    request_fingerprint = build_subnet_reward_request_fingerprint(
        project_id,
        routing.routing_id,
        request.total_pool_points,
        requested_weights,
    )
    existing = find_subnet_reward_cycle_by_routing(
        protocol_data_root,
        routing.routing_id,
    )
    if existing is not None:
        if existing.request_fingerprint == request_fingerprint:
            return existing
        raise SubnetRewardCycleConflictError(
            "A differently configured subnet reward cycle already exists for routing"
        )

    total_weight = sum(requested_weights.values(), Decimal("0"))
    draft_pools = []
    result_by_category = {
        result.category.value: result for result in routing.results
    }
    for category in sorted(categories):
        result = result_by_category[category]
        allocated = category_pools[category]
        draft_pools.append(
            CategoryPoolAllocation(
                category=category,
                subnet_id=result.subnet_id or f"subnet_{category}",
                requested_weight=requested_weights[category],
                normalized_weight=requested_weights[category] / total_weight,
                allocated_pool_points=allocated,
                eligible_submissions=0,
                ineligible_submissions=0,
                total_raw_weight=Decimal("0"),
                distributed_points=Decimal("0.000000"),
                undistributed_points=allocated,
                allocations=[],
                exclusions=[],
                warnings=["Reward allocations have not been calculated."],
            )
        )
    now = _utc_now()
    cycle = SubnetRewardCycle(
        reward_cycle_id=str(uuid.uuid4()),
        reward_version=SUBNET_REWARD_VERSION,
        policy_version=SUBNET_REWARD_POLICY_VERSION,
        project_id=project_id,
        routing_id=routing.routing_id,
        routing_source_fingerprint=routing.source_fingerprint,
        status=SubnetRewardCycleStatus.DRAFT,
        total_pool_points=_points(request.total_pool_points),
        total_distributed_points=Decimal("0.000000"),
        total_undistributed_points=_points(request.total_pool_points),
        category_weights={
            FindingCategory(category): requested_weights[category]
            for category in sorted(categories)
        },
        category_pools=draft_pools,
        node_summaries=[],
        request_fingerprint=request_fingerprint,
        source_fingerprint=None,
        description=request.description,
        calculated_at=None,
        finalized_at=None,
        created_at=now,
        updated_at=now,
    )
    return save_subnet_reward_cycle(protocol_data_root, cycle)


def calculate_subnet_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    reward_cycle_id: str,
) -> SubnetRewardCalculationResponse:
    cycle = _load_required_cycle(
        protocol_data_root,
        project_id,
        reward_cycle_id,
    )
    if cycle.status == SubnetRewardCycleStatus.FINALIZED:
        raise SubnetRewardAlreadyFinalizedError(
            "Finalized subnet reward cycles cannot be recalculated"
        )
    calculated = _calculate_cycle_snapshot(
        protocol_data_root,
        project_workspace,
        cycle,
    )
    eligible = sum(
        len(pool.allocations) for pool in calculated.category_pools
    )
    exclusions = sum(
        len(pool.exclusions) for pool in calculated.category_pools
    )
    if (
        cycle.status == SubnetRewardCycleStatus.CALCULATED
        and cycle.source_fingerprint == calculated.source_fingerprint
    ):
        return SubnetRewardCalculationResponse(
            status=SubnetRewardProcessingStatus.UNCHANGED,
            cycle=cycle,
            eligible_allocations=eligible,
            exclusions=exclusions,
            message="Reward sources are unchanged; the calculated cycle was reused.",
        )
    saved = save_subnet_reward_cycle(protocol_data_root, calculated)
    return SubnetRewardCalculationResponse(
        status=SubnetRewardProcessingStatus.CALCULATED,
        cycle=saved,
        eligible_allocations=eligible,
        exclusions=exclusions,
        message="Subnet reward allocations were calculated.",
    )


def finalize_subnet_reward_cycle(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    reward_cycle_id: str,
) -> SubnetRewardFinalizationResponse:
    cycle = _load_required_cycle(
        protocol_data_root,
        project_id,
        reward_cycle_id,
    )
    if cycle.status == SubnetRewardCycleStatus.FINALIZED:
        event_count = sum(
            len(pool.allocations) for pool in cycle.category_pools
        )
        return SubnetRewardFinalizationResponse(
            status=SubnetRewardProcessingStatus.ALREADY_FINALIZED,
            cycle=cycle,
            reward_events_created=0,
            reward_events_existing=event_count,
            message="Reward cycle was already finalized; no events were duplicated.",
        )
    if cycle.status != SubnetRewardCycleStatus.CALCULATED:
        raise SubnetRewardCycleConflictError(
            "Reward cycle must be calculated before finalization"
        )
    current = _calculate_cycle_snapshot(
        protocol_data_root,
        project_workspace,
        cycle,
    )
    if current.source_fingerprint != cycle.source_fingerprint:
        raise SubnetRewardSourceChangedError(
            "Subnet reward sources changed after calculation"
        )
    if _immutable_calculation_payload(current) != _immutable_calculation_payload(
        cycle
    ):
        raise SubnetRewardSourceChangedError(
            "Subnet reward allocations changed after calculation"
        )

    created = 0
    existing = 0
    now = _utc_now()
    for allocation in (
        allocation
        for pool in cycle.category_pools
        for allocation in pool.allocations
    ):
        other_events = [
            event
            for event in list_subnet_reward_events(
                protocol_data_root,
                submission_id=allocation.submission_id,
            )
            if event.reward_cycle_id != cycle.reward_cycle_id
        ]
        if other_events:
            raise SubnetRewardAlreadyPaidError(
                "Submission was already rewarded in another subnet reward cycle"
            )
        week_5 = load_reward_event_by_submission(
            protocol_data_root,
            allocation.submission_id,
        )
        if week_5 is not None and week_5.event_status == RewardEventStatus.FINALIZED:
            raise SubnetRewardAlreadyPaidError(
                "Submission was already rewarded by the Week 5 reward simulator"
            )
        event = _build_reward_event(cycle, allocation, now)
        _, was_created = _save_reward_event_with_status(
            protocol_data_root,
            event,
        )
        if was_created:
            created += 1
        else:
            existing += 1

    finalized_at = _utc_now()
    finalized = SubnetRewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": SubnetRewardCycleStatus.FINALIZED,
            "finalized_at": finalized_at,
            "updated_at": finalized_at,
        }
    )
    saved = save_subnet_reward_cycle(protocol_data_root, finalized)
    return SubnetRewardFinalizationResponse(
        status=SubnetRewardProcessingStatus.FINALIZED,
        cycle=saved,
        reward_events_created=created,
        reward_events_existing=existing,
        message="Subnet reward cycle finalized and immutable events applied.",
    )


def save_subnet_reward_cycle(
    protocol_data_root: Path,
    cycle: SubnetRewardCycle,
) -> SubnetRewardCycle:
    validated = SubnetRewardCycle.model_validate(cycle.model_dump())
    existing = load_subnet_reward_cycle(
        protocol_data_root,
        validated.project_id,
        validated.reward_cycle_id,
    )
    if existing is not None:
        if existing.status == SubnetRewardCycleStatus.FINALIZED:
            if existing == validated:
                return existing
            raise SubnetRewardAlreadyFinalizedError(
                "Finalized subnet reward cycles are immutable"
            )
        if _immutable_request_payload(existing) != _immutable_request_payload(
            validated
        ):
            raise SubnetRewardCycleConflictError(
                "Reward-cycle request inputs cannot be changed"
            )
        values = validated.model_dump()
        values["created_at"] = existing.created_at
        validated = SubnetRewardCycle.model_validate(values)
    path = get_subnet_reward_cycle_path(
        protocol_data_root,
        validated.project_id,
        validated.reward_cycle_id,
    )
    _write_model_atomic(path, validated)
    return validated


def load_subnet_reward_cycle(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
) -> SubnetRewardCycle | None:
    path = get_subnet_reward_cycle_path(
        protocol_data_root,
        project_id,
        reward_cycle_id,
    )
    if not path.exists():
        return None
    if not path.is_file():
        raise SubnetRewardStorageError(
            "Stored subnet reward cycle is not a regular file"
        )
    return _load_model(path, SubnetRewardCycle, "Stored reward cycle is malformed")


def find_subnet_reward_cycle_by_routing(
    protocol_data_root: Path,
    routing_id: str,
) -> SubnetRewardCycle | None:
    _validate_identifier(routing_id, "routing")
    projects_root = (
        get_subnet_rewards_root(protocol_data_root)
        / SUBNET_REWARD_PROJECTS_DIRECTORY
    )
    if not projects_root.exists():
        return None
    matches = [
        cycle
        for path in sorted(
            projects_root.glob(f"*/*/{SUBNET_REWARD_CYCLE_FILENAME}")
        )
        for cycle in [
            load_subnet_reward_cycle(
                protocol_data_root,
                path.parents[1].name,
                path.parent.name,
            )
        ]
        if cycle is not None and cycle.routing_id == routing_id
    ]
    if len(matches) > 1:
        raise SubnetRewardStorageError(
            "More than one subnet reward cycle exists for routing"
        )
    return matches[0] if matches else None


def list_project_subnet_reward_cycles(
    protocol_data_root: Path,
    project_id: str,
    status: SubnetRewardCycleStatus | None = None,
) -> list[SubnetRewardCycle]:
    root = get_project_subnet_rewards_root(protocol_data_root, project_id)
    if not root.exists():
        return []
    cycles = []
    for path in sorted(root.glob(f"*/{SUBNET_REWARD_CYCLE_FILENAME}")):
        cycle = load_subnet_reward_cycle(
            protocol_data_root,
            project_id,
            path.parent.name,
        )
        if cycle is None or (status is not None and cycle.status != status):
            continue
        cycles.append(cycle)
    return sorted(
        cycles,
        key=lambda item: (-item.created_at.timestamp(), item.reward_cycle_id),
    )


def save_subnet_reward_event_exclusively(
    protocol_data_root: Path,
    event: SubnetRewardEvent,
) -> SubnetRewardEvent:
    stored, _ = _save_reward_event_with_status(protocol_data_root, event)
    return stored


def list_subnet_reward_events(
    protocol_data_root: Path,
    node_id: str | None = None,
    subnet_id: str | None = None,
    category: str | FindingCategory | None = None,
    project_id: str | None = None,
    submission_id: str | None = None,
) -> list[SubnetRewardEvent]:
    for value, label in (
        (node_id, "node"),
        (subnet_id, "subnet"),
        (project_id, "project"),
        (submission_id, "submission"),
    ):
        if value is not None:
            _validate_identifier(value, label)
    normalized_category = (
        normalize_category(category) if category is not None else None
    )
    root = (
        get_subnet_rewards_root(protocol_data_root)
        / SUBNET_REWARD_EVENTS_DIRECTORY
        / SUBNET_REWARD_SUBMISSION_EVENTS_DIRECTORY
    )
    if not root.exists():
        return []
    paths = (
        sorted((root / submission_id).glob("*.json"))
        if submission_id is not None
        else sorted(root.glob("*/*.json"))
    )
    events = []
    for path in paths:
        event = _load_model(
            path,
            SubnetRewardEvent,
            "Stored subnet reward event is malformed",
        )
        if node_id is not None and event.node_id != node_id:
            continue
        if subnet_id is not None and event.subnet_id != subnet_id:
            continue
        if project_id is not None and event.project_id != project_id:
            continue
        if (
            normalized_category is not None
            and event.category.value != normalized_category
        ):
            continue
        if submission_id is not None and event.submission_id != submission_id:
            continue
        events.append(event)
    return sorted(
        events,
        key=lambda item: (
            item.applied_at,
            item.node_id,
            item.submission_id,
            item.reward_event_id,
        ),
    )


def build_node_subnet_reward_summaries(
    cycle: SubnetRewardCycle,
) -> list[NodeSubnetRewardSummary]:
    grouped: dict[str, list[SubnetSubmissionRewardAllocation]] = {}
    for allocation in (
        allocation
        for pool in cycle.category_pools
        for allocation in pool.allocations
    ):
        grouped.setdefault(allocation.node_id, []).append(allocation)
    summaries = []
    for node_id, allocations in grouped.items():
        production = _points(
            sum(
                (
                    item.reward_points
                    for item in allocations
                    if item.assignment_mode == RoutingAssignmentMode.PRODUCTION
                ),
                Decimal("0"),
            )
        )
        shadow = _points(
            sum(
                (
                    item.reward_points
                    for item in allocations
                    if item.assignment_mode == RoutingAssignmentMode.SHADOW
                ),
                Decimal("0"),
            )
        )
        summaries.append(
            NodeSubnetRewardSummary(
                node_id=node_id,
                project_id=cycle.project_id,
                reward_cycle_id=cycle.reward_cycle_id,
                total_reward_points=_points(production + shadow),
                production_reward_points=production,
                shadow_reward_points=shadow,
                rewarded_submissions=len(allocations),
                categories=sorted(
                    {item.category for item in allocations},
                    key=lambda item: item.value,
                ),
            )
        )
    return sorted(
        summaries,
        key=lambda item: (-item.total_reward_points, item.node_id),
    )


def _calculate_cycle_snapshot(
    protocol_data_root: Path,
    project_workspace: Path,
    cycle: SubnetRewardCycle,
) -> SubnetRewardCycle:
    _verify_project_workspace(project_workspace, cycle.project_id)
    routing = load_routing_record(
        protocol_data_root,
        cycle.project_id,
        cycle.routing_id,
    )
    if routing is None:
        raise SubnetRewardRoutingNotFoundError("Routing record not found")
    if routing.status != RoutingStatus.FINALIZED:
        raise SubnetRewardRoutingNotFinalizedError(
            "Subnet rewards require finalized routing"
        )
    if (
        routing.project_id != cycle.project_id
        or routing.source_fingerprint != cycle.routing_source_fingerprint
    ):
        raise SubnetRewardSourceChangedError(
            "Routing identity or source fingerprint changed"
        )
    submissions = collect_routed_submissions(
        protocol_data_root,
        project_workspace,
        cycle.project_id,
        routing,
    )
    assignments = {
        assignment.assignment_id: assignment
        for result in routing.results
        for assignment in result.assignments
    }
    bundles = [
        _load_source_bundle(
            protocol_data_root,
            project_workspace,
            routing,
            assignments.get(submission.routing_assignment_id or ""),
            submission,
            cycle.reward_cycle_id,
        )
        for submission in submissions
    ]
    pools_by_category = {
        pool.category: pool for pool in cycle.category_pools
    }
    result_by_category = {
        result.category: result for result in routing.results
    }
    now = _utc_now()
    calculated_pools = []
    for category in sorted(result_by_category, key=lambda item: item.value):
        draft_pool = pools_by_category[category]
        calculated_pools.append(
            calculate_category_reward_allocation(
                cycle.reward_cycle_id,
                cycle.project_id,
                routing,
                result_by_category[category],
                draft_pool.allocated_pool_points,
                [
                    bundle
                    for bundle in bundles
                    if bundle.submission.category == category.value
                ],
                now,
                requested_weight=draft_pool.requested_weight,
                normalized_weight=draft_pool.normalized_weight,
            )
        )
    external_events: list[Any] = []
    for bundle in bundles:
        external_events.extend(bundle.existing_week_5_reward_events)
        external_events.extend(bundle.existing_subnet_reward_events)
    provisional = SubnetRewardCycle.model_validate(
        {
            **cycle.model_dump(),
            "status": SubnetRewardCycleStatus.CALCULATED,
            "category_pools": calculated_pools,
            "total_distributed_points": _points(
                sum(
                    (pool.distributed_points for pool in calculated_pools),
                    Decimal("0"),
                )
            ),
            "total_undistributed_points": _points(
                sum(
                    (pool.undistributed_points for pool in calculated_pools),
                    Decimal("0"),
                )
            ),
            "node_summaries": [],
            "source_fingerprint": "0" * 64,
            "calculated_at": now,
            "finalized_at": None,
            "updated_at": now,
        }
    )
    summaries = build_node_subnet_reward_summaries(provisional)
    source_fingerprint = compute_subnet_reward_source_fingerprint(
        build_subnet_reward_source_payload(
            provisional,
            routing,
            bundles,
            external_events,
        )
    )
    return SubnetRewardCycle.model_validate(
        {
            **provisional.model_dump(),
            "node_summaries": summaries,
            "source_fingerprint": source_fingerprint,
        }
    )


def _load_source_bundle(
    protocol_data_root: Path,
    project_workspace: Path,
    routing: ProjectRoutingRecord,
    assignment: RoutingAssignment | None,
    submission: SubmissionRecord,
    reward_cycle_id: str,
) -> SubnetRewardSourceBundle:
    contribution = load_contribution_score(
        protocol_data_root,
        submission.submission_id,
    )
    validation = load_validation_decision(
        project_workspace,
        submission.finding_id,
    )
    reproduction = load_reproduction_result(
        project_workspace,
        submission.finding_id,
    )
    week_5 = load_reward_event_by_submission(
        protocol_data_root,
        submission.submission_id,
    )
    subnet_events = list_subnet_reward_events(
        protocol_data_root,
        submission_id=submission.submission_id,
    )
    node = load_node(protocol_data_root, submission.node_id)
    return SubnetRewardSourceBundle(
        routing_record=routing,
        routing_assignment=assignment,
        submission=submission,
        contribution_score_record=contribution,
        validation_decision=validation,
        reproduction_result=reproduction,
        existing_week_5_reward_events=(
            (week_5,) if week_5 is not None else ()
        ),
        existing_subnet_reward_events=tuple(subnet_events),
        current_node_record=node,
        reward_cycle_id=reward_cycle_id,
    )


def _verify_structural_source_consistency(
    bundle: SubnetRewardSourceBundle,
) -> None:
    submission = bundle.submission
    assignment = bundle.routing_assignment
    if assignment is None:
        return
    contribution = bundle.contribution_score_record
    if contribution is not None and (
        contribution.submission_id != submission.submission_id
        or contribution.project_id != submission.project_id
        or contribution.finding_id != submission.finding_id
        or contribution.node_id != submission.node_id
        or contribution.validation_id != submission.validation_id
        or contribution.reproduction_id != submission.reproduction_id
    ):
        raise SubnetRewardSourceMismatchError(
            "Contribution source references do not match the submission"
        )
    validation = bundle.validation_decision
    if validation is not None and (
        validation.project_id != submission.project_id
        or validation.finding_id != submission.finding_id
        or (
            submission.validation_id is not None
            and validation.validation_id != submission.validation_id
        )
    ):
        raise SubnetRewardSourceMismatchError(
            "Validation source references do not match the submission"
        )
    reproduction = bundle.reproduction_result
    if reproduction is not None and (
        reproduction.project_id != submission.project_id
        or reproduction.finding_id != submission.finding_id
        or (
            submission.reproduction_id is not None
            and reproduction.reproduction_id != submission.reproduction_id
        )
    ):
        raise SubnetRewardSourceMismatchError(
            "Reproduction source references do not match the submission"
        )


def _build_source_snapshot(
    bundle: SubnetRewardSourceBundle,
) -> SubnetRewardSourceSnapshot:
    assignment = bundle.routing_assignment
    contribution = bundle.contribution_score_record
    if assignment is None or contribution is None:
        raise SubnetRewardSourceMismatchError(
            "Eligible allocation lacks required source records"
        )
    validation = bundle.validation_decision
    reproduction = bundle.reproduction_result
    event_ids = sorted(
        [
            event.reward_event_id
            for event in bundle.existing_week_5_reward_events
            if event.event_status == RewardEventStatus.FINALIZED
        ]
        + [
            event.reward_event_id
            for event in bundle.existing_subnet_reward_events
            if event.reward_cycle_id != bundle.reward_cycle_id
        ]
    )
    return SubnetRewardSourceSnapshot(
        routing_id=bundle.routing_record.routing_id,
        routing_source_fingerprint=bundle.routing_record.source_fingerprint,
        routing_assignment_id=assignment.assignment_id,
        routing_assignment_membership_status=assignment.membership_status,
        routing_assignment_category_score=_decimal(assignment.category_score),
        submission_id=bundle.submission.submission_id,
        submission_source_fingerprint=_submission_fingerprint(
            bundle.submission
        ),
        contribution_score_id=contribution.score_id,
        contribution_source_fingerprint=_contribution_fingerprint(contribution),
        contribution_score=_decimal(contribution.total_score),
        validation_decision_id=(
            validation.validation_id if validation is not None else None
        ),
        validation_source_fingerprint=(
            _validation_fingerprint(validation)
            if validation is not None
            else None
        ),
        reproduction_result_id=(
            reproduction.reproduction_id if reproduction is not None else None
        ),
        reproduction_source_fingerprint=(
            _reproduction_fingerprint(reproduction)
            if reproduction is not None
            else None
        ),
        existing_reward_event_ids=event_ids,
    )


def _bundle_source_input(bundle: SubnetRewardSourceBundle) -> dict[str, Any]:
    submission = bundle.submission
    assignment = bundle.routing_assignment
    contribution = bundle.contribution_score_record
    validation = bundle.validation_decision
    reproduction = bundle.reproduction_result
    return {
        "submission": {
            "submission_id": submission.submission_id,
            "project_id": submission.project_id,
            "finding_id": submission.finding_id,
            "node_id": submission.node_id,
            "category": submission.category,
            "routing_id": submission.routing_id,
            "routing_assignment_id": submission.routing_assignment_id,
            "status": submission.status.value,
            "reward_status": submission.reward_status.value,
            "source_fingerprint": _submission_fingerprint(submission),
        },
        "assignment_id": (
            assignment.assignment_id if assignment is not None else None
        ),
        "contribution": (
            None
            if contribution is None
            else {
                "score_id": contribution.score_id,
                "source_fingerprint": _contribution_fingerprint(contribution),
                "total_score": _decimal(contribution.total_score),
                "eligibility_status": contribution.eligibility_status.value,
                "eligible_for_reward": contribution.eligible_for_reward,
            }
        ),
        "validation": (
            None
            if validation is None
            else {
                "validation_id": validation.validation_id,
                "source_fingerprint": _validation_fingerprint(validation),
                "status": validation.status.value,
                "in_scope": validation.evidence.in_scope,
                "is_duplicate": validation.evidence.is_duplicate,
            }
        ),
        "reproduction": (
            None
            if reproduction is None
            else {
                "reproduction_id": reproduction.reproduction_id,
                "source_fingerprint": _reproduction_fingerprint(reproduction),
                "status": reproduction.status.value,
            }
        ),
        "node_status": (
            bundle.current_node_record.status.value
            if bundle.current_node_record is not None
            else None
        ),
    }


def _build_reward_event(
    cycle: SubnetRewardCycle,
    allocation: SubnetSubmissionRewardAllocation,
    applied_at: datetime,
) -> SubnetRewardEvent:
    event_id = _reward_event_id(
        cycle.reward_cycle_id,
        allocation.submission_id,
    )
    source_fingerprint = _fingerprint(
        {
            "reward_event_version": SUBNET_REWARD_EVENT_VERSION,
            "reward_cycle_id": cycle.reward_cycle_id,
            "cycle_source_fingerprint": cycle.source_fingerprint,
            "allocation_id": allocation.allocation_id,
            "allocation_source_fingerprint": allocation.source_fingerprint,
            "submission_id": allocation.submission_id,
            "reward_points": allocation.reward_points,
        }
    )
    return SubnetRewardEvent(
        reward_event_id=event_id,
        reward_event_version=SUBNET_REWARD_EVENT_VERSION,
        reward_cycle_id=cycle.reward_cycle_id,
        allocation_id=allocation.allocation_id,
        project_id=allocation.project_id,
        routing_id=allocation.routing_id,
        routing_assignment_id=allocation.routing_assignment_id,
        submission_id=allocation.submission_id,
        finding_id=allocation.finding_id,
        node_id=allocation.node_id,
        subnet_id=allocation.subnet_id,
        category=allocation.category,
        reward_points=allocation.reward_points,
        contribution_score=allocation.contribution_score,
        category_multiplier=allocation.category_multiplier,
        membership_multiplier=allocation.membership_multiplier,
        raw_weight=allocation.raw_weight,
        source_fingerprint=source_fingerprint,
        status=SubnetRewardEventStatus.APPLIED,
        applied_at=applied_at,
    )


def _save_reward_event_with_status(
    protocol_data_root: Path,
    event: SubnetRewardEvent,
) -> tuple[SubnetRewardEvent, bool]:
    validated = SubnetRewardEvent.model_validate(event.model_dump())
    path = (
        get_submission_reward_events_root(
            protocol_data_root,
            validated.submission_id,
        )
        / f"{validated.reward_event_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stored = _load_model(
            path,
            SubnetRewardEvent,
            "Stored subnet reward event is malformed",
        )
        if _reward_event_identity(stored) != _reward_event_identity(validated):
            raise SubnetRewardDuplicateEventError(
                "A conflicting subnet reward event already exists"
            )
        return stored, False
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".subnet-reward-event-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(_serialize(validated))
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            stored = _load_model(
                path,
                SubnetRewardEvent,
                "Stored subnet reward event is malformed",
            )
            if _reward_event_identity(stored) != _reward_event_identity(validated):
                raise SubnetRewardDuplicateEventError(
                    "A conflicting subnet reward event already exists"
                )
            return stored, False
        return validated, True
    except OSError as exc:
        raise SubnetRewardStorageError(
            "Unable to persist subnet reward event"
        ) from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _reward_event_identity(event: SubnetRewardEvent) -> dict[str, Any]:
    return event.model_dump(exclude={"applied_at"})


def _existing_event_source_input(event: Any) -> dict[str, Any]:
    if isinstance(event, SubnetRewardEvent):
        return {
            "event_id": event.reward_event_id,
            "event_type": "subnet_reward",
            "submission_id": event.submission_id,
            "cycle_id": event.reward_cycle_id,
            "source_fingerprint": event.source_fingerprint,
            "status": event.status.value,
        }
    return {
        "event_id": event.reward_event_id,
        "event_type": "week_5_reward",
        "submission_id": event.submission_id,
        "cycle_id": event.cycle_id,
        "source_fingerprint": event.source_fingerprint,
        "status": event.event_status.value,
    }


def _immutable_request_payload(cycle: SubnetRewardCycle) -> dict[str, Any]:
    return {
        "reward_cycle_id": cycle.reward_cycle_id,
        "reward_version": cycle.reward_version,
        "policy_version": cycle.policy_version,
        "project_id": cycle.project_id,
        "routing_id": cycle.routing_id,
        "routing_source_fingerprint": cycle.routing_source_fingerprint,
        "total_pool_points": cycle.total_pool_points,
        "category_weights": cycle.category_weights,
        "request_fingerprint": cycle.request_fingerprint,
        "description": cycle.description,
    }


def _immutable_calculation_payload(cycle: SubnetRewardCycle) -> dict[str, Any]:
    category_pools = []
    for pool in cycle.category_pools:
        payload = pool.model_dump()
        payload["allocations"] = [
            allocation.model_dump(exclude={"created_at"})
            for allocation in pool.allocations
        ]
        category_pools.append(payload)
    return {
        "category_pools": category_pools,
        "node_summaries": cycle.node_summaries,
        "total_distributed_points": cycle.total_distributed_points,
        "total_undistributed_points": cycle.total_undistributed_points,
        "source_fingerprint": cycle.source_fingerprint,
    }


def _assignment_fingerprint(assignment: RoutingAssignment) -> str:
    return _fingerprint(
        assignment.model_dump(exclude={"created_at", "selection_reasons"})
    )


def _submission_fingerprint(submission: SubmissionRecord) -> str:
    return _fingerprint(
        submission.model_dump(
            exclude={
                "submitted_at",
                "created_at",
                "updated_at",
                "status_updated_at",
                "status_reason",
                "metadata",
            }
        )
    )


def _contribution_fingerprint(contribution: ContributionScoreRecord) -> str:
    return _fingerprint(
        contribution.model_dump(
            exclude={
                "created_at",
                "updated_at",
                "signals",
                "reason",
                "eligibility_reasons",
            }
        )
    )


def _validation_fingerprint(validation: ValidationDecision) -> str:
    return _fingerprint(
        {
            "validation_id": validation.validation_id,
            "project_id": validation.project_id,
            "finding_id": validation.finding_id,
            "status": validation.status.value,
            "confidence": validation.confidence,
            "evidence": {
                "has_finding": validation.evidence.has_finding,
                "has_reproduction": validation.evidence.has_reproduction,
                "reproduction_status": validation.evidence.reproduction_status,
                "in_scope": validation.evidence.in_scope,
                "is_duplicate": validation.evidence.is_duplicate,
                "duplicate_of": validation.evidence.duplicate_of,
                "normalized_severity": validation.evidence.normalized_severity,
            },
            "validator_name": validation.validator_name,
        }
    )


def _reproduction_fingerprint(reproduction: ReproductionResult) -> str:
    return _fingerprint(
        {
            "reproduction_id": reproduction.reproduction_id,
            "project_id": reproduction.project_id,
            "finding_id": reproduction.finding_id,
            "status": reproduction.status.value,
            "duration_ms": reproduction.duration_ms,
        }
    )


def _allocation_id(reward_cycle_id: str, submission_id: str) -> str:
    digest = hashlib.sha256(
        f"{reward_cycle_id}:{submission_id}".encode("utf-8")
    ).hexdigest()
    return f"subnet_reward_allocation_{digest}"


def _reward_event_id(reward_cycle_id: str, submission_id: str) -> str:
    digest = hashlib.sha256(
        f"{reward_cycle_id}:{submission_id}".encode("utf-8")
    ).hexdigest()
    return f"subnet_reward_event_{digest}"


def _verify_project_workspace(workspace: Path, project_id: str) -> None:
    _validate_identifier(project_id, "project")
    if not workspace.exists() or not workspace.is_dir():
        raise SubnetRewardProjectNotFoundError("Project workspace not found")
    metadata_path = workspace / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubnetRewardSourceMismatchError(
                "Stored project metadata is malformed"
            ) from exc
        stored_id = metadata.get("project_id") or metadata.get("id")
        if stored_id is not None and stored_id != project_id:
            raise SubnetRewardSourceMismatchError(
                "Project workspace does not match requested project"
            )


def _load_required_cycle(
    protocol_data_root: Path,
    project_id: str,
    reward_cycle_id: str,
) -> SubnetRewardCycle:
    cycle = load_subnet_reward_cycle(
        protocol_data_root,
        project_id,
        reward_cycle_id,
    )
    if cycle is None:
        raise SubnetRewardCycleNotFoundError("Subnet reward cycle not found")
    return cycle


def _validate_identifier(identifier: str, label: str) -> None:
    from app.schemas.subnet_reward import SAFE_SUBNET_REWARD_IDENTIFIER

    if (
        not isinstance(identifier, str)
        or not SAFE_SUBNET_REWARD_IDENTIFIER.fullmatch(identifier)
        or identifier in {".", ".."}
        or "/" in identifier
        or "\\" in identifier
        or Path(identifier).is_absolute()
    ):
        raise InvalidSubnetRewardIdentifierError(
            f"Invalid {label} identifier"
        )


def _safe_child(root: Path, identifier: str) -> Path:
    _validate_identifier(identifier, "subnet reward")
    child = root / identifier
    try:
        child.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidSubnetRewardIdentifierError(
            "Invalid subnet reward identifier"
        ) from exc
    return child


def _write_model_atomic(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".subnet-reward-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(_serialize(model))
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise SubnetRewardStorageError(
            "Unable to persist subnet reward data"
        ) from exc


def _load_model(path: Path, model: type[BaseModel], message: str):
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise SubnetRewardStorageError(message) from exc


def _serialize(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _points(value: Any) -> Decimal:
    return _decimal(value).quantize(
        PROTOCOL_POINTS_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        _canonical_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump())
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key.value if isinstance(key, Enum) else key): _canonical_value(
                nested
            )
            for key, nested in sorted(
                value.items(),
                key=lambda item: str(
                    item[0].value if isinstance(item[0], Enum) else item[0]
                ),
            )
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
