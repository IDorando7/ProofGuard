from app.core.paths import protocol_data_root
from app.main import app
from tests.test_validator_committee_routes import _context


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _finalized(client):
    _, routing, cluster_id, base = _context(client)
    committee = client.post(
        f"{base}/finding-clusters/{cluster_id}/validator-committees", json={}
    ).json()
    finalized = client.post(
        f"{base}/validator-committees/{committee['validator_committee_id']}/finalize",
        json={},
    )
    assert finalized.status_code == 200
    assignments = client.get(
        f"{base}/validator-committees/{committee['validator_committee_id']}/assignments"
    ).json()["assignments"]
    return routing, base, committee, assignments


def test_minimal_execution_api_derives_all_provenance_and_exposes_readiness(client):
    routing, base, committee, assignments = _finalized(client)
    assignment = assignments[0]
    endpoint = (
        f"{base}/validator-assignments/{assignment['validator_assignment_id']}/reproduction"
    )
    response = client.post(
        endpoint,
        json={"validator_node_id": assignment["validator_node_id"]},
    )
    assert response.status_code == 200
    record = response.json()
    assert record["validator_committee_id"] == committee["validator_committee_id"]
    assert record["assignment_role"] == "authoritative"
    assert record["reproduction_mode"] == "submitted_poc"
    assert record["reproduction_status"] == "unsupported"
    assert record["underlying_reproduction_request_id"]
    assert record["underlying_reproduction_result_storage_ref"].startswith(
        "reproductions/validator/"
    )
    assert client.get(endpoint).json() == record
    listed = client.get(
        f"{base}/validator-committees/{committee['validator_committee_id']}/reproductions"
    )
    assert listed.status_code == 200
    assert listed.json()["reproductions"] == [record]
    readiness = client.get(
        f"{base}/validator-committees/{committee['validator_committee_id']}/reproduction-readiness"
    )
    assert readiness.status_code == 200
    assert readiness.json()["authoritative_terminal_count"] == 1
    assert readiness.json()["authoritative_target"] == 5
    assert readiness.json()["status"] == "in_progress"
    assert not (_root() / "validator-consensus").exists()
    assert not (_root() / "validator-rewards").exists()


def test_execution_api_is_idempotent_and_rejects_derived_fields(client):
    _, base, _, assignments = _finalized(client)
    assignment = assignments[0]
    endpoint = (
        f"{base}/validator-assignments/{assignment['validator_assignment_id']}/reproduction"
    )
    payload = {"validator_node_id": assignment["validator_node_id"]}
    first = client.post(endpoint, json=payload)
    repeated = client.post(endpoint, json=payload)
    assert first.status_code == repeated.status_code == 200
    assert repeated.json() == first.json()
    for forged in (
        {"reproduction_status": "reproduced"},
        {"operator_id": "forged"},
        {"source_revision": "forged"},
        {"environment_fingerprint": "0" * 64},
        {"poc_file": "/tmp/host-file.t.sol"},
        {"consensus_result": "accepted"},
    ):
        assert client.post(endpoint, json={**payload, **forged}).status_code == 422


def test_public_independent_mode_cannot_upload_or_select_host_artifact(client):
    _, base, _, assignments = _finalized(client)
    assignment = assignments[1]
    endpoint = (
        f"{base}/validator-assignments/{assignment['validator_assignment_id']}/reproduction"
    )
    response = client.post(
        endpoint,
        json={
            "validator_node_id": assignment["validator_node_id"],
            "reproduction_mode": "independent_reproduction",
        },
    )
    assert response.status_code == 409
    assert "not public" in response.json()["detail"]
    assert client.post(
        endpoint,
        json={
            "validator_node_id": assignment["validator_node_id"],
            "reproduction_mode": "independent_reproduction",
            "poc_file": "/home/validator/repro.t.sol",
        },
    ).status_code == 422


def test_committee_assignment_rejects_legacy_result_adoption_api(client):
    _, base, _, assignments = _finalized(client)
    assignment = assignments[0]
    response = client.post(
        f"{base}/validator-assignments/{assignment['validator_assignment_id']}/reproduction",
        json={
            "validator_node_id": assignment["validator_node_id"],
            "reproduction_mode": "submitted_poc",
            "underlying_reproduction_result_id": "reporter-result",
        },
    )
    assert response.status_code == 409
    assert "independently executed" in response.json()["detail"]
