from datetime import datetime

from pydantic import BaseModel

from app.schemas.scope import ScopeManifest, ScopeSummary


class ProjectCreateResponse(BaseModel):
    project_id: str
    project_name: str
    status: str
    workspace_path: str
    scope_summary: ScopeSummary


class ProjectMetadataResponse(BaseModel):
    project_id: str
    project_name: str
    created_at: datetime
    status: str
    source_type: str
    github_url: str | None
    workspace_path: str


class ProjectStatusResponse(BaseModel):
    project_id: str
    status: str
    last_error: str | None


class PrepareJobResponse(BaseModel):
    project_id: str
    job: str
    status: str


class ProjectScopeResponse(ScopeManifest):
    pass

