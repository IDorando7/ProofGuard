import pytest
from fastapi import HTTPException

from app.services.scope_service import parse_scope_yaml


VALID_SCOPE = b"""
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"
contracts_in_scope:
  - src/Vault.sol
attack_categories:
  - access_control
known_issues:
  - "Owner can pause protocol intentionally"
"""


def test_valid_scope_manifest_parses_successfully():
    scope = parse_scope_yaml(VALID_SCOPE)

    assert scope.project_name == "MiniLendingProtocol"
    assert scope.contracts_out_of_scope == []
    assert scope.attack_categories == ["access_control"]


def test_invalid_attack_category_fails_validation():
    invalid_scope = VALID_SCOPE.replace(b"access_control", b"not_a_category")

    with pytest.raises(HTTPException) as exc_info:
        parse_scope_yaml(invalid_scope)

    assert exc_info.value.status_code == 400


def test_missing_contracts_in_scope_fails_validation():
    invalid_scope = b"""
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
attack_categories:
  - access_control
"""

    with pytest.raises(HTTPException) as exc_info:
        parse_scope_yaml(invalid_scope)

    assert exc_info.value.status_code == 400

