from pydantic import BaseModel, Field


class FoundryProjectInfo(BaseModel):
    is_foundry_project: bool
    has_foundry_toml: bool
    has_src_dir: bool
    has_test_dir: bool
    can_create_test_dir: bool
    foundry_toml_path: str | None
    src_dir_path: str | None
    test_dir_path: str | None
    notes: list[str] = Field(default_factory=list)


class ToolAvailability(BaseModel):
    tool_name: str
    available: bool
    version: str | None
    error_message: str | None


class SandboxToolingInfo(BaseModel):
    sandbox_required: bool = True
    sandbox_image: str
    sandbox_available: bool
    docker_available: bool
    foundry_available_in_sandbox: bool
    host_foundry_available: bool
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProjectToolingReport(BaseModel):
    project_id: str
    foundry: FoundryProjectInfo
    sandbox: SandboxToolingInfo
    status: str

