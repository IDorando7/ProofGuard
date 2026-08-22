from app.core.paths import protocol_data_root
from app.main import app
from app.schemas.node import NodeCreate
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    load_finding_cluster,
)
from app.services.node_registry_service import create_node
from app.services.project_service import project_workspace
from app.services.reproduction_service import load_reproduction_result
from app.services.validator_assignment_service import create_validator_assignment
from tests.test_project_creation import _create_project
from tests.test_subnet_reward_routes import _setup


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _route_context(client):
    project_id, _, _, routing, submission = _setup(client)
    root = _root()
    rebuilt = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/rebuild",
        json={},
    )
    assert rebuilt.status_code == 200
    cluster_id = rebuilt.json()["clusters"][0]["finding_cluster_id"]
    finalize_finding_clusters_for_task(root, project_id, routing.routing_id)
    cluster = load_finding_cluster(root, project_id, routing.routing_id, cluster_id)
    validator = create_node(
        root,
        NodeCreate(
            node_type="validator",
            display_name="Independent Validator",
            operator_id="independent-validator-operator",
            supported_categories=[],
        ),
    )
    assignment = create_validator_assignment(
        root,
        project_id=project_id,
        routing_id=routing.routing_id,
        finding_cluster_id=cluster_id,
        validator_node_id=validator.node_id,
    )
    result = load_reproduction_result(
        project_workspace(project_id), submission.finding_id
    )
    assert result is not None
    return project_id, routing, cluster, validator, assignment, result


def _reproduction_endpoint(project_id, routing_id, assignment_id):
    return (
        f"/projects/{project_id}/routing/{routing_id}/validator-assignments/"
        f"{assignment_id}/reproduction"
    )


def _attestation_endpoint(project_id, routing_id, assignment_id):
    return (
        f"/projects/{project_id}/routing/{routing_id}/validator-assignments/"
        f"{assignment_id}/attestations"
    )


def _attestation_payload(validator_node_id, validator_reproduction_id):
    return {
        "validator_node_id": validator_node_id,
        "validator_reproduction_id": validator_reproduction_id,
        "validity_decision": "accepted",
        "root_cause_decision": "confirmed",
        "normalized_severity": "High",
        "impact_decision": "validated",
        "reason_codes": [
            "root_cause_confirmed",
            "reproduction_confirmed",
            "impact_confirmed",
        ],
        "evidence_references": [],
    }


def test_assignment_reproduction_attestation_read_and_list_api(client):
    project_id, routing, cluster, validator, assignment, result = _route_context(client)
    base = f"/projects/{project_id}/routing/{routing.routing_id}"
    read_assignment = client.get(
        f"{base}/validator-assignments/{assignment.validator_assignment_id}"
    )
    assert read_assignment.status_code == 200
    assert read_assignment.json()["validator_operator_id"] == validator.operator_id
    listed_assignments = client.get(
        f"{base}/finding-clusters/{cluster.finding_cluster_id}/validator-assignments"
    )
    assert listed_assignments.status_code == 200
    assert listed_assignments.json()["assignments"] == [read_assignment.json()]

    reproduction_response = client.post(
        _reproduction_endpoint(
            project_id, routing.routing_id, assignment.validator_assignment_id
        ),
        json={
            "validator_node_id": validator.node_id,
            "reproduction_mode": "submitted_poc",
            "underlying_reproduction_result_id": result.reproduction_id,
        },
    )
    assert reproduction_response.status_code == 200
    reproduction = reproduction_response.json()
    assert reproduction["reproduction_status"] == "reproduced"
    assert client.get(
        f"{base}/validator-reproductions/{reproduction['validator_reproduction_id']}"
    ).json() == reproduction

    payload = _attestation_payload(
        validator.node_id, reproduction["validator_reproduction_id"]
    )
    attestation_response = client.post(
        _attestation_endpoint(
            project_id, routing.routing_id, assignment.validator_assignment_id
        ),
        json=payload,
    )
    assert attestation_response.status_code == 200
    attestation = attestation_response.json()
    assert attestation["reproduction_decision"] == "reproduced"
    assert attestation["validator_operator_id"] == validator.operator_id
    assert client.get(
        f"{base}/attestations/{attestation['attestation_id']}"
    ).json() == attestation
    listed = client.get(
        f"{base}/finding-clusters/{cluster.finding_cluster_id}/attestations"
    )
    assert listed.status_code == 200
    assert listed.json()["attestations"] == [attestation]


def test_attestation_api_is_idempotent_and_conflict_is_409(client):
    project_id, routing, _, validator, assignment, result = _route_context(client)
    reproduction = client.post(
        _reproduction_endpoint(project_id, routing.routing_id, assignment.validator_assignment_id),
        json={
            "validator_node_id": validator.node_id,
            "reproduction_mode": "independent_reproduction",
            "underlying_reproduction_result_id": result.reproduction_id,
        },
    ).json()
    endpoint = _attestation_endpoint(
        project_id, routing.routing_id, assignment.validator_assignment_id
    )
    payload = _attestation_payload(
        validator.node_id, reproduction["validator_reproduction_id"]
    )
    first = client.post(endpoint, json=payload)
    repeated = client.post(endpoint, json=payload)
    assert first.status_code == repeated.status_code == 200
    assert repeated.json() == first.json()
    conflict = client.post(
        endpoint,
        json={
            **payload,
            "validity_decision": "insufficient_evidence",
            "root_cause_decision": "insufficient_evidence",
            "normalized_severity": None,
            "impact_decision": "insufficient_evidence",
        },
    )
    assert conflict.status_code == 409


def test_api_rejects_wrong_validator_and_derived_field_forgery(client):
    project_id, routing, _, validator, assignment, result = _route_context(client)
    reproduction_endpoint = _reproduction_endpoint(
        project_id, routing.routing_id, assignment.validator_assignment_id
    )
    base_reproduction = {
        "validator_node_id": validator.node_id,
        "reproduction_mode": "submitted_poc",
        "underlying_reproduction_result_id": result.reproduction_id,
    }
    wrong_node = client.post(
        reproduction_endpoint,
        json={**base_reproduction, "validator_node_id": "another-validator"},
    )
    assert wrong_node.status_code == 409
    for field, value in (
        ("reproduction_status", "reproduced"),
        ("validator_operator_id", "forged"),
        ("trusted", True),
        ("validator_override", True),
    ):
        assert client.post(
            reproduction_endpoint, json={**base_reproduction, field: value}
        ).status_code == 422

    reproduction = client.post(reproduction_endpoint, json=base_reproduction).json()
    endpoint = _attestation_endpoint(
        project_id, routing.routing_id, assignment.validator_assignment_id
    )
    payload = _attestation_payload(
        validator.node_id, reproduction["validator_reproduction_id"]
    )
    for field, value in (
        ("validator_operator_id", "forged"),
        ("reproduction_decision", "reproduced"),
        ("consensus_result", "accepted"),
        ("final_severity", "Critical"),
        ("reward_amount", "100"),
        ("private_key", "secret"),
    ):
        assert client.post(endpoint, json={**payload, field: value}).status_code == 422


def test_no_public_self_assignment_endpoint_exists(client):
    project_id, routing, cluster, validator, _, _ = _route_context(client)
    response = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/"
        f"{cluster.finding_cluster_id}/assign-me",
        json={"validator_node_id": validator.node_id},
    )
    assert response.status_code in {404, 405}


def test_cross_project_and_unknown_records_are_rejected(client):
    project_id, routing, _, _, assignment, _ = _route_context(client)
    other_project = _create_project(client).json()["project_id"]
    assert other_project != project_id
    cross = client.get(
        f"/projects/{other_project}/routing/{routing.routing_id}/validator-assignments/"
        f"{assignment.validator_assignment_id}"
    )
    assert cross.status_code == 409
    missing = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/validator-assignments/"
        f"validator_assignment_{'0' * 64}"
    )
    assert missing.status_code == 404
