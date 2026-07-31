from pathlib import Path
from typing import List

from app.agents.base import BaseAgent
from app.schemas.finding import (
    FindingCreate,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
)


class MockAgent(BaseAgent):
    name = "mock_agent"
    category = FindingCategory.OTHER

    def run(
        self,
        project_id: str,
        repo_path: Path,
        scope: dict,
    ) -> List[FindingCreate]:
        return [
            FindingCreate(
                title="Mock candidate finding",
                category=FindingCategory.OTHER,
                severity=FindingSeverity.INFORMATIONAL,
                confidence=0.5,
                contracts=scope.get("contracts_in_scope", []),
                functions=[],
                root_cause="This is a mock root cause for testing.",
                attack_path="This is a mock attack path for testing.",
                impact="This is a mock impact for testing.",
                conditions="This finding is generated only for tests.",
                reproduction_steps=[],
                poc_type="none",
                poc_file=None,
                recommended_fix="Replace the mock agent with a real security agent.",
                status=FindingStatus.CANDIDATE,
                agent_name=self.name,
            )
        ]