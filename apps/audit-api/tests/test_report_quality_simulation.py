import json
from decimal import Decimal

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.report_quality import ReportQualityAssessmentRequest
from app.schemas.reproduction import ReproductionStatus
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.subnet import SubnetCreate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.finding_cluster_service import rebuild_finding_clusters_for_task
from app.services.node_registry_service import create_node, load_node, save_node
from app.services.report_quality_assessment_service import (
    assess_report_quality,
    rebuild_task_report_quality_assessments,
)
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    save_reproduction_result,
)
from app.services.submission_service import create_submission, update_submission_status
from app.services.subnet_registry_service import create_subnet
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
)
from app.services.validation_service import create_validation_decision
from tests.test_subnet_router_service import _member, _workspace


CATEGORIES = [
    "access_control",
    "reentrancy",
    "oracle_manipulation",
    "accounting",
    "upgradeability",
    "mev",
    "governance",
    "cross_chain",
]


def _append_finding(workspace, finding):
    path = workspace / "findings" / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    values.append(finding.model_dump(mode="json"))
    path.write_text(json.dumps(values, indent=2), encoding="utf-8")


def _accepted_report(root, workspace, routing, assignment, finding, canonical_id=None, index=0):
    _append_finding(workspace, finding)
    submission = create_submission(
        root,
        workspace,
        SubmissionCreate(
            project_id="project-1",
            finding_id=finding.finding_id,
            node_id=assignment.node_id,
            routing_id=routing.routing_id,
            routing_assignment_id=assignment.assignment_id,
        ),
    )
    reproduction = create_initial_reproduction_result(
        "project-1", finding.finding_id, workspace
    )
    reproduction_update = {"status": ReproductionStatus.REPRODUCED}
    if index % 3 == 0:
        reproduction_update.update(
            {
                "poc_file": f"test/{finding.finding_id}.t.sol",
                "test_name": f"test_{index}",
                "command": ["forge", "test"],
                "stdout": "1 passed",
            }
        )
    elif index % 3 == 1:
        reproduction_update["stdout"] = "partial reproduction output"
    reproduction = save_reproduction_result(
        reproduction.model_copy(update=reproduction_update), workspace
    )
    duplicate = canonical_id is not None
    validation = create_validation_decision(
        "project-1",
        finding.finding_id,
        workspace,
        status=ValidationStatus.ACCEPTED,
        reason="Accepted simulation report.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            in_scope=True,
            is_duplicate=duplicate,
            duplicate_of=canonical_id,
            duplicate_kind="independent_root_cause" if duplicate else None,
            is_valid_duplicate=True if duplicate else None,
            canonical_finding_id=canonical_id,
            original_severity="High",
            normalized_severity="High",
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Simulation validation pending.",
            reproduction_id=reproduction.reproduction_id,
        ),
    )
    return update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="accepted",
            reason="Simulation accepted.",
            validation_id=validation.validation_id,
        ),
    )


def test_large_report_quality_simulation_is_deterministic_and_side_effect_free(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path, categories=CATEGORIES)
    operator_ids = [f"operator-{index:02d}" for index in range(20)]
    assignments_by_category = {}
    node_index = 0
    for category in CATEGORIES:
        subnet = create_subnet(root, SubnetCreate(category=category))
        for local_index in range(4):
            node = create_node(
                root,
                NodeCreate(
                    node_type="agent",
                    display_name=f"Simulation {category} {local_index}",
                    operator_id=operator_ids[node_index % len(operator_ids)],
                    supported_categories=[category],
                ),
            )
            _member(root, subnet, node, "active", [90] * 6)
            node_index += 1
    routing = finalize_project_routing(
        root,
        workspace,
        "project-1",
        calculate_project_routing(
            root,
            workspace,
            "project-1",
            ProjectRoutingRequest(
                categories=CATEGORIES,
                nodes_per_category=4,
                include_exploration=False,
            ),
        ).record.routing_id,
    ).record
    for result in routing.results:
        assignments_by_category[result.category.value] = result.assignments

    submissions = []
    global_index = 0
    for category_index, category in enumerate(CATEGORIES):
        report_total = 7 if category_index < 2 else 6
        canonical_id = f"quality-sim-{category_index}-0"
        for report_index in range(report_total):
            finding_id = f"quality-sim-{category_index}-{report_index}"
            finding = Finding(
                finding_id=finding_id,
                project_id="project-1",
                title=f"Simulation vulnerability {category_index} {report_index}",
                category=category,
                severity="Critical" if report_index % 4 == 0 else "High",
                confidence=0.9,
                contracts=[f"src/Module{category_index}.sol"],
                functions=[f"execute{category_index}"],
                root_cause=f"Missing invariant check for root cause {category_index}",
                attack_path=(
                    f"Attacker triggers vulnerable transition {category_index} "
                    f"using report path {report_index}"
                ),
                impact=(
                    f"Assets controlled by module {category_index} can be lost "
                    f"under report condition {report_index}"
                ),
                reproduction_steps=["Run the isolated targeted test"],
                poc_type="foundry",
                recommended_fix=f"Enforce invariant before transition {category_index}",
                agent_name="quality_simulation_agent",
            )
            assignment = assignments_by_category[category][report_index % 4]
            submissions.append(
                _accepted_report(
                    root,
                    workspace,
                    routing,
                    assignment,
                    finding,
                    None if report_index == 0 else canonical_id,
                    global_index,
                )
            )
            global_index += 1

    clusters = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters
    assert len(clusters) == 8
    assert sum(cluster.report_count for cluster in clusters) == 50
    assert len({submission.node_id for submission in submissions}) == 32
    assert len({load_node(root, submission.node_id).operator_id for submission in submissions}) == 20

    reputations_before = {
        submission.node_id: load_node(root, submission.node_id).reputation_score
        for submission in submissions
    }
    first_scores = {}
    for cluster in clusters:
        for member in cluster.members:
            report_number = int(member.finding_id.rsplit("-", 1)[1])
            incomplete = len(first_scores) % 10 == 0
            request = ReportQualityAssessmentRequest(
                poc_quality_score=(
                    Decimal("0.650000") + Decimal(report_number % 4) * Decimal("0.100000")
                ),
                root_cause_quality_score=(
                    Decimal("0.700000") + Decimal(report_number % 3) * Decimal("0.100000")
                ),
                impact_quality_score=(
                    Decimal("0.600000") + Decimal(report_number % 4) * Decimal("0.100000")
                ),
                fix_quality_score=None if incomplete else (
                    Decimal("0.550000") + Decimal(report_number % 5) * Decimal("0.100000")
                ),
            )
            assessment, _ = assess_report_quality(
                root,
                workspace,
                "project-1",
                routing.routing_id,
                cluster.finding_cluster_id,
                member.submission_id,
                request,
            )
            first_scores[member.submission_id] = assessment.quality_score

    assert len(first_scores) == 50
    assert sum(value is None for value in first_scores.values()) == 5
    assert all(value is None or Decimal("0") <= value <= Decimal("1") for value in first_scores.values())
    rebuilt = rebuild_task_report_quality_assessments(
        root, workspace, "project-1", routing.routing_id
    )
    second_scores = {
        assessment.submission_id: assessment.quality_score
        for assessment in rebuilt.assessments
    }
    assert second_scores == first_scores
    assert rebuilt.draft_assessments == 5
    assert rebuilt.finalized_assessments == 45
    assert {
        submission.node_id: load_node(root, submission.node_id).reputation_score
        for submission in submissions
    } == reputations_before
    assert not (root / "rewards" / "events").exists()
    assert not (root / "task-rewards" / "events").exists()
    assert not (root / "top-k").exists()
    assert not (root / "chief-finder").exists()
