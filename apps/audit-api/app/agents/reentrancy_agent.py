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

EXTERNAL_CALL_PATTERN = re.compile(r"\.call\s*(?:\{|\()|\.transfer\s*\(|\.send\s*\(")
STATE_UPDATE_PATTERN = re.compile(
    r"\b(balances|shares|rewards|credits|deposits)\s*\[[^\]]+\]\s*(=|-=|\+=)|"
    r"\b(totalSupply|totalShares)\s*(=|-=|\+=)"
)


class ReentrancyAgent(BaseAgent):
    name = "reentrancy_agent"
    category = FindingCategory.REENTRANCY

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
                if "nonReentrant" in header:
                    continue

                external_call = EXTERNAL_CALL_PATTERN.search(body)
                state_update = STATE_UPDATE_PATTERN.search(body)
                if not external_call or not state_update:
                    continue
                if external_call.start() > state_update.start():
                    continue

                findings.append(
                    FindingCreate(
                        title=f"Potential reentrancy in {function_name}",
                        category=FindingCategory.REENTRANCY,
                        severity=FindingSeverity.HIGH,
                        confidence=0.7,
                        contracts=[contract_path],
                        functions=[function_name],
                        root_cause="External value transfer occurs before internal accounting is updated.",
                        attack_path="A receiver can reenter before its balance or reward state is reduced.",
                        impact="Funds or rewards may be withdrawn more than once.",
                        reproduction_steps=[
                            f"Call {function_name} from a contract with a reentering fallback.",
                        ],
                        recommended_fix="Update state before external calls or protect the function with nonReentrant.",
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
