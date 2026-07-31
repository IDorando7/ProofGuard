from pathlib import Path
import shutil
import subprocess
import tempfile

from fastapi import HTTPException, UploadFile

from app.utils.file_utils import safe_extract_zip


async def store_zip_repo(repo_zip: UploadFile, repo_dir: Path) -> None:
    if not repo_zip.filename or not repo_zip.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="repo_zip must be a .zip file")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
        temp_path = Path(temp_file.name)
        while chunk := await repo_zip.read(1024 * 1024):
            temp_file.write(chunk)

    try:
        safe_extract_zip(temp_path, repo_dir)
    finally:
        temp_path.unlink(missing_ok=True)


def clone_github_repo(github_url: str, repo_dir: Path) -> None:
    if not github_url.strip():
        raise HTTPException(status_code=400, detail="github_url cannot be empty")

    if shutil.which("git") is None:
        raise HTTPException(status_code=400, detail="git is not installed on this server")

    result = subprocess.run(
        ["git", "clone", "--depth", "1", github_url, str(repo_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git clone failed"
        raise HTTPException(status_code=400, detail=f"Could not clone github_url: {detail}")

