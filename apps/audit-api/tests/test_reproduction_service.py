from pathlib import Path

from app.schemas.reproduction import ReproductionStatus
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    get_reproduction_dir,
    list_reproduction_results,
    load_reproduction_result,
    save_reproduction_result,
    write_reproduction_output_files,
)


def test_create_initial_reproduction_result_creates_folder_and_json(tmp_path: Path):
    result = create_initial_reproduction_result(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
    )

    reproduction_dir = get_reproduction_dir(tmp_path, "finding-1")
    assert reproduction_dir.is_dir()
    assert (reproduction_dir / "reproduction.json").is_file()
    assert result.status == ReproductionStatus.NOT_ATTEMPTED
    assert result.stdout is None
    assert result.stderr is None
    assert result.safety_notes == []


def test_load_reproduction_result_loads_saved_object(tmp_path: Path):
    created = create_initial_reproduction_result(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
    )

    loaded = load_reproduction_result(tmp_path, "finding-1")

    assert loaded == created


def test_load_reproduction_result_returns_none_when_missing(tmp_path: Path):
    assert load_reproduction_result(tmp_path, "missing-finding") is None


def test_save_reproduction_result_updates_status_correctly(tmp_path: Path):
    created = create_initial_reproduction_result(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
    )

    updated = created.model_copy(update={"status": ReproductionStatus.GENERATED})
    saved = save_reproduction_result(updated, tmp_path)
    loaded = load_reproduction_result(tmp_path, "finding-1")

    assert saved.status == ReproductionStatus.GENERATED
    assert loaded is not None
    assert loaded.status == ReproductionStatus.GENERATED
    assert saved.updated_at >= created.updated_at


def test_write_reproduction_output_files_writes_stdout_and_stderr(tmp_path: Path):
    write_reproduction_output_files(
        project_workspace=tmp_path,
        finding_id="finding-1",
        stdout="test stdout",
        stderr="test stderr",
    )

    reproduction_dir = get_reproduction_dir(tmp_path, "finding-1")
    assert (reproduction_dir / "stdout.txt").read_text(encoding="utf-8") == "test stdout"
    assert (reproduction_dir / "stderr.txt").read_text(encoding="utf-8") == "test stderr"


def test_list_reproduction_results_returns_all_saved_results(tmp_path: Path):
    first = create_initial_reproduction_result("project-1", "finding-1", tmp_path)
    second = create_initial_reproduction_result("project-1", "finding-2", tmp_path)

    results = list_reproduction_results(tmp_path)

    assert [result.reproduction_id for result in results] == [
        first.reproduction_id,
        second.reproduction_id,
    ]


def test_rejected_unsafe_status_can_be_saved_with_safety_notes(tmp_path: Path):
    created = create_initial_reproduction_result("project-1", "finding-1", tmp_path)
    unsafe = created.model_copy(
        update={
            "status": ReproductionStatus.REJECTED_UNSAFE,
            "safety_notes": ["Attempt used a forbidden external command."],
        }
    )

    saved = save_reproduction_result(unsafe, tmp_path)
    loaded = load_reproduction_result(tmp_path, "finding-1")

    assert saved.status == ReproductionStatus.REJECTED_UNSAFE
    assert loaded is not None
    assert loaded.safety_notes == ["Attempt used a forbidden external command."]


def test_timeout_status_can_be_saved_with_error_message(tmp_path: Path):
    created = create_initial_reproduction_result("project-1", "finding-1", tmp_path)
    timeout = created.model_copy(
        update={
            "status": ReproductionStatus.TIMEOUT,
            "error_message": "Execution exceeded the configured timeout.",
        }
    )

    saved = save_reproduction_result(timeout, tmp_path)
    loaded = load_reproduction_result(tmp_path, "finding-1")

    assert saved.status == ReproductionStatus.TIMEOUT
    assert loaded is not None
    assert loaded.error_message == "Execution exceeded the configured timeout."

