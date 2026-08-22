from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.agents.access_control_agent import AccessControlAgent
from app.agents.base import BaseAgent
from app.agents.reentrancy_agent import ReentrancyAgent
from app.schemas.agent_execution import AgentExecutionResult
from app.schemas.finding import FindingCategory, FindingCreate
from app.schemas.node import NodeRecord, NodeStatus, NodeType, normalize_category
from app.schemas.routing import RoutingAssignment


LOCAL_ACCESS_NODE_ID = "node_local_access"
LOCAL_REENTRANCY_NODE_ID = "node_local_reentrancy"


class AgentExecutorError(ValueError):
    pass


class LocalAgentNotRegisteredError(AgentExecutorError):
    pass


class LocalAgentCapabilityError(AgentExecutorError):
    pass


class LocalAgentExecutionError(AgentExecutorError):
    pass


@dataclass(frozen=True)
class AgentTaskContext:
    """Trusted executor input. `project_workspace` is internal and never serialized."""

    audit_run_id: str
    project_id: str
    routing_id: str
    assignment: RoutingAssignment
    node: NodeRecord
    project_workspace: Path
    workspace_ref: str
    scope: dict

    @property
    def repo_path(self) -> Path:
        return self.project_workspace / "repo"


class AgentExecutor(Protocol):
    def execute(
        self, *, context: AgentTaskContext, agent_execution_id: str
    ) -> AgentExecutionResult: ...


@dataclass(frozen=True)
class LocalAgentBinding:
    node_id: str
    category: FindingCategory
    agent_type: str
    agent_version: str
    factory: Callable[[], BaseAgent]


class LocalAgentRegistry:
    """Server-controlled node-to-runtime map; callers cannot name Python classes."""

    def __init__(self, bindings: list[LocalAgentBinding] | None = None) -> None:
        selected = bindings if bindings is not None else default_local_bindings()
        self._bindings: dict[str, LocalAgentBinding] = {}
        for binding in selected:
            if binding.node_id in self._bindings:
                raise ValueError("Duplicate local node runtime binding")
            self._bindings[binding.node_id] = binding

    def resolve(
        self, node: NodeRecord, category: str | FindingCategory
    ) -> tuple[LocalAgentBinding, BaseAgent]:
        binding = self._bindings.get(node.node_id)
        if binding is None:
            raise LocalAgentNotRegisteredError("Node has no registered local runtime")
        normalized = normalize_category(category)
        if binding.category.value != normalized:
            raise LocalAgentCapabilityError(
                "Routing assignment category does not match local agent runtime"
            )
        if normalized not in node.supported_categories:
            raise LocalAgentCapabilityError(
                "Node does not declare the assigned vulnerability category"
            )
        agent = binding.factory()
        if agent.category.value != normalized:
            raise LocalAgentCapabilityError(
                "Registered agent implementation category is inconsistent"
            )
        return binding, agent

    def binding_for_node(self, node_id: str) -> LocalAgentBinding:
        binding = self._bindings.get(node_id)
        if binding is None:
            raise LocalAgentNotRegisteredError("Node has no registered local runtime")
        return binding

    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))


class LocalAgentExecutor:
    def __init__(self, registry: LocalAgentRegistry | None = None) -> None:
        self.registry = registry or LocalAgentRegistry()

    def execute(
        self, *, context: AgentTaskContext, agent_execution_id: str
    ) -> AgentExecutionResult:
        self._validate_context(context)
        binding, agent = self.registry.resolve(
            context.node, context.assignment.category
        )
        try:
            candidates = agent.run(
                context.project_id,
                context.repo_path,
                context.scope,
            )
            normalized = _normalize_candidates(
                candidates, expected_category=context.assignment.category
            )
        except AgentExecutorError:
            raise
        except Exception as exc:
            raise LocalAgentExecutionError(
                "Local agent execution failed"
            ) from exc
        return AgentExecutionResult(
            agent_execution_id=agent_execution_id,
            success=True,
            finding_candidates=normalized,
            summary=f"{binding.agent_type} produced {len(normalized)} candidate findings.",
            metadata={
                "executor_type": "local",
                "agent_type": binding.agent_type,
                "agent_version": binding.agent_version,
                "finding_candidate_count": len(normalized),
            },
        )

    @staticmethod
    def _validate_context(context: AgentTaskContext) -> None:
        assignment = context.assignment
        if assignment.project_id != context.project_id:
            raise LocalAgentCapabilityError("Assignment project identity mismatch")
        if assignment.routing_id != context.routing_id:
            raise LocalAgentCapabilityError("Assignment routing identity mismatch")
        if assignment.node_id != context.node.node_id:
            raise LocalAgentCapabilityError("Assignment node identity mismatch")
        if context.node.status != NodeStatus.ACTIVE:
            raise LocalAgentCapabilityError("Assigned local node is not active")
        if context.node.node_type not in {NodeType.AGENT, NodeType.HYBRID}:
            raise LocalAgentCapabilityError("Assigned node cannot execute agent work")
        workspace = context.project_workspace.resolve()
        repo = context.repo_path.resolve()
        try:
            repo.relative_to(workspace)
        except ValueError as exc:
            raise LocalAgentCapabilityError("Repository is outside project workspace") from exc
        if not repo.is_dir():
            raise LocalAgentCapabilityError("Prepared project repository is missing")
        allowed = context.scope.get("contracts_in_scope", [])
        if not isinstance(allowed, list) or not allowed:
            raise LocalAgentCapabilityError("Project scope has no contracts_in_scope")
        for item in allowed:
            candidate = Path(item)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise LocalAgentCapabilityError("Project scope contains an unsafe path")


def default_local_bindings() -> list[LocalAgentBinding]:
    return [
        LocalAgentBinding(
            node_id=LOCAL_ACCESS_NODE_ID,
            category=FindingCategory.ACCESS_CONTROL,
            agent_type="AccessControlAgent",
            agent_version="access_control_static_v1",
            factory=AccessControlAgent,
        ),
        LocalAgentBinding(
            node_id=LOCAL_REENTRANCY_NODE_ID,
            category=FindingCategory.REENTRANCY,
            agent_type="ReentrancyAgent",
            agent_version="reentrancy_static_v1",
            factory=ReentrancyAgent,
        ),
    ]


def _normalize_candidates(
    candidates: list[FindingCreate], *, expected_category: FindingCategory
) -> list[FindingCreate]:
    validated = [FindingCreate.model_validate(item) for item in candidates]
    if any(item.category != expected_category for item in validated):
        raise LocalAgentCapabilityError(
            "Agent returned a candidate outside its assigned category"
        )
    return sorted(
        validated,
        key=lambda item: (
            item.category.value,
            tuple(sorted(item.contracts)),
            tuple(sorted(item.functions)),
            item.title,
            item.root_cause,
        ),
    )
