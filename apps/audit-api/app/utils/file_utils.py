from pathlib import Path
import zipfile

from fastapi import HTTPException


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if target != destination and destination not in target.parents:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsafe zip entry rejected: {member.filename}",
                    )
                if Path(member.filename).is_absolute() or ".." in Path(member.filename).parts:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsafe zip entry rejected: {member.filename}",
                    )
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="repo_zip must be a valid zip archive") from exc


def ensure_child_path(base: Path, candidate: Path) -> bool:
    base = base.resolve()
    candidate = candidate.resolve()
    return candidate == base or base in candidate.parents

