from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ReproductionStatus(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    GENERATED = "generated"
    RUNNING = "running"
    REPRODUCED = "reproduced"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    REJECTED_UNSAFE = "rejected_unsafe"
    SANDBOX_ERROR = "sandbox_error"


class ReproductionResultCreate(BaseModel):
    finding_id: str
    project_id: str
    status: ReproductionStatus = ReproductionStatus.NOT_ATTEMPTED
    poc_file: Optional[str] = None
    test_name: Optional[str] = None
    command: Optional[List[str]] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    error_message: Optional[str] = None
    safety_notes: List[str] = Field(default_factory=list)


class ReproductionResult(ReproductionResultCreate):
    reproduction_id: str
    created_at: datetime
    updated_at: datetime

