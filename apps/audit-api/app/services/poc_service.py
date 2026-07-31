import re
from pathlib import Path

from app.services.reproduction_service import get_reproduction_dir


SAFE_POC_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.t\.sol$")


def get_repo_test_dir(project_workspace: Path) -> Path:
    return project_workspace / "repo" / "test"


def validate_poc_filename(poc_filename: str) -> None:
    path = Path(poc_filename)
    if path.is_absolute() or "/" in poc_filename or "\\" in poc_filename or ".." in path.parts:
        raise ValueError("PoC filename must be a safe filename, not a path.")
    if not SAFE_POC_FILENAME_PATTERN.fullmatch(poc_filename):
        raise ValueError("PoC filename must match ^[A-Za-z0-9_-]+\\.t\\.sol$.")


def store_poc_for_finding(
    project_id: str,
    finding_id: str,
    project_workspace: Path,
    poc_filename: str,
    poc_content: str,
) -> str:
    validate_poc_filename(poc_filename)

    test_dir = get_repo_test_dir(project_workspace)
    test_dir.mkdir(parents=True, exist_ok=True)
    repo_poc_path = test_dir / poc_filename
    repo_poc_path.write_text(poc_content, encoding="utf-8")

    reproduction_dir = get_reproduction_dir(project_workspace, finding_id)
    reproduction_dir.mkdir(parents=True, exist_ok=True)
    (reproduction_dir / poc_filename).write_text(poc_content, encoding="utf-8")

    return f"test/{poc_filename}"


def get_poc_path(
    project_workspace: Path,
    poc_file: str,
) -> Path:
    poc_path = Path(poc_file)
    if poc_path.is_absolute() or ".." in poc_path.parts:
        raise ValueError("PoC file must be a relative path inside repo/test.")
    if "\\" in poc_file or not poc_file.startswith("test/"):
        raise ValueError("PoC file must be inside repo/test.")
    if poc_path.name != poc_path.parts[-1] or not poc_file.endswith(".t.sol"):
        raise ValueError("PoC file must be a Solidity test file.")

    repo_path = project_workspace / "repo"
    resolved_repo = repo_path.resolve(strict=False)
    resolved_poc = (resolved_repo / poc_path).resolve(strict=False)
    try:
        resolved_poc.relative_to(resolved_repo)
    except ValueError as exc:
        raise ValueError("PoC file must not escape the repository.") from exc

    if not resolved_poc.relative_to(resolved_repo).as_posix().startswith("test/"):
        raise ValueError("PoC file must be inside repo/test.")
    return resolved_poc


def poc_exists(project_workspace: Path, poc_file: str) -> bool:
    try:
        return get_poc_path(project_workspace, poc_file).is_file()
    except ValueError:
        return False

