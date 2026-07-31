from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
RESULTS_DIR = RESEARCH_ROOT / "results"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.schemas.finding import FindingCreate
from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.reproduction import ReproductionStatus
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.scope import ScopeManifest
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.subnet import SubnetMemberStatus, SubnetUpdate
from app.schemas.subnet_reward import SubnetRewardCycleCreateRequest
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.category_performance_service import (
    list_category_performance,
    rebuild_all_category_performance,
)
from app.services.category_scoring_service import (
    list_category_scores,
    rebuild_all_category_scores,
)
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    load_contribution_score,
)
from app.services.finding_service import save_findings_to_file
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    list_nodes,
    load_node,
)
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.reputation_service import (
    list_reputation_events,
    process_submission_reputation,
)
from app.services.scope_service import parse_scope_yaml, write_scope_files
from app.services.submission_service import (
    create_submission,
    list_submissions,
    load_submission,
    update_submission_status,
)
from app.services.subnet_membership_service import (
    administratively_remove_member,
    list_membership_events,
    refresh_subnet_memberships,
)
from app.services.subnet_registry_service import (
    bootstrap_default_subnets,
    find_subnet_by_category,
    list_subnet_members,
    update_subnet,
)
from app.services.subnet_reward_allocation_service import (
    calculate_subnet_reward_cycle,
    create_subnet_reward_cycle,
    finalize_subnet_reward_cycle,
    list_subnet_reward_events,
)
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
    list_project_routing_records,
    list_routing_usage_events,
    load_routing_record,
)
from app.services.validation_service import (
    create_validation_decision,
    load_validation_decision,
)
from research.reports.week6_subnet_report import render_week6_report
from research.schemas.week6_subnet_benchmark import (
    BENCHMARK_VERSION,
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    BenchmarkAssertionResult,
    SubnetBenchmarkCaseResult,
    Week6BenchmarkSummary,
    Week6CaseResultsDocument,
)


PROTOCOL_VERSION = "protocol_v0"
FIXED_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
HISTORY_PROJECT_ID = "benchmark_history_project"
ACCESS_PROJECT_ID = "benchmark_access_project"
MULTI_PROJECT_ID = "benchmark_multicategory_project"
RELEVANT_CATEGORIES = ("access_control", "reentrancy")
NODE_CATEGORIES = {
    "node_access_expert": ["access_control"],
    "node_access_active": ["access_control"],
    "node_access_weak": ["access_control"],
    "node_multi_category": ["access_control", "reentrancy"],
    "node_reentrancy_expert": ["reentrancy"],
    "node_reentrancy_weak": ["reentrancy"],
    "node_new_candidate": ["access_control"],
    "node_probation_explorer": ["access_control"],
    "node_unsafe": ["access_control"],
    "node_banned_control": ["access_control"],
    "node_removed_control": ["access_control"],
}
OUTCOME_STATUS = {
    "accepted_contribution": (
        ReproductionStatus.REPRODUCED,
        ValidationStatus.ACCEPTED,
        "accepted",
    ),
    "duplicate_finding": (
        ReproductionStatus.REPRODUCED,
        ValidationStatus.DUPLICATE,
        "duplicate",
    ),
    "rejected_finding": (
        ReproductionStatus.REPRODUCED,
        ValidationStatus.REJECTED,
        "rejected",
    ),
    "out_of_scope_finding": (
        ReproductionStatus.REPRODUCED,
        ValidationStatus.OUT_OF_SCOPE,
        "out_of_scope",
    ),
    "insufficient_evidence": (
        ReproductionStatus.FAILED,
        ValidationStatus.INSUFFICIENT_EVIDENCE,
        "insufficient_evidence",
    ),
    "unsafe_submission": (
        ReproductionStatus.REJECTED_UNSAFE,
        ValidationStatus.UNSAFE_POC,
        "unsafe",
    ),
    "unsupported_submission": (
        ReproductionStatus.UNSUPPORTED,
        ValidationStatus.UNSUPPORTED,
        "unsupported",
    ),
}


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


@dataclass(frozen=True)
class FixtureRoots:
    root: Path
    protocol_data_root: Path
    project_workspace_root: Path


@dataclass
class SyntheticSubmissionBundle:
    finding_id: str
    submission_id: str
    reproduction_id: str
    validation_id: str
    contribution_score_id: str
    reputation_event_id: str
    node_id: str
    project_id: str
    category: str
    outcome: str
    contribution_score: float
    reproduced: bool


@dataclass
class BenchmarkState:
    roots: FixtureRoots
    projects: dict[str, Path] = field(default_factory=dict)
    nodes: dict[str, Any] = field(default_factory=dict)
    histories: list[SyntheticSubmissionBundle] = field(default_factory=list)
    sequence: int = 0
    performances: dict[tuple[str, str], Any] = field(default_factory=dict)
    scores: dict[tuple[str, str], Any] = field(default_factory=dict)
    members: dict[tuple[str, str], Any] = field(default_factory=dict)
    access_routing: Any | None = None
    multi_routing: Any | None = None
    reward_cycle: Any | None = None
    reward_submissions: dict[str, SyntheticSubmissionBundle] = field(default_factory=dict)
    idempotency: dict[str, bool] = field(default_factory=dict)


@dataclass
class BenchmarkExecution:
    state: BenchmarkState
    cases: Week6CaseResultsDocument
    leaderboard: dict[str, Any]
    normalized: dict[str, Any]


@contextmanager
def temporary_fixture_roots(
    base: Path | None = None,
    *,
    keep: bool = False,
) -> Iterator[FixtureRoots]:
    if base is not None:
        root = base
        root.mkdir(parents=True, exist_ok=False)
        cleanup = not keep
    else:
        root = Path(tempfile.mkdtemp(prefix="proofguard-week6-"))
        cleanup = not keep
    roots = FixtureRoots(
        root=root,
        protocol_data_root=root / "data" / "protocol",
        project_workspace_root=root / "data" / "audits",
    )
    roots.protocol_data_root.mkdir(parents=True)
    roots.project_workspace_root.mkdir(parents=True)
    try:
        yield roots
    finally:
        if cleanup:
            shutil.rmtree(root, ignore_errors=True)


@contextmanager
def _fixed_service_clock() -> Iterator[None]:
    clock_targets = (
        "app.services.node_registry_service._utc_now",
        "app.services.reproduction_service._utc_now",
        "app.services.validation_service._utc_now",
        "app.services.submission_service._utc_now",
        "app.services.contribution_scoring_service._utc_now",
        "app.services.reputation_service._utc_now",
        "app.services.subnet_registry_service._utc_now",
        "app.services.category_performance_service._utc_now",
        "app.services.category_scoring_service._utc_now",
        "app.services.subnet_reward_allocation_service._utc_now",
    )
    with ExitStack() as stack:
        for target in clock_targets:
            stack.enter_context(patch(target, return_value=FIXED_NOW))
        stack.enter_context(
            patch("app.services.subnet_router_service.datetime", FrozenDateTime)
        )
        stack.enter_context(
            patch("app.services.subnet_membership_service.datetime", FrozenDateTime)
        )
        yield


def _with_uuid(target: str, value: str, function, *args, **kwargs):
    with patch(f"{target}.uuid.uuid4", return_value=value):
        return function(*args, **kwargs)


def create_project_fixture(
    roots: FixtureRoots,
    project_id: str,
    categories: list[str],
) -> Path:
    workspace = roots.project_workspace_root / project_id
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "findings").mkdir()
    scope_payload = {
        "project_name": project_id,
        "language": "Solidity",
        "framework": "Foundry",
        "chain": "local-synthetic",
        "commit_hash": "0" * 40,
        "contracts_in_scope": ["src/BenchmarkTarget.sol"],
        "contracts_out_of_scope": [],
        "assets_at_risk": ["synthetic benchmark state"],
        "attack_categories": categories,
        "known_issues": [],
        "forbidden_actions": [
            "No execution",
            "No network access",
            "No external services",
        ],
    }
    scope_text = json.dumps(scope_payload, indent=2).encode("utf-8")
    scope = parse_scope_yaml(scope_text)
    write_scope_files(workspace, scope_text, scope)
    (workspace / "metadata.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "project_name": project_id,
                "created_at": FIXED_NOW.isoformat(),
                "status": "created",
                "source_type": "synthetic",
                "github_url": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (workspace / "repo" / "src" / "BenchmarkTarget.sol").write_text(
        "// Synthetic inert source fixture. It is never analyzed or executed.\n",
        encoding="utf-8",
    )
    return workspace


def create_synthetic_nodes(state: BenchmarkState) -> None:
    for node_id, categories in NODE_CATEGORIES.items():
        state.nodes[node_id] = _with_uuid(
            "app.services.node_registry_service",
            node_id,
            create_node,
            state.roots.protocol_data_root,
            NodeCreate(
                node_type="agent",
                display_name=node_id.replace("_", "-"),
                operator_id=f"operator_{node_id}",
                public_key=f"benchmark-public-{node_id}",
                supported_categories=categories,
                description="Synthetic Week 6 benchmark node.",
            ),
        )
    state.nodes["node_banned_control"] = change_node_status(
        state.roots.protocol_data_root,
        "node_banned_control",
        NodeStatusChangeRequest(
            status="banned",
            reason="Synthetic banned-node routing security control.",
        ),
    )


def create_finalized_submission_history(
    state: BenchmarkState,
    *,
    node_id: str,
    project_id: str,
    category: str,
    outcome: str,
    routing_assignment: Any | None = None,
) -> SyntheticSubmissionBundle:
    if outcome not in OUTCOME_STATUS:
        raise ValueError(f"Unsupported synthetic outcome: {outcome}")
    state.sequence += 1
    serial = f"{state.sequence:03d}"
    workspace = state.projects[project_id]
    finding_id = f"finding_{serial}"
    finding_create = FindingCreate(
        title=f"Synthetic {category} finding {serial}",
        category=category,
        severity="High",
        confidence=0.90,
        contracts=["src/BenchmarkTarget.sol"],
        functions=[f"syntheticFunction{serial}"],
        root_cause=f"Deterministic synthetic root cause {serial}",
        attack_path=f"Deterministic synthetic attack path {serial}",
        impact=f"Deterministic synthetic impact {serial}",
        conditions=f"Deterministic synthetic condition {serial}",
        reproduction_steps=[],
        poc_type="none",
        poc_file=None,
        recommended_fix=f"Deterministic synthetic remediation {serial}",
        agent_name=f"{category}_benchmark_agent",
    )
    finding = _with_uuid(
        "app.services.finding_service",
        finding_id,
        save_findings_to_file,
        project_id,
        [finding_create],
        workspace / "findings",
        f"{finding_id}.json",
    )[0]
    routing_id = routing_assignment.routing_id if routing_assignment is not None else None
    assignment_id = (
        routing_assignment.assignment_id if routing_assignment is not None else None
    )
    submission = _with_uuid(
        "app.services.submission_service",
        f"submission_{serial}",
        create_submission,
        state.roots.protocol_data_root,
        workspace,
        SubmissionCreate(
            project_id=project_id,
            finding_id=finding.finding_id,
            node_id=node_id,
            agent_name=f"{category}_benchmark_agent",
            agent_version="week6-v0",
            routing_id=routing_id,
            routing_assignment_id=assignment_id,
            metadata={"fixture": "week6", "serial": serial},
        ),
    )
    reproduction = _with_uuid(
        "app.services.reproduction_service",
        f"reproduction_{serial}",
        create_initial_reproduction_result,
        project_id,
        finding.finding_id,
        workspace,
    )
    reproduction_status, validation_status, final_submission_status = OUTCOME_STATUS[
        outcome
    ]
    reproduction = save_reproduction_result(
        reproduction.model_copy(
            update={
                "status": reproduction_status,
                "stdout": (
                    "Synthetic reproduction succeeded."
                    if reproduction_status == ReproductionStatus.REPRODUCED
                    else None
                ),
                "error_message": (
                    "Synthetic non-success outcome."
                    if reproduction_status != ReproductionStatus.REPRODUCED
                    else None
                ),
                "safety_notes": (
                    ["Synthetic unsafe marker; no artifact was executed."]
                    if reproduction_status == ReproductionStatus.REJECTED_UNSAFE
                    else []
                ),
            }
        ),
        workspace,
    )
    in_scope = validation_status != ValidationStatus.OUT_OF_SCOPE
    is_duplicate = validation_status == ValidationStatus.DUPLICATE
    validation = _with_uuid(
        "app.services.validation_service",
        f"validation_{serial}",
        create_validation_decision,
        project_id,
        finding.finding_id,
        workspace,
        status=validation_status,
        reason=f"Synthetic finalized outcome: {outcome}.",
        confidence=0.95 if validation_status == ValidationStatus.ACCEPTED else 0.80,
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status=reproduction.status.value,
            has_poc_file=False,
            has_stdout=bool(reproduction.stdout),
            has_stderr=False,
            in_scope=in_scope,
            is_duplicate=is_duplicate,
            duplicate_of="synthetic_primary" if is_duplicate else None,
            original_severity="High",
            normalized_severity="High",
            notes=["Deterministic schema-valid benchmark evidence."],
        ),
        validator_name="synthetic_validator_v0",
    )
    submission = update_submission_status(
        state.roots.protocol_data_root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Synthetic reproduction source attached.",
            reproduction_id=reproduction.reproduction_id,
        ),
    )
    submission = update_submission_status(
        state.roots.protocol_data_root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status=final_submission_status,
            reason="Synthetic validation outcome finalized.",
            validation_id=validation.validation_id,
        ),
    )
    contribution = _with_uuid(
        "app.services.contribution_scoring_service",
        f"contribution_{serial}",
        calculate_contribution_for_submission,
        state.roots.protocol_data_root,
        workspace,
        submission.submission_id,
    )
    reputation = _with_uuid(
        "app.services.reputation_service",
        f"reputation_{serial}",
        process_submission_reputation,
        state.roots.protocol_data_root,
        workspace,
        submission.submission_id,
    )
    if reputation.event is None:
        raise RuntimeError("Finalized synthetic history did not create a ReputationEvent")
    bundle = SyntheticSubmissionBundle(
        finding_id=finding.finding_id,
        submission_id=submission.submission_id,
        reproduction_id=reproduction.reproduction_id,
        validation_id=validation.validation_id,
        contribution_score_id=contribution.score_id,
        reputation_event_id=reputation.event.event_id,
        node_id=node_id,
        project_id=project_id,
        category=category,
        outcome=outcome,
        contribution_score=contribution.total_score,
        reproduced=reproduction.status == ReproductionStatus.REPRODUCED,
    )
    state.histories.append(bundle)
    return bundle


def _generate_historical_profiles(state: BenchmarkState) -> None:
    profiles: list[tuple[str, str, list[str]]] = [
        ("node_access_expert", "access_control", ["accepted_contribution"] * 12),
        ("node_access_active", "access_control", ["accepted_contribution"] * 7),
        (
            "node_access_weak",
            "access_control",
            [
                "accepted_contribution",
                "duplicate_finding",
                "rejected_finding",
                "out_of_scope_finding",
                "insufficient_evidence",
                "rejected_finding",
                "unsupported_submission",
            ],
        ),
        ("node_multi_category", "access_control", ["accepted_contribution"] * 6),
        (
            "node_multi_category",
            "reentrancy",
            [
                "accepted_contribution",
                "duplicate_finding",
                "rejected_finding",
                "out_of_scope_finding",
                "insufficient_evidence",
                "unsupported_submission",
            ],
        ),
        (
            "node_reentrancy_expert",
            "reentrancy",
            ["accepted_contribution"] * 12,
        ),
        (
            "node_reentrancy_weak",
            "reentrancy",
            [
                "duplicate_finding",
                "rejected_finding",
                "out_of_scope_finding",
                "insufficient_evidence",
                "unsupported_submission",
                "rejected_finding",
            ],
        ),
        (
            "node_probation_explorer",
            "access_control",
            ["accepted_contribution"] * 4,
        ),
        (
            "node_unsafe",
            "access_control",
            ["accepted_contribution"] * 10 + ["unsafe_submission"],
        ),
    ]
    for node_id, category, outcomes in profiles:
        for outcome in outcomes:
            create_finalized_submission_history(
                state,
                node_id=node_id,
                project_id=HISTORY_PROJECT_ID,
                category=category,
                outcome=outcome,
            )


def _refresh_sources_and_memberships(state: BenchmarkState) -> None:
    first_performance = rebuild_all_category_performance(
        state.roots.protocol_data_root
    )
    first_perf_snapshot = {
        (record.node_id, record.category.value): (
            record.counts.model_dump(),
            record.source_fingerprint,
        )
        for record in first_performance
    }
    second_performance = rebuild_all_category_performance(
        state.roots.protocol_data_root
    )
    second_perf_snapshot = {
        (record.node_id, record.category.value): (
            record.counts.model_dump(),
            record.source_fingerprint,
        )
        for record in second_performance
    }
    state.idempotency["category_performance_rebuild"] = (
        first_perf_snapshot == second_perf_snapshot
    )
    state.performances = {
        (record.node_id, record.category.value): record
        for record in second_performance
    }

    first_batch = rebuild_all_category_scores(state.roots.protocol_data_root)
    second_batch = rebuild_all_category_scores(state.roots.protocol_data_root)
    if first_batch.errors or second_batch.errors:
        raise RuntimeError(
            "Category-score rebuild failed: "
            + "; ".join(first_batch.errors + second_batch.errors)
        )
    first_scores = first_batch.results
    second_scores = second_batch.results
    state.idempotency["category_score_rebuild"] = (
        all(item.status.value == "unchanged" for item in second_scores)
        and {
            (item.record.node_id, item.record.category.value): (
                item.record.category_score,
                item.record.source_fingerprint,
            )
            for item in first_scores
        }
        == {
            (item.record.node_id, item.record.category.value): (
                item.record.category_score,
                item.record.source_fingerprint,
            )
            for item in second_scores
        }
    )
    state.scores = {
        (item.record.node_id, item.record.category.value): item.record
        for item in second_scores
    }

    for category in RELEVANT_CATEGORIES:
        subnet_id = f"subnet_{category}"
        first = refresh_subnet_memberships(
            state.roots.protocol_data_root,
            subnet_id,
            evaluated_at=FIXED_NOW,
        )
        before_events = len(
            list_membership_events(state.roots.protocol_data_root, subnet_id)
        )
        second = refresh_subnet_memberships(
            state.roots.protocol_data_root,
            subnet_id,
            evaluated_at=FIXED_NOW,
        )
        after_events = len(
            list_membership_events(state.roots.protocol_data_root, subnet_id)
        )
        state.idempotency[f"membership_refresh_{category}"] = (
            not first.errors
            and not second.errors
            and second.unchanged_members == second.evaluated_nodes
            and before_events == after_events
        )
        for member in list_subnet_members(
            state.roots.protocol_data_root, subnet_id
        ):
            state.members[(member.node_id, category)] = member
    removed = administratively_remove_member(
        state.roots.protocol_data_root,
        "subnet_access_control",
        "node_removed_control",
        "Synthetic administrative removal security control.",
    )
    state.members[("node_removed_control", "access_control")] = (
        removed.current_member
    )


def _calculate_and_finalize_routing(
    state: BenchmarkState,
    *,
    project_id: str,
    request: ProjectRoutingRequest,
    routing_id: str,
) -> Any:
    workspace = state.projects[project_id]
    calculated = _with_uuid(
        "app.services.subnet_router_service",
        routing_id,
        calculate_project_routing,
        state.roots.protocol_data_root,
        workspace,
        project_id,
        request,
    )
    repeated_calculation = calculate_project_routing(
        state.roots.protocol_data_root,
        workspace,
        project_id,
        request,
    )
    state.idempotency[f"routing_calculation_{project_id}"] = (
        repeated_calculation.status.value == "unchanged"
        and repeated_calculation.record.routing_id == calculated.record.routing_id
    )
    finalized = finalize_project_routing(
        state.roots.protocol_data_root,
        workspace,
        project_id,
        calculated.record.routing_id,
    )
    before_usage = len(list_routing_usage_events(state.roots.protocol_data_root))
    repeated_finalization = finalize_project_routing(
        state.roots.protocol_data_root,
        workspace,
        project_id,
        calculated.record.routing_id,
    )
    after_usage = len(list_routing_usage_events(state.roots.protocol_data_root))
    state.idempotency[f"routing_finalization_{project_id}"] = (
        repeated_finalization.status.value == "already_finalized"
        and before_usage == after_usage
    )
    return finalized.record


def _assignment(routing: Any, category: str, node_id: str) -> Any:
    return next(
        assignment
        for result in routing.results
        if result.category.value == category
        for assignment in result.assignments
        if assignment.node_id == node_id
    )


def _create_reward_inputs(state: BenchmarkState) -> None:
    routing = state.multi_routing
    required = {
        "expert": ("access_control", "node_access_expert", "accepted_contribution"),
        "active": ("access_control", "node_access_active", "accepted_contribution"),
        "probation": (
            "access_control",
            "node_probation_explorer",
            "accepted_contribution",
        ),
        "candidate": (
            "access_control",
            "node_new_candidate",
            "accepted_contribution",
        ),
        "reentrancy_expert": (
            "reentrancy",
            "node_reentrancy_expert",
            "accepted_contribution",
        ),
        "duplicate": ("access_control", "node_access_active", "duplicate_finding"),
        "out_of_scope": (
            "access_control",
            "node_access_active",
            "out_of_scope_finding",
        ),
        "non_reproduced": (
            "access_control",
            "node_access_active",
            "insufficient_evidence",
        ),
    }
    for label, (category, node_id, outcome) in required.items():
        state.reward_submissions[label] = create_finalized_submission_history(
            state,
            node_id=node_id,
            project_id=MULTI_PROJECT_ID,
            category=category,
            outcome=outcome,
            routing_assignment=_assignment(routing, category, node_id),
        )
    state.reward_submissions["unlinked"] = create_finalized_submission_history(
        state,
        node_id="node_access_expert",
        project_id=MULTI_PROJECT_ID,
        category="access_control",
        outcome="accepted_contribution",
    )


def _create_calculate_finalize_reward(state: BenchmarkState) -> None:
    request = SubnetRewardCycleCreateRequest(
        routing_id=state.multi_routing.routing_id,
        total_pool_points="10000.000000",
        category_weights={
            "access_control": "3",
            "reentrancy": "2",
        },
        description="Synthetic Week 6 category-isolated reward benchmark.",
    )
    workspace = state.projects[MULTI_PROJECT_ID]
    first = _with_uuid(
        "app.services.subnet_reward_allocation_service",
        "reward_cycle_week6",
        create_subnet_reward_cycle,
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        request,
    )
    second = create_subnet_reward_cycle(
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        request,
    )
    state.idempotency["reward_cycle_creation"] = (
        first.reward_cycle_id == second.reward_cycle_id
    )
    calculated = calculate_subnet_reward_cycle(
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        first.reward_cycle_id,
    )
    unchanged = calculate_subnet_reward_cycle(
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        first.reward_cycle_id,
    )
    state.idempotency["reward_calculation"] = (
        unchanged.status.value == "unchanged"
        and unchanged.cycle.category_pools == calculated.cycle.category_pools
        and unchanged.cycle.source_fingerprint == calculated.cycle.source_fingerprint
    )
    source_snapshot = {
        "routing": deepcopy(
            load_routing_record(
                state.roots.protocol_data_root,
                MULTI_PROJECT_ID,
                state.multi_routing.routing_id,
            )
        ),
        "submissions": {
            label: deepcopy(
                load_submission(
                    state.roots.protocol_data_root, bundle.submission_id
                )
            )
            for label, bundle in state.reward_submissions.items()
        },
        "contributions": {
            label: deepcopy(
                load_contribution_score(
                    state.roots.protocol_data_root, bundle.submission_id
                )
            )
            for label, bundle in state.reward_submissions.items()
        },
        "validations": {
            label: deepcopy(
                load_validation_decision(
                    workspace, bundle.finding_id
                )
            )
            for label, bundle in state.reward_submissions.items()
        },
        "reproductions": {
            label: deepcopy(
                load_reproduction_result(
                    workspace, bundle.finding_id
                )
            )
            for label, bundle in state.reward_submissions.items()
        },
        "nodes": {
            node_id: deepcopy(load_node(state.roots.protocol_data_root, node_id))
            for node_id in NODE_CATEGORIES
        },
    }
    finalized = finalize_subnet_reward_cycle(
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        first.reward_cycle_id,
    )
    event_count = len(list_subnet_reward_events(state.roots.protocol_data_root))
    repeated = finalize_subnet_reward_cycle(
        state.roots.protocol_data_root,
        workspace,
        MULTI_PROJECT_ID,
        first.reward_cycle_id,
    )
    state.idempotency["reward_finalization"] = (
        repeated.status.value == "already_finalized"
        and len(list_subnet_reward_events(state.roots.protocol_data_root))
        == event_count
    )
    state.idempotency["reward_source_immutability"] = (
        source_snapshot["routing"]
        == load_routing_record(
            state.roots.protocol_data_root,
            MULTI_PROJECT_ID,
            state.multi_routing.routing_id,
        )
        and all(
            source_snapshot["submissions"][label]
            == load_submission(state.roots.protocol_data_root, bundle.submission_id)
            and source_snapshot["contributions"][label]
            == load_contribution_score(
                state.roots.protocol_data_root, bundle.submission_id
            )
            and source_snapshot["validations"][label]
            == load_validation_decision(workspace, bundle.finding_id)
            and source_snapshot["reproductions"][label]
            == load_reproduction_result(workspace, bundle.finding_id)
            for label, bundle in state.reward_submissions.items()
        )
        and all(
            source_snapshot["nodes"][node_id]
            == load_node(state.roots.protocol_data_root, node_id)
            for node_id in NODE_CATEGORIES
        )
    )
    state.reward_cycle = finalized.cycle


def _assert(
    assertion_id: str,
    description: str,
    expected: Any,
    actual: Any,
    passed: bool | None = None,
) -> BenchmarkAssertionResult:
    return BenchmarkAssertionResult(
        assertion_id=assertion_id,
        description=description,
        passed=(actual == expected if passed is None else passed),
        expected=_json_value(expected),
        actual=_json_value(actual),
        details=None,
    )


def _case(
    case_id: str,
    title: str,
    description: str,
    phase: str,
    assertions: list[BenchmarkAssertionResult],
    *,
    nodes: list[str],
    projects: list[str],
    subnets: list[str],
    metrics: dict[str, Any],
    setup: list[str],
    operations: list[str],
    expected_behavior: list[str],
    actual_behavior: list[str],
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
) -> SubnetBenchmarkCaseResult:
    errors = errors or []
    return SubnetBenchmarkCaseResult(
        case_id=case_id,
        title=title,
        description=description,
        passed=all(item.passed for item in assertions) and not errors,
        phase=phase,
        setup_summary=setup,
        service_operations=operations,
        expected_behavior=expected_behavior,
        actual_behavior=actual_behavior,
        node_ids=nodes,
        project_ids=projects,
        subnet_ids=subnets,
        assertions=assertions,
        metrics={key: _json_value(value) for key, value in metrics.items()},
        warnings=warnings or [],
        errors=errors,
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
        duration_ms=0,
    )


def _build_cases(state: BenchmarkState) -> Week6CaseResultsDocument:
    access_member = lambda node: state.members[(node, "access_control")]
    re_member = lambda node: state.members[(node, "reentrancy")]
    access_result = next(
        item
        for item in state.access_routing.results
        if item.category.value == "access_control"
    )
    multi_access = next(
        item
        for item in state.multi_routing.results
        if item.category.value == "access_control"
    )
    multi_reentrancy = next(
        item
        for item in state.multi_routing.results
        if item.category.value == "reentrancy"
    )
    access_assignments = {item.node_id: item for item in access_result.assignments}
    all_multi_assignments = [
        item for result in state.multi_routing.results for item in result.assignments
    ]
    usage = list_routing_usage_events(state.roots.protocol_data_root)
    reward_events = list_subnet_reward_events(state.roots.protocol_data_root)
    pools = {
        pool.category.value: pool for pool in state.reward_cycle.category_pools
    }
    allocations = [
        allocation
        for pool in state.reward_cycle.category_pools
        for allocation in pool.allocations
    ]
    allocation_by_submission = {
        allocation.submission_id: allocation for allocation in allocations
    }
    exclusions = [
        exclusion
        for pool in state.reward_cycle.category_pools
        for exclusion in pool.exclusions
    ]
    exclusion_reasons = {
        exclusion.submission_id: [reason.value for reason in exclusion.reasons]
        for exclusion in exclusions
    }
    events_by_submission = {event.submission_id: event for event in reward_events}
    expert_threshold = max(
        0.8,
        find_subnet_by_category(
            state.roots.protocol_data_root, "access_control"
        ).minimum_category_score,
    )

    case1 = _case(
        "best_access_control_selected",
        "Best access-control nodes are selected",
        "Verify that the strongest qualified access-control nodes receive ranked production assignments.",
        "routing",
        [
            _assert(
                "access_expert_membership",
                "The strongest access-control node is an expert.",
                "expert",
                access_member("node_access_expert").status.value,
            ),
            _assert(
                "access_expert_threshold",
                "The expert score satisfies the production expert threshold.",
                f">={expert_threshold}",
                state.scores[
                    ("node_access_expert", "access_control")
                ].category_score,
                state.scores[
                    ("node_access_expert", "access_control")
                ].category_score
                >= expert_threshold,
            ),
            _assert(
                "access_expert_ranked",
                "The expert uses ranked selection.",
                "ranked",
                access_assignments["node_access_expert"].selection_type.value,
            ),
            _assert(
                "access_expert_production",
                "The expert uses production mode.",
                "production",
                access_assignments["node_access_expert"].assignment_mode.value,
            ),
            _assert(
                "access_active_production",
                "The qualified active node also receives production.",
                "production",
                access_assignments["node_access_active"].assignment_mode.value,
            ),
            _assert(
                "access_weak_not_production",
                "The weak access-control node is not selected for production.",
                False,
                any(
                    item.node_id == "node_access_weak"
                    and item.assignment_mode.value == "production"
                    for item in access_result.assignments
                ),
            ),
            _assert(
                "access_selection_reasons",
                "Every selected access-control node has reasons.",
                True,
                all(item.selection_reasons for item in access_result.assignments),
            ),
            _assert(
                "access_assignment_identity",
                "All assignments use exactly the access-control subnet.",
                True,
                all(
                    item.category.value == "access_control"
                    and item.subnet_id == "subnet_access_control"
                    for item in access_result.assignments
                ),
            ),
        ],
        nodes=[
            "node_access_expert",
            "node_access_active",
            "node_access_weak",
        ],
        projects=[ACCESS_PROJECT_ID],
        subnets=["subnet_access_control"],
        metrics={
            "expert_score": state.scores[
                ("node_access_expert", "access_control")
            ].category_score,
            "production_assignments": access_result.ranked_selected,
            "shadow_assignments": access_result.exploration_selected,
        },
        setup=["Active access-control subnet with expert, active, and developing members."],
        operations=["Subnet Membership Manager refresh", "Subnet Router calculation and finalization"],
        expected_behavior=["Qualified expert and active nodes receive production; weak nodes do not."],
        actual_behavior=[
            "Production nodes: "
            + ", ".join(
                item.node_id
                for item in access_result.assignments
                if item.assignment_mode.value == "production"
            )
        ],
    )

    weak_re_score = state.scores[
        ("node_reentrancy_weak", "reentrancy")
    ].category_score
    case2 = _case(
        "weak_reentrancy_not_selected",
        "Weak reentrancy node is not selected",
        "Verify that weak category history cannot become a ranked production fallback.",
        "routing",
        [
            _assert(
                "weak_re_performance_exists",
                "The weak node has real reentrancy performance.",
                True,
                ("node_reentrancy_weak", "reentrancy") in state.performances,
            ),
            _assert(
                "weak_re_score_valid",
                "The weak node has a bounded score.",
                "0..1",
                weak_re_score,
                0 <= weak_re_score <= 1,
            ),
            _assert(
                "weak_re_not_core",
                "The weak membership is not active or expert.",
                True,
                re_member("node_reentrancy_weak").status
                not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT},
            ),
            _assert(
                "weak_re_not_ranked",
                "The weak node has no ranked assignment.",
                False,
                any(
                    item.node_id == "node_reentrancy_weak"
                    and item.selection_type.value == "ranked"
                    for item in multi_reentrancy.assignments
                ),
            ),
            _assert(
                "weak_re_not_production",
                "The weak node has no production assignment.",
                False,
                any(
                    item.node_id == "node_reentrancy_weak"
                    and item.assignment_mode.value == "production"
                    for item in multi_reentrancy.assignments
                ),
            ),
            _assert(
                "re_expert_selected",
                "The reentrancy expert receives production.",
                True,
                any(
                    item.node_id == "node_reentrancy_expert"
                    and item.assignment_mode.value == "production"
                    for item in multi_reentrancy.assignments
                ),
            ),
            _assert(
                "re_partial_safe",
                "A production shortage remains partial instead of promoting a weak node.",
                False,
                multi_reentrancy.complete,
            ),
        ],
        nodes=["node_reentrancy_expert", "node_reentrancy_weak"],
        projects=[MULTI_PROJECT_ID],
        subnets=["subnet_reentrancy"],
        metrics={
            "weak_score": weak_re_score,
            "ranked_selected": multi_reentrancy.ranked_selected,
            "requested": multi_reentrancy.requested_assignments,
        },
        setup=["One expert and weak developing reentrancy histories."],
        operations=["Category performance and scoring", "Membership refresh", "Multi-category routing"],
        expected_behavior=["Weak membership is never an unsafe production fallback."],
        actual_behavior=["The expert was selected; the shortage stayed explicit and partial."],
    )

    exploration = [
        item
        for item in access_result.assignments
        if item.selection_type.value == "exploration"
    ]
    case3 = _case(
        "new_node_exploration",
        "Developing nodes receive shadow exploration",
        "Verify candidate and probation exploration without production authority or promotion.",
        "routing",
        [
            _assert(
                "candidate_membership",
                "The new node remains candidate.",
                "candidate",
                access_member("node_new_candidate").status.value,
            ),
            _assert(
                "probation_membership",
                "The developing node is probation.",
                "probation",
                access_member("node_probation_explorer").status.value,
            ),
            _assert(
                "developing_not_ranked",
                "Neither developing node receives ranked production.",
                False,
                any(
                    item.node_id
                    in {"node_new_candidate", "node_probation_explorer"}
                    and item.selection_type.value == "ranked"
                    for item in access_result.assignments
                ),
            ),
            _assert(
                "shadow_exploration_present",
                "At least one candidate/probation node receives exploration shadow.",
                True,
                bool(exploration)
                and all(
                    item.assignment_mode.value == "shadow"
                    and item.membership_status.value in {"candidate", "probation"}
                    for item in exploration
                ),
            ),
            _assert(
                "production_remains_core",
                "Production assignments remain active/expert.",
                True,
                all(
                    item.membership_status.value in {"active", "expert"}
                    for item in access_result.assignments
                    if item.assignment_mode.value == "production"
                ),
            ),
            _assert(
                "exploration_reasons",
                "Exploration reasons are present.",
                True,
                all(item.selection_reasons for item in exploration),
            ),
            _assert(
                "qualification_null",
                "Qualification references remain null in v0.",
                True,
                all(item.qualification_reference is None for item in exploration),
            ),
            _assert(
                "selection_no_promotion",
                "Routing does not promote candidate or probation membership.",
                ["candidate", "probation"],
                [
                    access_member("node_new_candidate").status.value,
                    access_member("node_probation_explorer").status.value,
                ],
            ),
            _assert(
                "shadow_usage_once",
                "Each shadow assignment creates one finalized usage event.",
                len(exploration),
                sum(
                    event.routing_id == state.access_routing.routing_id
                    and event.assignment_mode.value == "shadow"
                    for event in usage
                ),
            ),
            _assert(
                "routing_finalize_idempotent",
                "Repeated access-route finalization does not duplicate usage.",
                True,
                state.idempotency[f"routing_finalization_{ACCESS_PROJECT_ID}"],
            ),
        ],
        nodes=["node_new_candidate", "node_probation_explorer"],
        projects=[ACCESS_PROJECT_ID],
        subnets=["subnet_access_control"],
        metrics={
            "exploration_assignments": len(exploration),
            "first_explorer": exploration[0].node_id if exploration else "none",
        },
        setup=["Exploration ratio 0.40 with both candidate and probation members."],
        operations=["Production Subnet Router calculation", "Idempotent routing finalization"],
        expected_behavior=["Developing nodes receive deterministic shadow-only opportunities."],
        actual_behavior=[
            "Shadow nodes: " + ", ".join(item.node_id for item in exploration)
        ],
    )

    unsafe_member = access_member("node_unsafe")
    unsafe_score = state.scores[("node_unsafe", "access_control")].category_score
    unsafe_events = [
        item
        for item in list_reputation_events(state.roots.protocol_data_root)
        if item.node_id == "node_unsafe"
        and item.event_type.value == "unsafe_submission"
    ]
    case4 = _case(
        "unsafe_node_suspended",
        "Unsafe node is suspended",
        "Verify that a recent unsafe event overrides history, score, and routing eligibility.",
        "membership_and_routing",
        [
            _assert(
                "unsafe_event_found",
                "A recent applied unsafe event is present.",
                1,
                len(unsafe_events),
            ),
            _assert(
                "unsafe_suspended",
                "Unsafe membership is suspended.",
                "suspended",
                unsafe_member.status.value,
            ),
            _assert(
                "unsafe_reason",
                "Suspension explains the recent unsafe event.",
                True,
                "recent_unsafe_submission" in unsafe_member.status_reason_codes,
            ),
            _assert(
                "score_does_not_override",
                "A calculated score cannot override suspension.",
                True,
                0 <= unsafe_score <= 1
                and unsafe_member.status.value == "suspended",
            ),
            _assert(
                "unsafe_no_assignment",
                "Unsafe node receives neither production nor shadow work.",
                False,
                any(
                    item.node_id == "node_unsafe"
                    for item in access_result.assignments + multi_access.assignments
                ),
            ),
            _assert(
                "unsafe_no_usage",
                "Unsafe node receives no routing usage event.",
                0,
                sum(item.node_id == "node_unsafe" for item in usage),
            ),
            _assert(
                "unsafe_no_reward",
                "Unsafe node receives no subnet reward event.",
                0,
                sum(item.node_id == "node_unsafe" for item in reward_events),
            ),
            _assert(
                "unsafe_hysteresis_blocked",
                "Safety override wins independently of previous core status.",
                True,
                unsafe_member.status.value == "suspended",
            ),
            _assert(
                "banned_node_excluded",
                "The banned-node control receives no production or shadow assignment.",
                False,
                any(
                    item.node_id == "node_banned_control"
                    for item in access_result.assignments
                    + multi_access.assignments
                ),
            ),
            _assert(
                "removed_member_excluded",
                "The removed-member control receives no production or shadow assignment.",
                False,
                any(
                    item.node_id == "node_removed_control"
                    for item in access_result.assignments
                    + multi_access.assignments
                ),
            ),
            _assert(
                "blocked_controls_no_usage_or_reward",
                "Banned and removed controls create no usage or reward event.",
                0,
                sum(
                    item.node_id
                    in {"node_banned_control", "node_removed_control"}
                    for item in [*usage, *reward_events]
                ),
            ),
        ],
        nodes=["node_unsafe", "node_banned_control", "node_removed_control"],
        projects=[HISTORY_PROJECT_ID, ACCESS_PROJECT_ID, MULTI_PROJECT_ID],
        subnets=["subnet_access_control"],
        metrics={"category_score": unsafe_score, "unsafe_events": len(unsafe_events)},
        setup=["Strong accepted history plus one applied unsafe event at the fixed evaluation time."],
        operations=["Week 5 reputation processing", "Membership safety override", "Routing exclusion"],
        expected_behavior=["Unsafe participation is suspended regardless of score."],
        actual_behavior=["The node is suspended and absent from assignments, usage, and rewards."],
    )

    multi_access_perf = state.performances[
        ("node_multi_category", "access_control")
    ]
    multi_re_perf = state.performances[("node_multi_category", "reentrancy")]
    multi_access_score = state.scores[
        ("node_multi_category", "access_control")
    ].category_score
    multi_re_score = state.scores[
        ("node_multi_category", "reentrancy")
    ].category_score
    case5 = _case(
        "category_score_isolation",
        "Category scores remain isolated",
        "Verify that one node can be strong in access control and weak in reentrancy.",
        "aggregation_scoring_membership_routing",
        [
            _assert(
                "two_performance_records",
                "Separate performance records exist.",
                2,
                sum(
                    key[0] == "node_multi_category"
                    for key in state.performances
                ),
            ),
            _assert(
                "source_lists_disjoint",
                "Category source event lists are disjoint.",
                True,
                set(multi_access_perf.source_event_ids).isdisjoint(
                    multi_re_perf.source_event_ids
                ),
            ),
            _assert(
                "accepted_access_isolated",
                "Access accepted history stays in access control.",
                6,
                multi_access_perf.counts.accepted_unique_submissions,
            ),
            _assert(
                "invalid_re_isolated",
                "Reentrancy invalid outcomes do not reduce access counters.",
                0,
                multi_access_perf.counts.rejected_submissions
                + multi_access_perf.counts.duplicate_submissions
                + multi_access_perf.counts.out_of_scope_submissions,
            ),
            _assert(
                "two_score_records",
                "Separate category scores exist.",
                True,
                ("node_multi_category", "access_control") in state.scores
                and ("node_multi_category", "reentrancy") in state.scores,
            ),
            _assert(
                "access_materially_higher",
                "Access score is materially higher.",
                "> reentrancy by 0.20",
                round(multi_access_score - multi_re_score, 6),
                multi_access_score - multi_re_score >= 0.20,
            ),
            _assert(
                "access_core_membership",
                "Access membership is production eligible.",
                True,
                access_member("node_multi_category").status
                in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT},
            ),
            _assert(
                "re_nonproduction_membership",
                "Reentrancy membership is non-production.",
                True,
                re_member("node_multi_category").status
                not in {SubnetMemberStatus.ACTIVE, SubnetMemberStatus.EXPERT},
            ),
            _assert(
                "access_route_can_select",
                "Access routing selects the multi-category node for production.",
                True,
                any(
                    item.node_id == "node_multi_category"
                    and item.assignment_mode.value == "production"
                    for item in multi_access.assignments
                ),
            ),
            _assert(
                "re_route_cannot_borrow",
                "Reentrancy cannot borrow the access score for production.",
                False,
                any(
                    item.node_id == "node_multi_category"
                    and item.assignment_mode.value == "production"
                    for item in multi_reentrancy.assignments
                ),
            ),
        ],
        nodes=["node_multi_category"],
        projects=[HISTORY_PROJECT_ID, MULTI_PROJECT_ID],
        subnets=["subnet_access_control", "subnet_reentrancy"],
        metrics={
            "access_score": multi_access_score,
            "reentrancy_score": multi_re_score,
            "access_events": len(multi_access_perf.source_event_ids),
            "reentrancy_events": len(multi_re_perf.source_event_ids),
        },
        setup=["One node with six accepted access-control outcomes and mostly invalid reentrancy outcomes."],
        operations=["Category Performance rebuild", "Category Score rebuild", "Membership refresh", "Router"],
        expected_behavior=["Access-control strength cannot confer reentrancy production authority."],
        actual_behavior=["The node is production-eligible only in access control."],
    )

    category_assignment_sum = sum(
        len(result.assignments) for result in state.multi_routing.results
    )
    case6 = _case(
        "multi_category_routing",
        "Multi-category project uses separate subnets",
        "Verify independent access-control and reentrancy routing for one project.",
        "routing",
        [
            _assert(
                "two_requested_categories",
                "Both scope categories are routed.",
                ["access_control", "reentrancy"],
                [item.value for item in state.multi_routing.requested_categories],
            ),
            _assert(
                "access_subnet",
                "Access control uses its exact subnet.",
                "subnet_access_control",
                multi_access.subnet_id,
            ),
            _assert(
                "reentrancy_subnet",
                "Reentrancy uses its exact subnet.",
                "subnet_reentrancy",
                multi_reentrancy.subnet_id,
            ),
            _assert(
                "assignments_separated",
                "Each category result contains only matching assignments.",
                True,
                all(
                    assignment.category == result.category
                    for result in state.multi_routing.results
                    for assignment in result.assignments
                ),
            ),
            _assert(
                "assignment_subnet_match",
                "Every assignment references its category subnet.",
                True,
                all(
                    assignment.subnet_id == f"subnet_{assignment.category.value}"
                    for assignment in all_multi_assignments
                ),
            ),
            _assert(
                "assignment_membership_match",
                "Every selected node has matching category membership.",
                True,
                all(
                    state.members[(assignment.node_id, assignment.category.value)].status
                    == assignment.membership_status
                    for assignment in all_multi_assignments
                ),
            ),
            _assert(
                "assignment_supported_category",
                "Every selected node declares the exact assignment category.",
                True,
                all(
                    assignment.category.value
                    in load_node(
                        state.roots.protocol_data_root, assignment.node_id
                    ).supported_categories
                    for assignment in all_multi_assignments
                ),
            ),
            _assert(
                "multi_node_modes",
                "The multi-category node can differ by category.",
                True,
                any(
                    item.node_id == "node_multi_category"
                    and item.assignment_mode.value == "production"
                    for item in multi_access.assignments
                )
                and not any(
                    item.node_id == "node_multi_category"
                    and item.assignment_mode.value == "production"
                    for item in multi_reentrancy.assignments
                ),
            ),
            _assert(
                "category_score_snapshot_match",
                "Assignments use their exact category score snapshots.",
                True,
                all(
                    assignment.category_score
                    == state.members[
                        (assignment.node_id, assignment.category.value)
                    ].category_score
                    for assignment in all_multi_assignments
                ),
            ),
            _assert(
                "routing_total",
                "Routing total equals category totals.",
                category_assignment_sum,
                state.multi_routing.total_assignments,
            ),
            _assert(
                "usage_total_once",
                "Finalization creates one usage event per assignment.",
                state.multi_routing.total_assignments,
                sum(
                    item.routing_id == state.multi_routing.routing_id
                    for item in usage
                ),
            ),
            _assert(
                "multi_finalize_idempotent",
                "Repeated multi-route finalization does not duplicate usage.",
                True,
                state.idempotency[
                    f"routing_finalization_{MULTI_PROJECT_ID}"
                ],
            ),
        ],
        nodes=sorted({item.node_id for item in all_multi_assignments}),
        projects=[MULTI_PROJECT_ID],
        subnets=["subnet_access_control", "subnet_reentrancy"],
        metrics={
            "total_assignments": state.multi_routing.total_assignments,
            "production": state.multi_routing.production_assignments,
            "shadow": state.multi_routing.shadow_assignments,
        },
        setup=["One scope manifest declaring access control and reentrancy."],
        operations=["Production multi-category route calculation and finalization"],
        expected_behavior=["Each category routes only through its own subnet and snapshots."],
        actual_behavior=["Both category results are present with isolated assignments."],
    )

    def reward_for(label: str):
        return allocation_by_submission.get(
            state.reward_submissions[label].submission_id
        )

    case7 = _case(
        "subnet_reward_distribution",
        "Subnet rewards stay category isolated",
        "Verify eligibility, multipliers, proportional allocation, exact conservation, and immutable finalization.",
        "reward_allocation",
        [
            _assert(
                "category_pool_total",
                "Category pools equal the project pool.",
                "10000.000000",
                _points(
                    sum(
                        (
                            pool.allocated_pool_points
                            for pool in state.reward_cycle.category_pools
                        ),
                        Decimal("0"),
                    )
                ),
            ),
            _assert(
                "access_pool",
                "The 3/5 access-control pool is exact.",
                "6000.000000",
                _points(pools["access_control"].allocated_pool_points),
            ),
            _assert(
                "reentrancy_pool",
                "The 2/5 reentrancy pool is exact.",
                "4000.000000",
                _points(pools["reentrancy"].allocated_pool_points),
            ),
            _assert(
                "allocation_category_isolation",
                "Every allocation stays in its submission category.",
                True,
                all(
                    allocation.category.value
                    == load_submission(
                        state.roots.protocol_data_root,
                        allocation.submission_id,
                    ).category
                    for allocation in allocations
                ),
            ),
            _assert(
                "expert_multiplier",
                "Expert production uses multiplier 1.10.",
                "1.10",
                str(reward_for("expert").membership_multiplier),
            ),
            _assert(
                "active_multiplier",
                "Active production uses multiplier 1.00.",
                "1.00",
                str(reward_for("active").membership_multiplier),
            ),
            _assert(
                "probation_multiplier",
                "Probation shadow uses multiplier 0.90.",
                "0.90",
                str(reward_for("probation").membership_multiplier),
            ),
            _assert(
                "candidate_no_allocation",
                "Candidate shadow receives no allocation.",
                None,
                _json_value(reward_for("candidate")),
            ),
            _assert(
                "candidate_exclusion_reason",
                "Candidate exclusion is explicit.",
                True,
                "candidate_shadow_ineligible"
                in exclusion_reasons[
                    state.reward_submissions["candidate"].submission_id
                ],
            ),
            _assert(
                "duplicate_no_allocation",
                "Duplicate contribution receives no allocation.",
                None,
                _json_value(reward_for("duplicate")),
            ),
            _assert(
                "invalid_outcomes_excluded",
                "Out-of-scope and non-reproduced contributions receive no allocation.",
                True,
                reward_for("out_of_scope") is None
                and reward_for("non_reproduced") is None,
            ),
            _assert(
                "unlinked_no_reward",
                "An unlinked accepted submission receives no subnet reward.",
                False,
                state.reward_submissions["unlinked"].submission_id
                in events_by_submission,
            ),
            _assert(
                "routing_score_snapshot",
                "Category multiplier inputs use the routing-time score snapshot.",
                True,
                all(
                    allocation.category_score
                    == allocation.source_snapshot.routing_assignment_category_score
                    for allocation in allocations
                ),
            ),
            _assert(
                "raw_weights_schema_validated",
                "Production allocation models validate stored raw weights.",
                True,
                all(allocation.raw_weight > 0 for allocation in allocations),
            ),
            _assert(
                "category_conservation",
                "Every category exactly conserves its pool.",
                True,
                all(
                    pool.distributed_points + pool.undistributed_points
                    == pool.allocated_pool_points
                    for pool in state.reward_cycle.category_pools
                ),
            ),
            _assert(
                "cycle_conservation",
                "Distributed plus undistributed equals total pool.",
                state.reward_cycle.total_pool_points,
                state.reward_cycle.total_distributed_points
                + state.reward_cycle.total_undistributed_points,
            ),
            _assert(
                "immutable_events",
                "Finalization creates one event per eligible allocation.",
                len(allocations),
                len(reward_events),
            ),
            _assert(
                "unique_rewarded_submission",
                "No submission is rewarded twice.",
                len(reward_events),
                len({event.submission_id for event in reward_events}),
            ),
            _assert(
                "reward_create_idempotent",
                "Repeated reward-cycle creation returns the same cycle.",
                True,
                state.idempotency["reward_cycle_creation"],
            ),
            _assert(
                "reward_calculate_idempotent",
                "Repeated unchanged calculation returns the same allocations.",
                True,
                state.idempotency["reward_calculation"],
            ),
            _assert(
                "reward_finalize_idempotent",
                "Repeated finalization creates no duplicate event.",
                True,
                state.idempotency["reward_finalization"],
            ),
            _assert(
                "reward_sources_immutable",
                "Reward finalization does not mutate source records.",
                True,
                state.idempotency["reward_source_immutability"],
            ),
            _assert(
                "unsafe_no_reward_allocation",
                "Unsafe node has no allocation.",
                False,
                any(item.node_id == "node_unsafe" for item in allocations),
            ),
        ],
        nodes=sorted({item.node_id for item in all_multi_assignments}),
        projects=[MULTI_PROJECT_ID],
        subnets=["subnet_access_control", "subnet_reentrancy"],
        metrics={
            "eligible_allocations": len(allocations),
            "excluded_submissions": len(exclusions),
            "reward_events": len(reward_events),
            "distributed_points": _points(
                state.reward_cycle.total_distributed_points
            ),
            "undistributed_points": _points(
                state.reward_cycle.total_undistributed_points
            ),
        },
        setup=["Finalized multi-category route and 10,000.000000 simulated protocol-point pool."],
        operations=["Routed submissions", "Reward cycle create/calculate/finalize", "Repeated finalization"],
        expected_behavior=["Only eligible category-linked contributions share their own category pool."],
        actual_behavior=["Expert, active, probation, and reentrancy-expert contributions were allocated; invalid and candidate contributions were excluded."],
    )
    return Week6CaseResultsDocument(
        benchmark_version=BENCHMARK_VERSION,
        cases=[case1, case2, case3, case4, case5, case6, case7],
    )


def execute_benchmark_once(roots: FixtureRoots) -> BenchmarkExecution:
    state = BenchmarkState(roots=roots)
    with _fixed_service_clock():
        first_bootstrap = bootstrap_default_subnets(roots.protocol_data_root)
        access_subnet = find_subnet_by_category(
            roots.protocol_data_root, "access_control"
        )
        update_subnet(
            roots.protocol_data_root,
            access_subnet.subnet_id,
            SubnetUpdate(exploration_ratio=0.40),
        )
        second_bootstrap = bootstrap_default_subnets(roots.protocol_data_root)
        preserved = find_subnet_by_category(
            roots.protocol_data_root, "access_control"
        )
        state.idempotency["subnet_bootstrap"] = (
            len(first_bootstrap) == len(second_bootstrap)
            and len({item.subnet_id for item in second_bootstrap})
            == len(second_bootstrap)
            and preserved.exploration_ratio == 0.40
        )
        state.projects = {
            HISTORY_PROJECT_ID: create_project_fixture(
                roots, HISTORY_PROJECT_ID, list(RELEVANT_CATEGORIES)
            ),
            ACCESS_PROJECT_ID: create_project_fixture(
                roots, ACCESS_PROJECT_ID, ["access_control"]
            ),
            MULTI_PROJECT_ID: create_project_fixture(
                roots, MULTI_PROJECT_ID, list(RELEVANT_CATEGORIES)
            ),
        }
        create_synthetic_nodes(state)
        _generate_historical_profiles(state)
        _refresh_sources_and_memberships(state)
        state.access_routing = _calculate_and_finalize_routing(
            state,
            project_id=ACCESS_PROJECT_ID,
            request=ProjectRoutingRequest(
                categories=["access_control"],
                nodes_per_category=5,
                include_exploration=True,
                allow_partial=True,
                description="Deterministic access-control benchmark route.",
            ),
            routing_id="routing_access_week6",
        )
        state.multi_routing = _calculate_and_finalize_routing(
            state,
            project_id=MULTI_PROJECT_ID,
            request=ProjectRoutingRequest(
                categories=None,
                nodes_per_category=5,
                include_exploration=True,
                allow_partial=True,
                description="Deterministic multi-category benchmark route.",
            ),
            routing_id="routing_multicategory_week6",
        )
        _create_reward_inputs(state)
        _create_calculate_finalize_reward(state)
        cases = _build_cases(state)
        leaderboard = build_subnet_leaderboard(state)
        normalized = normalize_logical_results(state, cases)
        return BenchmarkExecution(
            state=state,
            cases=cases,
            leaderboard=leaderboard,
            normalized=normalized,
        )


def normalize_logical_results(
    state: BenchmarkState,
    cases: Week6CaseResultsDocument,
) -> dict[str, Any]:
    return {
        "performance": {
            f"{node}.{category}": {
                "counts": record.counts.model_dump(mode="json"),
                "source_event_ids": record.source_event_ids,
                "source_fingerprint": record.source_fingerprint,
            }
            for (node, category), record in sorted(state.performances.items())
        },
        "scores": {
            f"{node}.{category}": {
                "category_score": record.category_score,
                "experience_confidence": record.components.experience_confidence,
                "source_fingerprint": record.source_fingerprint,
            }
            for (node, category), record in sorted(state.scores.items())
        },
        "memberships": {
            f"{node}.{category}": {
                "status": member.status.value,
                "category_score": member.category_score,
                "source_fingerprint": member.membership_source_fingerprint,
            }
            for (node, category), member in sorted(state.members.items())
        },
        "routing": [
            {
                "project_id": routing.project_id,
                "categories": [
                    {
                        "category": result.category.value,
                        "assignments": [
                            {
                                "node_id": item.node_id,
                                "selection_type": item.selection_type.value,
                                "assignment_mode": item.assignment_mode.value,
                                "membership_status": item.membership_status.value,
                                "category_score": item.category_score,
                            }
                            for item in result.assignments
                        ],
                    }
                    for result in routing.results
                ],
            }
            for routing in (state.access_routing, state.multi_routing)
        ],
        "rewards": {
            "category_pools": [
                {
                    "category": pool.category.value,
                    "allocated": _points(pool.allocated_pool_points),
                    "distributed": _points(pool.distributed_points),
                    "undistributed": _points(pool.undistributed_points),
                    "allocations": [
                        {
                            "submission_id": item.submission_id,
                            "node_id": item.node_id,
                            "reward_points": _points(item.reward_points),
                            "raw_weight": str(item.raw_weight),
                        }
                        for item in pool.allocations
                    ],
                    "exclusions": [
                        {
                            "submission_id": item.submission_id,
                            "reasons": [reason.value for reason in item.reasons],
                        }
                        for item in pool.exclusions
                    ],
                }
                for pool in state.reward_cycle.category_pools
            ]
        },
        "cases": [
            {
                "case_id": case.case_id,
                "passed": case.passed,
                "assertions": [
                    {
                        "assertion_id": assertion.assertion_id,
                        "passed": assertion.passed,
                    }
                    for assertion in case.assertions
                ],
            }
            for case in cases.cases
        ],
        "idempotency": dict(sorted(state.idempotency.items())),
    }


def run_deterministic_replay_check(
    first: dict[str, Any],
    second: dict[str, Any],
) -> tuple[bool, list[str]]:
    if first == second:
        return True, []
    return False, _logical_diff(first, second)


def _logical_diff(first: Any, second: Any, path: str = "$") -> list[str]:
    if type(first) is not type(second):
        return [f"{path}: type {type(first).__name__} != {type(second).__name__}"]
    if isinstance(first, dict):
        differences: list[str] = []
        for key in sorted(set(first) | set(second)):
            if key not in first:
                differences.append(f"{path}.{key}: missing from first replay")
            elif key not in second:
                differences.append(f"{path}.{key}: missing from second replay")
            else:
                differences.extend(
                    _logical_diff(first[key], second[key], f"{path}.{key}")
                )
        return differences[:100]
    if isinstance(first, list):
        if len(first) != len(second):
            return [f"{path}: length {len(first)} != {len(second)}"]
        differences = []
        for index, (left, right) in enumerate(zip(first, second)):
            differences.extend(_logical_diff(left, right, f"{path}[{index}]"))
        return differences[:100]
    return [] if first == second else [f"{path}: {first!r} != {second!r}"]


def build_subnet_leaderboard(state: BenchmarkState) -> dict[str, Any]:
    tier_order = {
        "expert": 0,
        "active": 1,
        "probation": 2,
        "candidate": 3,
        "suspended": 4,
        "removed": 5,
    }
    routings = list_project_routing_records(
        state.roots.protocol_data_root, ACCESS_PROJECT_ID
    ) + list_project_routing_records(
        state.roots.protocol_data_root, MULTI_PROJECT_ID
    )
    assignments = [
        assignment
        for routing in routings
        if routing.status.value == "finalized"
        for result in routing.results
        for assignment in result.assignments
    ]
    reward_by_node_category: dict[tuple[str, str], Decimal] = {}
    for event in list_subnet_reward_events(state.roots.protocol_data_root):
        key = (event.node_id, event.category.value)
        reward_by_node_category[key] = (
            reward_by_node_category.get(key, Decimal("0")) + event.reward_points
        )
    categories = []
    membership_distribution = {
        status: 0
        for status in ("candidate", "probation", "active", "expert", "suspended", "removed")
    }
    for category in RELEVANT_CATEGORIES:
        subnet_id = f"subnet_{category}"
        rows = []
        for member in list_subnet_members(
            state.roots.protocol_data_root, subnet_id
        ):
            membership_distribution[member.status.value] += 1
            performance = state.performances.get((member.node_id, category))
            score = state.scores.get((member.node_id, category))
            rows.append(
                {
                    "node_id": member.node_id,
                    "membership_status": member.status.value,
                    "category_score": member.category_score,
                    "experience_confidence": (
                        score.components.experience_confidence if score else 0.0
                    ),
                    "finalized_submissions": (
                        performance.counts.total_finalized_submissions
                        if performance
                        else 0
                    ),
                    "accepted_unique_submissions": (
                        performance.counts.accepted_unique_submissions
                        if performance
                        else 0
                    ),
                    "production_assignments": sum(
                        item.node_id == member.node_id
                        and item.category.value == category
                        and item.assignment_mode.value == "production"
                        for item in assignments
                    ),
                    "shadow_assignments": sum(
                        item.node_id == member.node_id
                        and item.category.value == category
                        and item.assignment_mode.value == "shadow"
                        for item in assignments
                    ),
                    "reward_points": _points(
                        reward_by_node_category.get(
                            (member.node_id, category), Decimal("0")
                        )
                    ),
                }
            )
        rows.sort(
            key=lambda row: (
                tier_order[row["membership_status"]],
                -row["category_score"],
                -row["experience_confidence"],
                -row["accepted_unique_submissions"],
                -row["finalized_submissions"],
                row["node_id"],
            )
        )
        for position, row in enumerate(rows, 1):
            row["position"] = position
        categories.append(
            {
                "category": category,
                "subnet_id": subnet_id,
                "nodes": rows,
            }
        )
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "categories": categories,
        "membership_distribution": membership_distribution,
        "score_snapshot": {
            f"{node}.{category}": record.category_score
            for (node, category), record in sorted(state.scores.items())
        },
        "report_only_position": True,
    }


def build_week6_summary(
    execution: BenchmarkExecution,
    cases: Week6CaseResultsDocument,
    deterministic_replay_passed: bool,
    replay_diff: list[str],
) -> Week6BenchmarkSummary:
    assertions = [
        assertion for case in cases.cases for assertion in case.assertions
    ]
    passed_assertions = sum(item.passed for item in assertions)
    passed_cases = sum(case.passed for case in cases.cases)
    routings = list_project_routing_records(
        execution.state.roots.protocol_data_root, ACCESS_PROJECT_ID
    ) + list_project_routing_records(
        execution.state.roots.protocol_data_root, MULTI_PROJECT_ID
    )
    assignments = [
        assignment
        for routing in routings
        if routing.status.value == "finalized"
        for result in routing.results
        for assignment in result.assignments
    ]
    reward_events = list_subnet_reward_events(
        execution.state.roots.protocol_data_root
    )
    exclusions = sum(
        len(pool.exclusions) for pool in execution.state.reward_cycle.category_pools
    )
    limitations = [
        "The benchmark uses a small synthetic network and a centralized coordinator.",
        "Membership and routing policy thresholds are v0 policy choices, not economic guarantees.",
        "Shadow exploration does not yet evaluate candidate output or guarantee waiting-time fairness.",
        "No private rotating qualification benchmark or benchmark-leakage protection exists.",
        "No Sybil resistance, operator diversity, assignment lease, load balancing, or remote execution exists.",
        "Protocol points are simulated accounting units with no monetary value.",
    ]
    if replay_diff:
        limitations.append("Deterministic replay differences: " + "; ".join(replay_diff))
    return Week6BenchmarkSummary(
        benchmark_version=BENCHMARK_VERSION,
        protocol_version=PROTOCOL_VERSION,
        week=6,
        benchmark_passed=(
            passed_cases == len(cases.cases) and deterministic_replay_passed
        ),
        total_cases=len(cases.cases),
        passed_cases=passed_cases,
        failed_cases=len(cases.cases) - passed_cases,
        total_assertions=len(assertions),
        passed_assertions=passed_assertions,
        failed_assertions=len(assertions) - passed_assertions,
        pass_rate=(
            round(passed_assertions / len(assertions), 6) if assertions else 0.0
        ),
        generated_files=list(GENERATED_FILENAMES),
        category_count=len(RELEVANT_CATEGORIES),
        node_count=len(execution.state.nodes),
        project_count=len(execution.state.projects),
        routing_count=sum(item.status.value == "finalized" for item in routings),
        routing_assignment_count=len(assignments),
        reward_cycle_count=1,
        reward_event_count=len(reward_events),
        total_simulated_pool_points=_points(
            execution.state.reward_cycle.total_pool_points
        ),
        total_distributed_points=_points(
            execution.state.reward_cycle.total_distributed_points
        ),
        total_undistributed_points=_points(
            execution.state.reward_cycle.total_undistributed_points
        ),
        deterministic_replay_passed=deterministic_replay_passed,
        failed_case_ids=[case.case_id for case in cases.cases if not case.passed],
        categories=list(RELEVANT_CATEGORIES),
        membership_distribution=execution.leaderboard["membership_distribution"],
        routing_distribution={
            "production_assignments": sum(
                item.assignment_mode.value == "production" for item in assignments
            ),
            "shadow_assignments": sum(
                item.assignment_mode.value == "shadow" for item in assignments
            ),
        },
        reward_distribution={
            "eligible_allocations": sum(
                len(pool.allocations)
                for pool in execution.state.reward_cycle.category_pools
            ),
            "excluded_submissions": exclusions,
            "reward_events": len(reward_events),
        },
        idempotency_checks=dict(sorted(execution.state.idempotency.items())),
        known_limitations=limitations,
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )


def run_week6_subnet_benchmark(
    output_dir: Path = RESULTS_DIR,
    *,
    keep_fixtures: bool = False,
) -> tuple[Week6CaseResultsDocument, dict[str, Any], Week6BenchmarkSummary]:
    with temporary_fixture_roots(keep=keep_fixtures) as first_roots:
        primary = execute_benchmark_once(first_roots)
        with temporary_fixture_roots() as replay_roots:
            replay = execute_benchmark_once(replay_roots)
            replay_passed, replay_diff = run_deterministic_replay_check(
                primary.normalized, replay.normalized
            )
        replay_assertion = _assert(
            "deterministic_replay",
            "A complete isolated replay produces identical normalized logical output.",
            True,
            replay_passed,
        )
        last = primary.cases.cases[-1]
        updated_assertions = [*last.assertions, replay_assertion]
        updated_last = last.model_copy(
            update={
                "assertions": updated_assertions,
                "passed": all(item.passed for item in updated_assertions)
                and not last.errors,
                "errors": (
                    last.errors
                    if replay_passed
                    else [*last.errors, *replay_diff]
                ),
            }
        )
        cases = Week6CaseResultsDocument(
            benchmark_version=BENCHMARK_VERSION,
            cases=[*primary.cases.cases[:-1], updated_last],
        )
        summary = build_week6_summary(
            primary, cases, replay_passed, replay_diff
        )
        write_benchmark_outputs(
            output_dir,
            cases,
            primary.leaderboard,
            summary,
        )
        return cases, primary.leaderboard, summary


def write_benchmark_outputs(
    output_dir: Path,
    cases: Week6CaseResultsDocument,
    leaderboard: dict[str, Any],
    summary: Week6BenchmarkSummary,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        output_dir / "subnet_case_results.json",
        cases.model_dump(mode="json"),
    )
    _write_json(output_dir / "subnet_leaderboard.json", leaderboard)
    _write_json(
        output_dir / "week_6_summary.json",
        summary.model_dump(mode="json"),
    )
    report = render_week6_report(
        cases,
        leaderboard,
        summary.model_dump(mode="json"),
    )
    (output_dir / "week_6_report.md").write_text(report, encoding="utf-8")
    _validate_generated_outputs(output_dir)


def _validate_generated_outputs(output_dir: Path) -> None:
    for filename in GENERATED_FILENAMES:
        path = output_dir / filename
        if not path.is_file():
            raise RuntimeError(f"Required benchmark output was not created: {filename}")
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        forbidden = (
            "/home/",
            "\\users\\",
            "private_key",
            "seed_phrase",
            "mnemonic",
            "wallet_address",
            "poc_content",
            "poc_code",
        )
        if any(token in lowered for token in forbidden):
            raise RuntimeError(f"Generated benchmark output contains forbidden data: {filename}")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def _points(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001')):.6f}"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    message = re.sub(
        r"(?:[A-Za-z]:[\\/]|/)(?:[^\s:]+[\\/])+[^\s:]+",
        "[path]",
        message,
    )
    return f"{exc.__class__.__name__}: {message}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deterministic Week 6 subnet benchmark."
    )
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--keep-fixtures", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cases, _, summary = run_week6_subnet_benchmark(
            args.output_dir,
            keep_fixtures=args.keep_fixtures,
        )
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 2
    print(
        f"Week 6 subnet benchmark: {summary.passed_cases}/{summary.total_cases} "
        f"cases, {summary.passed_assertions}/{summary.total_assertions} assertions."
    )
    print(f"Outputs: {args.output_dir}")
    return 0 if summary.benchmark_passed and all(case.passed for case in cases.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
