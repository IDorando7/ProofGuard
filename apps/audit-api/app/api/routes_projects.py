from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.project import (
    PrepareJobResponse,
    ProjectCreateResponse,
    ProjectMetadataResponse,
    ProjectScopeResponse,
    ProjectStatusResponse,
)
from app.services.job_service import start_prepare_job
from app.services.project_service import (
    create_project,
    get_project_or_404,
    list_projects,
    load_project_scope,
    metadata_response,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectMetadataResponse])
def get_projects(db: Session = Depends(get_db)) -> list[ProjectMetadataResponse]:
    return [metadata_response(project) for project in list_projects(db)]


@router.post("", response_model=ProjectCreateResponse)
async def create_audit_project(
    project_name: str = Form(...),
    scope_file: UploadFile = File(...),
    repo_zip: UploadFile | None = File(None),
    github_url: str | None = Form(None),
    db: Session = Depends(get_db),
) -> ProjectCreateResponse:
    return await create_project(db, project_name, scope_file, repo_zip, github_url)


@router.get("/{project_id}", response_model=ProjectMetadataResponse)
def get_project(project_id: str, db: Session = Depends(get_db)) -> ProjectMetadataResponse:
    return metadata_response(get_project_or_404(db, project_id))


@router.get("/{project_id}/scope", response_model=ProjectScopeResponse)
def get_project_scope(project_id: str, db: Session = Depends(get_db)) -> ProjectScopeResponse:
    get_project_or_404(db, project_id)
    return ProjectScopeResponse.model_validate(load_project_scope(project_id).model_dump())


@router.get("/{project_id}/status", response_model=ProjectStatusResponse)
def get_project_status(project_id: str, db: Session = Depends(get_db)) -> ProjectStatusResponse:
    project = get_project_or_404(db, project_id)
    return ProjectStatusResponse(project_id=project.id, status=project.status, last_error=project.last_error)


@router.post("/{project_id}/prepare", response_model=PrepareJobResponse)
def prepare_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> PrepareJobResponse:
    get_project_or_404(db, project_id)
    start_prepare_job(background_tasks, project_id)
    return PrepareJobResponse(project_id=project_id, job="prepare", status="started")
