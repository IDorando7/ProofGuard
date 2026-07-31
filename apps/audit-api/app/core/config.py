from functools import lru_cache
from pathlib import Path
import os

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "Proofguard Audit API"
    project_root: Path = Path(__file__).resolve().parents[2]
    data_dir: Path = Path("data/audits")
    protocol_data_dir: Path = Path("data/protocol")
    sqlite_path: Path = Path("data/audit_api.sqlite3")

    @property
    def absolute_data_dir(self) -> Path:
        return self._resolve_under_project(self.data_dir)

    @property
    def absolute_sqlite_path(self) -> Path:
        return self._resolve_under_project(self.sqlite_path)

    @property
    def absolute_protocol_data_dir(self) -> Path:
        return self._resolve_under_project(self.protocol_data_dir)

    def display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return path.name

    def _resolve_under_project(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()


@lru_cache
def get_settings() -> Settings:
    data_dir = Path(os.getenv("AUDIT_API_DATA_DIR", "data/audits"))
    protocol_data_dir = Path(os.getenv("AUDIT_API_PROTOCOL_DATA_DIR", "data/protocol"))
    sqlite_path = Path(os.getenv("AUDIT_API_SQLITE_PATH", "data/audit_api.sqlite3"))
    return Settings(
        data_dir=data_dir,
        protocol_data_dir=protocol_data_dir,
        sqlite_path=sqlite_path,
    )
