from __future__ import annotations

from pathlib import Path

from app.schemas.demo_bootstrap import (
    DemoBootstrapCategoryResult,
    DemoBootstrapNodeResult,
    DemoBootstrapResult,
)
from app.schemas.finding import (
    Finding,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
)
from app.schemas.node import NodeRecord, NodeStatus, NodeType
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionStatusUpdate,
)
from app.schemas.subnet import SubnetMemberStatus
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.category_performance_service import rebuild_category_performance
from app.services.category_scoring_service import (
    load_category_score,
    rebuild_category_score,
)
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
)
from app.services.finding_service import save_finding_exclusively
from app.services.local_agent_executor import (
    LocalAgentNotRegisteredError,
    LocalAgentRegistry,
    category_local_binding,
)
from app.services.node_registry_service import list_nodes, load_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.reputation_service import process_submission_reputation
from app.services.submission_service import (
    create_submission,
    list_submissions,
    update_submission_status,
)
from app.services.subnet_membership_service import refresh_subnet_memberships
from app.services.subnet_registry_service import (
    bootstrap_default_subnets,
    list_subnet_members,
    load_subnet_member,
)
from app.services.validation_service import (
    create_validation_decision,
    load_validation_decision,
)


DEMO_BOOTSTRAP_VERSION = "gold_demo_bootstrap_v1"
DEMO_HISTORY_PROJECT_ID = "proofguard_gold_demo_history_v1"
DEMO_CATEGORIES = (
    FindingCategory.ACCESS_CONTROL,
    FindingCategory.REENTRANCY,
)
DEMO_OPERATOR_PREFIXES = {
    FindingCategory.ACCESS_CONTROL: "demo-agent-access-op-",
    FindingCategory.REENTRANCY: "demo-agent-reentrancy-op-",
}
DEMO_ACTIVE_PROFILES = (
    ("established_strong", 7, FindingSeverity.MEDIUM),
    ("established_proven", 6, FindingSeverity.MEDIUM),
    ("established_developing", 5, FindingSeverity.INFORMATIONAL),
)


class DemoBootstrapError(ValueError):
    pass


class DemoBootstrapDisabledError(DemoBootstrapError):
    pass


class DemoBootstrapConfigurationError(DemoBootstrapError):
    pass


def bootstrap_gold_demo_network(
    protocol_data_root: Path,
    registry: LocalAgentRegistry,
    *,
    demo_mode: bool,
) -> DemoBootstrapResult:
    """Create demo-only runtime bindings and real derived historical state."""
    if not demo_mode:
        raise DemoBootstrapDisabledError("ProofGuard demo mode is disabled")

    demo_nodes = {
        category: _demo_nodes_for_category(protocol_data_root, category)
        for category in DEMO_CATEGORIES
    }
    _register_discovery_runtimes(protocol_data_root, registry)
    bootstrap_default_subnets(protocol_data_root)

    # CategoryPerformance resolves source workspaces from the protocol root's
    # sibling audits directory. Keep the synthetic project in that canonical
    # location so source verification uses the same path as ordinary projects.
    history_workspace = (
        protocol_data_root.parent / "audits" / DEMO_HISTORY_PROJECT_ID
    )
    historical_submission_ids: set[str] = set()
    profile_names: dict[str, str] = {}
    for category in DEMO_CATEGORIES:
        for profile_index, (profile_name, history_count, severity) in enumerate(
            DEMO_ACTIVE_PROFILES,
            start=1,
        ):
            node = demo_nodes[category][profile_index - 1]
            profile_names[node.node_id] = profile_name
            for history_index in range(1, history_count + 1):
                submission = _ensure_accepted_history(
                    protocol_data_root,
                    history_workspace,
                    node,
                    category,
                    profile_index,
                    history_index,
                    severity,
                )
                historical_submission_ids.add(submission.submission_id)
            rebuild_category_performance(
                protocol_data_root,
                node.node_id,
                category,
            )
            rebuild_category_score(
                protocol_data_root,
                node.node_id,
                category,
            )
        for node in demo_nodes[category][len(DEMO_ACTIVE_PROFILES) :]:
            profile_names[node.node_id] = "cold_start_exploration"

    categories: list[DemoBootstrapCategoryResult] = []
    for category in DEMO_CATEGORIES:
        subnet_id = f"subnet_{category.value}"
        refresh_subnet_memberships(protocol_data_root, subnet_id)
        _verify_selectable_runtimes(
            protocol_data_root,
            registry,
            subnet_id,
            category,
        )
        members = {
            member.node_id: member
            for member in list_subnet_members(protocol_data_root, subnet_id)
        }
        node_results: list[DemoBootstrapNodeResult] = []
        for node in demo_nodes[category]:
            member = load_subnet_member(
                protocol_data_root,
                subnet_id,
                node.node_id,
            )
            if member is None:
                raise DemoBootstrapConfigurationError(
                    f"Demo node '{node.node_id}' has no derived subnet membership"
                )
            score = load_category_score(
                protocol_data_root,
                node.node_id,
                category,
            )
            binding = registry.binding_for_node(node.node_id)
            node_results.append(
                DemoBootstrapNodeResult(
                    node_id=node.node_id,
                    operator_id=node.operator_id,
                    category=category,
                    runtime_type=binding.agent_type,
                    history_profile=profile_names[node.node_id],
                    category_score=(
                        score.category_score if score is not None else member.category_score
                    ),
                    finalized_submissions=member.finalized_submissions,
                    membership_status=member.status,
                )
            )
        active_count = sum(
            member.status in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT}
            for member in members.values()
        )
        candidate_count = sum(
            member.status == SubnetMemberStatus.CANDIDATE
            for member in members.values()
        )
        probation_count = sum(
            member.status == SubnetMemberStatus.PROBATION
            for member in members.values()
        )
        if active_count < 3:
            raise DemoBootstrapConfigurationError(
                f"Demo bootstrap produced fewer than three active {category.value} members"
            )
        if candidate_count + probation_count < 1:
            raise DemoBootstrapConfigurationError(
                f"Demo bootstrap left no exploration {category.value} member"
            )
        categories.append(
            DemoBootstrapCategoryResult(
                category=category,
                active_members=active_count,
                candidate_members=candidate_count,
                probation_members=probation_count,
                nodes=node_results,
            )
        )

    return DemoBootstrapResult(
        bootstrap_version=DEMO_BOOTSTRAP_VERSION,
        historical_submission_count=len(historical_submission_ids),
        runtime_node_ids=list(registry.node_ids()),
        categories=categories,
    )


def _demo_nodes_for_category(
    protocol_data_root: Path,
    category: FindingCategory,
) -> list[NodeRecord]:
    prefix = DEMO_OPERATOR_PREFIXES[category]
    nodes = sorted(
        (
            node
            for node in list_nodes(protocol_data_root)
            if node.operator_id.startswith(prefix)
        ),
        key=lambda node: (node.operator_id, node.node_id),
    )
    if len(nodes) < 4:
        raise DemoBootstrapConfigurationError(
            f"Gold Demo requires at least four specialist {category.value} nodes"
        )
    for node in nodes:
        if (
            node.node_type != NodeType.AGENT
            or node.status != NodeStatus.ACTIVE
            or node.supported_categories != [category.value]
        ):
            raise DemoBootstrapConfigurationError(
                f"Demo node '{node.node_id}' is not an active single-category agent"
            )
    return nodes


def _register_discovery_runtimes(
    protocol_data_root: Path,
    registry: LocalAgentRegistry,
) -> None:
    for node in list_nodes(protocol_data_root):
        relevant = [
            category
            for category in DEMO_CATEGORIES
            if category.value in node.supported_categories
        ]
        if not relevant or node.status != NodeStatus.ACTIVE:
            continue
        if node.node_type not in {NodeType.AGENT, NodeType.HYBRID}:
            continue
        if len(node.supported_categories) != 1 or len(relevant) != 1:
            raise DemoBootstrapConfigurationError(
                f"Local demo runtime requires specialist node '{node.node_id}'"
            )
        registry.register(category_local_binding(node.node_id, relevant[0]))


def _ensure_accepted_history(
    protocol_data_root: Path,
    history_workspace: Path,
    node: NodeRecord,
    category: FindingCategory,
    profile_index: int,
    history_index: int,
    severity: FindingSeverity,
) -> SubmissionRecord:
    serial = f"{category.value}_{profile_index}_{history_index}"
    finding_id = f"demo_history_{serial}"
    finding, _created = save_finding_exclusively(
        history_workspace,
        Finding(
            finding_id=finding_id,
            project_id=DEMO_HISTORY_PROJECT_ID,
            title=f"Historical calibrated {category.value} finding {profile_index}-{history_index}",
            category=category,
            severity=severity,
            confidence=0.90,
            contracts=["src/HistoricalCalibration.sol"],
            functions=[f"calibratedFunction{profile_index}_{history_index}"],
            root_cause="Deterministic historical authorization or interaction flaw.",
            attack_path="A synthetic local caller reaches the calibrated vulnerable path.",
            impact="The historical local fixture records a validated protocol impact.",
            conditions="Synthetic calibration input; no external target is contacted.",
            reproduction_steps=[],
            poc_type="none",
            poc_file=None,
            recommended_fix="Apply the category-appropriate defensive control.",
            status=FindingStatus.CANDIDATE,
            agent_name=f"{category.value}_demo_calibration_agent",
        ),
    )
    submission = next(
        (
            item
            for item in list_submissions(
                protocol_data_root,
                project_id=DEMO_HISTORY_PROJECT_ID,
                node_id=node.node_id,
            )
            if item.finding_id == finding_id
        ),
        None,
    )
    if submission is None:
        submission = create_submission(
            protocol_data_root,
            history_workspace,
            SubmissionCreate(
                project_id=DEMO_HISTORY_PROJECT_ID,
                finding_id=finding.finding_id,
                node_id=node.node_id,
                agent_name=f"{category.value}_demo_calibration_agent",
                agent_version=DEMO_BOOTSTRAP_VERSION,
                metadata={"fixture_version": DEMO_BOOTSTRAP_VERSION},
            ),
        )

    reproduction = load_reproduction_result(history_workspace, finding_id)
    if reproduction is None:
        reproduction = create_initial_reproduction_result(
            DEMO_HISTORY_PROJECT_ID,
            finding_id,
            history_workspace,
        )
    if reproduction.status != ReproductionStatus.REPRODUCED:
        reproduction = save_reproduction_result(
            reproduction.model_copy(
                update={
                    "status": ReproductionStatus.REPRODUCED,
                    "stdout": "Deterministic local calibration reproduction succeeded.",
                    "stderr": None,
                    "error_message": None,
                    "safety_notes": ["Synthetic local fixture; no external execution."],
                }
            ),
            history_workspace,
        )

    validation = load_validation_decision(history_workspace, finding_id)
    if validation is None:
        validation = create_validation_decision(
            DEMO_HISTORY_PROJECT_ID,
            finding_id,
            history_workspace,
            status=ValidationStatus.ACCEPTED,
            reason="Deterministic Gold Demo historical calibration outcome.",
            confidence=0.95,
            evidence=ValidationEvidence(
                has_finding=True,
                has_reproduction=True,
                reproduction_status=ReproductionStatus.REPRODUCED.value,
                has_poc_file=False,
                has_stdout=True,
                has_stderr=False,
                in_scope=True,
                is_duplicate=False,
                original_severity=severity.value,
                normalized_severity=severity.value,
                notes=["Application-supported synthetic history fixture."],
            ),
            validator_name="proofguard_demo_calibration_validator",
        )
    elif (
        validation.status != ValidationStatus.ACCEPTED
        or validation.evidence.normalized_severity != severity.value
    ):
        raise DemoBootstrapConfigurationError(
            f"Historical validation conflicts for finding '{finding_id}'"
        )

    if submission.status == SubmissionStatus.SUBMITTED:
        submission = update_submission_status(
            protocol_data_root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status=SubmissionStatus.VALIDATION_PENDING,
                reason="Demo historical reproduction source attached.",
                reproduction_id=reproduction.reproduction_id,
            ),
        )
    if submission.status == SubmissionStatus.VALIDATION_PENDING:
        submission = update_submission_status(
            protocol_data_root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status=SubmissionStatus.ACCEPTED,
                reason="Demo historical validation outcome finalized.",
                validation_id=validation.validation_id,
            ),
        )
    if (
        submission.status != SubmissionStatus.ACCEPTED
        or submission.reproduction_id != reproduction.reproduction_id
        or submission.validation_id != validation.validation_id
    ):
        raise DemoBootstrapConfigurationError(
            f"Historical submission conflicts for finding '{finding_id}'"
        )

    calculate_contribution_for_submission(
        protocol_data_root,
        history_workspace,
        submission.submission_id,
    )
    processed = process_submission_reputation(
        protocol_data_root,
        history_workspace,
        submission.submission_id,
    )
    if processed.event is None:
        raise DemoBootstrapConfigurationError(
            f"Historical reputation event was not applied for finding '{finding_id}'"
        )
    return submission


def _verify_selectable_runtimes(
    protocol_data_root: Path,
    registry: LocalAgentRegistry,
    subnet_id: str,
    category: FindingCategory,
) -> None:
    selectable = {
        SubnetMemberStatus.CANDIDATE,
        SubnetMemberStatus.PROBATION,
        SubnetMemberStatus.ACTIVE,
        SubnetMemberStatus.EXPERT,
    }
    for member in list_subnet_members(protocol_data_root, subnet_id):
        if member.status not in selectable:
            continue
        node = load_node(protocol_data_root, member.node_id)
        if node is None or node.node_type not in {NodeType.AGENT, NodeType.HYBRID}:
            raise DemoBootstrapConfigurationError(
                f"Selectable demo member '{member.node_id}' cannot execute agent work"
            )
        try:
            registry.resolve(node, category)
        except LocalAgentNotRegisteredError as exc:
            raise DemoBootstrapConfigurationError(
                f"Selectable demo member '{member.node_id}' has no local runtime"
            ) from exc
