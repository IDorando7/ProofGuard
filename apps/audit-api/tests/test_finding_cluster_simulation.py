import json

import pytest

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionStatusUpdate,
)
from app.schemas.subnet import SubnetCreate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.finding_cluster_service import rebuild_finding_clusters_for_task
from app.services.node_registry_service import create_node, save_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    save_reproduction_result,
)
from app.schemas.reproduction import ReproductionStatus
from app.services.submission_service import (
    DuplicateSubmissionError,
    create_submission,
    update_submission_status,
)
from app.services.subnet_registry_service import create_subnet
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
)
from app.services.validation_service import create_validation_decision
from tests.test_subnet_router_service import _member, _workspace


def _finding(finding_id, root_index, report_index, status="accepted"):
    suffix = f"root {root_index} report {report_index} {status}"
    return Finding(
        finding_id=finding_id,
        project_id="project-1",
        title=f"Access control vulnerability {suffix}",
        category="access_control",
        severity="Critical" if report_index % 2 else "Low",
        confidence=0.9,
        contracts=[f"src/Vault{root_index}.sol"],
        functions=[f"withdraw{root_index}"],
        root_cause=f"Missing authorization check for {suffix}",
        attack_path=f"Attacker invokes unrestricted operation for {suffix}",
        impact=f"Unauthorized asset transfer occurs for {suffix}",
        recommended_fix="Require a validated role before the state transition.",
        agent_name="simulation_agent",
    )


def _persist_findings(workspace, findings):
    path = workspace / "findings" / "findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([item.model_dump(mode="json") for item in findings], indent=2),
        encoding="utf-8",
    )


def _persist_source(root, workspace, routing, assignment, finding, status, canonical=None):
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
    reproduction = save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.REPRODUCED}),
        workspace,
    )
    evidence_values = {
        "has_finding": True,
        "has_reproduction": True,
        "reproduction_status": "reproduced",
        "in_scope": status != ValidationStatus.OUT_OF_SCOPE,
        "is_duplicate": canonical is not None,
        "original_severity": finding.severity.value,
        # Validator-approved severity deliberately disagrees with some reporters.
        "normalized_severity": "High",
    }
    if canonical is not None:
        evidence_values.update(
            {
                "duplicate_of": canonical,
                "duplicate_kind": "independent_root_cause",
                "is_valid_duplicate": True,
                "canonical_finding_id": canonical,
            }
        )
    validation = create_validation_decision(
        "project-1",
        finding.finding_id,
        workspace,
        status=status,
        reason=f"Simulation validation {status.value}.",
        evidence=ValidationEvidence(**evidence_values),
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
    target = {
        ValidationStatus.ACCEPTED: "accepted",
        ValidationStatus.REJECTED: "rejected",
        ValidationStatus.OUT_OF_SCOPE: "out_of_scope",
    }[status]
    return update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status=target,
            reason="Simulation finalized.",
            validation_id=validation.validation_id,
        ),
    )


def test_large_deterministic_root_cause_clustering_simulation(tmp_path):
    root = tmp_path / "protocol"
    workspace = _workspace(tmp_path)
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    nodes = []
    for index in range(32):
        node = create_node(
            root,
            NodeCreate(
                node_type="agent",
                display_name=f"simulation-node-{index:02d}",
                operator_id=f"operator-{index % 20:02d}",
                supported_categories=["access_control"],
            ),
        )
        _member(root, subnet, node, "active", [90] * 6)
        nodes.append(node)
    routed = calculate_project_routing(
        root,
        workspace,
        "project-1",
        ProjectRoutingRequest(
            categories=["access_control"],
            nodes_per_category=32,
            include_exploration=False,
        ),
    )
    routing = finalize_project_routing(
        root, workspace, "project-1", routed.record.routing_id
    ).record
    assignments = routing.results[0].assignments
    by_node = {assignment.node_id: assignment for assignment in assignments}
    assert len(assignments) == 32

    valid_findings = []
    invalid_findings = []
    for root_index in range(8):
        for report_index in range(7):
            valid_findings.append(
                _finding(
                    f"finding-root-{root_index}-report-{report_index}",
                    root_index,
                    report_index,
                )
            )
        invalid_findings.extend(
            [
                _finding(f"finding-root-{root_index}-rejected", root_index, 90, "rejected"),
                _finding(f"finding-root-{root_index}-oos", root_index, 91, "oos"),
            ]
        )
    _persist_findings(workspace, [*valid_findings, *invalid_findings])

    expected_operators: dict[int, set[str]] = {index: set() for index in range(8)}
    submissions = []
    for root_index in range(8):
        canonical_id = f"finding-root-{root_index}-report-0"
        for report_index in range(7):
            finding = next(
                item
                for item in valid_findings
                if item.finding_id == f"finding-root-{root_index}-report-{report_index}"
            )
            node = nodes[(root_index * 7 + report_index) % len(nodes)]
            assignment = by_node[node.node_id]
            expected_operators[root_index].add(node.operator_id)
            submissions.append(
                _persist_source(
                    root,
                    workspace,
                    routing,
                    assignment,
                    finding,
                    ValidationStatus.ACCEPTED,
                    canonical=None if report_index == 0 else canonical_id,
                )
            )

        for suffix, status, offset in (
            ("rejected", ValidationStatus.REJECTED, 13),
            ("oos", ValidationStatus.OUT_OF_SCOPE, 17),
        ):
            finding = next(
                item
                for item in invalid_findings
                if item.finding_id == f"finding-root-{root_index}-{suffix}"
            )
            node = nodes[(root_index + offset) % len(nodes)]
            _persist_source(
                root,
                workspace,
                routing,
                by_node[node.node_id],
                finding,
                status,
            )

    for root_index in range(8):
        canonical = submissions[root_index * 7]
        with pytest.raises(DuplicateSubmissionError):
            create_submission(
                root,
                workspace,
                SubmissionCreate(
                    project_id="project-1",
                    finding_id=canonical.finding_id,
                    node_id=canonical.node_id,
                    routing_id=routing.routing_id,
                    routing_assignment_id=canonical.routing_assignment_id,
                ),
            )

    first = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    second = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert first.total_clusters == 8
    assert sum(cluster.report_count for cluster in first.clusters) == 56
    assert all(cluster.report_count == 7 for cluster in first.clusters)
    assert sorted(cluster.distinct_operator_count for cluster in first.clusters) == sorted(
        len(value) for value in expected_operators.values()
    )
    assert all(cluster.final_severity.value == "High" for cluster in first.clusters)
    assert all(
        cluster.canonical_finding_id.endswith("report-0")
        for cluster in first.clusters
    )
    assert first.clusters == second.clusters
    assert second.unchanged_clusters == 8
