from app.core.paths import protocol_data_root
from app.main import app
from tests.test_validator_committee_routes import _context


def _root():
    return app.dependency_overrides[protocol_data_root]()


def test_consensus_api_derives_no_quorum_finalizes_dispute_and_verifies(client):
    _, _, cluster_id, base = _context(client)
    committee = client.post(
        f"{base}/finding-clusters/{cluster_id}/validator-committees", json={}
    ).json()
    assert client.post(
        f"{base}/validator-committees/{committee['validator_committee_id']}/finalize",
        json={},
    ).status_code == 200
    created = client.post(
        f"{base}/finding-clusters/{cluster_id}/validation-consensus", json={}
    )
    assert created.status_code == 201
    consensus = created.json()
    assert consensus["consensus_outcome"] == "no_quorum"
    assert consensus["valid_authoritative_attestation_count"] == 0
    consensus_id = consensus["validation_consensus_id"]
    assert client.get(f"{base}/validation-consensus/{consensus_id}").status_code == 200
    finalized = client.post(
        f"{base}/validation-consensus/{consensus_id}/finalize", json={}
    )
    assert finalized.status_code == 200
    assert finalized.json()["lifecycle_status"] == "finalized"
    disputes = client.get(
        f"{base}/finding-clusters/{cluster_id}/validation-disputes"
    )
    assert disputes.status_code == 200
    assert disputes.json()["total"] == 1
    verified = client.get(f"{base}/validation-consensus/{consensus_id}/verify")
    assert verified.status_code == 200
    assert verified.json()["valid"] is True


def test_consensus_api_rejects_caller_supplied_counts_outcome_and_validator_ids(client):
    _, _, cluster_id, base = _context(client)
    endpoint = f"{base}/finding-clusters/{cluster_id}/validation-consensus"
    for payload in (
        {"accept_count": 5},
        {"consensus_outcome": "confirmed"},
        {"final_normalized_severity": "Critical"},
        {"validator_node_ids": ["friendly-validator"]},
        {"quorum_required": 1},
    ):
        assert client.post(endpoint, json=payload).status_code == 422
