import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.scope import ScopeManifest, ScopeSummary


def parse_scope_yaml(content: bytes) -> ScopeManifest:
    try:
        loaded: Any = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML scope_file: {exc}") from exc

    if not isinstance(loaded, dict):
        raise HTTPException(status_code=400, detail="scope_file must contain a YAML object")

    try:
        return ScopeManifest.model_validate(loaded)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc


def scope_summary(scope: ScopeManifest) -> ScopeSummary:
    return ScopeSummary(
        language=scope.language,
        framework=scope.framework,
        contracts_in_scope_count=len(scope.contracts_in_scope),
        attack_categories=list(scope.attack_categories),
    )


def write_scope_files(workspace: Path, original_content: bytes, scope: ScopeManifest) -> None:
    scope_dir = workspace / "scope"
    scope_dir.mkdir(parents=True, exist_ok=True)
    (scope_dir / "scope.yaml").write_bytes(original_content)
    (scope_dir / "parsed_scope.json").write_text(
        json.dumps(scope.model_dump(), indent=2),
        encoding="utf-8",
    )


def read_parsed_scope(workspace: Path) -> ScopeManifest:
    path = workspace / "scope" / "parsed_scope.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Parsed scope not found")
    return ScopeManifest.model_validate_json(path.read_text(encoding="utf-8"))

