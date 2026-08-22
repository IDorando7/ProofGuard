from tests.test_validator_committee_routes import _context


def test_performance_api_rejects_unresolved_truth_and_self_scoring(client):
    _, _, cluster_id, base = _context(client)
    committee = client.post(
        f"{base}/finding-clusters/{cluster_id}/validator-committees", json={}
    ).json()
    client.post(
        f"{base}/validator-committees/{committee['validator_committee_id']}/finalize",
        json={},
    )
    consensus = client.post(
        f"{base}/finding-clusters/{cluster_id}/validation-consensus", json={}
    ).json()
    consensus_id = consensus["validation_consensus_id"]
    client.post(f"{base}/validation-consensus/{consensus_id}/finalize", json={})

    endpoint = f"{base}/validation-consensus/{consensus_id}/validator-performance/evaluate"
    unresolved = client.post(endpoint, json={})
    assert unresolved.status_code == 409
    assert "DISPUTED and NO_QUORUM" in unresolved.json()["detail"]
    for forged in (
        {"validity_accuracy": "1.000000"},
        {"validation_quality_score": "1.000000"},
        {"validator_category_score": "1.000000"},
        {"membership": "expert"},
    ):
        assert client.post(endpoint, json=forged).status_code == 422


def test_validator_performance_read_routes_are_explicitly_not_found(client):
    assert client.get(
        "/validators/missing-validator/categories/access_control/performance"
    ).status_code == 404
    assert client.get(
        "/validators/missing-validator/categories/access_control/score"
    ).status_code == 404
    assert client.get(
        "/validators/missing-validator/categories/access_control/membership"
    ).status_code == 404


def test_openapi_exposes_validator_performance_without_score_inputs(client):
    schema = client.get("/openapi.json").json()
    path = "/projects/{project_id}/routing/{routing_id}/validation-consensus/{consensus_id}/validator-performance/evaluate"
    assert path in schema["paths"]
    assert "validator-performance" in schema["paths"][path]["post"]["tags"]
