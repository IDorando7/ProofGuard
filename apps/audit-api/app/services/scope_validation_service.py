import json
import re
from pathlib import Path
from typing import Any

import yaml

from app.schemas.scope_validation import (
    ScopeIssueSeverity,
    ScopeValidationIssue,
    ScopeValidationResult,
    ScopeValidationStatus,
)
from app.schemas.validation import ValidationEvidence


EXCLUDED_PREFIXES = (
    "test/",
    "tests/",
    "script/",
    "scripts/",
    "lib/",
    "node_modules/",
    "out/",
    "cache/",
)
STANDARD_NOTES = [
    "Scope validation only checks declared scope constraints.",
    "It does not validate exploit correctness.",
]


def normalize_contract_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = normalized[3:]
    normalized = normalized.lstrip("/")
    return normalized


def get_contracts_from_finding(finding: Any) -> list[str]:
    contracts: list[str] = []
    for value in (
        _get_value(finding, "contracts"),
        _get_value(finding, "contract"),
        _get_value(finding, "affected_contracts"),
    ):
        if isinstance(value, str):
            contracts.append(value)
        elif isinstance(value, list):
            contracts.extend(item for item in value if isinstance(item, str))
    return _unique([normalize_contract_path(contract) for contract in contracts if contract.strip()])


def get_categories_from_finding(finding: Any) -> list[str]:
    categories: list[str] = []
    for value in (
        _get_value(finding, "category"),
        _get_value(finding, "categories"),
        _get_value(finding, "attack_categories"),
    ):
        if isinstance(value, str):
            categories.append(value)
        elif isinstance(value, list):
            categories.extend(item for item in value if isinstance(item, str))
    return _unique([_normalize_category(category) for category in categories if category.strip()])


def load_scope_from_project_workspace(project_workspace: Path) -> dict:
    candidates = [
        project_workspace / "scope" / "scope.yaml",
        project_workspace / "scope.yaml",
        project_workspace / "repo" / "scope.yaml",
        project_workspace / "scope" / "parsed_scope.json",
    ]
    workspace_root = project_workspace.resolve(strict=False)
    for path in candidates:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        if resolved.suffix == ".json":
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
        else:
            loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    return {}


def validate_contracts_against_scope(
    finding_contracts: list[str],
    scope: dict,
) -> list[ScopeValidationIssue]:
    issues: list[ScopeValidationIssue] = []
    normalized_contracts = [normalize_contract_path(contract) for contract in finding_contracts]
    contracts_in_scope = {
        normalize_contract_path(contract)
        for contract in scope.get("contracts_in_scope", []) or []
        if isinstance(contract, str)
    }
    contracts_out_of_scope = {
        normalize_contract_path(contract)
        for contract in scope.get("contracts_out_of_scope", []) or []
        if isinstance(contract, str)
    }

    if not normalized_contracts:
        issues.append(
            _issue(
                "NO_CONTRACTS_IN_FINDING",
                ScopeIssueSeverity.WARNING,
                "Finding does not include any contract paths.",
                "contracts",
            )
        )
        return issues

    if not contracts_in_scope:
        issues.append(
            _issue(
                "NO_CONTRACTS_IN_SCOPE_DECLARED",
                ScopeIssueSeverity.WARNING,
                "Scope does not declare contracts_in_scope.",
                "contracts_in_scope",
            )
        )

    for contract in normalized_contracts:
        if contract in contracts_out_of_scope:
            issues.append(
                _issue(
                    "CONTRACT_EXPLICITLY_OUT_OF_SCOPE",
                    ScopeIssueSeverity.HIGH,
                    "Finding contract is explicitly out of scope.",
                    "contracts_out_of_scope",
                    contract,
                )
            )
        if any(contract.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            issues.append(
                _issue(
                    "CONTRACT_IN_EXCLUDED_DIRECTORY",
                    ScopeIssueSeverity.HIGH,
                    "Finding contract is in an excluded directory.",
                    "contracts",
                    contract,
                )
            )
        if contracts_in_scope and contract not in contracts_in_scope:
            issues.append(
                _issue(
                    "CONTRACT_NOT_IN_SCOPE",
                    ScopeIssueSeverity.HIGH,
                    "Finding contract is not listed in contracts_in_scope.",
                    "contracts_in_scope",
                    contract,
                )
            )
    return issues


def validate_categories_against_scope(
    finding_categories: list[str],
    scope: dict,
) -> list[ScopeValidationIssue]:
    issues: list[ScopeValidationIssue] = []
    normalized_categories = [_normalize_category(category) for category in finding_categories]
    allowed_categories = {
        _normalize_category(category)
        for category in scope.get("attack_categories", []) or []
        if isinstance(category, str)
    }

    if not normalized_categories:
        issues.append(
            _issue(
                "NO_CATEGORY_IN_FINDING",
                ScopeIssueSeverity.WARNING,
                "Finding does not include a category.",
                "category",
            )
        )
        return issues

    if not allowed_categories:
        issues.append(
            _issue(
                "NO_ATTACK_CATEGORIES_DECLARED",
                ScopeIssueSeverity.WARNING,
                "Scope does not declare attack_categories.",
                "attack_categories",
            )
        )
        return issues

    for category in normalized_categories:
        if category not in allowed_categories:
            issues.append(
                _issue(
                    "CATEGORY_NOT_ALLOWED",
                    ScopeIssueSeverity.HIGH,
                    "Finding category is not listed in attack_categories.",
                    "attack_categories",
                    category,
                )
            )
    return issues


def validate_finding_scope(
    finding: Any,
    scope: dict,
) -> ScopeValidationResult:
    if not scope:
        return ScopeValidationResult(
            passed=False,
            status=ScopeValidationStatus.INVALID_SCOPE,
            issues=[
                _issue(
                    "INVALID_SCOPE",
                    ScopeIssueSeverity.CRITICAL,
                    "Scope is missing or malformed.",
                )
            ],
            notes=STANDARD_NOTES.copy(),
        )

    contracts = get_contracts_from_finding(finding)
    categories = get_categories_from_finding(finding)
    issues = validate_contracts_against_scope(contracts, scope)
    issues.extend(validate_categories_against_scope(categories, scope))

    if any(issue.severity in {ScopeIssueSeverity.HIGH, ScopeIssueSeverity.CRITICAL} for issue in issues):
        status = ScopeValidationStatus.OUT_OF_SCOPE
    elif issues:
        status = ScopeValidationStatus.UNKNOWN
    else:
        status = ScopeValidationStatus.IN_SCOPE

    return ScopeValidationResult(
        passed=status == ScopeValidationStatus.IN_SCOPE,
        status=status,
        issues=issues,
        contracts_checked=contracts,
        categories_checked=categories,
        notes=STANDARD_NOTES.copy(),
    )


def validate_finding_scope_from_workspace(
    project_workspace: Path,
    finding: Any,
) -> ScopeValidationResult:
    scope = load_scope_from_project_workspace(project_workspace)
    return validate_finding_scope(finding, scope)


def apply_scope_result_to_validation_evidence(
    evidence: ValidationEvidence,
    scope_result: ScopeValidationResult,
) -> ValidationEvidence:
    return evidence.model_copy(
        update={
            "in_scope": scope_result.passed,
            "notes": [*evidence.notes, *scope_result.notes],
        }
    )


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _normalize_category(category: str) -> str:
    stripped = category.strip().lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", "_", stripped)


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def _issue(
    code: str,
    severity: ScopeIssueSeverity,
    message: str,
    field: str | None = None,
    value: str | None = None,
) -> ScopeValidationIssue:
    return ScopeValidationIssue(
        code=code,
        severity=severity,
        message=message,
        field=field,
        value=value,
    )

