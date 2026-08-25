from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.paths import ensure_workspace_is_unique, workspace_path
from app.models.audit_project import AuditProject
from app.schemas.project import ProjectCreateResponse, ProjectMetadataResponse
from app.services.repo_service import clone_github_repo, store_zip_repo
from app.services.scope_service import parse_scope_yaml, read_parsed_scope, scope_summary, write_scope_files


WORKSPACE_DIRS = ("repo", "scope", "findings", "reports", "logs")


def get_project_or_404(db: Session, project_id: str) -> AuditProject:
    project = db.get(AuditProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def list_projects(db: Session) -> list[AuditProject]:
    return list(
        db.query(AuditProject)
        .order_by(AuditProject.created_at.desc(), AuditProject.id.asc())
        .all()
    )


async def create_project(
    db: Session,
    project_name: str,
    scope_file: UploadFile,
    repo_zip: UploadFile | None,
    github_url: str | None,
) -> ProjectCreateResponse:
    cleaned_name = project_name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="project_name cannot be empty")
    if repo_zip is None and not github_url:
        raise HTTPException(status_code=400, detail="Either repo_zip or github_url must be provided")
    if repo_zip is not None and github_url:
        raise HTTPException(status_code=400, detail="Provide either repo_zip or github_url, not both")

    scope_content = await scope_file.read()
    scope = parse_scope_yaml(scope_content)

    project_id = str(uuid4())
    workspace = ensure_workspace_is_unique(project_id)
    try:
        for dirname in WORKSPACE_DIRS:
            (workspace / dirname).mkdir(parents=True, exist_ok=False)

        write_scope_files(workspace, scope_content, scope)
        source_type = "zip" if repo_zip is not None else "github"
        if repo_zip is not None:
            await store_zip_repo(repo_zip, workspace / "repo")
            stored_github_url = None
        else:
            stored_github_url = github_url
            clone_github_repo(github_url or "", workspace / "repo")

        metadata = {
            "project_id": project_id,
            "project_name": cleaned_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "created",
            "source_type": source_type,
            "github_url": stored_github_url,
        }
        (workspace / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        display_workspace = get_settings().display_path(workspace)
        db_project = AuditProject(
            id=project_id,
            project_name=cleaned_name,
            workspace_path=display_workspace,
            status="created",
            source_type=source_type,
            github_url=stored_github_url,
        )
        db.add(db_project)
        db.commit()

        return ProjectCreateResponse(
            project_id=project_id,
            project_name=cleaned_name,
            status="created",
            workspace_path=display_workspace,
            scope_summary=scope_summary(scope),
        )
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def metadata_response(project: AuditProject) -> ProjectMetadataResponse:
    return ProjectMetadataResponse(
        project_id=project.id,
        project_name=project.project_name,
        created_at=project.created_at,
        status=project.status,
        source_type=project.source_type,
        github_url=project.github_url,
        workspace_path=project.workspace_path,
    )


def project_workspace(project_id: str) -> Path:
    return workspace_path(project_id)


def load_project_scope(project_id: str):
    return read_parsed_scope(project_workspace(project_id))
