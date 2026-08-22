from app.core.paths import protocol_data_root
from app.main import app
from datetime import datetime, timezone

from app.schemas.audit_run import AuditArtifactRef, AuditEventType, AuditStage
from app.services.audit_run_service import append_audit_event
from tests.test_project_creation import _create_project


def _root():
    return app.dependency_overrides[protocol_data_root]()


def _create_run(client, project_id, payload=None):
    return client.post(
        f"/projects/{project_id}/audit-runs",
        json=payload if payload is not None else {"execution_mode": "local_simulator"},
    )


def test_create_get_list_and_start_audit_run(client):
    project_id = _create_project(client).json()["project_id"]
    created_response = _create_run(client, project_id)
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["project_id"] == project_id
    assert created["status"] == "CREATED"
    assert created["current_stage"] is None
    assert created["execution_mode"] == "local_simulator"
    assert created["event_count"] == 1
    assert created["progress"] == {
        "completed_stage_count": 0,
        "total_stage_count": 10,
        "progress_percentage": 0.0,
    }
    run_id = created["audit_run_id"]
    assert client.get(f"/projects/{project_id}/audit-runs/{run_id}").json() == created

    listing = client.get(f"/projects/{project_id}/audit-runs")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["audit_runs"] == [created]

    started = client.post(f"/projects/{project_id}/audit-runs/{run_id}/start")
    assert started.status_code == 200
    assert started.json()["status"] == "RUNNING"
    assert started.json()["current_stage"] == "PREPARING"
    assert started.json()["stage_states"][0]["status"] == "RUNNING"
    assert started.json()["stage_states"][1]["status"] == "PENDING"


def test_project_not_found_and_wrong_project_run_combination_fail(client):
    assert _create_run(client, "missing-project").status_code == 404
    first_project = _create_project(client).json()["project_id"]
    second_project = _create_project(client).json()["project_id"]
    run_id = _create_run(client, first_project).json()["audit_run_id"]
    assert client.get(
        f"/projects/{second_project}/audit-runs/{run_id}"
    ).status_code == 404
    assert client.get(
        f"/projects/{first_project}/audit-runs/missing-run"
    ).status_code == 404


def test_list_order_is_newest_first_and_deterministic(client):
    project_id = _create_project(client).json()["project_id"]
    first = _create_run(client, project_id).json()
    second = _create_run(client, project_id).json()
    runs = client.get(f"/projects/{project_id}/audit-runs").json()["audit_runs"]
    assert [run["audit_run_id"] for run in runs] == [
        second["audit_run_id"],
        first["audit_run_id"],
    ]


def test_events_endpoint_supports_incremental_pagination_and_filters(client):
    project_id = _create_project(client).json()["project_id"]
    run = _create_run(client, project_id).json()
    run_id = run["audit_run_id"]
    client.post(f"/projects/{project_id}/audit-runs/{run_id}/start")
    append_audit_event(
        _root(),
        project_id,
        run_id,
        event_type=AuditEventType.ROUTING_NODE_SELECTED,
        stage=AuditStage.ROUTING,
        message="Synthetic routing selection recorded.",
        entity_type="routing_assignment",
        entity_id="assignment_1",
        node_id="node_17",
        operator_id="operator_4",
        category="reentrancy",
        metadata={"routing_score": "0.930000"},
    )
    first_page = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/events?after_sequence=0&limit=2"
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert [event["sequence_number"] for event in body["events"]] == [1, 2]
    assert body["next_after_sequence"] == 2
    assert body["has_more"] is True

    second_page = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/events?after_sequence=2&limit=2"
    ).json()
    assert [event["sequence_number"] for event in second_page["events"]] == [3]
    assert second_page["has_more"] is False
    assert second_page["events"][0]["node_id"] == "node_17"
    assert second_page["events"][0]["operator_id"] == "operator_4"

    filtered = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/events"
        "?stage=ROUTING&event_type=ROUTING_NODE_SELECTED&node_id=node_17"
    ).json()
    assert filtered["returned"] == 1
    assert filtered["events"][0]["metadata"]["routing_score"] == "0.930000"
    empty = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/events?after_sequence=3"
    ).json()
    assert empty["events"] == []


def test_public_request_cannot_forge_lifecycle_or_downstream_state(client):
    project_id = _create_project(client).json()["project_id"]
    for payload in (
        {"status": "COMPLETED"},
        {"current_stage": "REWARDING"},
        {"routing_id": "routing_1"},
        {"reward_cycle_id": "reward_cycle_1"},
        {"task_reward_budget_id": "budget_1"},
        {"report_id": "report_1"},
        {"execution_mode": "remote_network"},
    ):
        assert _create_run(client, project_id, payload).status_code == 422

    run_id = _create_run(client, project_id).json()["audit_run_id"]
    assert client.request(
        "PATCH",
        f"/projects/{project_id}/audit-runs/{run_id}",
        json={"status": "COMPLETED"},
    ).status_code in {404, 405}
    assert client.post(
        f"/projects/{project_id}/audit-runs/{run_id}/start",
        json={"current_stage": "REPORTING"},
    ).status_code == 422


def test_day1_api_has_no_execution_side_effects_or_host_path_leakage(client):
    project_id = _create_project(client).json()["project_id"]
    run = _create_run(client, project_id).json()
    response = client.post(
        f"/projects/{project_id}/audit-runs/{run['audit_run_id']}/start"
    )
    assert response.status_code == 200
    text = response.text.lower()
    for forbidden in (
        "/home/",
        "/tmp/",
        "private_key",
        "stdout\":",
        "stderr\":",
        "reward_event_id",
        "validation_id",
        "submission_id",
    ):
        assert forbidden not in text

    protocol_entries = sorted(path.name for path in _root().iterdir())
    assert protocol_entries == ["audit-runs"]
    events = client.get(
        f"/projects/{project_id}/audit-runs/{run['audit_run_id']}/events"
    ).json()["events"]
    assert [event["event_type"] for event in events] == [
        "AUDIT_CREATED",
        "STAGE_STARTED",
    ]


def test_events_api_exposes_logical_foundry_artifact_without_host_path(client):
    project_id = _create_project(client).json()["project_id"]
    run_id = _create_run(client, project_id).json()["audit_run_id"]
    artifact = AuditArtifactRef(
        artifact_id="artifact_foundry_stdout_1",
        audit_run_id=run_id,
        artifact_type="FOUNDRY_STDOUT",
        entity_type="reproduction",
        entity_id="reproduction_1",
        display_name="forge-stdout.txt",
        content_type="text/plain",
        storage_ref=f"audit-artifacts/{run_id}/artifact_foundry_stdout_1",
        size_bytes=128,
        sha256="c" * 64,
        created_at=datetime.now(timezone.utc),
    )
    append_audit_event(
        _root(),
        project_id,
        run_id,
        event_type=AuditEventType.REPRODUCTION_LOG,
        stage=AuditStage.REPRODUCING,
        message="Synthetic Foundry stdout artifact recorded.",
        entity_type="reproduction",
        entity_id="reproduction_1",
        metadata={"stream": "stdout", "test_name": "test_ReentrantWithdraw"},
        artifact_refs=[artifact],
    )
    response = client.get(
        f"/projects/{project_id}/audit-runs/{run_id}/events"
    )
    assert response.status_code == 200
    event = response.json()["events"][-1]
    assert event["artifact_refs"][0]["storage_ref"].startswith("audit-artifacts/")
    assert event["artifact_refs"][0]["artifact_type"] == "FOUNDRY_STDOUT"
    assert "/tmp/" not in response.text
    assert "/home/" not in response.text
