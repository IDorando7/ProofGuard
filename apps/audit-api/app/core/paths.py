from pathlib import Path

from app.core.config import get_settings


def audits_root() -> Path:
    root = get_settings().absolute_data_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def protocol_data_root() -> Path:
    root = get_settings().absolute_protocol_data_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_path(project_id: str) -> Path:
    return audits_root() / project_id


def ensure_workspace_is_unique(project_id: str) -> Path:
    path = workspace_path(project_id)
    path.mkdir(parents=False, exist_ok=False)
    return path
