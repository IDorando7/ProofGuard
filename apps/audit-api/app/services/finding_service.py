import json
import re
import uuid
from pathlib import Path
from typing import List, Optional

from app.schemas.finding import FindingCreate, Finding
from app.utils.protocol_serialization import atomic_create_json


SAFE_FINDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


def save_findings_to_file(
    project_id: str,
    findings: List[FindingCreate],
    findings_dir: Path,
    filename: str,
) -> List[Finding]:
    findings_dir.mkdir(parents=True, exist_ok=True)

    enriched_findings = [
        Finding(
            finding_id=str(uuid.uuid4()),
            project_id=project_id,
            **finding.model_dump(),
        )
        for finding in findings
    ]

    output_path = findings_dir / filename

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            [finding.model_dump(mode="json") for finding in enriched_findings],
            f,
            indent=2,
            ensure_ascii=False,
        )

    return enriched_findings


def list_project_findings(project_workspace: Path) -> List[Finding]:
    findings_dir = project_workspace / "findings"
    if not findings_dir.exists():
        return []

    findings: list[Finding] = []
    for path in sorted(findings_dir.rglob("*.json")):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            findings.extend(Finding.model_validate(item) for item in loaded)
        elif isinstance(loaded, dict):
            findings.append(Finding.model_validate(loaded))
    return findings


def load_project_finding(project_workspace: Path, finding_id: str) -> Optional[Finding]:
    for finding in list_project_findings(project_workspace):
        if finding.finding_id == finding_id:
            return finding
    return None


def save_finding_exclusively(
    project_workspace: Path,
    finding: Finding,
) -> tuple[Finding, bool]:
    """Persist an integration finding without replacing historical agent output."""
    if not SAFE_FINDING_ID.fullmatch(finding.finding_id):
        raise ValueError("Invalid finding identifier")
    findings_root = project_workspace / "findings" / "by-id"
    output_path = findings_root / f"{finding.finding_id}.json"
    try:
        output_path.resolve().relative_to(findings_root.resolve())
    except ValueError as exc:
        raise ValueError("Invalid finding identifier") from exc
    created = atomic_create_json(
        output_path,
        finding,
        temporary_prefix=".finding-",
    )
    if created:
        return finding, True
    try:
        existing = Finding.model_validate_json(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("Stored finding is malformed") from exc
    if existing != finding:
        raise ValueError("Finding identifier conflicts with existing content")
    return existing, False
