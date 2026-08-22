import json
from copy import deepcopy

import pytest

from app.schemas.node import NodeCreate
from app.schemas.submission import SubmissionCreate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.finding_cluster_service import (
    build_finding_cluster_id,
    find_cluster_by_submission,
    get_cluster_path,
    list_task_finding_clusters,
    rebuild_finding_clusters_for_task,
)
from app.services.node_registry_service import create_node, load_node, save_node
from app.services.submission_service import (
    DuplicateSubmissionError,
    create_submission,
    load_submission,
)
from app.services.validation_service import (
    load_validation_decision,
    save_validation_decision,
)
from tests.test_subnet_reward_allocation_service import _setup


def _mark_independent(workspace, finding_id, canonical_finding_id):
    validation = load_validation_decision(workspace, finding_id)
    changed = validation.model_copy(
        update={
            "status": ValidationStatus.ACCEPTED,
            "reason": "Valid independent root-cause report.",
            "evidence": ValidationEvidence(
                **{
                    **validation.evidence.model_dump(),
                    "is_duplicate": True,
                    "duplicate_of": canonical_finding_id,
                    "duplicate_kind": "independent_root_cause",
                    "is_valid_duplicate": True,
                    "canonical_finding_id": canonical_finding_id,
                }
            ),
        }
    )
    return save_validation_decision(changed, workspace)


def _snapshot_sources(root, workspace, submission_id, finding_id):
    return {
        "submission": deepcopy(load_submission(root, submission_id)),
        "validation": deepcopy(load_validation_decision(workspace, finding_id)),
        "finding": (workspace / "findings" / "findings.json").read_bytes(),
        "reproduction": (
            workspace / "reproductions" / finding_id / "reproduction.json"
        ).read_bytes(),
    }


def test_one_accepted_finding_builds_one_idempotent_cluster(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path)
    submission = submissions[0][0]
    before = _snapshot_sources(root, workspace, submission.submission_id, submission.finding_id)

    first = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    second = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert first.total_clusters == 1
    assert first.clusters[0].report_count == 1
    assert first.clusters[0].distinct_operator_count == 1
    assert second.unchanged_clusters == 1
    assert second.clusters == first.clusters
    assert list_task_finding_clusters(root, "project-1", routing.routing_id) == first.clusters
    assert find_cluster_by_submission(root, submission.submission_id) == first.clusters[0]
    assert _snapshot_sources(root, workspace, submission.submission_id, submission.finding_id) == before
    assert not (root / "rewards" / "events").exists()


def test_independent_duplicate_groups_and_operator_count_uses_registry(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path, include_candidate=True)
    first_submission = submissions[0][0]
    second_submission = submissions[1][0]
    _mark_independent(
        workspace, second_submission.finding_id, first_submission.finding_id
    )
    result = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert result.total_clusters == 1
    cluster = result.clusters[0]
    assert cluster.report_count == 2
    assert cluster.distinct_operator_count == 2
    assert {member.relation.value for member in cluster.members} == {
        "canonical",
        "independent_duplicate",
    }

    second_node = load_node(root, second_submission.node_id)
    save_node(
        root,
        second_node.model_copy(
            update={"operator_id": load_node(root, first_submission.node_id).operator_id}
        ),
    )
    rebuilt = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert rebuilt.clusters[0].report_count == 2
    assert rebuilt.clusters[0].distinct_operator_count == 1
    assert rebuilt.clusters[0].source_fingerprint != cluster.source_fingerprint


def test_rejected_and_insufficient_evidence_do_not_enter_clusters(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path, include_candidate=True)
    rejected = submissions[1][0]
    validation = load_validation_decision(workspace, rejected.finding_id)
    save_validation_decision(
        validation.model_copy(update={"status": ValidationStatus.REJECTED}),
        workspace,
    )
    result = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert result.total_clusters == 1
    assert result.clusters[0].report_count == 1

    accepted = submissions[0][0]
    validation = load_validation_decision(workspace, accepted.finding_id)
    save_validation_decision(
        validation.model_copy(
            update={"status": ValidationStatus.INSUFFICIENT_EVIDENCE}
        ),
        workspace,
    )
    assert rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).total_clusters == 0


def test_same_node_same_payload_retry_is_blocked_and_never_counted(tmp_path):
    root, workspace, _, routing, submissions = _setup(tmp_path)
    original = submissions[0][0]
    with pytest.raises(DuplicateSubmissionError):
        create_submission(
            root,
            workspace,
            SubmissionCreate(
                project_id="project-1",
                finding_id=original.finding_id,
                node_id=original.node_id,
                routing_id=routing.routing_id,
                routing_assignment_id=original.routing_assignment_id,
            ),
        )
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    assert cluster.report_count == 1
    assert cluster.distinct_operator_count == 1


def test_cluster_storage_is_task_scoped_human_readable_and_safe(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    cluster = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    ).clusters[0]
    path = get_cluster_path(root, routing.routing_id, cluster.finding_cluster_id)
    assert path == (
        root
        / "finding-clusters"
        / "tasks"
        / routing.routing_id
        / cluster.finding_cluster_id
        / "cluster.json"
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["finding_cluster_id"] == cluster.finding_cluster_id
    assert "/home/" not in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))


def test_cluster_identity_is_project_task_category_scoped_not_reporter_scoped():
    root_fingerprint = "f" * 64
    base = build_finding_cluster_id(
        "project-1", "routing-1", "access_control", root_fingerprint
    )
    assert base == build_finding_cluster_id(
        "project-1", "routing-1", "access_control", root_fingerprint
    )
    assert base != build_finding_cluster_id(
        "project-2", "routing-1", "access_control", root_fingerprint
    )
    assert base != build_finding_cluster_id(
        "project-1", "routing-2", "access_control", root_fingerprint
    )
    assert base != build_finding_cluster_id(
        "project-1", "routing-1", "reentrancy", root_fingerprint
    )
