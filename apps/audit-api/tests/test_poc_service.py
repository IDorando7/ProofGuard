from pathlib import Path

import pytest

from app.services.poc_service import (
    get_poc_path,
    poc_exists,
    store_poc_for_finding,
    validate_poc_filename,
)


def test_valid_poc_filename_passes():
    validate_poc_filename("PoC_AccessControl.t.sol")


@pytest.mark.parametrize(
    "filename",
    [
        "../evil.t.sol",
        "/tmp/evil.t.sol",
        "evil.sol",
        "evil t.sol",
        "evil;rm.t.sol",
        "subdir/evil.t.sol",
    ],
)
def test_invalid_poc_filenames_fail(filename: str):
    with pytest.raises(ValueError):
        validate_poc_filename(filename)


def test_store_poc_for_finding_creates_repo_and_reproduction_copies(tmp_path: Path):
    poc_file = store_poc_for_finding(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
        poc_filename="PoC_AccessControl.t.sol",
        poc_content="contract DummyTest {}",
    )

    assert poc_file == "test/PoC_AccessControl.t.sol"
    assert (tmp_path / "repo" / "test" / "PoC_AccessControl.t.sol").is_file()
    assert (tmp_path / "reproductions" / "finding-1" / "PoC_AccessControl.t.sol").is_file()


def test_get_poc_path_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError):
        get_poc_path(tmp_path, "test/../evil.t.sol")


def test_get_poc_path_rejects_paths_outside_repo_test(tmp_path: Path):
    with pytest.raises(ValueError):
        get_poc_path(tmp_path, "src/PoC.t.sol")


def test_poc_exists_returns_true_for_stored_poc(tmp_path: Path):
    poc_file = store_poc_for_finding(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=tmp_path,
        poc_filename="PoC_AccessControl.t.sol",
        poc_content="contract DummyTest {}",
    )

    assert poc_exists(tmp_path, poc_file) is True

