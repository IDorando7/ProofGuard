import json

import pytest

from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.subnet import SubnetCreate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
)
from app.services.node_registry_service import create_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    save_reproduction_result,
)
from app.services.reputation_service import process_submission_reputation
from app.services.submission_service import create_submission, update_submission_status
from app.services.subnet_registry_service import create_subnet
from app.services.validation_service import create_validation_decision


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _node(root, name="route-node"):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control", "reentrancy"],
        ),
    )


def _finalized_source(root, node, suffix, category="access_control"):
    project_id = f"project-{suffix}"
    finding_id = f"finding-{suffix}"
    workspace = root.parent / "audits" / project_id
    finding = Finding(
        finding_id=finding_id,
        project_id=project_id,
        title="Missing authorization on protected operation",
        category=category,
        severity="High",
        confidence=0.9,
        contracts=["src/Protocol.sol"],
        functions=["operate"],
        root_cause="Authorization check is missing",
        attack_path="Unauthorized caller invokes operation",
        impact="Protected state can be modified",
        recommended_fix="Add an authorization check.",
        agent_name="category_agent",
    )
    findings_dir = workspace / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json")], indent=2),
        encoding="utf-8",
    )
    submission = create_submission(
        root,
        workspace,
        SubmissionCreate(
            project_id=project_id,
            finding_id=finding_id,
            node_id=node.node_id,
        ),
    )
    reproduction = create_initial_reproduction_result(
        project_id, finding_id, workspace
    )
    reproduction = save_reproduction_result(
        reproduction.model_copy(update={"status": ReproductionStatus.REPRODUCED}),
        workspace,
    )
    validation = create_validation_decision(
        project_id,
        finding_id,
        workspace,
        status=ValidationStatus.ACCEPTED,
        reason="Finalized route fixture.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            in_scope=True,
            is_duplicate=False,
            normalized_severity="High",
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Validation pending.",
            reproduction_id=reproduction.reproduction_id,
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="accepted",
            reason="Validation finalized.",
            validation_id=validation.validation_id,
        ),
    )
    calculate_contribution_for_submission(root, workspace, submission.submission_id)
    process_submission_reputation(root, workspace, submission.submission_id)


def test_rebuild_one_category_returns_zero_record_for_explicit_empty_request(client):
    node = _node(_root())
    response = client.post(
        f"/nodes/{node.node_id}/category-performance/access_control/rebuild"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["node_id"] == node.node_id
    assert body["category"] == "access_control"
    assert body["counts"]["total_finalized_submissions"] == 0
    assert body["contribution_stats"]["average_contribution_score"] == 0
    assert len(body["source_fingerprint"]) == 64


def test_node_and_global_rebuild_process_discovered_categories(client):
    root = _root()
    node = _node(root)
    _finalized_source(root, node, "access", "access_control")
    _finalized_source(root, node, "reentrancy", "reentrancy")

    node_response = client.post(
        f"/nodes/{node.node_id}/category-performance/rebuild"
    )
    assert node_response.status_code == 200
    assert [record["category"] for record in node_response.json()] == [
        "access_control",
        "reentrancy",
    ]
    assert all(
        record["counts"]["accepted_unique_submissions"] == 1
        for record in node_response.json()
    )

    global_response = client.post("/category-performance/rebuild")
    assert global_response.status_code == 200
    assert len(global_response.json()) == 2


def test_read_and_list_endpoints_support_filters(client):
    root = _root()
    node = _node(root)
    _finalized_source(root, node, "listed")
    rebuilt = client.post(
        f"/nodes/{node.node_id}/category-performance/access_control/rebuild"
    ).json()
    client.post(
        f"/nodes/{node.node_id}/category-performance/reentrancy/rebuild"
    )

    read = client.get(
        f"/nodes/{node.node_id}/category-performance/access_control"
    )
    assert read.status_code == 200
    assert read.json() == rebuilt
    assert len(client.get(f"/nodes/{node.node_id}/category-performance").json()) == 2
    assert len(client.get("/category-performance").json()) == 2
    assert len(
        client.get(f"/category-performance?node_id={node.node_id}").json()
    ) == 2
    assert len(
        client.get("/category-performance?category=access_control").json()
    ) == 1
    assert len(
        client.get(
            "/category-performance?minimum_finalized_submissions=1"
        ).json()
    ) == 1


def test_subnet_category_endpoint_filters_by_category_without_ranking(client):
    root = _root()
    node = _node(root)
    _finalized_source(root, node, "subnet-access")
    client.post(
        f"/nodes/{node.node_id}/category-performance/access_control/rebuild"
    )
    client.post(
        f"/nodes/{node.node_id}/category-performance/reentrancy/rebuild"
    )
    subnet = create_subnet(root, SubnetCreate(category="access_control"))

    response = client.get(
        f"/subnets/{subnet.subnet_id}/category-performance"
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["category"] == "access_control"
    assert "rank" not in response.json()[0]
    assert "category_score" not in response.json()[0]


def test_missing_and_invalid_resources_fail_clearly(client):
    node = _node(_root())
    assert client.get(
        f"/nodes/{node.node_id}/category-performance/access_control"
    ).status_code == 404
    assert client.post(
        "/nodes/00000000-0000-0000-0000-000000000000/"
        "category-performance/access_control/rebuild"
    ).status_code == 404
    assert client.post(
        f"/nodes/{node.node_id}/category-performance/not-a-category/rebuild"
    ).status_code == 400
    assert client.get(
        "/subnets/subnet_access_control/category-performance"
    ).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"counts": {"total_finalized_submissions": 100}},
        {"contribution_stats": {"average_contribution_score": 100}},
        {"source_event_ids": ["client-event"]},
        {"source_fingerprint": "a" * 64},
    ],
)
def test_rebuild_api_rejects_client_controlled_aggregate_fields(client, payload):
    node = _node(_root())
    response = client.post(
        f"/nodes/{node.node_id}/category-performance/access_control/rebuild",
        json=payload,
    )
    assert response.status_code == 422


def test_no_direct_editing_endpoint_and_no_sensitive_or_future_fields(client):
    node = _node(_root())
    endpoint = (
        f"/nodes/{node.node_id}/category-performance/access_control"
    )
    client.post(f"{endpoint}/rebuild")
    assert client.patch(endpoint, json={"counts": {}}).status_code == 405

    response = client.get(endpoint)
    assert response.status_code == 200
    text = response.text
    body = response.json()
    assert "/home/" not in text
    assert "data/protocol" not in text
    for field in (
        "private_key",
        "category_score",
        "rank",
        "membership_status",
        "routing_assignment",
        "token",
        "stake",
    ):
        assert field not in body
