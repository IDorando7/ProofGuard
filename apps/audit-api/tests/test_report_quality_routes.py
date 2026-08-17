from app.core.paths import protocol_data_root
from app.main import app
from tests.test_subnet_reward_routes import _setup


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _quality_setup(client):
    project_id, _, node, routing, submission = _setup(client)
    cluster_response = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/rebuild",
        json={},
    )
    assert cluster_response.status_code == 200
    cluster = cluster_response.json()["clusters"][0]
    return project_id, node, routing, submission, cluster


def _endpoint(project_id, routing_id, cluster_id, submission_id):
    return (
        f"/projects/{project_id}/routing/{routing_id}/finding-clusters/"
        f"{cluster_id}/submissions/{submission_id}/quality-assessment"
    )


VALIDATOR_INPUT = {
    "correctness_score": "1.000000",
    "poc_quality_score": "0.800000",
    "root_cause_quality_score": "0.900000",
    "impact_quality_score": "0.700000",
    "fix_quality_score": "0.600000",
    "reason_codes": {
        "correctness": ["accepted_validation"],
        "fix_quality": ["actionable_root_cause_fix"],
    },
}


def test_quality_create_read_list_and_rebuild_routes(client):
    project_id, node, routing, submission, cluster = _quality_setup(client)
    endpoint = _endpoint(
        project_id,
        routing.routing_id,
        cluster["finding_cluster_id"],
        submission.submission_id,
    )
    created = client.post(endpoint, json=VALIDATOR_INPUT)
    assert created.status_code == 200
    body = created.json()
    assert body["quality_score"] == "0.860000"
    assert body["assessment_status"] == "finalized"
    assert body["node_id"] == node.node_id
    assert body["operator_id"] == node.operator_id
    assert body["components"]["correctness"]["weighted_value"] == "0.350000"

    repeated = client.post(endpoint, json=VALIDATOR_INPUT)
    assert repeated.status_code == 200
    assert repeated.json() == body
    read = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/submissions/"
        f"{submission.submission_id}/quality-assessment"
    )
    assert read.status_code == 200
    assert read.json() == body
    listed = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/"
        f"{cluster['finding_cluster_id']}/quality-assessments"
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["assessments"] == [body]
    rebuilt = client.post(
        f"/projects/{project_id}/routing/{routing.routing_id}/quality-assessments/rebuild",
        json={},
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["unchanged_assessments"] == 1


def test_deterministic_route_returns_explicit_incomplete_draft(client):
    project_id, _, routing, submission, cluster = _quality_setup(client)
    response = client.post(
        _endpoint(
            project_id,
            routing.routing_id,
            cluster["finding_cluster_id"],
            submission.submission_id,
        ),
        json={},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["assessment_status"] == "draft"
    assert body["quality_score"] is None
    assert body["components"]["fix_quality"]["state"] == "not_assessed"


def test_client_cannot_supply_derived_identity_score_or_reward_fields(client):
    project_id, _, routing, submission, cluster = _quality_setup(client)
    endpoint = _endpoint(
        project_id,
        routing.routing_id,
        cluster["finding_cluster_id"],
        submission.submission_id,
    )
    for field, value in (
        ("quality_score", "1.000000"),
        ("report_quality_assessment_id", "forged"),
        ("operator_id", "forged-operator"),
        ("node_id", "forged-node"),
        ("quality_rank", 1),
        ("top_k", True),
        ("chief_finder", True),
        ("reward", "100"),
    ):
        response = client.post(endpoint, json={**VALIDATOR_INPUT, field: value})
        assert response.status_code == 422


def test_invalid_component_and_linkage_fail_cleanly(client):
    project_id, _, routing, submission, cluster = _quality_setup(client)
    endpoint = _endpoint(
        project_id,
        routing.routing_id,
        cluster["finding_cluster_id"],
        submission.submission_id,
    )
    assert client.post(
        endpoint, json={**VALIDATOR_INPUT, "fix_quality_score": "1.01"}
    ).status_code == 422
    assert client.post(
        endpoint, json={**VALIDATOR_INPUT, "fix_quality_score": "NaN"}
    ).status_code == 422
    assert client.post(
        _endpoint(
            project_id,
            routing.routing_id,
            cluster["finding_cluster_id"],
            "missing-submission",
        ),
        json=VALIDATOR_INPUT,
    ).status_code == 409
    assert client.post(
        _endpoint(
            "missing-project",
            routing.routing_id,
            cluster["finding_cluster_id"],
            submission.submission_id,
        ),
        json=VALIDATOR_INPUT,
    ).status_code == 404


def test_finalized_api_conflict_and_explicit_supersession(client):
    project_id, _, routing, submission, cluster = _quality_setup(client)
    endpoint = _endpoint(
        project_id,
        routing.routing_id,
        cluster["finding_cluster_id"],
        submission.submission_id,
    )
    first = client.post(endpoint, json=VALIDATOR_INPUT)
    assert first.status_code == 200
    changed = {**VALIDATOR_INPUT, "fix_quality_score": "0.900000"}
    assert client.post(endpoint, json=changed).status_code == 409
    superseded = client.post(
        endpoint, json={**changed, "supersede_existing": True}
    )
    assert superseded.status_code == 200
    assert superseded.json()["assessment_version"] == 2
    history = client.get(
        f"/projects/{project_id}/routing/{routing.routing_id}/finding-clusters/"
        f"{cluster['finding_cluster_id']}/quality-assessments?include_superseded=true"
    )
    assert history.status_code == 200
    assert [item["assessment_status"] for item in history.json()["assessments"]] == [
        "superseded",
        "finalized",
    ]


def test_quality_route_creates_no_reward_or_reputation_side_effect(client):
    project_id, node, routing, submission, cluster = _quality_setup(client)
    root = _root()
    node_before = client.get(f"/nodes/{node.node_id}").json()
    contribution_before = client.get(
        f"/submissions/{submission.submission_id}/contribution"
    ).json()
    response = client.post(
        _endpoint(
            project_id,
            routing.routing_id,
            cluster["finding_cluster_id"],
            submission.submission_id,
        ),
        json=VALIDATOR_INPUT,
    )
    assert response.status_code == 200
    assert client.get(f"/nodes/{node.node_id}").json() == node_before
    assert client.get(
        f"/submissions/{submission.submission_id}/contribution"
    ).json() == contribution_before
    assert not (root / "rewards" / "events").exists()
    assert not (root / "task-rewards" / "events").exists()
