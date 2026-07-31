import pytest
from pydantic import ValidationError

from app.schemas.reproduction import ReproductionResultCreate, ReproductionStatus


def test_reproduction_result_create_accepts_minimal_valid_data():
    result = ReproductionResultCreate(
        finding_id="finding-1",
        project_id="project-1",
    )

    assert result.finding_id == "finding-1"
    assert result.project_id == "project-1"


def test_default_status_is_not_attempted():
    result = ReproductionResultCreate(
        finding_id="finding-1",
        project_id="project-1",
    )

    assert result.status == ReproductionStatus.NOT_ATTEMPTED


def test_invalid_status_fails_validation():
    with pytest.raises(ValidationError):
        ReproductionResultCreate(
            finding_id="finding-1",
            project_id="project-1",
            status="confirmed",
        )


def test_negative_duration_ms_fails_validation():
    with pytest.raises(ValidationError):
        ReproductionResultCreate(
            finding_id="finding-1",
            project_id="project-1",
            duration_ms=-1,
        )


def test_command_as_list_of_strings_is_accepted():
    result = ReproductionResultCreate(
        finding_id="finding-1",
        project_id="project-1",
        command=["forge", "test", "--match-test", "testWithdraw"],
    )

    assert result.command == ["forge", "test", "--match-test", "testWithdraw"]


def test_command_as_shell_string_fails_validation():
    with pytest.raises(ValidationError):
        ReproductionResultCreate(
            finding_id="finding-1",
            project_id="project-1",
            command="forge test --match-test testWithdraw",
        )


def test_rejected_unsafe_is_valid_without_stdout_or_stderr():
    result = ReproductionResultCreate(
        finding_id="finding-1",
        project_id="project-1",
        status=ReproductionStatus.REJECTED_UNSAFE,
        safety_notes=["PoC attempted to access a forbidden path."],
    )

    assert result.status == ReproductionStatus.REJECTED_UNSAFE
    assert result.stdout is None
    assert result.stderr is None

