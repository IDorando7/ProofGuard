from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from app.schemas.finding import FindingCreate, FindingCategory


class BaseAgent(ABC):
    name: str
    category: FindingCategory

    @abstractmethod
    def run(
        self,
        project_id: str,
        repo_path: Path,
        scope: dict,
    ) -> List[FindingCreate]:
        """
        Run the agent on a project repository and return candidate findings.

        Important:
        - Agents only return candidate/unverified findings.
        - Agents do not validate findings.
        - Agents do not execute exploits.
        - Agents must respect contracts_in_scope from scope.yaml.
        """
        raise NotImplementedError