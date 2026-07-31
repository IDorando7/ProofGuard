import json
import uuid
from pathlib import Path
from typing import List, Optional

from app.schemas.finding import FindingCreate, Finding


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
