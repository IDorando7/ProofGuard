from io import BytesIO
from pathlib import Path
import zipfile


SCOPE_YAML = b"""
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Vault.sol
  - src/OracleRouter.sol
attack_categories:
  - access_control
  - reentrancy
"""


def _zip_bytes(files: dict[str, str]) -> BytesIO:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as zip_file:
        for name, content in files.items():
            zip_file.writestr(name, content)
    archive.seek(0)
    return archive


def _create_project(client, repo_archive: BytesIO | None = None):
    archive = repo_archive or _zip_bytes(
        {
            "src/Vault.sol": "contract Vault {}",
            "src/OracleRouter.sol": "contract OracleRouter {}",
        }
    )
    return client.post(
        "/projects",
        data={"project_name": "MiniLendingProtocol"},
        files={
            "scope_file": ("scope.yaml", SCOPE_YAML, "application/x-yaml"),
            "repo_zip": ("repo.zip", archive, "application/zip"),
        },
    )


def test_project_creation_with_zip_creates_workspace_folders(client):
    response = _create_project(client)

    assert response.status_code == 200
    payload = response.json()
    workspace = Path(__file__).resolve().parents[1] / payload["workspace_path"]
    assert (workspace / "repo").is_dir()
    assert (workspace / "scope" / "scope.yaml").is_file()
    assert (workspace / "scope" / "parsed_scope.json").is_file()
    assert (workspace / "findings").is_dir()
    assert (workspace / "reports").is_dir()
    assert (workspace / "logs").is_dir()
    assert (workspace / "metadata.json").is_file()
    assert payload["scope_summary"]["contracts_in_scope_count"] == 2


def test_zip_slip_malicious_archive_is_rejected(client):
    response = _create_project(client, _zip_bytes({"../evil.sol": "contract Evil {}"}))

    assert response.status_code == 400
    assert "Unsafe zip entry" in response.json()["detail"]


def test_get_project_scope_returns_parsed_scope(client):
    create_response = _create_project(client)
    project_id = create_response.json()["project_id"]

    response = client.get(f"/projects/{project_id}/scope")

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_name"] == "MiniLendingProtocol"
    assert payload["contracts_in_scope"] == ["src/Vault.sol", "src/OracleRouter.sol"]


def test_list_projects_returns_public_metadata_without_internal_workspace(client):
    first = _create_project(client).json()
    second = _create_project(client).json()

    response = client.get("/projects")

    assert response.status_code == 200
    payload = response.json()
    assert [item["project_id"] for item in payload] == [
        second["project_id"],
        first["project_id"],
    ]
    assert all("last_error" not in item for item in payload)
    assert all(not item["workspace_path"].startswith("/") for item in payload)


def test_frontend_development_origin_is_allowed_by_cors(client):
    response = client.get(
        "/projects",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
