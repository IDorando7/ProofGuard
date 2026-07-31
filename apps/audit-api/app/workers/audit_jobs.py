from datetime import datetime, timezone
from pathlib import Path

from app.core.database import SessionLocal
from app.models.audit_project import AuditProject
from app.services.project_service import project_workspace
from app.services.scope_service import read_parsed_scope


def prepare_audit_project(project_id: str) -> None:
    db = SessionLocal()
    project = db.get(AuditProject, project_id)
    if project is None:
        db.close()
        return

    workspace = project_workspace(project_id)
    log_path = workspace / "logs" / "prepare.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        project.status = "preparing"
        project.last_error = None
        db.commit()

        messages: list[str] = [f"{datetime.now(timezone.utc).isoformat()} prepare started"]
        repo_dir = workspace / "repo"
        if not repo_dir.exists() or not repo_dir.is_dir():
            raise ValueError("repo folder is missing")

        scope = read_parsed_scope(workspace)
        messages.append("scope manifest loaded")

        missing_contracts = _missing_contracts(repo_dir, scope.contracts_in_scope)
        if missing_contracts:
            raise ValueError(f"contracts_in_scope paths missing from repo: {', '.join(missing_contracts)}")

        messages.append("contracts_in_scope paths verified")
        messages.append(f"{datetime.now(timezone.utc).isoformat()} prepare completed")
        log_path.write_text("\n".join(messages) + "\n", encoding="utf-8")

        project.status = "ready"
        project.last_error = None
        db.commit()
    except Exception as exc:
        error = str(exc)
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        log_path.write_text(existing + f"{datetime.now(timezone.utc).isoformat()} prepare failed: {error}\n", encoding="utf-8")
        project.status = "failed"
        project.last_error = error
        db.commit()
    finally:
        db.close()


def _missing_contracts(repo_dir: Path, contracts_in_scope: list[str]) -> list[str]:
    missing: list[str] = []
    for contract_path in contracts_in_scope:
        normalized = Path(contract_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            missing.append(contract_path)
            continue
        direct_path = repo_dir / normalized
        if direct_path.exists():
            continue
        matches = list(repo_dir.rglob(normalized.name))
        if not any(match.as_posix().endswith(normalized.as_posix()) for match in matches):
            missing.append(contract_path)
    return missing

