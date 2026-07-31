from pathlib import Path

from app.services.foundry_service import detect_foundry_project


def _make_repo(tmp_path: Path, *, foundry_toml: bool = False, src: bool = False, test: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if foundry_toml:
        (repo / "foundry.toml").write_text("[profile.default]\n", encoding="utf-8")
    if src:
        (repo / "src").mkdir()
    if test:
        (repo / "test").mkdir()
    return repo


def test_repo_with_foundry_toml_and_src_is_foundry_project(tmp_path: Path):
    repo = _make_repo(tmp_path, foundry_toml=True, src=True)

    info = detect_foundry_project(repo)

    assert info.is_foundry_project is True
    assert info.has_foundry_toml is True
    assert info.has_src_dir is True


def test_repo_missing_foundry_toml_is_not_foundry_project(tmp_path: Path):
    repo = _make_repo(tmp_path, src=True)

    info = detect_foundry_project(repo)

    assert info.is_foundry_project is False
    assert info.has_foundry_toml is False


def test_repo_missing_src_is_not_foundry_project(tmp_path: Path):
    repo = _make_repo(tmp_path, foundry_toml=True)

    info = detect_foundry_project(repo)

    assert info.is_foundry_project is False
    assert info.has_src_dir is False


def test_missing_test_dir_can_be_created_later_for_foundry_project(tmp_path: Path):
    repo = _make_repo(tmp_path, foundry_toml=True, src=True)

    info = detect_foundry_project(repo)

    assert info.has_test_dir is False
    assert info.can_create_test_dir is True
    assert "test/ directory is missing but can be created later" in info.notes


def test_returned_paths_are_relative_not_absolute(tmp_path: Path):
    repo = _make_repo(tmp_path, foundry_toml=True, src=True, test=True)

    info = detect_foundry_project(repo)

    assert info.foundry_toml_path == "foundry.toml"
    assert info.src_dir_path == "src"
    assert info.test_dir_path == "test"
    assert not Path(info.foundry_toml_path).is_absolute()
    assert not Path(info.src_dir_path).is_absolute()
    assert not Path(info.test_dir_path).is_absolute()


def test_notes_include_missing_foundry_toml(tmp_path: Path):
    repo = _make_repo(tmp_path, src=True)

    info = detect_foundry_project(repo)

    assert "foundry.toml is missing" in info.notes


def test_notes_include_missing_src(tmp_path: Path):
    repo = _make_repo(tmp_path, foundry_toml=True)

    info = detect_foundry_project(repo)

    assert "src/ directory is missing" in info.notes

