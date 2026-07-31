import os
from pathlib import Path

from app.schemas.tooling import FoundryProjectInfo


def detect_foundry_project(repo_path: Path) -> FoundryProjectInfo:
    notes: list[str] = []
    repo_exists = repo_path.exists() and repo_path.is_dir()

    foundry_toml = repo_path / "foundry.toml"
    src_dir = repo_path / "src"
    test_dir = repo_path / "test"

    has_foundry_toml = repo_exists and foundry_toml.is_file()
    has_src_dir = repo_exists and src_dir.is_dir()
    has_test_dir = repo_exists and test_dir.is_dir()
    can_create_test_dir = repo_exists and os.access(repo_path, os.W_OK)

    if not repo_exists:
        notes.append("repo path does not exist or is not a directory")
    if not has_foundry_toml:
        notes.append("foundry.toml is missing")
    if not has_src_dir:
        notes.append("src/ directory is missing")
    if not has_test_dir:
        if can_create_test_dir:
            notes.append("test/ directory is missing but can be created later")
        else:
            notes.append("test/ directory is missing and repo is not writable")

    return FoundryProjectInfo(
        is_foundry_project=has_foundry_toml and has_src_dir,
        has_foundry_toml=has_foundry_toml,
        has_src_dir=has_src_dir,
        has_test_dir=has_test_dir,
        can_create_test_dir=can_create_test_dir,
        foundry_toml_path="foundry.toml" if has_foundry_toml else None,
        src_dir_path="src" if has_src_dir else None,
        test_dir_path="test" if has_test_dir else None,
        notes=notes,
    )

