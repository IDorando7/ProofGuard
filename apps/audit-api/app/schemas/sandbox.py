from enum import Enum

from pydantic import BaseModel, Field


class SandboxRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    SANDBOX_ERROR = "sandbox_error"


class SandboxCommandRequest(BaseModel):
    repo_path: str = Field(..., min_length=1)
    command: list[str]
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    sandbox_image: str = "foundry-sandbox:latest"
    network_disabled: bool = True
    memory_limit: str = "1g"
    cpus: str = "1"
    pids_limit: int = 256


class SandboxCommandResult(BaseModel):
    status: SandboxRunStatus
    command: list[str]
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    duration_ms: int
    error_message: str | None
    timed_out: bool = False

