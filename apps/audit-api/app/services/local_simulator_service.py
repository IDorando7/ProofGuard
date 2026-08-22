from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, NodeStatistics, NodeStatus, NodeType
from app.schemas.subnet import SubnetMemberRecord, SubnetMemberStatus
from app.services.local_agent_executor import (
    LOCAL_ACCESS_NODE_ID,
    LOCAL_REENTRANCY_NODE_ID,
)
from app.services.node_registry_service import load_node, save_node
from app.services.subnet_registry_service import (
    bootstrap_default_subnets,
    load_subnet_member,
    save_subnet_member,
)
from app.utils.protocol_serialization import protocol_fingerprint


LOCAL_NODE_SPECS = (
    (
        LOCAL_ACCESS_NODE_ID,
        "operator_local_access",
        "Local Access Control Agent",
        FindingCategory.ACCESS_CONTROL,
    ),
    (
        LOCAL_REENTRANCY_NODE_ID,
        "operator_local_reentrancy",
        "Local Reentrancy Agent",
        FindingCategory.REENTRANCY,
    ),
)


class LocalSimulatorConfigurationError(ValueError):
    pass


def bootstrap_local_simulator_nodes(
    protocol_data_root: Path,
) -> list[NodeRecord]:
    """Explicit, idempotent deployment/test setup; never called by an AuditRun."""
    nodes: list[NodeRecord] = []
    for node_id, operator_id, display_name, category in LOCAL_NODE_SPECS:
        existing = load_node(protocol_data_root, node_id)
        if existing is not None:
            if (
                existing.operator_id != operator_id
                or existing.node_type != NodeType.AGENT
                or existing.supported_categories != [category.value]
            ):
                raise LocalSimulatorConfigurationError(
                    f"Local simulator node '{node_id}' conflicts with existing registry data"
                )
            nodes.append(existing)
            continue
        now = datetime.now(timezone.utc)
        nodes.append(
            save_node(
                protocol_data_root,
                NodeRecord(
                    node_id=node_id,
                    node_type=NodeType.AGENT,
                    display_name=display_name,
                    operator_id=operator_id,
                    public_key=None,
                    supported_categories=[category.value],
                    description="Server-controlled in-process simulator runtime.",
                    status=NodeStatus.ACTIVE,
                    reputation_score=0.5,
                    statistics=NodeStatistics(),
                    status_reason="Local simulator node registered.",
                    created_at=now,
                    updated_at=now,
                    status_updated_at=now,
                ),
            )
        )
    return sorted(nodes, key=lambda item: item.node_id)


def bootstrap_local_simulator_routing_capacity(
    protocol_data_root: Path,
) -> list[SubnetMemberRecord]:
    """Explicit setup of one exploration member per local runtime.

    This helper is intentionally separate from audit execution: starting an audit
    never creates or recalibrates membership.
    """
    nodes = {node.node_id: node for node in bootstrap_local_simulator_nodes(protocol_data_root)}
    bootstrap_default_subnets(protocol_data_root)
    members: list[SubnetMemberRecord] = []
    for node_id, _operator_id, _display_name, category in LOCAL_NODE_SPECS:
        subnet_id = f"subnet_{category.value}"
        existing = load_subnet_member(protocol_data_root, subnet_id, node_id)
        if existing is not None:
            if existing.category != category:
                raise LocalSimulatorConfigurationError(
                    "Local simulator membership category mismatch"
                )
            members.append(existing)
            continue
        now = datetime.now(timezone.utc)
        member = SubnetMemberRecord(
            subnet_id=subnet_id,
            node_id=nodes[node_id].node_id,
            category=category,
            status=SubnetMemberStatus.CANDIDATE,
            category_score=0.5,
            rank=None,
            finalized_submissions=0,
            accepted_unique_submissions=0,
            exploration_assignments=0,
            last_assigned_at=None,
            joined_at=now,
            updated_at=now,
            status_reason="Local simulator bootstrap candidate.",
            status_updated_at=now,
            status_reason_codes=["local_simulator_bootstrap"],
            membership_source_fingerprint=protocol_fingerprint(
                {
                    "source": "local_simulator_bootstrap_v1",
                    "subnet_id": subnet_id,
                    "node_id": node_id,
                    "category": category.value,
                }
            ),
        )
        members.append(save_subnet_member(protocol_data_root, member))
    return sorted(members, key=lambda item: item.node_id)
