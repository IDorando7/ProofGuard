import re
from pathlib import Path
from typing import List

from app.agents.base import BaseAgent
from app.schemas.finding import (
    FindingCategory,
    FindingCreate,
    FindingSeverity,
    FindingStatus,
)


FUNCTION_PATTERN = re.compile(
    r"function\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*"
    r"(?P<header>[^{;]*)\{",
    re.MULTILINE,
)

PRIVILEGED_NAME_PATTERN = re.compile(
    r"^(set|update|change|configure)|mint|pause|unpause|admin|owner|treasury",
    re.IGNORECASE,
)

AUTH_PATTERN = re.compile(
    r"\bonlyOwner\b|\bonlyRole\b|\bhasRole\s*\(|"
    r"require\s*\(\s*msg\.sender\s*==\s*(owner|admin|treasury)|"
    r"if\s*\(\s*msg\.sender\s*!=\s*(owner|admin|treasury)",
    re.IGNORECASE,
)


class AccessControlAgent(BaseAgent):
    name = "access_control_agent"
    category = FindingCategory.ACCESS_CONTROL

    def run(
        self,
        project_id: str,
        repo_path: Path,
        scope: dict,
    ) -> List[FindingCreate]:
        findings: list[FindingCreate] = []

        for contract_path in scope.get("contracts_in_scope", []):
            source_path = repo_path / contract_path
            if not source_path.is_file():
                continue

            source = source_path.read_text(encoding="utf-8")
            for function_name, header, body in _iter_functions(source):
                if not _looks_privileged(function_name):
                    continue
                if _has_authorization(header, body):
                    continue

                findings.append(
                    FindingCreate(
                        title=f"Missing access control on {function_name}",
                        category=FindingCategory.ACCESS_CONTROL,
                        severity=FindingSeverity.HIGH,
                        confidence=0.75,
                        contracts=[contract_path],
                        functions=[function_name],
                        root_cause="Privileged function is externally callable without authorization.",
                        attack_path="An unauthorized caller can invoke the function directly.",
                        impact="Protocol configuration or privileged state can be changed by anyone.",
                        reproduction_steps=[
                            f"Call {function_name} from an address without privileges.",
                        ],
                        recommended_fix="Restrict the function with onlyOwner, role checks, or equivalent authorization.",
                        status=FindingStatus.CANDIDATE,
                        agent_name=self.name,
                    )
                )

        return findings


def _iter_functions(source: str) -> list[tuple[str, str, str]]:
    functions: list[tuple[str, str, str]] = []
    for match in FUNCTION_PATTERN.finditer(source):
        header = match.group("header")
        if "public" not in header and "external" not in header:
            continue

        body_start = match.end() - 1
        body_end = _find_matching_brace(source, body_start)
        if body_end is None:
            continue
        functions.append((match.group("name"), header, source[body_start:body_end]))
    return functions


def _find_matching_brace(source: str, open_brace_index: int) -> int | None:
    depth = 0
    for index in range(open_brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _looks_privileged(function_name: str) -> bool:
    return bool(PRIVILEGED_NAME_PATTERN.search(function_name))


def _has_authorization(header: str, body: str) -> bool:
    return bool(AUTH_PATTERN.search(f"{header}\n{body}"))

