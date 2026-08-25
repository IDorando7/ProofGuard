from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.finding import Finding
from app.schemas.finding_cluster import (
    FindingCluster,
    FindingClusterMember,
    has_resolved_accepted_cluster_truth,
)
from app.schemas.report_quality import (
    REPORT_QUALITY_QUANTUM,
    REPORT_QUALITY_SCHEMA_VERSION,
    QualityAssessmentSource,
    QualityComponentAssessment,
    QualityComponentName,
    QualityComponentState,
    ReportQualityAssessment,
    ReportQualityAssessmentMethod,
    ReportQualityAssessmentRequest,
    ReportQualityAssessmentStatus,
    ReportQualityComponentAssessments,
    ReportQualityConfig,
    ReportQualityRebuildResult,
)
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.submission import SubmissionRecord, SubmissionStatus
from app.schemas.validation import DuplicateKind, ValidationDecision, ValidationStatus
from app.services.finding_cluster_service import (
    FindingClusterProjectMismatchError,
    list_task_finding_clusters,
    load_finding_cluster,
)
from app.services.finding_service import load_project_finding
from app.services.node_registry_service import load_node
from app.services.reproduction_service import load_reproduction_result
from app.services.report_quality_calculator import calculate_report_quality
from app.services.submission_service import load_submission
from app.services.validation_service import load_validation_decision
from app.utils.protocol_serialization import (
    atomic_create_json,
    atomic_write_json,
    protocol_fingerprint,
)


INDEX_FILENAME = "index.json"
ASSESSMENT_FILENAME_PREFIX = "assessment-"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
MEANINGLESS_TEXT = {"unknown", "n/a", "none", "todo", "placeholder", "not applicable"}
ELIGIBLE_SUBMISSION_STATUSES = {
    SubmissionStatus.ACCEPTED,
    SubmissionStatus.REWARD_PENDING,
    SubmissionStatus.REWARDED,
}


class ReportQualityServiceError(ValueError):
    pass


class ReportQualityNotFoundError(ReportQualityServiceError):
    pass


class ReportQualityLinkageError(ReportQualityServiceError):
    pass


class ReportQualityEligibilityError(ReportQualityServiceError):
    pass


class ReportQualityConflictError(ReportQualityServiceError):
    pass


class ReportQualityStorageError(ReportQualityServiceError):
    pass


def get_report_quality_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "report-quality" / "tasks"


def get_submission_quality_root(
    protocol_data_root: Path, routing_id: str, submission_id: str
) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(submission_id, "submission")
    root = get_report_quality_root(protocol_data_root)
    path = root / routing_id / "submissions" / submission_id
    _ensure_within(path, root)
    return path


def get_report_quality_assessment_path(
    protocol_data_root: Path,
    routing_id: str,
    submission_id: str,
    assessment_version: int,
) -> Path:
    if assessment_version < 1:
        raise ReportQualityLinkageError("Assessment version must be positive")
    return get_submission_quality_root(
        protocol_data_root, routing_id, submission_id
    ) / f"{ASSESSMENT_FILENAME_PREFIX}{assessment_version:06d}.json"


def evaluate_correctness(
    validation: ValidationDecision,
    member: FindingClusterMember,
) -> tuple[Decimal, QualityAssessmentSource, list[str], list[str]]:
    independent = (
        validation.evidence.duplicate_kind == DuplicateKind.INDEPENDENT_ROOT_CAUSE
        and validation.evidence.is_valid_duplicate is True
        and validation.evidence.is_duplicate is True
    )
    reason = (
        "accepted_independent_root_cause"
        if independent
        else "accepted_cluster_valid_report"
    )
    return (
        Decimal("1.000000"),
        QualityAssessmentSource.VALIDATION_DECISION,
        [reason],
        [
            f"validation:{validation.validation_id}",
            f"cluster-member:{member.source_fingerprint}",
        ],
    )


def evaluate_poc_quality(
    reproduction: ReproductionResult | None,
    finding: Finding,
) -> tuple[Decimal, QualityAssessmentSource, list[str], list[str]]:
    if reproduction is None:
        return (
            Decimal("0.000000"),
            QualityAssessmentSource.REPRODUCTION_RESULT,
            ["reproduction_missing"],
            [],
        )
    reference = [f"reproduction:{reproduction.reproduction_id}"]
    if reproduction.status != ReproductionStatus.REPRODUCED:
        return (
            Decimal("0.000000"),
            QualityAssessmentSource.REPRODUCTION_RESULT,
            [f"reproduction_{reproduction.status.value}"],
            reference,
        )
    has_target = bool(reproduction.poc_file and reproduction.test_name)
    has_command = bool(reproduction.command)
    has_output = bool(reproduction.stdout or reproduction.stderr)
    if has_target and has_command and has_output:
        return (
            Decimal("1.000000"),
            QualityAssessmentSource.REPRODUCTION_RESULT,
            ["reproduced_targeted_deterministic_test"],
            reference,
        )
    if has_target or has_command or has_output:
        return (
            Decimal("0.750000"),
            QualityAssessmentSource.REPRODUCTION_RESULT,
            ["reproduced_partial_execution_evidence"],
            reference,
        )
    return (
        Decimal("0.750000"),
        QualityAssessmentSource.REPRODUCTION_RESULT,
        ["reproduced_without_targeted_artifact_metadata"],
        reference,
    )


def evaluate_root_cause_quality(
    finding: Finding,
    cluster: FindingCluster,
) -> tuple[Decimal, QualityAssessmentSource, list[str], list[str]]:
    root_present = _meaningful(finding.root_cause)
    location_present = bool(
        any(_meaningful(item) for item in finding.contracts)
        or any(_meaningful(item) for item in finding.functions)
    )
    references = [
        f"finding:{finding.finding_id}",
        f"cluster-root:{cluster.root_cause_fingerprint}",
    ]
    if root_present and location_present:
        return (
            Decimal("1.000000"),
            QualityAssessmentSource.FINDING_STRUCTURED_FIELDS,
            ["explicit_root_cause_with_affected_component", "cluster_root_relation_confirmed"],
            references,
        )
    if root_present:
        return (
            Decimal("0.500000"),
            QualityAssessmentSource.FINDING_STRUCTURED_FIELDS,
            ["explicit_root_cause_missing_affected_component"],
            references,
        )
    return (
        Decimal("0.000000"),
        QualityAssessmentSource.FINDING_STRUCTURED_FIELDS,
        ["root_cause_missing_or_placeholder"],
        references,
    )


def evaluate_impact_quality(
    finding: Finding,
    validation: ValidationDecision,
    cluster: FindingCluster,
) -> tuple[Decimal, QualityAssessmentSource, list[str], list[str]]:
    references = [
        f"finding:{finding.finding_id}",
        f"validation:{validation.validation_id}",
        f"accepted-severity:{cluster.final_severity.value}",
    ]
    if not _meaningful(finding.impact):
        return (
            Decimal("0.000000"),
            QualityAssessmentSource.SEVERITY_NORMALIZATION,
            ["impact_missing_or_placeholder"],
            references,
        )
    # The current validator provides final severity but no structured assertion
    # that arbitrary impact prose justifies it. Presence therefore earns only a
    # conservative partial value; validators can provide a reviewed score.
    return (
        Decimal("0.500000"),
        QualityAssessmentSource.SEVERITY_NORMALIZATION,
        ["impact_present_consistency_not_structurally_assessed"],
        references,
    )


def evaluate_fix_quality(
    finding: Finding,
) -> tuple[None, QualityAssessmentSource, list[str], list[str]]:
    reason = (
        "fix_present_but_no_structured_evaluator"
        if _meaningful(finding.recommended_fix)
        else "fix_missing_or_placeholder"
    )
    return (
        None,
        QualityAssessmentSource.NOT_ASSESSED,
        [reason, "validator_fix_assessment_required"],
        [f"finding:{finding.finding_id}"],
    )


def build_report_quality_source_payload(
    *,
    policy_version: str,
    configuration_version: str,
    config: ReportQualityConfig,
    project_id: str,
    routing_id: str,
    cluster: FindingCluster,
    member: FindingClusterMember,
    validation_id: str,
    validation_source_fingerprint: str,
    reproduction_id: str | None,
    reproduction_source_fingerprint: str | None,
    severity_source_fingerprint: str,
    finding_source_fingerprint: str,
    components: ReportQualityComponentAssessments,
    quality_score: Decimal | None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_QUALITY_SCHEMA_VERSION,
        "assessment_policy_version": policy_version,
        "configuration_version": configuration_version,
        "weights": config,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": cluster.finding_cluster_id,
        "cluster_source_fingerprint": cluster.source_fingerprint,
        "submission_id": member.submission_id,
        "finding_id": member.finding_id,
        "node_id": member.node_id,
        "operator_id": member.operator_id,
        "member_source_fingerprint": member.source_fingerprint,
        "validation_id": validation_id,
        "validation_source_fingerprint": validation_source_fingerprint,
        "reproduction_id": reproduction_id,
        "reproduction_source_fingerprint": reproduction_source_fingerprint,
        "severity_source_fingerprint": severity_source_fingerprint,
        "finding_source_fingerprint": finding_source_fingerprint,
        "components": components,
        "quality_score": quality_score,
    }


def compute_report_quality_source_fingerprint(payload: dict[str, Any]) -> str:
    return protocol_fingerprint(payload)


def assess_report_quality(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    submission_id: str,
    request: ReportQualityAssessmentRequest | None = None,
    *,
    configured_quality: ReportQualityConfig | None = None,
    policy_version: str | None = None,
    configuration_version: str | None = None,
) -> tuple[ReportQualityAssessment, str]:
    request = request or ReportQualityAssessmentRequest()
    settings = get_settings()
    config = ReportQualityConfig.model_validate(
        configured_quality or settings.report_quality
    )
    policy = policy_version or settings.report_quality_policy_version
    config_version = (
        configuration_version or settings.report_quality_configuration_version
    )
    cluster, member, submission, finding, validation, reproduction = _load_sources(
        protocol_data_root,
        project_workspace,
        project_id,
        routing_id,
        finding_cluster_id,
        submission_id,
    )
    _require_eligible(
        protocol_data_root,
        cluster,
        member,
        submission,
        finding,
        validation,
        reproduction,
    )
    existing = load_active_report_quality_assessment(
        protocol_data_root, routing_id, submission_id, policy
    )
    components = _build_components(
        config,
        policy,
        cluster,
        member,
        finding,
        validation,
        reproduction,
        request,
        existing,
    )
    scores = components.scores()
    quality_score = calculate_report_quality(scores, config) if scores is not None else None
    target_status = (
        ReportQualityAssessmentStatus.FINALIZED
        if request.finalize and scores is not None
        else ReportQualityAssessmentStatus.DRAFT
    )
    if (
        existing is not None
        and existing.assessment_status == ReportQualityAssessmentStatus.FINALIZED
        and target_status != ReportQualityAssessmentStatus.FINALIZED
        and request.supersede_existing
    ):
        raise ReportQualityConflictError(
            "A finalized assessment cannot be superseded by an incomplete draft"
        )

    validation_fingerprint = _validation_source_fingerprint(validation)
    reproduction_fingerprint = (
        _reproduction_source_fingerprint(reproduction)
        if reproduction is not None
        else None
    )
    severity_fingerprint = protocol_fingerprint(
        {
            "validation_id": validation.validation_id,
            "original_severity": validation.evidence.original_severity,
            "normalized_severity": cluster.final_severity.value,
            "cluster_source_fingerprint": cluster.source_fingerprint,
        }
    )
    finding_fingerprint = _finding_source_fingerprint(finding)
    payload = build_report_quality_source_payload(
        policy_version=policy,
        configuration_version=config_version,
        config=config,
        project_id=project_id,
        routing_id=routing_id,
        cluster=cluster,
        member=member,
        validation_id=validation.validation_id,
        validation_source_fingerprint=validation_fingerprint,
        reproduction_id=reproduction.reproduction_id if reproduction else None,
        reproduction_source_fingerprint=reproduction_fingerprint,
        severity_source_fingerprint=severity_fingerprint,
        finding_source_fingerprint=finding_fingerprint,
        components=components,
        quality_score=quality_score,
    )
    source_fingerprint = compute_report_quality_source_fingerprint(payload)
    if existing is not None and existing.source_fingerprint == source_fingerprint:
        if existing.assessment_status == target_status:
            return existing, "unchanged"
        if existing.assessment_status == ReportQualityAssessmentStatus.FINALIZED:
            raise ReportQualityConflictError("Finalized quality assessments are immutable")
        transitioned = _record_from_sources(
            existing.report_quality_assessment_id,
            existing.assessment_version,
            existing.created_at,
            None,
            target_status,
            policy,
            config_version,
            cluster,
            member,
            validation,
            reproduction,
            components,
            quality_score,
            validation_fingerprint,
            reproduction_fingerprint,
            severity_fingerprint,
            finding_fingerprint,
            source_fingerprint,
        )
        _write_assessment_and_index(protocol_data_root, transitioned)
        return transitioned, "updated"

    supersedes: ReportQualityAssessment | None = None
    if existing is not None and existing.assessment_status == ReportQualityAssessmentStatus.FINALIZED:
        if not request.supersede_existing:
            raise ReportQualityConflictError(
                "Finalized assessment sources changed; explicit supersession is required"
            )
        supersedes = existing
        version = _next_assessment_version(protocol_data_root, routing_id, submission_id)
        created_at = _utc_now()
    elif existing is not None:
        version = existing.assessment_version
        created_at = existing.created_at
    else:
        version = _next_assessment_version(protocol_data_root, routing_id, submission_id)
        created_at = _utc_now()

    assessment_id = _assessment_id(project_id, routing_id, submission_id, policy, version)
    record = _record_from_sources(
        assessment_id,
        version,
        created_at,
        supersedes.report_quality_assessment_id if supersedes else None,
        target_status,
        policy,
        config_version,
        cluster,
        member,
        validation,
        reproduction,
        components,
        quality_score,
        validation_fingerprint,
        reproduction_fingerprint,
        severity_fingerprint,
        finding_fingerprint,
        source_fingerprint,
    )
    if supersedes is not None:
        old = ReportQualityAssessment.model_validate(
            {
                **supersedes.model_dump(),
                "assessment_status": ReportQualityAssessmentStatus.SUPERSEDED,
                "superseded_by_assessment_id": record.report_quality_assessment_id,
                "updated_at": _utc_now(),
            }
        )
        _write_assessment_file(protocol_data_root, old, replace=True)
    _write_assessment_and_index(protocol_data_root, record)
    return record, "created" if existing is None or supersedes is not None else "updated"


def rebuild_task_report_quality_assessments(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
    *,
    supersede_changed_finalized: bool = False,
) -> ReportQualityRebuildResult:
    clusters = list_task_finding_clusters(protocol_data_root, project_id, routing_id)
    assessments: list[ReportQualityAssessment] = []
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for cluster in clusters:
        if not has_resolved_accepted_cluster_truth(cluster):
            # Bulk payout preparation ignores preserved candidate/rejected audit
            # records. Direct assessment remains a hard eligibility error.
            continue
        for member in cluster.members:
            try:
                assessment, operation = assess_report_quality(
                    protocol_data_root,
                    project_workspace,
                    project_id,
                    routing_id,
                    cluster.finding_cluster_id,
                    member.submission_id,
                    ReportQualityAssessmentRequest(
                        supersede_existing=supersede_changed_finalized
                    ),
                )
            except ReportQualityEligibilityError:
                # Week 8 may resolve cluster truth before a protocol-approved
                # consensus-to-agent report-quality adapter exists. Such reports
                # stay quality/reward-ineligible instead of being reinterpreted.
                continue
            assessments.append(assessment)
            counts[operation] += 1
    assessments.sort(key=lambda item: (item.finding_cluster_id, item.submitted_at, item.submission_id))
    return ReportQualityRebuildResult(
        project_id=project_id,
        routing_id=routing_id,
        total_assessments=len(assessments),
        created_assessments=counts["created"],
        updated_assessments=counts["updated"],
        unchanged_assessments=counts["unchanged"],
        draft_assessments=sum(
            item.assessment_status == ReportQualityAssessmentStatus.DRAFT
            for item in assessments
        ),
        finalized_assessments=sum(
            item.assessment_status == ReportQualityAssessmentStatus.FINALIZED
            for item in assessments
        ),
        assessments=assessments,
    )


def load_active_report_quality_assessment(
    protocol_data_root: Path,
    routing_id: str,
    submission_id: str,
    policy_version: str | None = None,
) -> ReportQualityAssessment | None:
    policy = policy_version or get_settings().report_quality_policy_version
    index = _load_index(protocol_data_root, routing_id, submission_id)
    if index is None:
        return None
    assessment_id = index.get("active_by_policy", {}).get(policy)
    if assessment_id is None:
        return None
    matches = [
        item
        for item in _load_all_for_submission(protocol_data_root, routing_id, submission_id)
        if item.report_quality_assessment_id == assessment_id
    ]
    if len(matches) != 1:
        raise ReportQualityStorageError("Quality index references a missing assessment")
    return matches[0]


def load_report_quality_assessment_by_id(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    assessment_id: str,
) -> ReportQualityAssessment | None:
    """Load any assessment version by its deterministic ID within URL scope."""
    for value, label in (
        (project_id, "project"),
        (routing_id, "routing"),
        (assessment_id, "report quality assessment"),
    ):
        _validate_identifier(value, label)
    if not assessment_id.startswith("report_quality_assessment_"):
        raise ReportQualityLinkageError("Invalid report quality assessment identifier")

    task_root = get_report_quality_root(protocol_data_root) / routing_id
    _ensure_within(task_root, get_report_quality_root(protocol_data_root))
    submissions_root = task_root / "submissions"
    if not submissions_root.exists():
        return None
    if not submissions_root.is_dir():
        raise ReportQualityStorageError("Stored report quality task is malformed")

    matches: list[ReportQualityAssessment] = []
    for submission_root in sorted(submissions_root.iterdir(), key=lambda item: item.name):
        if not submission_root.is_dir():
            raise ReportQualityStorageError("Stored report quality submission is malformed")
        _validate_identifier(submission_root.name, "submission")
        matches.extend(
            item
            for item in _load_all_for_submission(
                protocol_data_root, routing_id, submission_root.name
            )
            if item.report_quality_assessment_id == assessment_id
        )
    if len(matches) > 1:
        raise ReportQualityStorageError(
            "Report quality assessment identifier is not unique within the task"
        )
    if not matches or matches[0].project_id != project_id:
        return None
    return matches[0]


def list_submission_report_quality_assessments(
    protocol_data_root: Path,
    routing_id: str,
    submission_id: str,
    *,
    include_superseded: bool = False,
) -> list[ReportQualityAssessment]:
    records = _load_all_for_submission(protocol_data_root, routing_id, submission_id)
    if not include_superseded:
        records = [
            item
            for item in records
            if item.assessment_status != ReportQualityAssessmentStatus.SUPERSEDED
        ]
    return sorted(records, key=lambda item: (item.assessment_version, item.report_quality_assessment_id))


def list_cluster_report_quality_assessments(
    protocol_data_root: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    *,
    include_superseded: bool = False,
) -> list[ReportQualityAssessment]:
    cluster = load_finding_cluster(protocol_data_root, project_id, routing_id, finding_cluster_id)
    if cluster is None:
        raise ReportQualityNotFoundError("Finding cluster not found")
    output: list[ReportQualityAssessment] = []
    for member in cluster.members:
        output.extend(
            list_submission_report_quality_assessments(
                protocol_data_root,
                routing_id,
                member.submission_id,
                include_superseded=include_superseded,
            )
        )
    return sorted(
        [item for item in output if item.finding_cluster_id == finding_cluster_id],
        key=lambda item: (item.submitted_at, item.submission_id, item.assessment_version),
    )


def _build_components(
    config: ReportQualityConfig,
    policy: str,
    cluster: FindingCluster,
    member: FindingClusterMember,
    finding: Finding,
    validation: ValidationDecision,
    reproduction: ReproductionResult | None,
    request: ReportQualityAssessmentRequest,
    existing: ReportQualityAssessment | None,
) -> ReportQualityComponentAssessments:
    evaluated = {
        QualityComponentName.CORRECTNESS: evaluate_correctness(validation, member),
        QualityComponentName.POC_QUALITY: evaluate_poc_quality(reproduction, finding),
        QualityComponentName.ROOT_CAUSE_QUALITY: evaluate_root_cause_quality(finding, cluster),
        QualityComponentName.IMPACT_QUALITY: evaluate_impact_quality(finding, validation, cluster),
        QualityComponentName.FIX_QUALITY: evaluate_fix_quality(finding),
    }
    fields = {
        QualityComponentName.CORRECTNESS: "correctness_score",
        QualityComponentName.POC_QUALITY: "poc_quality_score",
        QualityComponentName.ROOT_CAUSE_QUALITY: "root_cause_quality_score",
        QualityComponentName.IMPACT_QUALITY: "impact_quality_score",
        QualityComponentName.FIX_QUALITY: "fix_quality_score",
    }
    weights = {
        QualityComponentName.CORRECTNESS: config.correctness,
        QualityComponentName.POC_QUALITY: config.poc_quality,
        QualityComponentName.ROOT_CAUSE_QUALITY: config.root_cause,
        QualityComponentName.IMPACT_QUALITY: config.impact,
        QualityComponentName.FIX_QUALITY: config.fix,
    }
    existing_by_name = (
        {item.component_name: item for item in existing.components.ordered()}
        if existing is not None
        else {}
    )
    built: dict[str, QualityComponentAssessment] = {}
    for name, input_field in fields.items():
        supplied_score = getattr(request, input_field)
        preserved = existing_by_name.get(name)
        if (
            supplied_score is None
            and preserved is not None
            and preserved.assessment_source == QualityAssessmentSource.VALIDATOR_ASSESSMENT
        ):
            supplied_score = preserved.score
            reasons = preserved.reason_codes
            references = preserved.evidence_references
            source = QualityAssessmentSource.VALIDATOR_ASSESSMENT
        elif supplied_score is not None:
            reasons = request.reason_codes.get(name, []) + ["validator_supplied_score"]
            references = request.evidence_references.get(name, []) + _default_references(
                name, cluster, member, validation, reproduction, finding
            )
            source = QualityAssessmentSource.VALIDATOR_ASSESSMENT
        else:
            supplied_score, source, reasons, references = evaluated[name]
        weight = weights[name]
        if supplied_score is None:
            component = QualityComponentAssessment(
                component_name=name,
                score=None,
                weight=weight,
                weighted_value=None,
                assessment_source=QualityAssessmentSource.NOT_ASSESSED,
                reason_codes=reasons,
                evidence_references=references,
                state=QualityComponentState.NOT_ASSESSED,
                assessment_policy_version=policy,
            )
        else:
            component = QualityComponentAssessment(
                component_name=name,
                score=supplied_score,
                weight=weight,
                weighted_value=(supplied_score * weight).quantize(
                    REPORT_QUALITY_QUANTUM
                ),
                assessment_source=source,
                reason_codes=reasons,
                evidence_references=references,
                state=QualityComponentState.ASSESSED,
                assessment_policy_version=policy,
            )
        built[name.value] = component
    return ReportQualityComponentAssessments(**built)


def _default_references(
    name: QualityComponentName,
    cluster: FindingCluster,
    member: FindingClusterMember,
    validation: ValidationDecision,
    reproduction: ReproductionResult | None,
    finding: Finding,
) -> list[str]:
    common = [f"finding:{finding.finding_id}", f"cluster:{cluster.finding_cluster_id}"]
    if name == QualityComponentName.CORRECTNESS:
        return common + [f"validation:{validation.validation_id}", f"cluster-member:{member.source_fingerprint}"]
    if name == QualityComponentName.POC_QUALITY and reproduction is not None:
        return common + [f"reproduction:{reproduction.reproduction_id}"]
    if name == QualityComponentName.IMPACT_QUALITY:
        return common + [f"validation:{validation.validation_id}", f"accepted-severity:{cluster.final_severity.value}"]
    return common


def _load_sources(
    protocol_data_root: Path,
    project_workspace: Path,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    submission_id: str,
) -> tuple[
    FindingCluster,
    FindingClusterMember,
    SubmissionRecord,
    Finding,
    ValidationDecision,
    ReproductionResult | None,
]:
    for value, label in (
        (project_id, "project"),
        (routing_id, "routing"),
        (finding_cluster_id, "finding cluster"),
        (submission_id, "submission"),
    ):
        _validate_identifier(value, label)
    try:
        cluster = load_finding_cluster(
            protocol_data_root, project_id, routing_id, finding_cluster_id
        )
    except FindingClusterProjectMismatchError as exc:
        raise ReportQualityLinkageError(str(exc)) from exc
    if cluster is None:
        raise ReportQualityNotFoundError("Finding cluster not found")
    members = [item for item in cluster.members if item.submission_id == submission_id]
    if len(members) != 1:
        raise ReportQualityLinkageError("Submission does not belong to the finding cluster")
    member = members[0]
    submission = load_submission(protocol_data_root, submission_id)
    if submission is None:
        raise ReportQualityNotFoundError("Submission not found")
    finding = load_project_finding(project_workspace, member.finding_id)
    if finding is None:
        raise ReportQualityNotFoundError("Finding not found")
    validation = load_validation_decision(project_workspace, member.finding_id)
    if validation is None:
        raise ReportQualityNotFoundError("Validation decision not found")
    reproduction = load_reproduction_result(project_workspace, member.finding_id)
    return cluster, member, submission, finding, validation, reproduction


def _require_eligible(
    protocol_data_root: Path,
    cluster: FindingCluster,
    member: FindingClusterMember,
    submission: SubmissionRecord,
    finding: Finding,
    validation: ValidationDecision,
    reproduction: ReproductionResult | None,
) -> None:
    if not has_resolved_accepted_cluster_truth(cluster):
        raise ReportQualityEligibilityError(
            "Report quality payout assessment requires resolved accepted cluster truth"
        )
    if (
        cluster.project_id != submission.project_id
        or cluster.routing_id != submission.routing_id
        or member.finding_id != submission.finding_id
        or member.node_id != submission.node_id
        or finding.project_id != cluster.project_id
        or validation.project_id != cluster.project_id
        or validation.finding_id != finding.finding_id
    ):
        raise ReportQualityLinkageError("Quality assessment source identities do not match")
    if submission.status not in ELIGIBLE_SUBMISSION_STATUSES:
        raise ReportQualityEligibilityError("Submission status is not quality-assessment eligible")
    if validation.status != ValidationStatus.ACCEPTED:
        raise ReportQualityEligibilityError("Only accepted validations can be finalized")
    if validation.evidence.in_scope is not True:
        raise ReportQualityEligibilityError("Validation must explicitly confirm scope")
    if reproduction is None or reproduction.status != ReproductionStatus.REPRODUCED:
        raise ReportQualityEligibilityError("Successful reproduction is required")
    if member.validation_id != validation.validation_id:
        raise ReportQualityLinkageError("Cluster member validation reference changed")
    if member.reproduction_id != reproduction.reproduction_id:
        raise ReportQualityLinkageError("Cluster member reproduction reference changed")
    node = load_node(protocol_data_root, submission.node_id)
    if node is None or node.operator_id != member.operator_id:
        raise ReportQualityLinkageError("Operator identity does not match the node registry")
def _record_from_sources(
    assessment_id: str,
    version: int,
    created_at: datetime,
    supersedes_id: str | None,
    status: ReportQualityAssessmentStatus,
    policy: str,
    configuration_version: str,
    cluster: FindingCluster,
    member: FindingClusterMember,
    validation: ValidationDecision,
    reproduction: ReproductionResult | None,
    components: ReportQualityComponentAssessments,
    quality_score: Decimal | None,
    validation_fingerprint: str,
    reproduction_fingerprint: str | None,
    severity_fingerprint: str,
    finding_fingerprint: str,
    source_fingerprint: str,
) -> ReportQualityAssessment:
    now = _utc_now()
    assessed_sources = {
        item.assessment_source
        for item in components.ordered()
        if item.state == QualityComponentState.ASSESSED
    }
    validator_count = sum(
        item.assessment_source == QualityAssessmentSource.VALIDATOR_ASSESSMENT
        for item in components.ordered()
        if item.state == QualityComponentState.ASSESSED
    )
    assessed_count = sum(
        item.state == QualityComponentState.ASSESSED for item in components.ordered()
    )
    if assessed_count and validator_count == assessed_count and len(assessed_sources) == 1:
        method = ReportQualityAssessmentMethod.VALIDATOR_SUPPLIED
    elif validator_count:
        method = ReportQualityAssessmentMethod.HYBRID
    else:
        method = ReportQualityAssessmentMethod.DETERMINISTIC
    return ReportQualityAssessment(
        report_quality_assessment_id=assessment_id,
        assessment_version=version,
        assessment_policy_version=policy,
        configuration_version=configuration_version,
        project_id=cluster.project_id,
        routing_id=cluster.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        submission_id=member.submission_id,
        finding_id=member.finding_id,
        node_id=member.node_id,
        operator_id=member.operator_id,
        submitted_at=member.submitted_at,
        validation_id=validation.validation_id,
        reproduction_id=reproduction.reproduction_id if reproduction else None,
        final_severity=cluster.final_severity.value,
        components=components,
        quality_score=quality_score,
        assessment_status=status,
        assessment_method=method,
        cluster_source_fingerprint=cluster.source_fingerprint,
        member_source_fingerprint=member.source_fingerprint,
        validation_source_fingerprint=validation_fingerprint,
        reproduction_source_fingerprint=reproduction_fingerprint,
        severity_source_fingerprint=severity_fingerprint,
        finding_source_fingerprint=finding_fingerprint,
        source_fingerprint=source_fingerprint,
        supersedes_assessment_id=supersedes_id,
        superseded_by_assessment_id=None,
        created_at=created_at,
        updated_at=now,
        finalized_at=now if status == ReportQualityAssessmentStatus.FINALIZED else None,
    )


def _write_assessment_and_index(
    protocol_data_root: Path, assessment: ReportQualityAssessment
) -> None:
    _write_assessment_file(protocol_data_root, assessment, replace=True)
    records = _load_all_for_submission(
        protocol_data_root, assessment.routing_id, assessment.submission_id
    )
    active = {
        item.assessment_policy_version: item.report_quality_assessment_id
        for item in records
        if item.assessment_status != ReportQualityAssessmentStatus.SUPERSEDED
    }
    payload = {
        "schema_version": REPORT_QUALITY_SCHEMA_VERSION,
        "project_id": assessment.project_id,
        "routing_id": assessment.routing_id,
        "submission_id": assessment.submission_id,
        "assessment_ids": [item.report_quality_assessment_id for item in records],
        "active_by_policy": active,
        "source_fingerprint": protocol_fingerprint(
            [
                {
                    "assessment_id": item.report_quality_assessment_id,
                    "assessment_version": item.assessment_version,
                    "policy_version": item.assessment_policy_version,
                    "status": item.assessment_status.value,
                    "source_fingerprint": item.source_fingerprint,
                }
                for item in records
            ]
        ),
    }
    atomic_write_json(
        get_submission_quality_root(
            protocol_data_root, assessment.routing_id, assessment.submission_id
        )
        / INDEX_FILENAME,
        payload,
        temporary_prefix=".report-quality-index-",
    )


def _write_assessment_file(
    protocol_data_root: Path,
    assessment: ReportQualityAssessment,
    *,
    replace: bool,
) -> None:
    path = get_report_quality_assessment_path(
        protocol_data_root,
        assessment.routing_id,
        assessment.submission_id,
        assessment.assessment_version,
    )
    try:
        if replace:
            atomic_write_json(path, assessment, temporary_prefix=".report-quality-")
        elif not atomic_create_json(
            path, assessment, temporary_prefix=".report-quality-create-"
        ):
            raise ReportQualityConflictError("Assessment was created concurrently")
    except OSError as exc:
        raise ReportQualityStorageError("Unable to persist report quality assessment") from exc


def _load_all_for_submission(
    protocol_data_root: Path, routing_id: str, submission_id: str
) -> list[ReportQualityAssessment]:
    root = get_submission_quality_root(protocol_data_root, routing_id, submission_id)
    if not root.exists():
        return []
    records: list[ReportQualityAssessment] = []
    for path in sorted(root.glob(f"{ASSESSMENT_FILENAME_PREFIX}*.json")):
        if not path.is_file():
            raise ReportQualityStorageError("Stored quality assessment is not a file")
        try:
            record = ReportQualityAssessment.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
            raise ReportQualityStorageError("Stored quality assessment is malformed") from exc
        if record.routing_id != routing_id or record.submission_id != submission_id:
            raise ReportQualityStorageError("Stored quality assessment path identity differs")
        records.append(record)
    return sorted(records, key=lambda item: item.assessment_version)


def _load_index(
    protocol_data_root: Path, routing_id: str, submission_id: str
) -> dict[str, Any] | None:
    path = get_submission_quality_root(protocol_data_root, routing_id, submission_id) / INDEX_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportQualityStorageError("Stored quality index is malformed") from exc
    if not isinstance(payload, dict):
        raise ReportQualityStorageError("Stored quality index is malformed")
    return payload


def _next_assessment_version(
    protocol_data_root: Path, routing_id: str, submission_id: str
) -> int:
    records = _load_all_for_submission(protocol_data_root, routing_id, submission_id)
    return max((item.assessment_version for item in records), default=0) + 1


def _assessment_id(
    project_id: str,
    routing_id: str,
    submission_id: str,
    policy_version: str,
    version: int,
) -> str:
    digest = protocol_fingerprint(
        {
            "schema_version": REPORT_QUALITY_SCHEMA_VERSION,
            "project_id": project_id,
            "routing_id": routing_id,
            "submission_id": submission_id,
            "policy_version": policy_version,
            "assessment_version": version,
        }
    )
    return f"report_quality_assessment_{digest[:32]}"


def _validation_source_fingerprint(validation: ValidationDecision) -> str:
    return protocol_fingerprint(
        validation.model_dump(exclude={"created_at", "updated_at"})
    )


def _reproduction_source_fingerprint(reproduction: ReproductionResult) -> str:
    return protocol_fingerprint(
        reproduction.model_dump(exclude={"created_at", "updated_at"})
    )


def _finding_source_fingerprint(finding: Finding) -> str:
    return protocol_fingerprint(
        {
            "finding_id": finding.finding_id,
            "project_id": finding.project_id,
            "title": finding.title,
            "category": finding.category,
            "reported_severity": finding.severity,
            "contracts": sorted(set(finding.contracts)),
            "functions": sorted(set(finding.functions)),
            "root_cause": finding.root_cause,
            "attack_path": finding.attack_path,
            "impact": finding.impact,
            "conditions": finding.conditions,
            "reproduction_steps": finding.reproduction_steps,
            "poc_type": finding.poc_type,
            "poc_file": finding.poc_file,
            "recommended_fix": finding.recommended_fix,
        }
    )


def _meaningful(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.strip().split())
    return len(normalized) >= 10 and normalized.lower() not in MEANINGLESS_TEXT


def _validate_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not SAFE_IDENTIFIER.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
    ):
        raise ReportQualityLinkageError(f"Invalid {label} identifier")


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReportQualityLinkageError("Invalid report quality storage path") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
