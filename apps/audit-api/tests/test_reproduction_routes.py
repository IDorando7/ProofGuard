from io import BytesIO
from pathlib import Path
import inspect
import zipfile

from app.schemas.safety import SafetyCheckStatus, SafetyPreflightResult
from app.schemas.sandbox import SandboxCommandResult, SandboxRunStatus


SCOPE_YAML = b"""
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Vault.sol
attack_categories:
  - access_control
"""


def _zip_bytes(files: dict[str, str]) -> BytesIO:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        for name, content in files.items():
            zip_file.writestr(name, content)
    archive.seek(0)
    return archive


def _create_project(client):
    return client.post(
        "/projects",
        data={"project_name": "MiniLendingProtocol"},
        files={
            "scope_file": ("scope.yaml", SCOPE_YAML, "application/x-yaml"),
            "repo_zip": (
                "repo.zip",
                _zip_bytes(
                    {
                        "foundry.toml": "[profile.default]\nffi = false\n",
                        "src/Vault.sol": "contract Vault {}",
                    }
                ),
                "application/zip",
            ),
        },
    )


def _upload_poc(client, project_id: str, finding_id: str, content: str = "contract DummyTest {}"):
    return client.post(
        f"/projects/{project_id}/findings/{finding_id}/poc",
        json={
            "poc_filename": "PoC_AccessControl.t.sol",
            "poc_content": content,
        },
    )


def _passed_preflight():
    return SafetyPreflightResult(
        passed=True,
        status=SafetyCheckStatus.PASSED,
        issues=[],
        notes=[],
    )


def _failed_preflight():
    from app.schemas.safety import SafetyIssue, SafetyIssueSeverity

    return SafetyPreflightResult(
        passed=False,
        status=SafetyCheckStatus.FAILED,
        issues=[
            SafetyIssue(
                code="UNSAFE_CHEATCODE",
                severity=SafetyIssueSeverity.CRITICAL,
                message="Unsafe cheatcode.",
                file_path="test/PoC_AccessControl.t.sol",
            )
        ],
        notes=[],
    )


def _sandbox_result(status: SandboxRunStatus) -> SandboxCommandResult:
    return SandboxCommandResult(
        status=status,
        command=["forge", "test", "--match-test", "testName"],
        exit_code=0 if status == SandboxRunStatus.COMPLETED else 1,
        stdout="sandbox stdout",
        stderr="sandbox stderr",
        duration_ms=123,
        error_message=None if status == SandboxRunStatus.COMPLETED else "sandbox failed",
        timed_out=status == SandboxRunStatus.TIMEOUT,
    )


def test_upload_poc_stores_file_and_returns_status(client):
    project_id = _create_project(client).json()["project_id"]

    response = _upload_poc(client, project_id, "finding-1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "generated"
    assert payload["poc_file"] == "test/PoC_AccessControl.t.sol"
    assert payload["stored_path"] == "repo/test/PoC_AccessControl.t.sol"
    assert not Path(payload["stored_path"]).is_absolute()


def test_upload_poc_preserves_validator_test_name_without_claiming_reproduction(client):
    project_id = _create_project(client).json()["project_id"]

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/poc",
        json={
            "poc_filename": "PoC_AccessControl.t.sol",
            "poc_content": "contract DummyTest {}",
            "test_name": "testUnauthorizedAccess",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "generated"
    assert response.json()["test_name"] == "testUnauthorizedAccess"
    stored = client.get(
        f"/projects/{project_id}/findings/finding-1/reproduction"
    )
    assert stored.status_code == 200
    assert stored.json()["status"] == "generated"
    assert stored.json()["test_name"] == "testUnauthorizedAccess"


def test_uploading_invalid_poc_filename_returns_400(client):
    project_id = _create_project(client).json()["project_id"]

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/poc",
        json={"poc_filename": "../evil.t.sol", "poc_content": "contract Evil {}"},
    )

    assert response.status_code == 400


def test_running_reproduction_with_missing_poc_returns_404(client):
    project_id = _create_project(client).json()["project_id"]

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/Missing.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 404


def test_running_reproduction_with_unsafe_poc_rejects_without_sandbox(client, monkeypatch):
    from app.api import routes_reproduction

    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")
    sandbox_called = False

    def fake_run_in_sandbox(*args, **kwargs):
        nonlocal sandbox_called
        sandbox_called = True
        raise AssertionError("sandbox should not run")

    monkeypatch.setattr(routes_reproduction.safety_preflight_service, "run_safety_preflight", lambda **kwargs: _failed_preflight())
    monkeypatch.setattr(routes_reproduction.sandbox_runner, "run_in_sandbox", fake_run_in_sandbox)

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected_unsafe"
    assert sandbox_called is False


def test_running_reproduction_with_safe_poc_calls_sandbox_and_maps_completed(client, monkeypatch):
    from app.api import routes_reproduction

    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")
    calls = {"safety": 0, "sandbox": 0}

    def fake_preflight(**kwargs):
        calls["safety"] += 1
        return _passed_preflight()

    def fake_run_in_sandbox(**kwargs):
        calls["sandbox"] += 1
        return _sandbox_result(SandboxRunStatus.COMPLETED)

    monkeypatch.setattr(routes_reproduction.safety_preflight_service, "run_safety_preflight", fake_preflight)
    monkeypatch.setattr(routes_reproduction.sandbox_runner, "run_in_sandbox", fake_run_in_sandbox)

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "reproduced"
    assert calls == {"safety": 1, "sandbox": 1}


def test_mocked_sandbox_failed_maps_to_reproduction_failed(client, monkeypatch):
    response = _run_with_mocked_sandbox(client, monkeypatch, SandboxRunStatus.FAILED)

    assert response.json()["status"] == "failed"


def test_mocked_sandbox_timeout_maps_to_reproduction_timeout(client, monkeypatch):
    response = _run_with_mocked_sandbox(client, monkeypatch, SandboxRunStatus.TIMEOUT)

    assert response.json()["status"] == "timeout"


def test_mocked_sandbox_error_maps_to_reproduction_sandbox_error(client, monkeypatch):
    response = _run_with_mocked_sandbox(client, monkeypatch, SandboxRunStatus.SANDBOX_ERROR)

    assert response.json()["status"] == "sandbox_error"


def test_get_reproduction_returns_saved_result(client, monkeypatch):
    project_id = _run_completed_reproduction(client, monkeypatch)

    response = client.get(f"/projects/{project_id}/findings/finding-1/reproduction")

    assert response.status_code == 200
    assert response.json()["status"] == "reproduced"


def test_get_reproductions_returns_list(client, monkeypatch):
    project_id = _run_completed_reproduction(client, monkeypatch)

    response = client.get(f"/projects/{project_id}/reproductions")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["finding_id"] == "finding-1"


def test_reproduction_run_rejects_poc_file_with_path_traversal(client):
    project_id = _create_project(client).json()["project_id"]

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/../PoC.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 400


def test_reproduction_run_rejects_invalid_test_name(client):
    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testBad;rm",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 400


def test_reproduction_responses_do_not_expose_absolute_paths(client, monkeypatch):
    project_id = _run_completed_reproduction(client, monkeypatch)

    response = client.get(f"/projects/{project_id}/findings/finding-1/reproduction")
    payload_text = response.text

    assert "/home/" not in payload_text
    assert "data/audits" not in payload_text


def test_reproduction_route_does_not_call_subprocess_directly():
    from app.api import routes_reproduction

    source = inspect.getsource(routes_reproduction)

    assert "subprocess" not in source


def test_reproduction_route_does_not_bypass_safety_preflight(client, monkeypatch):
    from app.api import routes_reproduction

    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")
    calls = {"safety": 0}

    def fake_preflight(**kwargs):
        calls["safety"] += 1
        return _passed_preflight()

    monkeypatch.setattr(routes_reproduction.safety_preflight_service, "run_safety_preflight", fake_preflight)
    monkeypatch.setattr(
        routes_reproduction.sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: _sandbox_result(SandboxRunStatus.COMPLETED),
    )

    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )

    assert response.status_code == 200
    assert calls["safety"] == 1


def _run_with_mocked_sandbox(client, monkeypatch, status: SandboxRunStatus):
    from app.api import routes_reproduction

    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")
    monkeypatch.setattr(routes_reproduction.safety_preflight_service, "run_safety_preflight", lambda **kwargs: _passed_preflight())
    monkeypatch.setattr(routes_reproduction.sandbox_runner, "run_in_sandbox", lambda **kwargs: _sandbox_result(status))
    return client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )


def _run_completed_reproduction(client, monkeypatch) -> str:
    project_id = _create_project(client).json()["project_id"]
    _upload_poc(client, project_id, "finding-1")

    from app.api import routes_reproduction

    monkeypatch.setattr(routes_reproduction.safety_preflight_service, "run_safety_preflight", lambda **kwargs: _passed_preflight())
    monkeypatch.setattr(
        routes_reproduction.sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: _sandbox_result(SandboxRunStatus.COMPLETED),
    )
    response = client.post(
        f"/projects/{project_id}/findings/finding-1/reproduction/run",
        json={
            "poc_file": "test/PoC_AccessControl.t.sol",
            "test_name": "testName",
            "timeout_seconds": 60,
        },
    )
    assert response.status_code == 200
    return project_id
