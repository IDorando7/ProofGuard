import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


SAFE_POC_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.t\.sol$")
SAFE_TEST_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


class PocUploadRequest(BaseModel):
    poc_filename: str
    poc_content: str

    @field_validator("poc_filename")
    @classmethod
    def validate_poc_filename(cls, value: str) -> str:
        if not SAFE_POC_FILENAME_PATTERN.fullmatch(value):
            raise ValueError("poc_filename must match ^[A-Za-z0-9_-]+\\.t\\.sol$")
        if Path(value).is_absolute() or "/" in value or "\\" in value or ".." in Path(value).parts:
            raise ValueError("poc_filename must be a safe filename, not a path")
        return value


class PocUploadResponse(BaseModel):
    project_id: str
    finding_id: str
    poc_file: str
    stored_path: str
    status: str
    message: str


class ReproductionRunRequest(BaseModel):
    poc_file: str
    test_name: str
    timeout_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator("poc_file")
    @classmethod
    def validate_poc_file(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("poc_file must be a relative path inside test/")
        if "\\" in value or not value.startswith("test/"):
            raise ValueError("poc_file must be inside test/")
        if not value.endswith(".t.sol"):
            raise ValueError("poc_file must end with .t.sol")
        return value

    @field_validator("test_name")
    @classmethod
    def validate_test_name(cls, value: str) -> str:
        if not SAFE_TEST_NAME_PATTERN.fullmatch(value):
            raise ValueError("test_name must contain only letters, numbers, and underscores")
        return value


class ReproductionRunResponse(BaseModel):
    project_id: str
    finding_id: str
    reproduction_id: str
    status: str
    poc_file: str | None
    test_name: str | None
    command: list[str] | None
    duration_ms: int | None
    error_message: str | None
    safety_notes: list[str]

