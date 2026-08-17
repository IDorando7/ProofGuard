from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.finding_cluster import FindingClusterStatus
from app.schemas.report_quality import (
    ReportQualityAssessmentStatus,
)
from app.schemas.routing import RoutingStatus
from app.schemas.submission import SubmissionStatus
from app.schemas.task_finding_reward import TaskFindingCalculationStatus
from app.schemas.task_operator_reward import (
    TASK_OPERATOR_CALCULATION_VERSION,
    EligibleReportRewardInput,
    OperatorRewardExclusionReason,
    OperatorTaskRewardSummary,
    ReportRewardExclusion,
    TaskOperatorCalculationStatus,
    TaskOperatorProcessingStatus,
    TaskOperatorRewardCalculation,
    TaskOperatorRewardCalculationListResponse,
    TaskOperatorRewardCalculationRequest,
    TaskOperatorRewardConfig,
)
from app.services.finding_cluster_service import load_finding_cluster
from app.services.node_registry_service import load_node
from app.services.report_quality_assessment_service import (
    load_active_report_quality_assessment,
)
from app.services.submission_service import load_submission
from app.services.subnet_router_service import load_routing_record
from app.services.task_finding_reward_service import (
    load_latest_task_finding_calculation,
    load_task_finding_calculation,
)
from app.services.task_operator_reward_calculator import (
    calculate_cluster_operator_payout,
)
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


CALCULATION_FILENAME = "calculation.json"
LATEST_FILENAME = "latest.json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
ELIGIBLE_SUBMISSION_STATUSES = {
    SubmissionStatus.ACCEPTED,
    SubmissionStatus.REWARD_PENDING,
    SubmissionStatus.REWARDED,
}
CHIEF_IMPACT_REASON_CODES = {
    "accepted_severity_consistent",
    "impact_consistent_with_final_severity",
    "validator_confirmed_impact_consistent",
}


class TaskOperatorRewardServiceError(ValueError):
    pass


class TaskOperatorRewardNotFoundError(TaskOperatorRewardServiceError):
    pass


class TaskOperatorRewardLinkageError(TaskOperatorRewardServiceError):
    pass


class TaskOperatorRewardStateError(TaskOperatorRewardServiceError):
    pass


class TaskOperatorRewardStorageError(TaskOperatorRewardServiceError):
    pass


class TaskOperatorRewardConflictError(TaskOperatorRewardServiceError):
    pass


def get_task_operator_calculations_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "task-rewards" / "calculations" / "routing"


def get_task_operator_routing_root(protocol_data_root: Path, routing_id: str) -> Path:
    _validate_identifier(routing_id, "routing")
    root = get_task_operator_calculations_root(protocol_data_root)
    path = root / routing_id / "operator-allocation"
    _ensure_within(path, root)
    return path


def get_task_operator_calculation_path(
    protocol_data_root: Path, routing_id: str, calculation_id: str
) -> Path:
    _validate_identifier(calculation_id, "operator calculation")
    root = get_task_operator_routing_root(protocol_data_root, routing_id)
    path = root / calculation_id / CALCULATION_FILENAME
    _ensure_within(path, root)
    return path


def calculate_task_operator_rewards(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    request: TaskOperatorRewardCalculationRequest,
    *,
    configured_policy: TaskOperatorRewardConfig | None = None,
    policy_version: str | None = None,
    configuration_version: str | None = None,
) -> tuple[TaskOperatorRewardCalculation, TaskOperatorProcessingStatus]:
    """Calculate a Day 5 preview without creating or finalizing RewardEvents."""
    _validate_identifier(project_id, "project")
    _validate_identifier(routing_id, "routing")
    routing = load_routing_record(protocol_data_root, project_id, routing_id)
    if routing is None:
        raise TaskOperatorRewardNotFoundError("Routing record not found")
    if routing.project_id != project_id:
        raise TaskOperatorRewardLinkageError("Routing belongs to another project")
    if routing.status != RoutingStatus.FINALIZED:
        raise TaskOperatorRewardStateError("Operator payout preview requires finalized routing")

    day4 = load_task_finding_calculation(
        protocol_data_root,
        project_id,
        routing_id,
        request.task_finding_reward_calculation_id,
    )
    if day4 is None:
        raise TaskOperatorRewardNotFoundError("Day 4 finding calculation not found")
    latest_day4 = load_latest_task_finding_calculation(
        protocol_data_root, project_id, routing_id
    )
    if (
        latest_day4 is None
        or latest_day4.calculation_id != day4.calculation_id
        or day4.status != TaskFindingCalculationStatus.CALCULATED
    ):
        raise TaskOperatorRewardStateError(
            "Operator payout preview requires the current calculated Day 4 snapshot"
        )

    settings = get_settings()
    config = TaskOperatorRewardConfig.model_validate(
        configured_policy or settings.task_operator_reward
    )
    policy = policy_version or settings.task_operator_policy_version
    config_version = (
        configuration_version or settings.task_operator_configuration_version
    )
    assignments = {
        assignment.assignment_id: assignment
        for result in routing.results
        for assignment in result.assignments
    }
    cluster_payouts = []
    for day4_allocation in day4.cluster_allocations:
        cluster = load_finding_cluster(
            protocol_data_root,
            project_id,
            routing_id,
            day4_allocation.finding_cluster_id,
        )
        if cluster is None:
            raise TaskOperatorRewardNotFoundError("Day 4 FindingCluster is missing")
        if (
            cluster.status != FindingClusterStatus.FINALIZED
            or cluster.source_fingerprint
            != day4_allocation.cluster_source_fingerprint
            or cluster.category != day4_allocation.category
        ):
            raise TaskOperatorRewardStateError(
                "FindingCluster changed after the Day 4 economic snapshot"
            )
        reports: list[EligibleReportRewardInput] = []
        exclusions: list[ReportRewardExclusion] = []
        for member in cluster.members:
            report, exclusion = _resolve_member_report(
                protocol_data_root,
                project_id,
                routing_id,
                cluster,
                member,
                assignments,
                settings.report_quality_policy_version,
            )
            if report is not None:
                reports.append(report)
            elif exclusion is not None:
                exclusions.append(exclusion)
        cluster_payouts.append(
            calculate_cluster_operator_payout(
                day4_allocation,
                reports,
                exclusions,
                config,
                policy_version=policy,
            )
        )
    cluster_payouts.sort(key=lambda item: item.finding_cluster_id)
    summaries = _operator_summaries(cluster_payouts)
    source_total = sum(
        (item.cluster_reward_points for item in cluster_payouts), Decimal("0")
    )
    distributed = sum(
        (item.distributed_points for item in cluster_payouts), Decimal("0")
    )
    undistributed = source_total - distributed
    source_payload = {
        "calculation_version": TASK_OPERATOR_CALCULATION_VERSION,
        "policy_version": policy,
        "configuration_version": config_version,
        "project_id": project_id,
        "routing_id": routing_id,
        "routing_source_fingerprint": routing.source_fingerprint,
        "task_reward_budget_id": day4.task_reward_budget_id,
        "day4_calculation_id": day4.calculation_id,
        "day4_source_fingerprint": day4.source_fingerprint,
        "protocol_quantum": Decimal("0.000001"),
        "operator_reward_config": config,
        "cluster_payouts": cluster_payouts,
        "operator_summaries": summaries,
        "source_cluster_reward_points": source_total,
        "distributed_operator_points": distributed,
        "undistributed_cluster_points": undistributed,
    }
    source_fingerprint = protocol_fingerprint(source_payload)
    calculation_id = f"task_operator_reward_calculation_{source_fingerprint[:32]}"
    existing = load_task_operator_calculation(
        protocol_data_root, project_id, routing_id, calculation_id
    )
    if existing is not None:
        if existing.status == TaskOperatorCalculationStatus.SUPERSEDED:
            raise TaskOperatorRewardConflictError(
                "The identical source belongs to a superseded operator preview"
            )
        return existing, TaskOperatorProcessingStatus.UNCHANGED

    previous = load_latest_task_operator_calculation(
        protocol_data_root, project_id, routing_id
    )
    now = datetime.now(timezone.utc)
    record = TaskOperatorRewardCalculation(
        calculation_id=calculation_id,
        policy_version=policy,
        configuration_version=config_version,
        status=TaskOperatorCalculationStatus.CALCULATED,
        project_id=project_id,
        routing_id=routing_id,
        task_reward_budget_id=day4.task_reward_budget_id,
        day4_calculation_id=day4.calculation_id,
        day4_source_fingerprint=day4.source_fingerprint,
        source_cluster_reward_points=source_total,
        cluster_payouts=cluster_payouts,
        operator_summaries=summaries,
        distributed_operator_points=distributed,
        undistributed_cluster_points=undistributed,
        source_fingerprint=source_fingerprint,
        supersedes_calculation_id=(previous.calculation_id if previous else None),
        superseded_by_calculation_id=None,
        created_at=now,
        calculated_at=now,
    )
    _persist_new_calculation(protocol_data_root, record)
    processing = TaskOperatorProcessingStatus.CALCULATED
    if previous is not None:
        old = TaskOperatorRewardCalculation.model_validate(
            {
                **previous.model_dump(),
                "status": TaskOperatorCalculationStatus.SUPERSEDED,
                "superseded_by_calculation_id": calculation_id,
            }
        )
        atomic_write_json(
            get_task_operator_calculation_path(
                protocol_data_root, routing_id, previous.calculation_id
            ),
            old,
            temporary_prefix=".task-operator-supersede-",
        )
        processing = TaskOperatorProcessingStatus.SUPERSEDED
    atomic_write_json(
        get_task_operator_routing_root(protocol_data_root, routing_id) / LATEST_FILENAME,
        {
            "project_id": project_id,
            "routing_id": routing_id,
            "calculation_id": calculation_id,
        },
        temporary_prefix=".task-operator-latest-",
    )
    return record, processing


def load_task_operator_calculation(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    calculation_id: str,
) -> TaskOperatorRewardCalculation | None:
    path = get_task_operator_calculation_path(
        protocol_data_root, routing_id, calculation_id
    )
    if not path.exists():
        return None
    record = _read_calculation(path)
    if record.project_id != project_id or record.routing_id != routing_id:
        raise TaskOperatorRewardStorageError(
            "Stored operator preview identity does not match its path"
        )
    return record


def load_latest_task_operator_calculation(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> TaskOperatorRewardCalculation | None:
    path = get_task_operator_routing_root(protocol_data_root, routing_id) / LATEST_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        calculation_id = payload["calculation_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TaskOperatorRewardStorageError("Stored latest operator index is malformed") from exc
    if payload.get("project_id") != project_id or payload.get("routing_id") != routing_id:
        raise TaskOperatorRewardStorageError("Latest operator index has wrong identity")
    record = load_task_operator_calculation(
        protocol_data_root, project_id, routing_id, calculation_id
    )
    if record is None:
        raise TaskOperatorRewardStorageError("Latest index references missing preview")
    return record


def list_task_operator_calculations(
    protocol_data_root: Path, project_id: str, routing_id: str
) -> TaskOperatorRewardCalculationListResponse:
    root = get_task_operator_routing_root(protocol_data_root, routing_id)
    records = []
    if root.exists():
        for path in sorted(root.glob(f"*/{CALCULATION_FILENAME}")):
            record = _read_calculation(path)
            if record.project_id != project_id or record.routing_id != routing_id:
                raise TaskOperatorRewardStorageError("Stored operator preview linkage is malformed")
            records.append(record)
    records.sort(key=lambda item: (item.created_at, item.calculation_id))
    return TaskOperatorRewardCalculationListResponse(
        project_id=project_id,
        routing_id=routing_id,
        total=len(records),
        calculations=records,
    )


def _resolve_member_report(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    cluster,
    member,
    assignments: dict[str, Any],
    quality_policy_version: str,
) -> tuple[EligibleReportRewardInput | None, ReportRewardExclusion | None]:
    submission = load_submission(protocol_data_root, member.submission_id)
    if submission is None:
        return None, _exclusion(cluster.finding_cluster_id, member, OperatorRewardExclusionReason.INVALID_CLUSTER_MEMBER)
    if (
        submission.project_id != project_id
        or submission.routing_id != routing_id
        or submission.finding_id != member.finding_id
        or submission.node_id != member.node_id
        or submission.category != cluster.category.value
        or submission.status not in ELIGIBLE_SUBMISSION_STATUSES
    ):
        return None, _exclusion(cluster.finding_cluster_id, member, OperatorRewardExclusionReason.INVALID_CLUSTER_MEMBER)
    assignment = assignments.get(submission.routing_assignment_id)
    if (
        assignment is None
        or assignment.project_id != project_id
        or assignment.routing_id != routing_id
        or assignment.node_id != submission.node_id
        or assignment.category != cluster.category
    ):
        return None, _exclusion(cluster.finding_cluster_id, member, OperatorRewardExclusionReason.UNAUTHORIZED_ASSIGNMENT)
    node = load_node(protocol_data_root, submission.node_id)
    if node is None or node.operator_id != member.operator_id:
        return None, _exclusion(
            cluster.finding_cluster_id,
            member,
            OperatorRewardExclusionReason.OPERATOR_IDENTITY_MISMATCH,
            operator_id=(node.operator_id if node else None),
        )
    assessment = load_active_report_quality_assessment(
        protocol_data_root,
        routing_id,
        submission.submission_id,
        quality_policy_version,
    )
    if assessment is None:
        return None, _exclusion(
            cluster.finding_cluster_id,
            member, OperatorRewardExclusionReason.MISSING_QUALITY_ASSESSMENT
        )
    if assessment.assessment_status != ReportQualityAssessmentStatus.FINALIZED:
        reason = (
            OperatorRewardExclusionReason.DRAFT_QUALITY_ASSESSMENT
            if assessment.assessment_status == ReportQualityAssessmentStatus.DRAFT
            else OperatorRewardExclusionReason.SUPERSEDED_QUALITY_ASSESSMENT
        )
        return None, _exclusion(
            cluster.finding_cluster_id,
            member,
            reason,
            quality_score=assessment.quality_score,
            assessment_id=assessment.report_quality_assessment_id,
        )
    if (
        assessment.quality_score is None
        or assessment.project_id != project_id
        or assessment.routing_id != routing_id
        or assessment.finding_cluster_id != cluster.finding_cluster_id
        or assessment.submission_id != submission.submission_id
        or assessment.node_id != submission.node_id
        or assessment.operator_id != node.operator_id
        or assessment.member_source_fingerprint != member.source_fingerprint
        or assessment.cluster_source_fingerprint != cluster.source_fingerprint
        or assessment.submitted_at != submission.submitted_at
    ):
        return None, _exclusion(
            cluster.finding_cluster_id,
            member,
            OperatorRewardExclusionReason.INVALID_CLUSTER_MEMBER,
            quality_score=assessment.quality_score,
            assessment_id=assessment.report_quality_assessment_id,
        )
    impact = assessment.components.impact_quality
    accepted_severity_reference = f"accepted-severity:{cluster.final_severity.value}"
    impact_qualified = (
        impact.score is not None
        and impact.score > 0
        and bool(set(impact.reason_codes).intersection(CHIEF_IMPACT_REASON_CODES))
        and accepted_severity_reference in impact.evidence_references
    )
    return (
        EligibleReportRewardInput(
            finding_cluster_id=cluster.finding_cluster_id,
            submission_id=submission.submission_id,
            finding_id=submission.finding_id,
            node_id=submission.node_id,
            operator_id=node.operator_id,
            routing_assignment_id=submission.routing_assignment_id,
            submitted_at=submission.submitted_at,
            member_relation=member.relation,
            member_source_fingerprint=member.source_fingerprint,
            assessment_id=assessment.report_quality_assessment_id,
            assessment_source_fingerprint=assessment.source_fingerprint,
            quality_score=assessment.quality_score,
            chief_root_cause_qualified=True,
            chief_impact_qualified=impact_qualified,
            chief_evidence_reason_codes=impact.reason_codes,
            chief_evidence_references=impact.evidence_references,
        ),
        None,
    )


def _exclusion(
    finding_cluster_id: str,
    member,
    reason: OperatorRewardExclusionReason,
    *,
    operator_id: str | None = None,
    quality_score: Decimal | None = None,
    assessment_id: str | None = None,
) -> ReportRewardExclusion:
    return ReportRewardExclusion(
        finding_cluster_id=finding_cluster_id,
        submission_id=member.submission_id,
        node_id=member.node_id,
        operator_id=operator_id if operator_id is not None else member.operator_id,
        quality_score=quality_score,
        reason=reason,
        representative_submission_id=None,
        quality_assessment_id=assessment_id,
    )


def _operator_summaries(cluster_payouts) -> list[OperatorTaskRewardSummary]:
    by_operator: dict[str, list[Any]] = defaultdict(list)
    for cluster in cluster_payouts:
        for allocation in cluster.operator_allocations:
            if allocation.rewarded:
                by_operator[allocation.operator_id].append(allocation)
    return [
        OperatorTaskRewardSummary(
            operator_id=operator_id,
            rewarded_cluster_count=len(allocations),
            rewarded_finding_cluster_ids=sorted(
                allocation.finding_cluster_id for allocation in allocations
            ),
            total_reward_points=sum(
                (allocation.total_reward_points for allocation in allocations),
                Decimal("0"),
            ),
        )
        for operator_id, allocations in sorted(by_operator.items())
    ]


def _persist_new_calculation(
    protocol_data_root: Path, record: TaskOperatorRewardCalculation
) -> None:
    path = get_task_operator_calculation_path(
        protocol_data_root, record.routing_id, record.calculation_id
    )
    try:
        created = atomic_create_json(
            path, record, temporary_prefix=".task-operator-create-"
        )
    except OSError as exc:
        raise TaskOperatorRewardStorageError("Unable to persist operator preview") from exc
    if not created and _read_calculation(path) != record:
        raise TaskOperatorRewardConflictError(
            "A conflicting operator preview was created concurrently"
        )


def _read_calculation(path: Path) -> TaskOperatorRewardCalculation:
    if not path.is_file():
        raise TaskOperatorRewardStorageError("Operator preview is not a regular file")
    try:
        return TaskOperatorRewardCalculation.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise TaskOperatorRewardStorageError("Stored operator preview is malformed") from exc


def _validate_identifier(value: str, label: str) -> None:
    if (
        not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise TaskOperatorRewardLinkageError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise TaskOperatorRewardLinkageError("Unsafe operator preview path") from exc
