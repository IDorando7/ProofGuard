import json
from pathlib import Path

import yaml

from app.schemas.scope_validation import ScopeIssueSeverity, ScopeValidationStatus
from app.schemas.validation import ValidationEvidence
from app.services.scope_validation_service import (
    apply_scope_result_to_validation_evidence,
    get_categories_from_finding,
    get_contracts_from_finding,
    load_scope_from_project_workspace,
    normalize_contract_path,
    validate_categories_against_scope,
    validate_contracts_against_scope,
    validate_finding_scope,
    validate_finding_scope_from_workspace,
)


VALID_SCOPE = {
    "contracts_in_scope": ["src/TAIEngine.sol", "src/Vault.sol"],
    "contracts_out_of_scope": ["test/Mock.t.sol", "scripts/Deploy.s.sol"],
    "attack_categories": ["access_control", "reentrancy"],
}


def _codes(issues):
    return [issue.code for issue in issues]


def _finding(contract="src/TAIEngine.sol", category="access_control"):
    return {"contracts": [contract], "category": category}


def _write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_normalize_contract_path_removes_leading_dot_slash():
    assert normalize_contract_path("./src/TAIEngine.sol") == "src/TAIEngine.sol"


def test_normalize_contract_path_converts_backslashes():
    assert normalize_contract_path("src\\TAIEngine.sol") == "src/TAIEngine.sol"


def test_normalize_contract_path_removes_duplicate_slashes():
    assert normalize_contract_path("src//TAIEngine.sol") == "src/TAIEngine.sol"


def test_normalize_contract_path_strips_whitespace():
    assert normalize_contract_path("  src/TAIEngine.sol  ") == "src/TAIEngine.sol"


def test_get_contracts_from_finding_supports_contract_variants():
    assert get_contracts_from_finding({"contract": "./src/TAIEngine.sol"}) == ["src/TAIEngine.sol"]
    assert get_contracts_from_finding({"affected_contracts": ["src\\Vault.sol"]}) == ["src/Vault.sol"]


def test_get_categories_from_finding_normalizes_values():
    assert get_categories_from_finding({"category": "Access Control"}) == ["access_control"]


def test_finding_contract_in_contracts_in_scope_passes_contract_validation():
    assert validate_contracts_against_scope(["src/TAIEngine.sol"], VALID_SCOPE) == []


def test_finding_contract_not_in_contracts_in_scope_fails():
    issues = validate_contracts_against_scope(["src/Other.sol"], VALID_SCOPE)

    assert "CONTRACT_NOT_IN_SCOPE" in _codes(issues)


def test_finding_contract_in_contracts_out_of_scope_fails():
    issues = validate_contracts_against_scope(["test/Mock.t.sol"], VALID_SCOPE)

    assert "CONTRACT_EXPLICITLY_OUT_OF_SCOPE" in _codes(issues)


def test_finding_contract_in_test_directory_fails():
    issues = validate_contracts_against_scope(["test/PoC.t.sol"], VALID_SCOPE)

    assert "CONTRACT_IN_EXCLUDED_DIRECTORY" in _codes(issues)


def test_finding_contract_in_scripts_directory_fails():
    issues = validate_contracts_against_scope(["scripts/Deploy.s.sol"], VALID_SCOPE)

    assert "CONTRACT_IN_EXCLUDED_DIRECTORY" in _codes(issues)


def test_finding_contract_in_lib_directory_fails():
    issues = validate_contracts_against_scope(["lib/Dependency.sol"], VALID_SCOPE)

    assert "CONTRACT_IN_EXCLUDED_DIRECTORY" in _codes(issues)


def test_no_contracts_in_finding_returns_warning():
    issues = validate_contracts_against_scope([], VALID_SCOPE)

    assert issues[0].code == "NO_CONTRACTS_IN_FINDING"
    assert issues[0].severity == ScopeIssueSeverity.WARNING


def test_missing_contracts_in_scope_returns_warning():
    issues = validate_contracts_against_scope(["src/TAIEngine.sol"], {"attack_categories": ["access_control"]})

    assert "NO_CONTRACTS_IN_SCOPE_DECLARED" in _codes(issues)


def test_allowed_category_passes():
    assert validate_categories_against_scope(["access_control"], VALID_SCOPE) == []


def test_category_not_in_attack_categories_fails():
    issues = validate_categories_against_scope(["oracle_manipulation"], VALID_SCOPE)

    assert "CATEGORY_NOT_ALLOWED" in _codes(issues)


def test_category_normalization_works():
    assert validate_categories_against_scope(["Access Control"], VALID_SCOPE) == []


def test_no_category_returns_warning():
    issues = validate_categories_against_scope([], VALID_SCOPE)

    assert issues[0].code == "NO_CATEGORY_IN_FINDING"
    assert issues[0].severity == ScopeIssueSeverity.WARNING


def test_missing_attack_categories_returns_warning():
    issues = validate_categories_against_scope(["access_control"], {"contracts_in_scope": ["src/TAIEngine.sol"]})

    assert "NO_ATTACK_CATEGORIES_DECLARED" in _codes(issues)


def test_valid_finding_and_scope_returns_in_scope():
    result = validate_finding_scope(_finding(), VALID_SCOPE)

    assert result.passed is True
    assert result.status == ScopeValidationStatus.IN_SCOPE


def test_out_of_scope_contract_returns_out_of_scope():
    result = validate_finding_scope(_finding(contract="src/Other.sol"), VALID_SCOPE)

    assert result.passed is False
    assert result.status == ScopeValidationStatus.OUT_OF_SCOPE


def test_out_of_scope_category_returns_out_of_scope():
    result = validate_finding_scope(_finding(category="oracle_manipulation"), VALID_SCOPE)

    assert result.passed is False
    assert result.status == ScopeValidationStatus.OUT_OF_SCOPE


def test_empty_scope_returns_invalid_scope():
    result = validate_finding_scope(_finding(), {})

    assert result.passed is False
    assert result.status == ScopeValidationStatus.INVALID_SCOPE


def test_only_warnings_returns_unknown():
    result = validate_finding_scope({"contracts": [], "category": "access_control"}, VALID_SCOPE)

    assert result.passed is False
    assert result.status == ScopeValidationStatus.UNKNOWN


def test_result_includes_contracts_and_categories_checked():
    result = validate_finding_scope(_finding(), VALID_SCOPE)

    assert result.contracts_checked == ["src/TAIEngine.sol"]
    assert result.categories_checked == ["access_control"]


def test_result_includes_standard_notes():
    result = validate_finding_scope(_finding(), VALID_SCOPE)

    assert "Scope validation only checks declared scope constraints." in result.notes
    assert "It does not validate exploit correctness." in result.notes


def test_loads_scope_from_scope_yaml(tmp_path: Path):
    _write_yaml(tmp_path / "scope" / "scope.yaml", VALID_SCOPE)

    assert load_scope_from_project_workspace(tmp_path)["contracts_in_scope"] == ["src/TAIEngine.sol", "src/Vault.sol"]


def test_loads_scope_from_root_scope_yaml_fallback(tmp_path: Path):
    _write_yaml(tmp_path / "scope.yaml", VALID_SCOPE)

    assert load_scope_from_project_workspace(tmp_path)["attack_categories"] == ["access_control", "reentrancy"]


def test_loads_scope_from_repo_scope_yaml_fallback(tmp_path: Path):
    _write_yaml(tmp_path / "repo" / "scope.yaml", VALID_SCOPE)

    assert load_scope_from_project_workspace(tmp_path)["contracts_out_of_scope"] == [
        "test/Mock.t.sol",
        "scripts/Deploy.s.sol",
    ]


def test_loads_scope_from_parsed_scope_json_fallback(tmp_path: Path):
    parsed_scope = tmp_path / "scope" / "parsed_scope.json"
    parsed_scope.parent.mkdir(parents=True, exist_ok=True)
    parsed_scope.write_text(json.dumps(VALID_SCOPE), encoding="utf-8")

    assert load_scope_from_project_workspace(tmp_path)["contracts_in_scope"] == ["src/TAIEngine.sol", "src/Vault.sol"]


def test_workspace_loading_does_not_read_outside_project_workspace(tmp_path: Path):
    outside = tmp_path / "outside" / "scope.yaml"
    _write_yaml(outside, {"contracts_in_scope": ["src/Outside.sol"]})
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert load_scope_from_project_workspace(workspace) == {}


def test_validate_finding_scope_from_workspace_loads_and_validates(tmp_path: Path):
    _write_yaml(tmp_path / "scope" / "scope.yaml", VALID_SCOPE)

    result = validate_finding_scope_from_workspace(tmp_path, _finding())

    assert result.status == ScopeValidationStatus.IN_SCOPE


def test_apply_scope_result_to_validation_evidence_sets_in_scope_and_notes():
    result = validate_finding_scope(_finding(), VALID_SCOPE)
    evidence = apply_scope_result_to_validation_evidence(ValidationEvidence(notes=["existing"]), result)

    assert evidence.in_scope is True
    assert "existing" in evidence.notes
    assert "Scope validation only checks declared scope constraints." in evidence.notes

