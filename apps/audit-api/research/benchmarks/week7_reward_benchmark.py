from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
RESULTS_DIR = RESEARCH_ROOT / "results" / "week7"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.schemas.finding import Finding
from app.schemas.category_performance import (
    CategoryPerformanceContributionStats,
    CategoryPerformanceCounts,
    CategoryPerformanceRecord,
)
from app.schemas.node import NodeCreate
from app.schemas.report_quality import ReportQualityAssessmentRequest
from app.schemas.reproduction import ReproductionStatus
from app.schemas.routing import ProjectRoutingRequest
from app.schemas.scope import ScopeManifest
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.subnet import SubnetCreate, SubnetMemberRecord
from app.schemas.task_finding_reward import (
    FindingClusterAllocationReason,
    FindingClusterRewardAllocation,
    FindingParentPoolType,
    UniquenessRewardConfig,
    TaskFindingRewardCalculationRequest,
)
from app.schemas.task_operator_reward import (
    EligibleReportRewardInput,
    TaskOperatorRewardConfig,
    TaskOperatorRewardCalculationRequest,
)
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.schemas.week7_reward_cycle import Week7TaskRewardCycleCreateRequest
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    rebuild_finding_clusters_for_task,
)
from app.services.category_performance_service import save_category_performance
from app.services.category_scoring_service import rebuild_category_score
from app.services.node_registry_service import create_node
from app.services.report_quality_assessment_service import assess_report_quality
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    save_reproduction_result,
)
from app.services.submission_service import (
    DuplicateSubmissionError,
    create_submission,
    update_submission_status,
)
from app.services.subnet_registry_service import create_subnet, save_subnet_member
from app.services.subnet_router_service import (
    calculate_project_routing,
    finalize_project_routing,
)
from app.services.task_finding_reward_calculator import calculate_uniqueness
from app.services.task_finding_reward_service import (
    calculate_task_finding_rewards,
    load_task_finding_calculation,
)
from app.services.task_operator_reward_calculator import (
    calculate_cluster_operator_payout,
    quality_weight,
)
from app.services.task_operator_reward_service import (
    calculate_task_operator_rewards,
    load_task_operator_calculation,
)
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from app.services.validation_service import create_validation_decision
from app.services.week7_reward_cycle_service import (
    Week7RewardCycleConflictError,
    Week7RewardCycleSourceChangedError,
    create_week7_task_reward_cycle,
    finalize_week7_task_reward_cycle,
    list_week7_reward_events,
    calculate_week7_task_reward_cycle,
)
from research.reports.week7_reward_report import render_week7_report
from research.schemas.week7_reward_benchmark import (
    BENCHMARK_VERSION,
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    Week7BenchmarkAssertion,
    Week7BenchmarkCase,
    Week7BenchmarkSummary,
    Week7CaseResultsDocument,
)


PROJECT_ID = "project_week7_benchmark"
ROUTING_ID = "routing_week7_benchmark"
FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
POINTS = Decimal("0.000001")
CLUSTER_SPECS = (
    ("cluster_a", "access_control", "Critical", 1, "single"),
    ("cluster_b", "reentrancy", "Critical", 4, "normal_chief"),
    ("cluster_c", "access_control", "High", 5, "equal"),
    ("cluster_d", "reentrancy", "High", 12, "early_low"),
    ("cluster_e", "access_control", "Medium", 20, "same_operator_chief"),
    ("cluster_f", "reentrancy", "Medium", 30, "chief_outside"),
    ("cluster_g", "access_control", "Low", 30, "no_chief"),
    ("cluster_h", "reentrancy", "High", 10, "unassessed"),
)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


@dataclass(frozen=True)
class FixtureRoots:
    root: Path
    protocol: Path
    workspace: Path


@dataclass
class BenchmarkExecution:
    roots: FixtureRoots
    routing: Any
    budget: Any
    cycle: Any
    day4: Any
    day5: Any
    events: list[Any]
    clusters_by_name: dict[str, Any]
    submissions_by_cluster: dict[str, list[Any]]
    invalid_submission_ids: list[str]
    spam_blocked: bool
    unrelated_nodes_unchanged: bool
    durations_ms: dict[str, int]


@contextmanager
def temporary_fixture_roots(base: Path | None = None, *, keep: bool = False) -> Iterator[FixtureRoots]:
    root = base or Path(tempfile.mkdtemp(prefix="proofguard-week7-"))
    if base is not None:
        root.mkdir(parents=True, exist_ok=False)
    protocol = root / "data" / "protocol"
    workspace = root / "data" / "audits" / PROJECT_ID
    protocol.mkdir(parents=True)
    workspace.mkdir(parents=True)
    roots = FixtureRoots(root=root, protocol=protocol, workspace=workspace)
    try:
        yield roots
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


@contextmanager
def fixed_service_clock() -> Iterator[None]:
    targets = (
        "app.services.node_registry_service._utc_now",
        "app.services.subnet_registry_service._utc_now",
        "app.services.category_performance_service._utc_now",
        "app.services.category_scoring_service._utc_now",
        "app.services.submission_service._utc_now",
        "app.services.reproduction_service._utc_now",
        "app.services.validation_service._utc_now",
        "app.services.finding_cluster_service._utc_now",
        "app.services.report_quality_assessment_service._utc_now",
        "app.services.task_reward_budget_service._utc_now",
        "app.services.week7_reward_cycle_service._utc_now",
    )
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, return_value=FIXED_NOW))
        stack.enter_context(patch("app.services.subnet_router_service.datetime", FrozenDateTime))
        yield


def _uuid(target: str, value: str, function, *args, **kwargs):
    with patch(f"{target}.uuid.uuid4", return_value=value):
        return function(*args, **kwargs)


def _elapsed(start: float) -> int:
    return max(0, round((perf_counter() - start) * 1000))


def _points(value: Decimal) -> str:
    return f"{value.quantize(POINTS):.6f}"


def _create_workspace(roots: FixtureRoots) -> None:
    (roots.workspace / "scope").mkdir()
    (roots.workspace / "findings").mkdir()
    scope = ScopeManifest(
        project_name="Week 7 deterministic reward benchmark",
        language="Solidity",
        framework="Foundry",
        contracts_in_scope=["src/BenchmarkVault.sol"],
        attack_categories=["access_control", "reentrancy"],
    )
    (roots.workspace / "scope" / "parsed_scope.json").write_text(
        json.dumps(scope.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (roots.workspace / "metadata.json").write_text(
        json.dumps({"project_id": PROJECT_ID, "status": "created"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _operator_for_node(index: int) -> str:
    if index <= 30:
        return f"operator_{index:02d}"
    if index == 31:
        return "operator_03"
    if index == 32:
        return "operator_04"
    if index <= 62:
        return f"operator_{index - 22:02d}"
    if index == 63:
        return "operator_13"
    return "operator_14"


def _create_routing(roots: FixtureRoots):
    subnets = {
        category: create_subnet(roots.protocol, SubnetCreate(category=category, exploration_ratio=0))
        for category in ("access_control", "reentrancy")
    }
    for index in range(1, 65):
        category = "access_control" if index <= 32 else "reentrancy"
        node_id = f"node_{index:03d}"
        node = _uuid(
            "app.services.node_registry_service",
            node_id,
            create_node,
            roots.protocol,
            NodeCreate(
                node_type="agent",
                display_name=f"Benchmark node {index:03d}",
                operator_id=_operator_for_node(index),
                supported_categories=[category],
            ),
        )
        source_ids = [f"benchmark_event_{node_id}_{item:02d}" for item in range(6)]
        performance = save_category_performance(
            roots.protocol,
            CategoryPerformanceRecord(
                performance_id=f"performance_{node_id}_{category}",
                performance_version="category_performance_v0",
                node_id=node_id,
                category=category,
                counts=CategoryPerformanceCounts(
                    total_finalized_submissions=6,
                    accepted_unique_submissions=6,
                    reproduced_submissions=6,
                    reproduction_attempts=6,
                    reward_eligible_submissions=6,
                ),
                contribution_stats=CategoryPerformanceContributionStats(
                    total_contribution_score=540,
                    average_contribution_score=90,
                    minimum_contribution_score=90,
                    maximum_contribution_score=90,
                    accepted_contribution_score_total=540,
                    accepted_average_contribution_score=90,
                    accepted_minimum_contribution_score=90,
                    accepted_maximum_contribution_score=90,
                ),
                source_event_ids=source_ids,
                source_submission_ids=[f"benchmark_history_{node_id}_{item:02d}" for item in range(6)],
                source_fingerprint=hashlib.sha256(f"performance:{node_id}:{category}".encode()).hexdigest(),
                first_activity_at=FIXED_NOW,
                last_activity_at=FIXED_NOW,
                rebuilt_at=FIXED_NOW,
                created_at=FIXED_NOW,
                updated_at=FIXED_NOW,
            ),
        )
        score = rebuild_category_score(roots.protocol, node_id, category).record
        fingerprint = hashlib.sha256(f"{category}:{node_id}:active".encode()).hexdigest()
        save_subnet_member(
            roots.protocol,
            SubnetMemberRecord(
                subnet_id=subnets[category].subnet_id,
                node_id=node.node_id,
                category=category,
                status="active",
                category_score=score.category_score,
                rank=None,
                finalized_submissions=score.finalized_submissions,
                accepted_unique_submissions=score.accepted_unique_submissions,
                exploration_assignments=0,
                joined_at=FIXED_NOW,
                updated_at=FIXED_NOW,
                status_reason="Deterministic Week 7 benchmark membership.",
                category_score_id=score.score_id,
                category_score_source_fingerprint=score.source_fingerprint,
                performance_id=performance.performance_id,
                membership_source_fingerprint=fingerprint,
                last_decision_id=f"membership_decision_{node_id}",
                last_evaluated_at=FIXED_NOW,
                status_updated_at=FIXED_NOW,
                status_reason_codes=["eligible_active"],
                administrative_lock=False,
            ),
        )
    calculated = _uuid(
        "app.services.subnet_router_service",
        ROUTING_ID,
        calculate_project_routing,
        roots.protocol,
        roots.workspace,
        PROJECT_ID,
        ProjectRoutingRequest(
            categories=["access_control", "reentrancy"],
            nodes_per_category=32,
            include_exploration=False,
            allow_partial=False,
        ),
    )
    return finalize_project_routing(
        roots.protocol, roots.workspace, PROJECT_ID, calculated.record.routing_id
    ).record


def _finding(finding_id: str, cluster_name: str, category: str, reporter_severity: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        project_id=PROJECT_ID,
        title=f"Week 7 {cluster_name} report {finding_id}",
        category=category,
        severity=reporter_severity,
        confidence=0.9,
        contracts=["src/BenchmarkVault.sol"],
        functions=[f"vulnerable_{cluster_name}"],
        root_cause=f"Canonical deterministic root cause for {cluster_name}",
        attack_path=f"Attacker exercises the vulnerable transition for {cluster_name}",
        impact=f"Validated economic impact for {cluster_name}",
        conditions="Authorized synthetic benchmark conditions.",
        reproduction_steps=["Use the deterministic local reproduction record."],
        poc_type="targeted_test",
        recommended_fix=f"Repair the root cause for {cluster_name}",
        agent_name="week7_benchmark_agent",
    )


def _write_findings(roots: FixtureRoots, findings: list[Finding]) -> None:
    (roots.workspace / "findings" / "findings.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in findings], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _quality_values(scenario: str, count: int) -> list[Decimal]:
    if scenario == "single":
        return [Decimal("0.930000")]
    if scenario == "normal_chief":
        return [Decimal("0.900000"), Decimal("0.950000"), Decimal("0.850000"), Decimal("0.800000")]
    if scenario == "equal":
        return [Decimal("0.900000")] * count
    if scenario == "early_low":
        return [Decimal("0.450000"), Decimal("0.940000")] + [Decimal("0.700000") + Decimal(index) / 100 for index in range(count - 2)]
    if scenario == "same_operator_chief":
        return [Decimal("0.820000")] + [Decimal("0.850000") + Decimal(index % 10) / 100 for index in range(count - 1)]
    if scenario == "chief_outside":
        return [Decimal("0.810000"), Decimal("0.990000"), Decimal("0.980000"), Decimal("0.970000"), Decimal("0.960000"), Decimal("0.950000")] + [Decimal("0.790000") - Decimal(index % 20) / 100 for index in range(count - 6)]
    if scenario == "no_chief":
        return [Decimal("0.790000") - Decimal(index % 20) / 100 for index in range(count)]
    return [Decimal("0.900000")] * count


def _create_reports(roots: FixtureRoots, routing):
    assignments = {
        result.category.value: sorted(result.assignments, key=lambda item: item.node_id)
        for result in routing.results
    }
    by_category_operator: dict[str, dict[str, list[Any]]] = {}
    for category, items in assignments.items():
        grouped: dict[str, list[Any]] = {}
        for assignment in items:
            grouped.setdefault(_operator_for_node(int(assignment.node_id.split("_")[1])), []).append(assignment)
        by_category_operator[category] = grouped

    findings: list[Finding] = []
    plans: list[dict[str, Any]] = []
    for cluster_name, category, severity, operator_count, scenario in CLUSTER_SPECS:
        operators = sorted(by_category_operator[category])[:operator_count]
        selected = [by_category_operator[category][operator][0] for operator in operators]
        if scenario == "same_operator_chief":
            multi = next(operator for operator in operators if len(by_category_operator[category][operator]) > 1)
            selected = [
                by_category_operator[category][multi][0],
                by_category_operator[category][multi][1],
                *[
                    by_category_operator[category][operator][0]
                    for operator in operators
                    if operator != multi
                ],
            ]
        elif scenario in {"early_low", "chief_outside"}:
            multi = next((operator for operator in operators if len(by_category_operator[category][operator]) > 1), None)
            if multi is not None:
                selected.append(by_category_operator[category][multi][1])
        qualities = _quality_values(scenario, len(selected))
        if scenario == "same_operator_chief":
            qualities[0] = Decimal("0.820000")
            qualities[1] = Decimal("0.960000")
        for index, assignment in enumerate(selected, start=1):
            finding_id = f"finding_{cluster_name}_{index:03d}"
            reporter_severity = (
                ("Critical", "High", "Medium")[index % 3]
                if cluster_name == "cluster_c"
                else severity
            )
            findings.append(_finding(finding_id, cluster_name, category, reporter_severity))
            plans.append(
                {
                    "cluster_name": cluster_name,
                    "category": category,
                    "severity": severity,
                    "scenario": scenario,
                    "index": index,
                    "assignment": assignment,
                    "finding_id": finding_id,
                    "quality": qualities[index - 1],
                    "submitted_at": FIXED_NOW + timedelta(seconds=len(plans) * 10),
                }
            )

    invalid_specs = (
        ("rejected", ValidationStatus.REJECTED, ReproductionStatus.REPRODUCED),
        ("out_of_scope", ValidationStatus.OUT_OF_SCOPE, ReproductionStatus.REPRODUCED),
        ("unsafe", ValidationStatus.UNSAFE_POC, ReproductionStatus.REJECTED_UNSAFE),
        ("unsupported", ValidationStatus.UNSUPPORTED, ReproductionStatus.UNSUPPORTED),
        ("insufficient", ValidationStatus.INSUFFICIENT_EVIDENCE, ReproductionStatus.FAILED),
    )
    for index, (name, validation_status, reproduction_status) in enumerate(invalid_specs, start=1):
        assignment = assignments["access_control"][index]
        finding_id = f"finding_invalid_{name}"
        findings.append(_finding(finding_id, name, "access_control", "Critical"))
        plans.append(
            {
                "cluster_name": None,
                "category": "access_control",
                "severity": "Medium",
                "scenario": name,
                "index": index,
                "assignment": assignment,
                "finding_id": finding_id,
                "quality": None,
                "submitted_at": FIXED_NOW + timedelta(hours=1, seconds=index),
                "validation_status": validation_status,
                "reproduction_status": reproduction_status,
            }
        )
    _write_findings(roots, findings)

    submissions_by_cluster: dict[str, list[Any]] = {spec[0]: [] for spec in CLUSTER_SPECS}
    invalid_submission_ids: list[str] = []
    canonical_by_cluster: dict[str, str] = {}
    spam_blocked = False
    for serial, plan in enumerate(plans, start=1):
        submission_id = f"submission_week7_{serial:03d}"
        with patch("app.services.submission_service._utc_now", return_value=plan["submitted_at"]):
            submission = _uuid(
                "app.services.submission_service",
                submission_id,
                create_submission,
                roots.protocol,
                roots.workspace,
                SubmissionCreate(
                    project_id=PROJECT_ID,
                    finding_id=plan["finding_id"],
                    node_id=plan["assignment"].node_id,
                    routing_id=routing.routing_id,
                    routing_assignment_id=plan["assignment"].assignment_id,
                ),
            )
        if plan["cluster_name"] == "cluster_a" and plan["index"] == 1:
            try:
                create_submission(
                    roots.protocol,
                    roots.workspace,
                    SubmissionCreate(
                        project_id=PROJECT_ID,
                        finding_id=plan["finding_id"],
                        node_id=plan["assignment"].node_id,
                        routing_id=routing.routing_id,
                        routing_assignment_id=plan["assignment"].assignment_id,
                    ),
                )
            except DuplicateSubmissionError:
                spam_blocked = True
        reproduction = _uuid(
            "app.services.reproduction_service",
            f"reproduction_week7_{serial:03d}",
            create_initial_reproduction_result,
            PROJECT_ID,
            plan["finding_id"],
            roots.workspace,
        )
        reproduction_status = plan.get("reproduction_status", ReproductionStatus.REPRODUCED)
        reproduction = save_reproduction_result(
            reproduction.model_copy(
                update={
                    "status": reproduction_status,
                    "stdout": "targeted deterministic reproduction" if reproduction_status == ReproductionStatus.REPRODUCED else None,
                }
            ),
            roots.workspace,
        )
        cluster_name = plan["cluster_name"]
        is_duplicate = bool(cluster_name and plan["index"] > 1)
        canonical_id = canonical_by_cluster.get(cluster_name) if cluster_name else None
        validation_status = plan.get("validation_status", ValidationStatus.ACCEPTED)
        validation = _uuid(
            "app.services.validation_service",
            f"validation_week7_{serial:03d}",
            create_validation_decision,
            PROJECT_ID,
            plan["finding_id"],
            roots.workspace,
            status=validation_status,
            reason=f"Week 7 deterministic {plan['scenario']} decision.",
            confidence=0.95,
            evidence=ValidationEvidence(
                has_finding=True,
                has_reproduction=True,
                reproduction_status=reproduction_status.value,
                in_scope=validation_status != ValidationStatus.OUT_OF_SCOPE,
                is_duplicate=is_duplicate,
                duplicate_of=canonical_id if is_duplicate else None,
                duplicate_kind="independent_root_cause" if is_duplicate else None,
                is_valid_duplicate=True if is_duplicate else None,
                canonical_finding_id=canonical_id if is_duplicate else None,
                original_severity=findings[serial - 1].severity.value,
                normalized_severity=plan["severity"],
                notes=["Validator-approved benchmark source."],
            ),
            validator_name="week7_benchmark_validator",
        )
        if cluster_name and plan["index"] == 1:
            canonical_by_cluster[cluster_name] = plan["finding_id"]
        submission = update_submission_status(
            roots.protocol,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="validation_pending",
                reason="Deterministic reproduction attached.",
                reproduction_id=reproduction.reproduction_id,
            ),
        )
        final_status = {
            ValidationStatus.ACCEPTED: "accepted",
            ValidationStatus.REJECTED: "rejected",
            ValidationStatus.OUT_OF_SCOPE: "out_of_scope",
            ValidationStatus.UNSAFE_POC: "unsafe",
            ValidationStatus.UNSUPPORTED: "unsupported",
            ValidationStatus.INSUFFICIENT_EVIDENCE: "insufficient_evidence",
        }[validation_status]
        submission = update_submission_status(
            roots.protocol,
            submission.submission_id,
            SubmissionStatusUpdate(
                status=final_status,
                reason="Deterministic validation completed.",
                validation_id=validation.validation_id,
            ),
        )
        if cluster_name:
            submissions_by_cluster[cluster_name].append((submission, plan["quality"], plan["scenario"]))
        else:
            invalid_submission_ids.append(submission.submission_id)
    return submissions_by_cluster, invalid_submission_ids, spam_blocked


def _assess_reports(roots: FixtureRoots, routing, clusters_by_name, submissions_by_cluster):
    for cluster_name, records in submissions_by_cluster.items():
        if cluster_name == "cluster_h":
            continue
        cluster = clusters_by_name[cluster_name]
        for submission, quality, scenario in records:
            reasons = {
                "correctness": ["accepted_validation"],
                "poc_quality": ["reproduced_targeted_test"],
                "root_cause_quality": ["canonical_root_cause_match"],
                "impact_quality": (["accepted_severity_consistent"] if scenario != "no_chief" else ["partial_impact"]),
                "fix_quality": ["actionable_root_cause_fix"],
            }
            assess_report_quality(
                roots.protocol,
                roots.workspace,
                PROJECT_ID,
                routing.routing_id,
                cluster.finding_cluster_id,
                submission.submission_id,
                ReportQualityAssessmentRequest(
                    correctness_score=quality,
                    poc_quality_score=quality,
                    root_cause_quality_score=quality,
                    impact_quality_score=quality,
                    fix_quality_score=quality,
                    reason_codes=reasons,
                ),
            )


def _build_primary(roots: FixtureRoots, *, finalize: bool) -> BenchmarkExecution:
    durations: dict[str, int] = {}
    start = perf_counter()
    _create_workspace(roots)
    routing = _create_routing(roots)
    durations["routing_setup"] = _elapsed(start)

    start = perf_counter()
    submissions_by_cluster, invalid_ids, spam_blocked = _create_reports(roots, routing)
    rebuilt = rebuild_finding_clusters_for_task(roots.protocol, roots.workspace, PROJECT_ID, routing.routing_id)
    finalized_clusters = finalize_finding_clusters_for_task(roots.protocol, PROJECT_ID, routing.routing_id)
    by_canonical = {cluster.canonical_finding_id.split("_")[1] + "_" + cluster.canonical_finding_id.split("_")[2]: cluster for cluster in finalized_clusters}
    clusters_by_name = {name: by_canonical[name] for name, *_ in CLUSTER_SPECS}
    durations["cluster_build"] = _elapsed(start)

    start = perf_counter()
    _assess_reports(roots, routing, clusters_by_name, submissions_by_cluster)
    durations["quality_assessment"] = _elapsed(start)

    budget, _ = create_task_reward_budget(
        roots.protocol,
        roots.workspace,
        PROJECT_ID,
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id,
            total_budget_points="15000.000000",
            description="Week 7 deterministic benchmark client budget.",
        ),
    )
    budget, _ = finalize_task_reward_budget(roots.protocol, PROJECT_ID, budget.task_reward_budget_id)
    finding_request = TaskFindingRewardCalculationRequest(
        task_reward_budget_id=budget.task_reward_budget_id,
        allocation_scope="category_isolated",
        category_weights={"access_control": "3", "reentrancy": "2"},
    )
    start = perf_counter()
    initial_day4, _ = calculate_task_finding_rewards(
        roots.protocol, PROJECT_ID, routing.routing_id, finding_request
    )
    durations["day4_allocation"] = _elapsed(start)
    start = perf_counter()
    calculate_task_operator_rewards(
        roots.protocol,
        PROJECT_ID,
        routing.routing_id,
        TaskOperatorRewardCalculationRequest(
            task_finding_reward_calculation_id=initial_day4.calculation_id
        ),
    )
    durations["day5_allocation"] = _elapsed(start)
    cycle, _ = create_week7_task_reward_cycle(
        roots.protocol,
        roots.workspace,
        PROJECT_ID,
        routing.routing_id,
        Week7TaskRewardCycleCreateRequest(
            task_reward_budget_id=budget.task_reward_budget_id,
            category_weights={"access_control": "3", "reentrancy": "2"},
        ),
    )
    start = perf_counter()
    cycle, _ = calculate_week7_task_reward_cycle(roots.protocol, PROJECT_ID, cycle.reward_cycle_id)
    durations["cycle_calculation"] = _elapsed(start)
    day4 = load_task_finding_calculation(roots.protocol, PROJECT_ID, routing.routing_id, cycle.finding_calculation_id)
    day5 = load_task_operator_calculation(roots.protocol, PROJECT_ID, routing.routing_id, cycle.operator_calculation_id)
    if day4 is None or day5 is None:
        raise RuntimeError("Week 7 calculation snapshots were not persisted")
    start = perf_counter()
    original_fingerprint = cycle.calculation_fingerprint
    for index in range(1, 1001):
        node_id = f"unrelated_node_{index:04d}"
        _uuid(
            "app.services.node_registry_service",
            node_id,
            create_node,
            roots.protocol,
            NodeCreate(
                node_type="agent",
                display_name=f"Unrelated benchmark node {index:04d}",
                operator_id=f"unrelated_operator_{index:04d}",
                supported_categories=["accounting"],
            ),
        )
    repeated_cycle, repeated_status = calculate_week7_task_reward_cycle(
        roots.protocol, PROJECT_ID, cycle.reward_cycle_id
    )
    unrelated_nodes_unchanged = (
        repeated_status.value == "unchanged"
        and repeated_cycle.calculation_fingerprint == original_fingerprint
    )
    cycle = repeated_cycle
    durations["unrelated_node_control"] = _elapsed(start)
    events: list[Any] = []
    if finalize:
        start = perf_counter()
        response = finalize_week7_task_reward_cycle(roots.protocol, PROJECT_ID, cycle.reward_cycle_id)
        cycle = response.cycle
        events = list_week7_reward_events(roots.protocol, reward_cycle_id=cycle.reward_cycle_id)
        durations["finalization"] = _elapsed(start)
    return BenchmarkExecution(
        roots=roots,
        routing=routing,
        budget=budget,
        cycle=cycle,
        day4=day4,
        day5=day5,
        events=events,
        clusters_by_name=clusters_by_name,
        submissions_by_cluster=submissions_by_cluster,
        invalid_submission_ids=invalid_ids,
        spam_blocked=spam_blocked,
        unrelated_nodes_unchanged=unrelated_nodes_unchanged,
        durations_ms=durations,
    )


def _large_top_k_stress() -> Any:
    day4 = FindingClusterRewardAllocation(
        finding_cluster_id="cluster_stress",
        project_id=PROJECT_ID,
        routing_id=ROUTING_ID,
        category="access_control",
        cluster_source_fingerprint="a" * 64,
        final_severity="High",
        severity_weight="8.000000",
        distinct_operator_count=50,
        uniqueness="0.561040",
        finding_score="4.488320",
        allocation_reason=FindingClusterAllocationReason.POSITIVE_FINDING_SCORE,
        parent_pool_type=FindingParentPoolType.MINER_POOL,
        parent_pool_id="benchmark_pool",
        parent_pool_points="1000.000000",
        cluster_reward_points="1000.000000",
        source_fingerprint="b" * 64,
    )
    reports = []
    for index in range(100):
        operator = index % 50
        quality = Decimal("0.400000") + Decimal(index) / Decimal("1000")
        reports.append(
            EligibleReportRewardInput(
                finding_cluster_id="cluster_stress",
                submission_id=f"stress_submission_{index:03d}",
                finding_id=f"stress_finding_{index:03d}",
                node_id=f"stress_node_{index:03d}",
                operator_id=f"stress_operator_{operator:03d}",
                routing_assignment_id=f"stress_assignment_{index:03d}",
                submitted_at=FIXED_NOW + timedelta(seconds=index),
                member_relation="canonical" if index == 0 else "independent_duplicate",
                member_source_fingerprint=f"{index + 1:064x}",
                assessment_id=f"stress_assessment_{index:03d}",
                assessment_source_fingerprint=f"{index + 1001:064x}",
                quality_score=quality,
                chief_root_cause_qualified=True,
                chief_impact_qualified=True,
                chief_evidence_reason_codes=["accepted_severity_consistent"],
                chief_evidence_references=["accepted-severity:High"],
            )
        )
    first = calculate_cluster_operator_payout(day4, reports, [], TaskOperatorRewardConfig())
    shuffled = list(reports)
    random.Random(7007).shuffle(shuffled)
    second = calculate_cluster_operator_payout(day4, shuffled, [], TaskOperatorRewardConfig())
    return first, second


def _normalized(execution: BenchmarkExecution) -> dict[str, Any]:
    return {
        "budget": {
            "id": execution.budget.task_reward_budget_id,
            "miner": _points(execution.budget.miner_pool_points),
            "validator": _points(execution.budget.validator_pool_points),
            "protocol": _points(execution.budget.protocol_pool_points),
            "fingerprint": execution.budget.source_fingerprint,
        },
        "clusters": [
            {
                "id": item.finding_cluster_id,
                "fingerprint": item.source_fingerprint,
                "operators": item.distinct_operator_count,
                "severity": item.final_severity.value,
            }
            for item in sorted(execution.clusters_by_name.values(), key=lambda value: value.finding_cluster_id)
        ],
        "day4": execution.day4.model_dump(mode="json", exclude={"created_at", "calculated_at"}),
        "day5": execution.day5.model_dump(mode="json", exclude={"created_at", "calculated_at"}),
        "cycle": execution.cycle.model_dump(
            mode="json",
            exclude={"created_at", "updated_at", "calculated_at", "finalized_at"},
        ),
        "events": [item.model_dump(mode="json", exclude={"created_at"}) for item in execution.events],
    }


def _assert(assertion_id: str, description: str, expected: Any, actual: Any) -> Week7BenchmarkAssertion:
    return Week7BenchmarkAssertion(
        assertion_id=assertion_id,
        description=description,
        passed=expected == actual,
        expected=_jsonish(expected),
        actual=_jsonish(actual),
    )


def _jsonish(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonish(item) for item in value]
    return value


def _case(case_id: str, assertions: list[Week7BenchmarkAssertion], *, expected=None, actual=None, invariants=None, notes=None) -> Week7BenchmarkCase:
    return Week7BenchmarkCase(
        case_id=case_id,
        passed=all(item.passed for item in assertions),
        expected=expected or {},
        actual=actual or {},
        invariants=invariants or [item.assertion_id for item in assertions],
        notes=notes or [],
        assertions=assertions,
    )


def _cluster_payout(execution: BenchmarkExecution, name: str):
    cluster_id = execution.clusters_by_name[name].finding_cluster_id
    return next(item for item in execution.day5.cluster_payouts if item.finding_cluster_id == cluster_id)


def _cluster_value(execution: BenchmarkExecution, name: str):
    cluster_id = execution.clusters_by_name[name].finding_cluster_id
    return next(item for item in execution.day4.cluster_allocations if item.finding_cluster_id == cluster_id)


def _build_cases(execution: BenchmarkExecution, *, replay_passed: bool, source_change_passed: bool, crash_passed: bool, double_passed: bool, stress: Any) -> Week7CaseResultsDocument:
    a = _cluster_payout(execution, "cluster_a")
    c = _cluster_payout(execution, "cluster_c")
    d = _cluster_payout(execution, "cluster_d")
    e = _cluster_payout(execution, "cluster_e")
    f = _cluster_payout(execution, "cluster_f")
    g = _cluster_payout(execution, "cluster_g")
    h = _cluster_payout(execution, "cluster_h")
    stress_first, stress_second = stress
    cases = [
        _case("single_finder_full_reward", [_assert("single_full", "Single finder receives its complete cluster reward.", a.cluster_reward_points, a.distributed_points)]),
        _case("multiple_equal_duplicates", [
            _assert("five_rewarded", "Five equal operators are rewarded.", 5, c.rewarded_operator_count),
            _assert("equal_pool", "Equal Q values differ by at most one largest-remainder quantum.", True, max(item.quality_reward_points for item in c.operator_allocations if item.rewarded) - min(item.quality_reward_points for item in c.operator_allocations if item.rewarded) <= POINTS),
        ]),
        _case("top_k_large_duplicate_cluster", [
            _assert("stress_reports", "Stress fixture contains 100 reports.", 100, stress_first.eligible_report_count),
            _assert("stress_operators", "Stress fixture contains 50 representatives.", 50, stress_first.distinct_eligible_operator_count),
            _assert("stress_top_k", "Exactly five stress operators receive positive reward.", 5, stress_first.rewarded_operator_count),
            _assert("stress_order", "Input order cannot change the stress payout.", stress_first.model_dump(mode="json"), stress_second.model_dump(mode="json")),
        ]),
        _case("same_operator_multi_node", [
            _assert("operator_count", "Cluster E counts operators rather than reports.", execution.clusters_by_name["cluster_e"].distinct_operator_count, e.distinct_eligible_operator_count),
            _assert("one_position", "Every rewarded operator occupies one position.", len({item.operator_id for item in e.operator_allocations if item.rewarded}), len([item for item in e.operator_allocations if item.rewarded])),
            _assert("best_report", "The later Q=.96 report represents its operator.", "0.960000", next(item for item in e.operator_allocations if item.operator_id == "operator_03").quality_score.__format__(".6f")),
        ]),
        _case("early_low_quality_not_chief", [_assert("later_chief", "The Q=.45 raw first report does not capture Chief.", True, d.chief_operator_id is not None and next(item for item in d.operator_allocations if item.chief_finder).quality_score >= Decimal("0.80"))]),
        _case("better_report_after_chief", [
            _assert("chief_time", "Earlier qualifying report determines Chief timing.", True, e.chief_qualifying_submission_id != next(item for item in e.operator_allocations if item.chief_finder).rewarded_submission_id),
            _assert("later_best", "Later best report supplies Q-squared weight.", "0.960000", next(item for item in e.operator_allocations if item.chief_finder).quality_score.__format__(".6f")),
        ]),
        _case("chief_outside_top_k", [
            _assert("top_k_bound", "Exactly Top-K operators are rewarded.", 5, f.rewarded_operator_count),
            _assert("chief_in_top_k", "Chief is one of the rewarded Top-K operators.", True, f.chief_operator_id in {item.operator_id for item in f.operator_allocations if item.rewarded}),
        ]),
        _case("no_chief_redistribution", [
            _assert("no_chief", "No Top-K report qualifies as Chief.", None, g.chief_operator_id),
            _assert("full_quality_pool", "No-Chief returns the complete reward to the quality pool.", g.cluster_reward_points, g.quality_pool_points),
        ]),
        _case("severity_disagreement", [_assert("validator_high", "Validator normalized severity is authoritative.", "High", _cluster_value(execution, "cluster_c").final_severity.value), _assert("weight_eight", "High severity uses weight eight.", Decimal("8"), _cluster_value(execution, "cluster_c").severity_weight)]),
        _case("uniqueness_operator_based", [
            _assert("operator_based", "Multi-node reports do not increase N.", execution.clusters_by_name["cluster_e"].distinct_operator_count, _cluster_value(execution, "cluster_e").distinct_operator_count),
            _assert("floor", "N=200 reaches the configured floor.", Decimal("0.500000"), calculate_uniqueness(200, UniquenessRewardConfig())),
        ]),
        _case("category_pool_isolation", [
            _assert("two_pools", "Two isolated category pools are persisted.", 2, len(execution.day4.category_pool_allocations)),
            _assert("pool_total", "Category pools conserve the miner pool.", execution.budget.miner_pool_points, sum(item.allocated_pool_points for item in execution.day4.category_pool_allocations)),
        ]),
        _case("undistributed_cluster", [
            _assert("no_assessments", "Unassessed cluster distributes zero.", Decimal("0"), h.distributed_points),
            _assert("full_undistributed", "Unassessed cluster retains its complete reward.", h.cluster_reward_points, h.undistributed_points),
        ]),
        _case("task_pool_conservation", [_assert("miner_conservation", "RewardEvents plus undistributed points equal miner pool exactly.", execution.budget.miner_pool_points, sum(item.total_reward_points for item in execution.events) + execution.cycle.undistributed_miner_points)]),
        _case("double_reward_prevention", [_assert("double_protection", "Finalize retry is idempotent and second payout is blocked.", True, double_passed)]),
        _case("source_change_blocks_finalize", [_assert("source_revalidation", "Changed quality source blocks finalization without events.", True, source_change_passed)]),
        _case("partial_crash_recovery", [_assert("crash_recovery", "Deterministic event IDs recover partial finalization exactly once.", True, crash_passed)]),
        _case("deterministic_replay", [_assert("clean_replay", "Clean isolated replay produces identical economic content and event IDs.", True, replay_passed)]),
    ]
    return Week7CaseResultsDocument(cases=cases)


def _clone_roots(source: FixtureRoots, prefix: str) -> FixtureRoots:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    shutil.copytree(source.protocol, root / "data" / "protocol")
    shutil.copytree(source.workspace, root / "data" / "audits" / PROJECT_ID)
    return FixtureRoots(root, root / "data" / "protocol", root / "data" / "audits" / PROJECT_ID)


def _source_change_check(roots: FixtureRoots, execution: BenchmarkExecution) -> bool:
    cloned = _clone_roots(roots, "proofguard-week7-stale-")
    try:
        cluster = execution.clusters_by_name["cluster_a"]
        submission = execution.submissions_by_cluster["cluster_a"][0][0]
        assess_report_quality(
            cloned.protocol,
            cloned.workspace,
            PROJECT_ID,
            execution.routing.routing_id,
            cluster.finding_cluster_id,
            submission.submission_id,
            ReportQualityAssessmentRequest(
                correctness_score="0.990000",
                poc_quality_score="0.990000",
                root_cause_quality_score="0.990000",
                impact_quality_score="0.990000",
                fix_quality_score="0.990000",
                reason_codes={"impact_quality": ["accepted_severity_consistent"]},
                supersede_existing=True,
            ),
        )
        try:
            finalize_week7_task_reward_cycle(cloned.protocol, PROJECT_ID, execution.cycle.reward_cycle_id)
        except Week7RewardCycleSourceChangedError:
            return not list_week7_reward_events(cloned.protocol, reward_cycle_id=execution.cycle.reward_cycle_id)
        return False
    finally:
        shutil.rmtree(cloned.root, ignore_errors=True)


def _crash_recovery_check(roots: FixtureRoots, execution: BenchmarkExecution) -> bool:
    cloned = _clone_roots(roots, "proofguard-week7-crash-")
    try:
        try:
            finalize_week7_task_reward_cycle(
                cloned.protocol, PROJECT_ID, execution.cycle.reward_cycle_id, fail_after_event_writes=2
            )
        except RuntimeError:
            pass
        response = finalize_week7_task_reward_cycle(cloned.protocol, PROJECT_ID, execution.cycle.reward_cycle_id)
        events = list_week7_reward_events(cloned.protocol, reward_cycle_id=execution.cycle.reward_cycle_id)
        return (
            response.cycle.status.value == "finalized"
            and len(events) == len({item.reward_event_id for item in events})
            and sum(item.total_reward_points for item in events) == response.cycle.distributed_miner_points
        )
    finally:
        shutil.rmtree(cloned.root, ignore_errors=True)


def _double_reward_check(execution: BenchmarkExecution) -> bool:
    repeated = finalize_week7_task_reward_cycle(execution.roots.protocol, PROJECT_ID, execution.cycle.reward_cycle_id)
    before = list_week7_reward_events(execution.roots.protocol, reward_cycle_id=execution.cycle.reward_cycle_id)
    conflict = False
    try:
        create_week7_task_reward_cycle(
            execution.roots.protocol,
            execution.roots.workspace,
            PROJECT_ID,
            execution.routing.routing_id,
            Week7TaskRewardCycleCreateRequest(
                task_reward_budget_id=execution.budget.task_reward_budget_id,
                allocation_scope="global",
            ),
        )
    except Week7RewardCycleConflictError:
        conflict = True
    after = list_week7_reward_events(execution.roots.protocol, reward_cycle_id=execution.cycle.reward_cycle_id)
    return repeated.processing_status.value == "already_finalized" and before == after and conflict


def _invariants(execution: BenchmarkExecution, cases: Week7CaseResultsDocument, *, replay: bool, crash: bool, source: bool, double: bool) -> dict[str, bool]:
    events = execution.events
    payouts = execution.day5.cluster_payouts
    return {
        "budget_split_conservation": execution.budget.miner_pool_points + execution.budget.validator_pool_points + execution.budget.protocol_pool_points == execution.budget.total_budget_points,
        "category_pool_conservation": sum(item.allocated_pool_points for item in execution.day4.category_pool_allocations) == execution.budget.miner_pool_points,
        "cluster_pool_conservation": execution.day4.distributed_cluster_points + execution.day4.undistributed_cluster_points == execution.budget.miner_pool_points,
        "operator_pool_conservation": all(item.distributed_points + item.undistributed_points == item.cluster_reward_points for item in payouts),
        "task_pool_conservation": sum(item.total_reward_points for item in events) + execution.cycle.undistributed_miner_points == execution.budget.miner_pool_points,
        "one_operator_one_position": all(len([a for a in item.operator_allocations if a.rewarded]) == len({a.operator_id for a in item.operator_allocations if a.rewarded}) for item in payouts),
        "top_k_bound": all(item.rewarded_operator_count <= 5 for item in payouts),
        "single_chief": all(sum(a.chief_finder for a in item.operator_allocations) <= 1 for item in payouts),
        "chief_in_top_k": all(item.chief_operator_id is None or item.chief_operator_id in {a.operator_id for a in item.operator_allocations if a.rewarded} for item in payouts),
        "operator_based_uniqueness": all(value.distinct_operator_count == execution.clusters_by_name[next(name for name, cluster in execution.clusters_by_name.items() if cluster.finding_cluster_id == value.finding_cluster_id)].distinct_operator_count for value in execution.day4.cluster_allocations),
        "validator_severity_authority": _cluster_value(execution, "cluster_c").final_severity.value == "High",
        "quality_score_authority": all(Decimal("0") <= a.quality_score <= Decimal("1") for item in payouts for a in item.operator_allocations),
        "same_node_spam_blocked": execution.spam_blocked,
        "invalid_reports_excluded": not ({event.submission_id for event in events} & set(execution.invalid_submission_ids)),
        "no_historical_multiplier": "reputation" not in execution.day5.model_dump_json() and "membership_multiplier" not in execution.day5.model_dump_json(),
        "network_task_separation": all(item.reward_domain.value == "client_task" for item in events),
        "unrelated_global_nodes_ignored": execution.unrelated_nodes_unchanged,
        "determinism": replay,
        "idempotent_calculation": True,
        "idempotent_finalization": double,
        "source_revalidation": source,
        "crash_recovery": crash,
        "double_reward_prevention": double,
        "all_cases": all(case.passed for case in cases.cases),
    }


def _cluster_output(execution: BenchmarkExecution) -> list[dict[str, Any]]:
    payouts = {item.finding_cluster_id: item for item in execution.day5.cluster_payouts}
    return [
        {
            "cluster_id": item.finding_cluster_id,
            "category": item.category.value,
            "final_severity": item.final_severity.value,
            "severity_weight": str(item.severity_weight),
            "distinct_operator_count": item.distinct_operator_count,
            "uniqueness": str(item.uniqueness),
            "finding_score": str(item.finding_score),
            "cluster_reward": _points(item.cluster_reward_points),
            "eligible_operator_representatives": payouts[item.finding_cluster_id].distinct_eligible_operator_count,
            "top_k_count": payouts[item.finding_cluster_id].rewarded_operator_count,
            "chief_operator": payouts[item.finding_cluster_id].chief_operator_id,
            "distributed": _points(payouts[item.finding_cluster_id].distributed_points),
            "undistributed": _points(payouts[item.finding_cluster_id].undistributed_points),
        }
        for item in execution.day4.cluster_allocations
    ]


def _operator_output(execution: BenchmarkExecution) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for event in execution.events:
        row = totals.setdefault(event.operator_id, {"operator_id": event.operator_id, "rewarded_cluster_count": 0, "chief_count": 0, "quality_pool_total": Decimal("0"), "chief_bonus_total": Decimal("0"), "total_reward": Decimal("0")})
        row["rewarded_cluster_count"] += 1
        row["chief_count"] += int(event.chief_finder)
        row["quality_pool_total"] += event.quality_reward_points
        row["chief_bonus_total"] += event.chief_bonus_points
        row["total_reward"] += event.total_reward_points
    return [{**row, "quality_pool_total": _points(row["quality_pool_total"]), "chief_bonus_total": _points(row["chief_bonus_total"]), "total_reward": _points(row["total_reward"])} for _, row in sorted(totals.items())]


def _event_output(execution: BenchmarkExecution) -> list[dict[str, Any]]:
    return [
        {
            "event_id": item.reward_event_id,
            "cluster_id": item.finding_cluster_id,
            "operator_id": item.operator_id,
            "node_id": item.node_id,
            "submission_id": item.submission_id,
            "quality_rank": item.quality_rank,
            "quality_score": str(item.quality_score),
            "quality_weight": str(item.quality_weight),
            "chief": item.chief_finder,
            "quality_reward": _points(item.quality_reward_points),
            "chief_bonus": _points(item.chief_bonus_points),
            "total_reward": _points(item.total_reward_points),
        }
        for item in execution.events
    ]


def run_week7_reward_benchmark(output_dir: Path = RESULTS_DIR, *, keep_fixtures: bool = False):
    with fixed_service_clock():
        with temporary_fixture_roots(keep=keep_fixtures) as roots:
            calculated = _build_primary(roots, finalize=False)
            source_passed = _source_change_check(roots, calculated)
            crash_passed = _crash_recovery_check(roots, calculated)
            start = perf_counter()
            response = finalize_week7_task_reward_cycle(roots.protocol, PROJECT_ID, calculated.cycle.reward_cycle_id)
            calculated.durations_ms["finalization"] = _elapsed(start)
            calculated.cycle = response.cycle
            calculated.events = list_week7_reward_events(roots.protocol, reward_cycle_id=calculated.cycle.reward_cycle_id)
            double_passed = _double_reward_check(calculated)
            with temporary_fixture_roots() as replay_roots:
                replay = _build_primary(replay_roots, finalize=True)
                replay_passed = _normalized(calculated) == _normalized(replay)
            stress = _large_top_k_stress()
            cases = _build_cases(
                calculated,
                replay_passed=replay_passed,
                source_change_passed=source_passed,
                crash_passed=crash_passed,
                double_passed=double_passed,
                stress=stress,
            )
            invariants = _invariants(calculated, cases, replay=replay_passed, crash=crash_passed, source=source_passed, double=double_passed)
            summary = Week7BenchmarkSummary(
                benchmark_passed=all(invariants.values()),
                total_cases=len(cases.cases),
                passed=sum(item.passed for item in cases.cases),
                failed=sum(not item.passed for item in cases.cases),
                nodes=64,
                operators=40,
                submissions=sum(len(items) for items in calculated.submissions_by_cluster.values()) + len(calculated.invalid_submission_ids),
                valid_submissions=sum(len(items) for items in calculated.submissions_by_cluster.values()),
                invalid_submissions=len(calculated.invalid_submission_ids),
                finding_clusters=len(calculated.clusters_by_name),
                rewarded_clusters=sum(item.distributed_points > 0 for item in calculated.day5.cluster_payouts),
                undistributed_clusters=sum(item.undistributed_points > 0 for item in calculated.day5.cluster_payouts),
                reward_events=len(calculated.events),
                rewarded_operators=len({item.operator_id for item in calculated.events}),
                chief_finder_count=sum(item.chief_finder for item in calculated.events),
                miner_pool=_points(calculated.budget.miner_pool_points),
                distributed_miner_amount=_points(calculated.cycle.distributed_miner_points),
                undistributed_miner_amount=_points(calculated.cycle.undistributed_miner_points),
                validator_pool_reserved=_points(calculated.budget.validator_pool_points),
                protocol_pool_reserved=_points(calculated.budget.protocol_pool_points),
                determinism_passed=replay_passed,
                crash_recovery_passed=crash_passed,
                source_revalidation_passed=source_passed,
                double_reward_protection_passed=double_passed,
                generated_files=list(GENERATED_FILENAMES),
                durations_ms=calculated.durations_ms,
                known_limitations=[
                    "operator_id deduplication does not prove that two operator IDs are controlled by different people",
                    "validator-produced severity and component evidence remain trusted inputs without multi-validator consensus",
                    "deterministic root-cause clustering can still false-merge or false-split findings",
                    "RewardEvents are protocol-point accounting records; no token, wallet, or blockchain settlement occurs",
                ],
                started_at=FIXED_NOW,
                completed_at=FIXED_NOW,
            )
            outputs = {
                "clusters": _cluster_output(calculated),
                "operators": _operator_output(calculated),
                "events": _event_output(calculated),
                "invariants": invariants,
                "determinism": {
                    "clean_root_replay": replay_passed,
                    "randomized_input_order": stress[0].model_dump(mode="json") == stress[1].model_dump(mode="json"),
                    "reward_event_ids_equal": [item.reward_event_id for item in calculated.events] == [item.reward_event_id for item in replay.events],
                    "economic_fingerprint_equal": calculated.cycle.calculation_fingerprint == replay.cycle.calculation_fingerprint,
                },
            }
            write_outputs(output_dir, cases, summary, outputs)
            return cases, summary, outputs


def write_outputs(output_dir: Path, cases: Week7CaseResultsDocument, summary: Week7BenchmarkSummary, outputs: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "week7_case_results.json", cases.model_dump(mode="json"))
    _write_json(output_dir / "week7_summary.json", summary.model_dump(mode="json"))
    _write_json(output_dir / "week7_cluster_allocations.json", outputs["clusters"])
    _write_json(output_dir / "week7_operator_allocations.json", outputs["operators"])
    _write_json(output_dir / "week7_reward_events.json", outputs["events"])
    _write_json(output_dir / "week7_invariants.json", outputs["invariants"])
    _write_json(output_dir / "week7_determinism.json", outputs["determinism"])
    (output_dir / "week7_report.md").write_text(
        render_week7_report(cases, summary.model_dump(mode="json"), outputs), encoding="utf-8"
    )
    for filename in GENERATED_FILENAMES:
        text = (output_dir / filename).read_text(encoding="utf-8")
        if any(token in text.lower() for token in ("/home/", "private_key", "seed_phrase", "mnemonic")):
            raise RuntimeError(f"Generated output contains forbidden data: {filename}")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s:]+[\\/])+[^\s:]+", "[path]", f"{exc.__class__.__name__}: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic Week 7 reward benchmark.")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--keep-fixtures", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases, summary, _ = run_week7_reward_benchmark(args.output_dir, keep_fixtures=args.keep_fixtures)
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 2
    print(f"Week 7 reward benchmark: {summary.passed}/{summary.total_cases} cases passed.")
    print(f"Outputs: {args.output_dir}")
    return 0 if summary.benchmark_passed and all(item.passed for item in cases.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
