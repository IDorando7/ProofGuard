from pathlib import Path

import pytest

from app.agents.base import BaseAgent
from app.agents.mock_agent import MockAgent
from app.schemas.finding import FindingCategory, FindingStatus


def test_base_agent_requires_run_implementation():
    class IncompleteAgent(BaseAgent):
        name = "incomplete_agent"
        category = FindingCategory.OTHER

    with pytest.raises(TypeError):
        IncompleteAgent()


def test_mock_agent_returns_one_candidate_finding(tmp_path: Path):
    finding = MockAgent().run(
        project_id="project-1",
        repo_path=tmp_path,
        scope={},
    )[0]

    assert finding.title == "Mock candidate finding"
    assert finding.category == FindingCategory.OTHER
    assert finding.status == FindingStatus.CANDIDATE
    assert finding.agent_name == "mock_agent"


def test_mock_agent_uses_contracts_in_scope(tmp_path: Path):
    findings = MockAgent().run(
        project_id="project-1",
        repo_path=tmp_path,
        scope={"contracts_in_scope": ["src/Vault.sol", "src/OracleRouter.sol"]},
    )

    assert len(findings) == 1
    assert findings[0].contracts == ["src/Vault.sol", "src/OracleRouter.sol"]

