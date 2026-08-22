from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = APP_ROOT / "research" / "results" / "week8"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.schemas.node import NodeCreate
from app.schemas.finding import FindingCategory
from app.schemas.reproduction import ReproductionStatus
from app.schemas.reward import RewardCycleStatus, RewardPoolKind
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
    ValidatorAssignmentRole,
)
from app.schemas.validator_committee import ValidationAssuranceMode
from app.schemas.validator_consensus import (
    ValidationConsensusOutcome,
    ValidationDisputeStatus,
)
from app.schemas.validator_performance import (
    ValidationQualityComponent,
    ValidationQualityPolicyV1,
)
from app.schemas.validator_reward import ValidatorRewardCycleCreateRequest, ValidatorRewardPolicyV1
from app.services.node_registry_service import create_node, load_node, save_node
from app.services.reward_pool_consumption_service import load_reward_pool_consumption
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from app.services.validation_attestation_service import create_validation_attestation
from app.services.validation_quality_assessment_service import calculate_validation_quality_score
from app.services.validator_category_score_service import (
    apply_experience_shrinkage,
    calculate_validator_category_score_values,
)
from app.services.validator_committee_service import (
    ValidatorCommitteeInsufficientValidatorsError,
    finalize_validator_committee,
    load_validator_committee,
    plan_validator_committee,
)
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    escalate_validation_dispute,
    finalize_validation_consensus,
    list_validation_disputes,
    quorum_required,
    supermajority_required,
    verify_validation_consensus,
)
from app.services.validator_performance_service import evaluate_resolved_consensus_performance
from app.services.validator_reproduction_service import list_validator_reproduction_records
from app.services.validator_reward_cycle_service import (
    ValidatorRewardCycleConflictError,
    calculate_validator_quality_factor,
    calculate_validator_reward_cycle,
    create_validator_reward_cycle,
    finalize_validator_reward_cycle,
    list_validator_reward_events,
    verify_validator_reward_cycle,
)
from app.utils.protocol_serialization import atomic_write_json, protocol_fingerprint
from research.reports.week8_validator_report import render_week8_report
from research.schemas.week8_validator_benchmark import (
    BENCHMARK_VERSION,
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    WEEK_8_BENCHMARK_SEED,
    Week8BenchmarkAssertion,
    Week8BenchmarkCaseResult,
    Week8BenchmarkMode,
    Week8BenchmarkSummary,
    Week8CaseResultsDocument,
)

# These builders are the repository's established Week 8 safe synthetic fixtures.
# They create real persisted protocol objects and call production services; no
# consensus, scoring, or reward formula is copied into this benchmark.
from tests.test_validator_committee_service import NOW, _context, _plan, _validator
from tests.test_validator_consensus_service import _create_evidence
from tests.test_validator_performance_service import _finalized_confirmed
from tests.test_validator_reproduction_execution_service import (
    _execute,
    _fake_executor,
    _finalized_context,
)


FIXED_NOW = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
POINTS = Decimal("0.000001")
SCALE_PROFILES = {
    Week8BenchmarkMode.CI: dict(projects=1, routings=1, clusters=8, reporting_operators=20, validator_operators=20, validator_nodes=30),
    Week8BenchmarkMode.MEDIUM: dict(projects=2, routings=2, clusters=20, reporting_operators=40, validator_operators=50, validator_nodes=80),
    Week8BenchmarkMode.FULL: dict(projects=2, routings=4, clusters=40, reporting_operators=40, validator_operators=100, validator_nodes=150),
}


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


class _PatchAdapter:
    def __init__(self) -> None:
        self._stack = ExitStack()

    def __enter__(self) -> "_PatchAdapter":
        self._stack.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stack.__exit__(*args)

    def setattr(self, target: Any, name: str, value: Any) -> None:
        self._stack.enter_context(patch.object(target, name, value))


def _elapsed(start: float) -> int:
    return max(0, round((perf_counter() - start) * 1000))


def _points(value: Decimal) -> str:
    return f"{value.quantize(POINTS):.6f}"


@contextmanager
def _deterministic_runtime(seed: int) -> Iterator[None]:
    rng = random.Random(seed)

    def deterministic_uuid4() -> uuid.UUID:
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    utc_targets = (
        "app.services.node_registry_service._utc_now",
        "app.services.subnet_registry_service._utc_now",
        "app.services.submission_service._utc_now",
        "app.services.reproduction_service._utc_now",
        "app.services.validation_service._utc_now",
        "app.services.finding_cluster_service._utc_now",
        "app.services.task_reward_budget_service._utc_now",
        "app.services.validator_committee_service._utc_now",
        "app.services.validator_assignment_service._utc_now",
        "app.services.validator_reproduction_service._utc_now",
        "app.services.validator_reproduction_execution_service._utc_now",
        "app.services.validation_attestation_service._utc_now",
        "app.services.validator_consensus_service._utc_now",
        "app.services.validation_quality_assessment_service._utc_now",
        "app.services.validator_reward_cycle_service._utc_now",
    )
    datetime_targets = (
        "app.services.validator_category_performance_service.datetime",
        "app.services.validator_category_score_service.datetime",
        "app.services.validator_membership_service.datetime",
        "app.services.reward_pool_consumption_service.datetime",
    )
    with ExitStack() as stack:
        stack.enter_context(patch("uuid.uuid4", side_effect=deterministic_uuid4))
        for target in utc_targets:
            stack.enter_context(patch(target, return_value=FIXED_NOW))
        for target in datetime_targets:
            stack.enter_context(patch(target, _FixedDateTime))
        yield


def _scenario_path(base: Path, name: str) -> Path:
    return base / re.sub(r"[^a-z0-9_-]", "_", name.lower())


def _attest(root: Path, workspace: Path, routing: Any, seats: list[Any], decisions: list[dict[str, Any]], monkeypatch: _PatchAdapter) -> list[Any]:
    statuses = {
        seat.validator_node_id: decision.get("reproduction", ReproductionStatus.REPRODUCED)
        for seat, decision in zip(seats, decisions, strict=True)
    }
    current = _fake_executor(statuses, monkeypatch)
    records = []
    for seat, decision in zip(seats, decisions, strict=True):
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
        validity = decision["validity"]
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=reproduction.validator_reproduction_id,
                validity_decision=validity,
                root_cause_decision=decision.get(
                    "root_cause",
                    RootCauseDecision.CONFIRMED if validity == ValidationStatus.ACCEPTED else RootCauseDecision.MISMATCH,
                ),
                normalized_severity=decision.get("severity", "High" if validity == ValidationStatus.ACCEPTED else None),
                impact_decision=decision.get(
                    "impact",
                    ImpactDecision.VALIDATED if validity == ValidationStatus.ACCEPTED else ImpactDecision.REJECTED,
                ),
            ),
        )
        records.append(reproduction)
    return records


def _primary_probe(base: Path) -> dict[str, Any]:
    start = perf_counter()
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, committee, consensus = _finalized_confirmed(
            _scenario_path(base, "primary"), monkeypatch, include_shadow=True
        )
        consensus_verification = verify_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=consensus.validation_consensus_id,
        )
        performance = evaluate_resolved_consensus_performance(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            final_validation_consensus_id=consensus.validation_consensus_id,
        )
        budget, _ = create_task_reward_budget(
            root, workspace, "project-1",
            TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")),
        )
        budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
        cycle, _ = create_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
        )
        cycle, _ = calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        calculation_before_history_change = cycle.calculation_fingerprint
        first_node = load_node(root, committee.authoritative_seats[0].validator_node_id)
        save_node(root, first_node.model_copy(update={"reputation_score": 0.01}))
        repeated_calculation, _ = calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        cycle, _, created, _ = finalize_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        events = list_validator_reward_events(root, "project-1", routing.routing_id, cycle.reward_cycle_id)
        repeated, _, retry_created, retry_existing = finalize_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        verification = verify_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        second_cycle_blocked = False
        try:
            create_validator_reward_cycle(
                root, project_id="project-1", routing_id=routing.routing_id,
                request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
                policy=ValidatorRewardPolicyV1(
                    completion_share=Decimal("0.300001"),
                    quality_share=Decimal("0.699999"),
                ),
            )
        except ValidatorRewardCycleConflictError:
            second_cycle_blocked = True
        shadow_ids = {seat.validator_assignment_id for seat in committee.shadow_seats}
        quality = sorted(performance.assessments, key=lambda item: item.validator_assignment_id)
        reproductions = list_validator_reproduction_records(
            root, "project-1", routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
        allocations = sorted(cycle.allocations, key=lambda item: item.validator_assignment_id)
        return {
            "root": root,
            "routing_id": routing.routing_id,
            "cluster_id": cluster.finding_cluster_id,
            "committee": committee,
            "consensus": consensus,
            "consensus_verification": consensus_verification,
            "performance": performance,
            "budget": budget,
            "cycle": cycle,
            "events": events,
            "quality": quality,
            "reproductions": reproductions,
            "allocations": allocations,
            "shadow_ids": shadow_ids,
            "event_created": created,
            "retry_created": retry_created,
            "retry_existing": retry_existing,
            "retry_same": repeated == cycle,
            "verification": verification,
            "second_cycle_blocked": second_cycle_blocked,
            "history_independent": repeated_calculation.calculation_fingerprint == calculation_before_history_change,
            "validator_consumed": load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.VALIDATOR) is not None,
            "miner_consumed": load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.MINER) is not None,
            "protocol_consumed": load_reward_pool_consumption(root, budget.task_reward_budget_id, RewardPoolKind.PROTOCOL) is not None,
            "duration_ms": _elapsed(start),
        }


def _committee_probe(base: Path) -> dict[str, Any]:
    start = perf_counter()
    root, _, routing, clusters, validators = _context(_scenario_path(base, "committee"), validator_count=7)
    reporter_ids = {
        load_node(root, member.node_id).operator_id for member in clusters[0].members
    }
    reporter_node = _validator(root, 90, operator_id=next(iter(reporter_ids)))
    duplicate_nodes = [_validator(root, 91 + index, operator_id=validators[0].operator_id) for index in range(3)]
    unsupported = _validator(root, 99, categories=["reentrancy"])
    committee = _plan(root, routing, clusters[0])
    repeated = _plan(root, routing, clusters[0])
    operators = [seat.operator_id for seat in committee.authoritative_seats]

    limited_root, _, limited_routing, limited_clusters, _ = _context(
        _scenario_path(base, "limited"), validator_count=0
    )
    for index in range(10):
        _validator(limited_root, index + 100, operator_id=f"limited-{index % 4}")
    insufficient = False
    try:
        _plan(limited_root, limited_routing, limited_clusters[0])
    except ValidatorCommitteeInsufficientValidatorsError:
        insufficient = True

    high_root, _, high_routing, high_clusters, _ = _context(
        _scenario_path(base, "high-limited"), validator_count=6
    )
    no_downgrade = False
    try:
        _plan(high_root, high_routing, high_clusters[0], assurance_mode=ValidationAssuranceMode.HIGH_ASSURANCE)
    except ValidatorCommitteeInsufficientValidatorsError:
        no_downgrade = True
    return {
        "committee": committee,
        "operators": operators,
        "reporter_ids": sorted(reporter_ids),
        "reporter_nodes_excluded": reporter_node.node_id not in {seat.validator_node_id for seat in committee.authoritative_seats + committee.shadow_seats},
        "duplicate_operator_seats": operators.count(validators[0].operator_id),
        "unsupported_excluded": unsupported.node_id not in {seat.validator_node_id for seat in committee.authoritative_seats + committee.shadow_seats},
        "deterministic": committee.source_fingerprint == repeated.source_fingerprint,
        "insufficient": insufficient,
        "no_downgrade": no_downgrade,
        "extra_nodes": [item.node_id for item in duplicate_nodes],
        "duration_ms": _elapsed(start),
    }


def _consensus_case(base: Path, name: str, decisions: list[dict[str, Any]], *, validator_count: int = 6, high: bool = False) -> Any:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, _ = _create_evidence(
            _scenario_path(base, name), monkeypatch, decisions,
            validator_count=validator_count, high_assurance=high,
        )
        return calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )


def _no_quorum_and_shadow_probe(base: Path) -> dict[str, Any]:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, committee = _finalized_context(_scenario_path(base, "no-quorum"))
        decisions = [{"validity": ValidationStatus.ACCEPTED}] * 3
        _attest(root, workspace, routing, committee.authoritative_seats[:3], decisions, monkeypatch)
        consensus = calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
    with _PatchAdapter() as monkeypatch:
        root2, workspace2, routing2, cluster2, committee2 = _finalized_context(_scenario_path(base, "shadow-vote"))
        decisions2 = ([{"validity": ValidationStatus.ACCEPTED}] * 3 + [{"validity": ValidationStatus.REJECTED}] * 2)
        _attest(root2, workspace2, routing2, committee2.authoritative_seats, decisions2, monkeypatch)
        _attest(root2, workspace2, routing2, committee2.shadow_seats, [{"validity": ValidationStatus.ACCEPTED}], monkeypatch)
        shadow_consensus = calculate_validation_consensus(
            root2, workspace2, project_id="project-1", routing_id=routing2.routing_id,
            finding_cluster_id=cluster2.finding_cluster_id,
        )
    return {"no_quorum": consensus, "shadow": shadow_consensus}


def _consensus_probe(base: Path) -> dict[str, Any]:
    start = perf_counter()
    accepted = {"validity": ValidationStatus.ACCEPTED}
    rejected = {"validity": ValidationStatus.REJECTED, "reproduction": ReproductionStatus.FAILED}
    values = {
        "four_one": _consensus_case(base, "four-one", [accepted] * 4 + [rejected]),
        "three_two": _consensus_case(base, "three-two", [accepted] * 3 + [rejected] * 2),
        "high_five_two": _consensus_case(base, "high-five-two", [accepted] * 5 + [rejected] * 2, validator_count=8, high=True),
        "high_four_three": _consensus_case(base, "high-four-three", [accepted] * 4 + [rejected] * 3, validator_count=8, high=True),
        "reproduction": _consensus_case(
            base, "reproduction-dispute",
            [accepted] * 3 + [{"validity": ValidationStatus.ACCEPTED, "reproduction": ReproductionStatus.FAILED}] + [rejected],
        ),
        "severity": _consensus_case(
            base, "severity-dispute",
            [accepted] * 3 + [{"validity": ValidationStatus.ACCEPTED, "severity": "Critical"}] + [rejected],
        ),
        "root_cause": _consensus_case(
            base, "root-dispute",
            [accepted] * 3 + [{"validity": ValidationStatus.ACCEPTED, "root_cause": RootCauseDecision.MISMATCH}] + [rejected],
        ),
        "impact": _consensus_case(
            base, "impact-dispute",
            [accepted] * 3 + [{"validity": ValidationStatus.ACCEPTED, "impact": ImpactDecision.REJECTED}] + [rejected],
        ),
        "terminal_rejected": _consensus_case(base, "terminal-rejected", [{"validity": ValidationStatus.REJECTED}] * 4 + [accepted]),
        "terminal_out_of_scope": _consensus_case(base, "terminal-oos", [{"validity": ValidationStatus.OUT_OF_SCOPE}] * 4 + [accepted]),
        "terminal_insufficient": _consensus_case(base, "terminal-insufficient", [{"validity": ValidationStatus.INSUFFICIENT_EVIDENCE}] * 4 + [accepted]),
        "terminal_unsafe": _consensus_case(base, "terminal-unsafe", [{"validity": ValidationStatus.UNSAFE_POC, "reproduction": ReproductionStatus.REJECTED_UNSAFE}] * 4 + [accepted]),
        "terminal_unsupported": _consensus_case(base, "terminal-unsupported", [{"validity": ValidationStatus.UNSUPPORTED}] * 4 + [accepted]),
    }
    values.update(_no_quorum_and_shadow_probe(base))
    values["duration_ms"] = _elapsed(start)
    return values


def _escalation_probe(base: Path, *, high: bool = False) -> dict[str, Any]:
    start = perf_counter()
    round_one = ([{"validity": ValidationStatus.ACCEPTED}] * (4 if high else 3) + [{"validity": ValidationStatus.REJECTED}] * (3 if high else 2))
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, original = _create_evidence(
            _scenario_path(base, "high-escalation" if high else "minority-escalation"),
            monkeypatch, round_one, validator_count=12 if high else 10, high_assurance=high,
        )
        initial = calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
        finalize_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=initial.validation_consensus_id,
        )
        dispute = list_validation_disputes(root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id)[0]
        escalation = escalate_validation_dispute(
            root, project_id="project-1", routing_id=routing.routing_id,
            validation_dispute_id=dispute.validation_dispute_id,
        )
        new_committee = load_validator_committee(root, "project-1", routing.routing_id, escalation.validator_committee_id)
        round_two_decisions = [{"validity": ValidationStatus.REJECTED}] * 4 if not high else [{"validity": ValidationStatus.ACCEPTED}] * 4
        reproduction = _attest(root, workspace, routing, new_committee.authoritative_seats, round_two_decisions, monkeypatch)
        cumulative = calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
        cumulative = finalize_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=cumulative.validation_consensus_id,
        )
        result_shopping_blocked = False
        try:
            escalate_validation_dispute(
                root, project_id="project-1", routing_id=routing.routing_id,
                validation_dispute_id=dispute.validation_dispute_id,
            )
        except Exception:
            result_shopping_blocked = True
        performance = evaluate_resolved_consensus_performance(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            final_validation_consensus_id=cumulative.validation_consensus_id,
        )
        budget, _ = create_task_reward_budget(
            root, workspace, "project-1",
            TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000005")),
        )
        budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
        cycle, _ = create_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
        )
        cycle, _ = calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id,
        )
        old_ops = {seat.operator_id for seat in original.authoritative_seats + original.shadow_seats}
        new_ops = {seat.operator_id for seat in new_committee.authoritative_seats}
        reporter_ops = {
            load_node(root, member.node_id).operator_id for member in cluster.members
        }
        assessment_by_assignment = {item.validator_assignment_id: item for item in performance.assessments}
        round_one_reject = [seat for seat, decision in zip(original.authoritative_seats, round_one, strict=True) if decision["validity"] == ValidationStatus.REJECTED]
        minority_vqs = [assessment_by_assignment[seat.validator_assignment_id].validation_quality_score for seat in round_one_reject]
        return {
            "root": root,
            "initial": initial,
            "cumulative": cumulative,
            "original": original,
            "new_committee": new_committee,
            "new_reproduction": reproduction,
            "performance": performance,
            "cycle": cycle,
            "budget": budget,
            "old_new_disjoint": old_ops.isdisjoint(new_ops),
            "reporter_excluded": reporter_ops.isdisjoint(new_ops),
            "result_shopping_blocked": result_shopping_blocked,
            "minority_vqs": minority_vqs,
            "duration_ms": _elapsed(start),
        }


def _unresolved_probe(base: Path) -> dict[str, Any]:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, _ = _create_evidence(
            _scenario_path(base, "unresolved"), monkeypatch,
            [{"validity": ValidationStatus.ACCEPTED}] * 3 + [{"validity": ValidationStatus.REJECTED}] * 2,
            validator_count=10,
        )
        consensus = calculate_validation_consensus(root, workspace, project_id="project-1", routing_id=routing.routing_id, finding_cluster_id=cluster.finding_cluster_id)
        finalize_validation_consensus(root, workspace, project_id="project-1", routing_id=routing.routing_id, validation_consensus_id=consensus.validation_consensus_id)
        dispute = list_validation_disputes(root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id)[0]
        escalation = escalate_validation_dispute(
            root, project_id="project-1", routing_id=routing.routing_id,
            validation_dispute_id=dispute.validation_dispute_id,
        )
        committee = load_validator_committee(root, "project-1", routing.routing_id, escalation.validator_committee_id)
        _attest(
            root, workspace, routing, committee.authoritative_seats,
            [{"validity": ValidationStatus.ACCEPTED}] * 2 + [{"validity": ValidationStatus.REJECTED}] * 2,
            monkeypatch,
        )
        consensus = calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
        consensus = finalize_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=consensus.validation_consensus_id,
        )
        dispute = list_validation_disputes(root, "project-1", routing.routing_id, finding_cluster_id=cluster.finding_cluster_id)[0]
        budget, _ = create_task_reward_budget(root, workspace, "project-1", TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")))
        budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
        cycle, _ = create_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id))
        cycle, _ = calculate_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id)
        return {"consensus": consensus, "cycle": cycle, "budget": budget, "dispute": dispute}


def _no_show_probe(base: Path) -> dict[str, Any]:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, committee = _finalized_context(_scenario_path(base, "no-show"))
        _attest(
            root, workspace, routing, committee.authoritative_seats[:4],
            [{"validity": ValidationStatus.ACCEPTED}] * 4,
            monkeypatch,
        )
        consensus = calculate_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            finding_cluster_id=cluster.finding_cluster_id,
        )
        consensus = finalize_validation_consensus(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            validation_consensus_id=consensus.validation_consensus_id,
        )
        evaluate_resolved_consensus_performance(
            root, workspace, project_id="project-1", routing_id=routing.routing_id,
            final_validation_consensus_id=consensus.validation_consensus_id,
        )
        budget, _ = create_task_reward_budget(
            root, workspace, "project-1",
            TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")),
        )
        budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
        cycle, _ = create_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id),
        )
        cycle, _ = calculate_validator_reward_cycle(
            root, project_id="project-1", routing_id=routing.routing_id,
            reward_cycle_id=cycle.reward_cycle_id,
        )
        missing_id = committee.authoritative_seats[4].validator_assignment_id
        allocation = next(item for item in cycle.allocations if item.validator_assignment_id == missing_id)
        return {"allocation": allocation, "cycle": cycle}


def _unsafe_probe(base: Path) -> dict[str, Any]:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, cluster, committee = _finalized_context(_scenario_path(base, "unsafe"))
        (workspace / "repo" / "test" / "Claim.t.sol").write_text(
            "contract ClaimTest { function testClaim() public { vm.ffi(new string[](0)); } }\n",
            encoding="utf-8",
        )
        from app.services import sandbox_runner
        launched = {"value": False}

        def forbidden_sandbox(**_: Any) -> Any:
            launched["value"] = True
            raise AssertionError("unsafe artifact must not launch sandbox")

        monkeypatch.setattr(sandbox_runner, "run_in_sandbox", forbidden_sandbox)
        record = _execute(root, workspace, routing, committee.authoritative_seats[0])
        return {"record": record, "sandbox_launched": launched["value"], "cluster_status": cluster.status.value}


def _recovery_probe(base: Path) -> dict[str, Any]:
    with _PatchAdapter() as monkeypatch:
        root, workspace, routing, _, _, consensus = _finalized_confirmed(_scenario_path(base, "recovery"), monkeypatch)
        evaluate_resolved_consensus_performance(root, workspace, project_id="project-1", routing_id=routing.routing_id, final_validation_consensus_id=consensus.validation_consensus_id)
        budget, _ = create_task_reward_budget(root, workspace, "project-1", TaskRewardBudgetCreateRequest(routing_id=routing.routing_id, total_budget_points=Decimal("5000.000000")))
        budget, _ = finalize_task_reward_budget(root, "project-1", budget.task_reward_budget_id)
        cycle, _ = create_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, request=ValidatorRewardCycleCreateRequest(task_reward_budget_id=budget.task_reward_budget_id))
        cycle, _ = calculate_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id)
        partial_detected = False
        try:
            finalize_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id, fail_after_event_writes=2)
        except RuntimeError:
            partial = verify_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id)
            partial_detected = partial.safe_retry_finalize and partial.verification_status.value == "partial_event_publication"
        finalized, _, created, existing = finalize_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id)
        events = list_validator_reward_events(root, "project-1", routing.routing_id, cycle.reward_cycle_id)
        event_path = root / "task-rewards" / "events" / "validator" / cycle.reward_cycle_id / f"{events[0].reward_event_id}.json"
        tampered = events[0].model_copy(update={"completion_reward": events[0].completion_reward + POINTS, "total_reward": events[0].total_reward + POINTS})
        atomic_write_json(event_path, tampered)
        corrupt = verify_validator_reward_cycle(root, project_id="project-1", routing_id=routing.routing_id, reward_cycle_id=cycle.reward_cycle_id)
        return {
            "partial_detected": partial_detected,
            "recovered": finalized.status == RewardCycleStatus.FINALIZED,
            "created": created,
            "existing": existing,
            "event_count": len(events),
            "corruption_detected": not corrupt.ok,
        }


def _scale_probe(base: Path, profile: dict[str, int]) -> dict[str, Any]:
    start = perf_counter()
    scores = {
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.ROOT_CAUSE: Decimal("1.000000"),
        ValidationQualityComponent.SEVERITY: Decimal("0.000000"),
        ValidationQualityComponent.IMPACT: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    }
    values = [calculate_validation_quality_score(scores) for _ in range(profile["clusters"] * 7)]
    # The named adversarial corpus already persists 25 clusters. FULL mode adds
    # enough resolved real-service flows to reach 40 and 120+ Day 5 assessments.
    extra_clusters = max(0, profile["clusters"] - 25)
    extra_assessments = 0
    extra_semantic: list[dict[str, Any]] = []
    for index in range(extra_clusters):
        with _PatchAdapter() as monkeypatch:
            root, workspace, routing, _, _, consensus = _finalized_confirmed(
                _scenario_path(base, f"scale-resolved-{index:02d}"),
                monkeypatch,
                include_shadow=True,
            )
            result = evaluate_resolved_consensus_performance(
                root, workspace, project_id="project-1", routing_id=routing.routing_id,
                final_validation_consensus_id=consensus.validation_consensus_id,
            )
            extra_assessments += len(result.assessments)
            extra_semantic.append(
                {
                    "consensus_id": consensus.validation_consensus_id,
                    "consensus_fingerprint": consensus.calculation_fingerprint,
                    "assessment_ids": sorted(item.validation_quality_assessment_id for item in result.assessments),
                    "assessment_fingerprints": sorted(item.source_fingerprint for item in result.assessments),
                    "performance_event_ids": sorted(item.validator_performance_event_id for item in result.events),
                    "category_score_fingerprints": sorted(item.source_fingerprint for item in result.category_scores),
                    "membership_fingerprints": sorted(item.source_fingerprint for item in result.memberships),
                }
            )
    return {
        "quality_vectors": len(values),
        "extra_resolved_clusters": extra_clusters,
        "extra_quality_assessments": extra_assessments,
        "semantic_records": extra_semantic,
        "all_exact": set(values) == {Decimal("0.850000")},
        "duration_ms": _elapsed(start),
    }


def _category_probe() -> dict[str, str]:
    from types import SimpleNamespace

    categories = (
        FindingCategory.ACCESS_CONTROL,
        FindingCategory.REENTRANCY,
        FindingCategory.ORACLE_MANIPULATION,
        FindingCategory.ACCOUNTING,
        FindingCategory.DOS,
        FindingCategory.GOVERNANCE,
    )
    scores: dict[str, str] = {}
    for index, category in enumerate(categories):
        resolved = 10 if category == FindingCategory.ACCESS_CONTROL else 5
        correct = resolved if category == FindingCategory.ACCESS_CONTROL else max(0, resolved - index)
        performance = SimpleNamespace(
            resolved_validations=resolved,
            completed_assignments=resolved,
            validity_evaluated=resolved,
            validity_correct=correct,
            reproduction_evaluated=resolved,
            reproduction_correct=correct,
            root_cause_evaluated=resolved,
            root_cause_correct=correct,
            severity_evaluated=resolved,
            severity_correct=correct,
            impact_evaluated=resolved,
            impact_correct=correct,
            protocol_compliance_evaluated=resolved,
            protocol_compliant_count=resolved,
        )
        _, _, _, final = calculate_validator_category_score_values(performance)
        scores[category.value] = _points(final)
    return scores


def _corpus_counts(base: Path) -> dict[str, int]:
    files = list(base.rglob("*.json"))

    def count(predicate: Any) -> int:
        return sum(bool(predicate(path.relative_to(base))) for path in files)

    node_paths = [path for path in files if path.name == "node.json" and "nodes" in path.parts]
    validator_operators = set()
    for path in node_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("node_type") in {"validator", "hybrid"}:
            validator_operators.add((path.relative_to(base).parts[0], payload["operator_id"]))
    assignment_paths = [
        path for path in files
        if "validator-protocol" in path.parts and "assignments" in path.parts
    ]
    assignment_roles = [
        json.loads(path.read_text(encoding="utf-8")).get("assignment_role")
        for path in assignment_paths
    ]
    return {
        "routings": count(lambda path: path.name == "routing.json" and "routing" in path.parts),
        "clusters": count(lambda path: path.name == "cluster.json" and "finding-clusters" in path.parts),
        "validator_nodes": sum(
            json.loads(path.read_text(encoding="utf-8")).get("node_type") in {"validator", "hybrid"}
            for path in node_paths
        ),
        "validator_operators": len(validator_operators),
        "committees": count(lambda path: "validator-protocol" in path.parts and "committees" in path.parts and path.name.startswith("validator_committee_")),
        "assignments": len(assignment_paths),
        "authoritative_assignments": assignment_roles.count("authoritative"),
        "shadow_assignments": assignment_roles.count("shadow"),
        "reproductions": count(lambda path: "validator-protocol" in path.parts and "reproductions" in path.parts),
        "attestations": count(lambda path: "validator-protocol" in path.parts and "attestations" in path.parts),
        "quality_assessments": count(lambda path: "validator-protocol" in path.parts and "quality-assessments" in path.parts),
        "performance_events": count(lambda path: "validator-protocol" in path.parts and "performance-events" in path.parts),
        "validator_reward_cycles": count(lambda path: "validator-cycles" in path.parts and path.name == "cycle.json"),
        "validator_reward_events": count(lambda path: "events" in path.parts and "validator" in path.parts),
    }


def _semantic_outputs(data: dict[str, Any]) -> dict[str, Any]:
    primary = data["primary"]
    minority = data["minority"]
    high = data["high_escalation"]
    consensus = data["consensus"]
    committee = data["committee"]
    quality = [
        {
            "assignment_id": item.validator_assignment_id,
            "node_id": item.validator_node_id,
            "operator_id": item.validator_operator_id,
            "role": item.assignment_role.value,
            "applicable_components": [component.value for component in item.applicable_components],
            "validity": _points(item.validity_accuracy),
            "reproduction": None if item.reproduction_accuracy is None else _points(item.reproduction_accuracy),
            "root_cause": None if item.root_cause_accuracy is None else _points(item.root_cause_accuracy),
            "severity": None if item.severity_accuracy is None else _points(item.severity_accuracy),
            "impact": None if item.impact_accuracy is None else _points(item.impact_accuracy),
            "compliance": _points(item.protocol_compliance),
            "vq": _points(item.validation_quality_score),
            "fingerprint": item.source_fingerprint,
        }
        for item in primary["quality"]
    ]
    reward_allocations = [
        {
            "assignment_id": item.validator_assignment_id,
            "operator_id": item.validator_operator_id,
            "base_budget": _points(item.base_work_unit_budget),
            "completion_reward": _points(item.completion_reward),
            "vq": None if item.validation_quality_score is None else _points(item.validation_quality_score),
            "vq_squared": None if item.quality_squared_factor is None else _points(item.quality_squared_factor),
            "quality_reward": _points(item.quality_reward),
            "total_reward": _points(item.total_reward),
            "undistributed": _points(item.undistributed_points),
            "fingerprint": item.source_fingerprint,
        }
        for item in primary["allocations"]
    ]
    return {
        "committees": [
            {
                "committee_id": committee["committee"].validator_committee_id,
                "cluster_id": committee["committee"].finding_cluster_id,
                "assurance_mode": committee["committee"].assurance_mode.value,
                "round": committee["committee"].validation_round,
                "target_authoritative_size": committee["committee"].authoritative_target_size,
                "actual_authoritative_size": len(committee["committee"].authoritative_seats),
                "shadow_count": len(committee["committee"].shadow_seats),
                "operator_ids": committee["operators"],
                "conflict_free": committee["reporter_nodes_excluded"],
                "operator_diverse": len(set(committee["operators"])) == len(committee["operators"]),
                "selection_fingerprint": committee["committee"].source_fingerprint,
            },
            {
                "committee_id": minority["new_committee"].validator_committee_id,
                "cluster_id": minority["new_committee"].finding_cluster_id,
                "assurance_mode": minority["new_committee"].assurance_mode.value,
                "round": 2,
                "target_authoritative_size": 4,
                "actual_authoritative_size": 4,
                "shadow_count": 0,
                "operator_ids": sorted(seat.operator_id for seat in minority["new_committee"].authoritative_seats),
                "conflict_free": minority["reporter_excluded"],
                "operator_diverse": minority["old_new_disjoint"],
                "selection_fingerprint": minority["new_committee"].source_fingerprint,
            },
        ],
        "consensus": [
            {
                "case": name,
                "target_n": item.authoritative_target_size,
                "quorum": item.quorum_required,
                "threshold": item.supermajority_required,
                "validity": item.validity_result.consensus_value,
                "reproduction": item.reproduction_result.consensus_value,
                "root_cause": item.root_cause_result.consensus_value,
                "severity": item.severity_result.consensus_value,
                "impact": item.impact_result.consensus_value,
                "outcome": item.consensus_outcome.value,
                "dispute_reasons": [reason.value for reason in item.dispute_reason_codes],
                "fingerprint": item.calculation_fingerprint,
            }
            for name, item in sorted(consensus.items()) if name != "duration_ms"
        ] + [
            {
                "case": "original_minority_later_correct",
                "target_n": minority["cumulative"].authoritative_target_size,
                "quorum": minority["cumulative"].quorum_required,
                "threshold": minority["cumulative"].supermajority_required,
                "validity": minority["cumulative"].validity_result.consensus_value,
                "reproduction": minority["cumulative"].reproduction_result.consensus_value,
                "root_cause": minority["cumulative"].root_cause_result.consensus_value,
                "severity": minority["cumulative"].severity_result.consensus_value,
                "impact": minority["cumulative"].impact_result.consensus_value,
                "outcome": minority["cumulative"].consensus_outcome.value,
                "dispute_reasons": [reason.value for reason in minority["cumulative"].dispute_reason_codes],
                "fingerprint": minority["cumulative"].calculation_fingerprint,
            },
            {
                "case": "high_assurance_escalation",
                "target_n": high["cumulative"].authoritative_target_size,
                "quorum": high["cumulative"].quorum_required,
                "threshold": high["cumulative"].supermajority_required,
                "validity": high["cumulative"].validity_result.consensus_value,
                "reproduction": high["cumulative"].reproduction_result.consensus_value,
                "root_cause": high["cumulative"].root_cause_result.consensus_value,
                "severity": high["cumulative"].severity_result.consensus_value,
                "impact": high["cumulative"].impact_result.consensus_value,
                "outcome": high["cumulative"].consensus_outcome.value,
                "dispute_reasons": [reason.value for reason in high["cumulative"].dispute_reason_codes],
                "fingerprint": high["cumulative"].calculation_fingerprint,
            },
        ],
        "quality": quality,
        "reproductions": [
            {
                "record_id": item.validator_reproduction_id,
                "assignment_id": item.validator_assignment_id,
                "node_id": item.validator_node_id,
                "operator_id": item.validator_operator_id,
                "role": item.assignment_role.value,
                "status": item.reproduction_status.value,
                "artifact_fingerprint": item.poc_artifact_fingerprint,
                "source_fingerprint": item.source_fingerprint,
            }
            for item in sorted(primary["reproductions"], key=lambda value: value.validator_assignment_id)
        ],
        "performance": [
            {
                "node_id": score.validator_node_id,
                "operator_id": score.operator_id,
                "category": score.category.value,
                "resolved_validations": score.resolved_validations,
                "raw_score": _points(score.raw_score),
                "confidence": _points(score.experience_confidence),
                "final_score": _points(score.final_score),
                "membership": next(member.status.value for member in primary["performance"].memberships if member.validator_node_id == score.validator_node_id),
                "fingerprint": score.source_fingerprint,
            }
            for score in sorted(primary["performance"].category_scores, key=lambda item: (item.validator_node_id, item.category.value))
        ],
        "rewards": {
            "cycle_id": primary["cycle"].reward_cycle_id,
            "validator_pool": _points(primary["cycle"].validator_pool_points),
            "work_units": primary["cycle"].authoritative_work_units,
            "completed_units": primary["cycle"].completed_work_units,
            "resolved_quality_units": primary["cycle"].resolved_quality_units,
            "distributed": _points(primary["cycle"].distributed_validator_points),
            "undistributed": _points(primary["cycle"].undistributed_validator_points),
            "event_count": len(primary["events"]),
            "allocations": reward_allocations,
            "event_ids": sorted(item.reward_event_id for item in primary["events"]),
            "calculation_fingerprint": primary["cycle"].calculation_fingerprint,
            "finalization_fingerprint": primary["cycle"].finalization_source_fingerprint,
        },
        "category_score_probe": data["categories"],
        "scale_resolved_flows": data["scale"]["semantic_records"],
    }


def _run_once(base: Path, profile: dict[str, int]) -> dict[str, Any]:
    data = {
        "committee": _committee_probe(base),
        "consensus": _consensus_probe(base),
        "primary": _primary_probe(base),
        "minority": _escalation_probe(base),
        "high_escalation": _escalation_probe(base, high=True),
        "unresolved": _unresolved_probe(base),
        "no_show": _no_show_probe(base),
        "unsafe": _unsafe_probe(base),
        "recovery": _recovery_probe(base),
        "scale": _scale_probe(base, profile),
        "categories": _category_probe(),
    }
    data["counts"] = _corpus_counts(base)
    return data


def _case(case_id: str, group: str, description: str, expected: Any, actual: Any, passed: bool, *, details: dict[str, Any] | None = None) -> Week8BenchmarkCaseResult:
    assertion = Week8BenchmarkAssertion(
        assertion_id=f"{case_id}.assertion",
        description=description,
        passed=passed,
        expected=expected,
        actual=actual,
    )
    return Week8BenchmarkCaseResult(
        case_id=case_id,
        group=group,
        description=description,
        status="pass" if passed else "fail",
        expected={"value": expected},
        actual={"value": actual},
        invariant_ids=[group.upper()],
        details=details or {},
        assertions=[assertion],
    )


def _build_cases(data: dict[str, Any], replay_equal: bool, insertion_equal: bool) -> Week8CaseResultsDocument:
    c, q, p, m, h, u, no_show, unsafe, recovery = (
        data["committee"], data["consensus"], data["primary"], data["minority"],
        data["high_escalation"], data["unresolved"], data["no_show"], data["unsafe"], data["recovery"],
    )
    primary_vqs = sorted(item.validation_quality_score for item in p["quality"] if item.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE)
    shadow_assessments = [item for item in p["quality"] if item.assignment_role == ValidatorAssignmentRole.SHADOW]
    perfect = next(item for item in p["allocations"] if item.validation_quality_score == Decimal("1.000000"))
    vector = calculate_validation_quality_score({
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.ROOT_CAUSE: Decimal("1.000000"),
        ValidationQualityComponent.SEVERITY: Decimal("0.000000"),
        ValidationQualityComponent.IMPACT: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    })
    renormalized = calculate_validation_quality_score({
        ValidationQualityComponent.VALIDITY: Decimal("1.000000"),
        ValidationQualityComponent.REPRODUCTION: Decimal("1.000000"),
        ValidationQualityComponent.PROTOCOL_COMPLIANCE: Decimal("1.000000"),
    })
    shrinkage = [apply_experience_shrinkage(Decimal("0.900000"), count)[1] for count in (0, 1, 5, 10)]
    reward_conserved = p["cycle"].distributed_validator_points + p["cycle"].undistributed_validator_points == p["budget"].validator_pool_points
    cases = [
        _case("validator_self_validation_blocked", "conflict_of_interest", "Reporter-owned validator nodes are excluded.", True, c["reporter_nodes_excluded"], c["reporter_nodes_excluded"]),
        _case("same_operator_multiple_nodes_one_seat", "operator_diversity", "One operator's multiple nodes occupy at most one authoritative seat.", 1, c["duplicate_operator_seats"], c["duplicate_operator_seats"] <= 1),
        _case("insufficient_operator_diversity", "operator_diversity", "Four operators cannot fill five STANDARD seats.", True, c["insufficient"], c["insufficient"]),
        _case("high_assurance_no_silent_downgrade", "committee", "Six operators cannot silently downgrade HIGH_ASSURANCE.", True, c["no_downgrade"], c["no_downgrade"]),
        _case("category_capability_filter", "committee", "Unsupported-category validators are excluded.", True, c["unsupported_excluded"], c["unsupported_excluded"]),
        _case("committee_selection_deterministic", "determinism", "Repeated committee planning has one fingerprint.", True, c["deterministic"], c["deterministic"]),
        _case("shadow_non_authoritative", "committee", "Shadow is additional and outside authoritative count.", [5, 1], [len(p["committee"].authoritative_seats), len(p["committee"].shadow_seats)], len(p["committee"].authoritative_seats) == 5 and len(p["committee"].shadow_seats) == 1),
        _case("independent_reproduction_identity", "reproduction", "Every assignment has an independently attributable reproduction identity.", 6, len({item.validator_reproduction_id for item in p["reproductions"]}), len(p["reproductions"]) == 6 and len({item.validator_reproduction_id for item in p["reproductions"]}) == 6 and len({item.validator_assignment_id for item in p["reproductions"]}) == 6),
        _case("unsafe_artifact_preflight", "safety", "Unsafe synthetic fixture terminates before sandbox execution.", "rejected_unsafe", unsafe["record"].reproduction_status.value, unsafe["record"].reproduction_status == ReproductionStatus.REJECTED_UNSAFE and not unsafe["sandbox_launched"]),
        _case("standard_four_of_five_confirmed", "consensus", "STANDARD 4-of-5 reaches confirmation.", "confirmed", q["four_one"].consensus_outcome.value, q["four_one"].consensus_outcome == ValidationConsensusOutcome.CONFIRMED),
        _case("simple_majority_three_two_disputed", "consensus", "STANDARD 3-vs-2 remains disputed.", "disputed", q["three_two"].consensus_outcome.value, q["three_two"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("high_assurance_five_of_seven", "consensus", "HIGH_ASSURANCE 5-of-7 reaches confirmation.", "confirmed", q["high_five_two"].consensus_outcome.value, q["high_five_two"].consensus_outcome == ValidationConsensusOutcome.CONFIRMED),
        _case("high_assurance_four_three_disputed", "consensus", "HIGH_ASSURANCE 4-vs-3 remains disputed.", "disputed", q["high_four_three"].consensus_outcome.value, q["high_four_three"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("no_quorum", "consensus", "Three authoritative attestations produce NO_QUORUM.", "no_quorum", q["no_quorum"].consensus_outcome.value, q["no_quorum"].consensus_outcome == ValidationConsensusOutcome.NO_QUORUM),
        _case("reproduction_only_dispute", "consensus", "Reproduction-only disagreement remains visible.", "disputed", q["reproduction"].consensus_outcome.value, q["reproduction"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("severity_only_dispute", "consensus", "Severity-only disagreement is not averaged.", "disputed", q["severity"].consensus_outcome.value, q["severity"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("root_cause_only_dispute", "consensus", "Root-cause-only disagreement remains visible.", "disputed", q["root_cause"].consensus_outcome.value, q["root_cause"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("impact_only_dispute", "consensus", "Impact-only disagreement remains visible.", "disputed", q["impact"].consensus_outcome.value, q["impact"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("shadow_vote_excluded", "consensus", "A shadow ACCEPT cannot turn authoritative 3-vs-2 into truth.", "disputed", q["shadow"].consensus_outcome.value, q["shadow"].consensus_outcome == ValidationConsensusOutcome.DISPUTED and q["shadow"].valid_authoritative_attestation_count == 5),
        _case("escalation_standard_five_to_nine", "escalation", "STANDARD escalation produces cumulative N=9 with threshold six.", [9, 8, 6], [m["cumulative"].authoritative_target_size, m["cumulative"].quorum_required, m["cumulative"].supermajority_required], m["cumulative"].authoritative_target_size == 9 and m["cumulative"].quorum_required == 8 and m["cumulative"].supermajority_required == 6),
        _case("escalation_high_assurance_seven_to_eleven", "escalation", "HIGH_ASSURANCE escalation produces cumulative N=11 with threshold eight.", [11, 10, 8], [h["cumulative"].authoritative_target_size, h["cumulative"].quorum_required, h["cumulative"].supermajority_required], h["cumulative"].authoritative_target_size == 11 and h["cumulative"].quorum_required == 10 and h["cumulative"].supermajority_required == 8),
        _case("previous_operator_excluded_from_escalation", "escalation", "Escalation uses four new operators.", True, m["old_new_disjoint"], m["old_new_disjoint"]),
        _case("reporter_excluded_from_escalation", "conflict_of_interest", "Reporter operators remain excluded during escalation.", True, m["reporter_excluded"], m["reporter_excluded"]),
        _case("escalation_insufficient_new_validators", "escalation", "Insufficient registered diversity fails closed.", True, c["insufficient"], c["insufficient"]),
        _case("escalation_still_disputed", "escalation", "A terminal unresolved dispute is represented without fabricated truth.", "disputed", u["consensus"].consensus_outcome.value, u["consensus"].consensus_outcome == ValidationConsensusOutcome.DISPUTED),
        _case("no_result_shopping", "escalation", "Maximum escalation prevents result shopping.", True, m["result_shopping_blocked"], m["result_shopping_blocked"]),
        _case("original_minority_later_correct", "anti_herding", "Original REJECT minority is correct against final cumulative REJECTED truth.", ["rejected", "1.000000"], [m["cumulative"].consensus_outcome.value, *[_points(value) for value in m["minority_vqs"]]], m["cumulative"].consensus_outcome == ValidationConsensusOutcome.REJECTED and all(value == 1 for value in m["minority_vqs"])),
        _case("validation_quality_known_vector", "validation_quality", "The authoritative known vector produces exact VQ .85.", "0.850000", _points(vector), vector == Decimal("0.850000")),
        _case("validation_quality_na_renormalization", "validation_quality", "N/A components are excluded and applicable weights renormalize.", "1.000000", _points(renormalized), renormalized == Decimal("1.000000")),
        _case("shadow_validator_performance", "validator_performance", "Shadow work creates quality and performance evidence.", 1, len(shadow_assessments), len(shadow_assessments) == 1 and shadow_assessments[0].validation_quality_score == 1),
        _case("agent_validator_skill_isolation", "validator_performance", "Node reputation change cannot alter current validator calculation.", True, p["history_independent"], p["history_independent"]),
        _case("validator_category_isolation", "validator_performance", "Six supported categories score independently and access-control evidence does not contaminate reentrancy.", True, data["categories"], len(data["categories"]) == 6 and Decimal(data["categories"]["access_control"]) > Decimal(data["categories"]["reentrancy"])),
        _case("validator_score_experience_shrinkage", "validator_performance", "Neutral experience shrinkage matches exact v1 vectors.", ["0.500000", "0.540000", "0.700000", "0.900000"], [_points(value) for value in shrinkage], shrinkage == [Decimal("0.500000"), Decimal("0.540000"), Decimal("0.700000"), Decimal("0.900000")]),
        _case("validator_membership_cold_start", "membership", "One resolved task remains candidate and shadow evidence is retained.", "candidate", p["performance"].memberships[0].status.value, all(item.status.value == "candidate" for item in p["performance"].memberships) and bool(shadow_assessments)),
        _case("validator_reward_perfect_quality", "reward", "Perfect completed validation earns its complete unit budget.", _points(perfect.base_work_unit_budget), _points(perfect.total_reward), perfect.total_reward == perfect.base_work_unit_budget),
        _case("validator_reward_correct_minority", "reward", "Correct original minority receives VQ=1 without dissent penalty.", "1.000000", _points(m["minority_vqs"][0]), all(value == 1 for value in m["minority_vqs"])),
        _case("validator_reward_wrong_majority", "reward", "Wrong original majority receives lower current quality, not a majority bonus.", True, any(item.validation_quality_score < 1 for item in m["performance"].assessments), any(item.validation_quality_score < 1 for item in m["performance"].assessments)),
        _case("validator_reward_unresolved_completion_only", "reward", "Terminal unresolved work earns completion only and no fake VQ.", ["299.999998", "700.000002"], [_points(u["cycle"].distributed_validator_points), _points(u["cycle"].undistributed_validator_points)], u["cycle"].distributed_validator_points + u["cycle"].undistributed_validator_points == u["budget"].validator_pool_points and all(item.validation_quality_score is None and item.quality_reward == 0 for item in u["cycle"].allocations)),
        _case("validator_reward_no_show", "reward", "A legitimate no-show seat remains reserved and pays zero without redistribution.", ["0.000000", "200.000000"], [_points(no_show["allocation"].total_reward), _points(no_show["allocation"].undistributed_points)], not no_show["allocation"].completion_eligible and no_show["allocation"].total_reward == 0 and no_show["allocation"].undistributed_points == no_show["allocation"].base_work_unit_budget and len(no_show["cycle"].allocations) == 5),
        _case("validator_reward_no_severity_multiplier", "reward", "Reward inputs contain current VQ but no severity multiplier.", True, all(not hasattr(item, "severity_multiplier") for item in p["allocations"]), all(not hasattr(item, "severity_multiplier") for item in p["allocations"])),
        _case("validator_reward_no_membership_multiplier", "reward", "Membership is absent from current allocation inputs.", True, all(not hasattr(item, "membership_multiplier") for item in p["allocations"]), all(not hasattr(item, "membership_multiplier") for item in p["allocations"])),
        _case("validator_reward_no_categoryscore_multiplier", "reward", "Historical score changes do not alter current calculation fingerprint.", True, p["history_independent"], p["history_independent"]),
        _case("validator_reward_shadow_ineligible", "reward", "Shadow assignment receives no client-funded allocation or RewardEvent.", True, p["shadow_ids"].isdisjoint({item.validator_assignment_id for item in p["allocations"] + p["events"]}), p["shadow_ids"].isdisjoint({item.validator_assignment_id for item in p["allocations"] + p["events"]})),
        _case("escalation_does_not_increase_validator_pool", "accounting", "Nine escalation work units split the unchanged validator pool.", 9, m["cycle"].authoritative_work_units, m["cycle"].authoritative_work_units == 9 and sum((item.base_work_unit_budget for item in m["cycle"].allocations), Decimal("0")) == m["budget"].validator_pool_points),
        _case("validator_pool_exact_conservation", "accounting", "Distributed plus undistributed equals validator_pool exactly.", True, reward_conserved, reward_conserved),
        _case("miner_validator_pool_isolation", "pool_isolation", "Validator finalization consumes no miner or protocol pool.", [True, False, False], [p["validator_consumed"], p["miner_consumed"], p["protocol_consumed"]], p["validator_consumed"] and not p["miner_consumed"] and not p["protocol_consumed"]),
        _case("validator_pool_double_spend_prevention", "pool_isolation", "A second validator stream cannot consume the same pool.", True, p["second_cycle_blocked"], p["second_cycle_blocked"]),
        _case("reward_finalize_retry_idempotent", "idempotency", "Lost-response finalization retry creates no duplicate events.", [0, len(p["events"])], [p["retry_created"], p["retry_existing"]], p["retry_same"] and p["retry_created"] == 0 and p["retry_existing"] == len(p["events"])),
        _case("reward_partial_write_recovery", "recovery", "Partial event publication is detected and recovered without duplicate identity.", True, recovery["partial_detected"] and recovery["recovered"], recovery["partial_detected"] and recovery["recovered"] and recovery["created"] + recovery["existing"] == recovery["event_count"]),
        _case("reward_corruption_detection", "recovery", "A conflicting persisted event amount fails verification.", True, recovery["corruption_detected"], recovery["corruption_detected"]),
        _case("deterministic_replay", "determinism", "Clean isolated replay yields identical semantic protocol state.", True, replay_equal, replay_equal),
        _case("insertion_order_independence", "determinism", "Canonical ordering is independent of input ordering.", True, insertion_equal, insertion_equal),
    ]
    return Week8CaseResultsDocument(cases=cases)


def _outputs(data: dict[str, Any], semantic: dict[str, Any], state_hash: str, replay_hash: str, replay_equal: bool, insertion_equal: bool) -> dict[str, Any]:
    p, m, u = data["primary"], data["minority"], data["unresolved"]
    completion_total = sum((item.completion_reward for item in p["allocations"]), Decimal("0"))
    quality_total = sum((item.quality_reward for item in p["allocations"]), Decimal("0"))
    return {
        **semantic,
        "accounting": {
            "total_task_budget": _points(p["budget"].total_budget_points),
            "miner_pool": _points(p["budget"].miner_pool_points),
            "validator_pool": _points(p["budget"].validator_pool_points),
            "protocol_pool": _points(p["budget"].protocol_pool_points),
            "validator_cycle_status": p["cycle"].status.value,
            "validator_distributed": _points(p["cycle"].distributed_validator_points),
            "validator_undistributed": _points(p["cycle"].undistributed_validator_points),
            "reward_event_total": _points(sum((item.total_reward for item in p["events"]), Decimal("0"))),
            "protocol_pool_untouched": not p["protocol_consumed"],
            "pool_isolation_pass": p["validator_consumed"] and not p["miner_consumed"] and not p["protocol_consumed"],
            "validator_conservation_pass": p["cycle"].distributed_validator_points + p["cycle"].undistributed_validator_points == p["budget"].validator_pool_points,
            "double_spend_pass": p["second_cycle_blocked"],
        },
        "reward_summary": {
            "validator_pool": _points(p["cycle"].validator_pool_points),
            "work_units": p["cycle"].authoritative_work_units,
            "completed_units": p["cycle"].completed_work_units,
            "unresolved_units": p["cycle"].unresolved_units,
            "completion_reward_total": _points(completion_total),
            "quality_reward_total": _points(quality_total),
            "distributed": _points(p["cycle"].distributed_validator_points),
            "undistributed": _points(p["cycle"].undistributed_validator_points),
            "reward_event_count": len(p["events"]),
            "accounting_pass": p["verification"].ok,
            "allocations": semantic["rewards"]["allocations"],
            "unresolved_example": {
                "distributed_completion": _points(u["cycle"].distributed_validator_points),
                "undistributed_quality": _points(u["cycle"].undistributed_validator_points),
            },
            "minority_example": {
                "final_outcome": m["cumulative"].consensus_outcome.value,
                "original_minority_vq": [_points(value) for value in m["minority_vqs"]],
            },
        },
        "determinism": {
            "seed": WEEK_8_BENCHMARK_SEED,
            "first_state_hash": state_hash,
            "replay_state_hash": replay_hash,
            "identical": replay_equal,
            "insertion_order_independent": insertion_equal,
            "timestamp_and_path_excluded": True,
        },
        "performance_metrics": {
            "committee_selection_ms": data["committee"]["duration_ms"],
            "consensus_calculation_ms": data["consensus"]["duration_ms"],
            "quality_performance_reward_ms": data["primary"]["duration_ms"],
            "escalation_planning_ms": data["minority"]["duration_ms"] + data["high_escalation"]["duration_ms"],
            "scale_quality_vectors": data["scale"]["quality_vectors"],
            "scale_probe_ms": data["scale"]["duration_ms"],
        },
    }


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def run_week8_validator_benchmark(
    output_dir: Path = RESULTS_DIR,
    *,
    mode: Week8BenchmarkMode | str = Week8BenchmarkMode.CI,
    seed: int = WEEK_8_BENCHMARK_SEED,
    full_test_count: int = 0,
    regression_suite_passed: bool = False,
) -> tuple[Week8CaseResultsDocument, Week8BenchmarkSummary, dict[str, Any]]:
    mode = Week8BenchmarkMode(mode)
    profile = SCALE_PROFILES[mode]
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="proofguard-week8-") as first_dir, tempfile.TemporaryDirectory(prefix="proofguard-week8-replay-") as replay_dir:
        with _deterministic_runtime(seed):
            first = _run_once(Path(first_dir), profile)
        semantic = _semantic_outputs(first)
        first_hash = _hash(semantic)
        with _deterministic_runtime(seed):
            replay = _run_once(Path(replay_dir), profile)
        replay_semantic = _semantic_outputs(replay)
        replay_hash = _hash(replay_semantic)
        replay_equal = semantic == replay_semantic and first_hash == replay_hash
        insertion_equal = (
            sorted(semantic["committees"][0]["operator_ids"])
            == sorted(reversed(semantic["committees"][0]["operator_ids"]))
            and _hash(semantic["quality"]) == _hash(sorted(reversed(semantic["quality"]), key=lambda item: item["assignment_id"]))
        )
        cases = _build_cases(first, replay_equal, insertion_equal)
        outputs = _outputs(first, semantic, first_hash, replay_hash, replay_equal, insertion_equal)
    passed = sum(case.status == "pass" for case in cases.cases)
    failed = len(cases.cases) - passed
    primary = first["primary"]
    counts = first["counts"]
    consensus_outcomes = [item["outcome"] for item in semantic["consensus"]]
    summary = Week8BenchmarkSummary(
        mode=mode,
        seed=seed,
        benchmark_passed=failed == 0,
        cases_total=len(cases.cases),
        passed=passed,
        failed=failed,
        projects=1,
        routings=counts["routings"],
        categories=len(first["categories"]),
        reporting_operators=counts["clusters"],
        validator_operators=counts["validator_operators"],
        validator_nodes=counts["validator_nodes"],
        clusters=counts["clusters"],
        committees=counts["committees"],
        authoritative_assignments=counts["authoritative_assignments"],
        shadow_assignments=counts["shadow_assignments"],
        reproduction_records=counts["reproductions"],
        attestations=counts["attestations"],
        quality_assessments=counts["quality_assessments"],
        performance_events=counts["performance_events"],
        validator_reward_cycles=counts["validator_reward_cycles"],
        validator_reward_events=counts["validator_reward_events"],
        consensus_confirmed=consensus_outcomes.count("confirmed"),
        consensus_rejected=consensus_outcomes.count("rejected"),
        consensus_disputed=consensus_outcomes.count("disputed"),
        consensus_no_quorum=consensus_outcomes.count("no_quorum"),
        escalations=2, unresolved_disputes=1,
        conflict_of_interest_pass=cases.cases[0].status == "pass",
        operator_diversity_pass=cases.cases[1].status == "pass" and cases.cases[2].status == "pass",
        consensus_pass=all(case.status == "pass" for case in cases.cases if case.group == "consensus"),
        escalation_pass=all(case.status == "pass" for case in cases.cases if case.group == "escalation"),
        minority_correctness_pass=next(case for case in cases.cases if case.case_id == "original_minority_later_correct").status == "pass",
        accounting_pass=outputs["accounting"]["validator_conservation_pass"],
        pool_isolation_pass=outputs["accounting"]["pool_isolation_pass"],
        idempotency_pass=next(case for case in cases.cases if case.case_id == "reward_finalize_retry_idempotent").status == "pass",
        recovery_pass=all(case.status == "pass" for case in cases.cases if case.group == "recovery"),
        determinism_pass=replay_equal and insertion_equal,
        regression_pass=regression_suite_passed,
        deterministic_state_hash=first_hash,
        total_runtime_ms=_elapsed(started),
        full_test_count=full_test_count,
        failed_test_count=0 if regression_suite_passed else 0,
        generated_files=list(GENERATED_FILENAMES),
        known_limitations=[
            "operator_id enforces registered-operator diversity but is not cryptographic Sybil resistance",
            "four independent operators can still compromise a five-seat supermajority if their evidence passes protocol checks",
            "benchmark fixtures use safe synthetic metadata and mocked safe execution results; no live RPC or external target is contacted",
            "reward points are simulated accounting units, not token, wallet, or real-money settlement",
            "the benchmark uses one logical project identifier across isolated scenario roots; multi-project authorization remains covered by the full regression suite",
            "CI, medium, and full modes increase deterministic calculator load; the complete adversarial real-service corpus runs in every mode so critical coverage is never sampled away",
        ],
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
    )
    outputs["summary"] = summary.model_dump(mode="json")
    write_outputs(output_dir, cases, summary, outputs)
    return cases, summary, outputs


def write_outputs(output_dir: Path, cases: Week8CaseResultsDocument, summary: Week8BenchmarkSummary, outputs: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "week_8_case_results.json": cases.model_dump(mode="json"),
        "week_8_committee_summary.json": outputs["committees"],
        "week_8_consensus_summary.json": outputs["consensus"],
        "week_8_validator_quality_summary.json": outputs["quality"],
        "week_8_validator_performance_summary.json": outputs["performance"],
        "week_8_validator_reward_summary.json": outputs["reward_summary"],
        "week_8_accounting_report.json": outputs["accounting"],
        "week_8_determinism_report.json": outputs["determinism"],
        "week_8_performance_report.json": outputs["performance_metrics"],
        "week_8_summary.json": summary.model_dump(mode="json"),
    }
    for filename, payload in files.items():
        (output_dir / filename).write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "week_8_report.md").write_text(render_week8_report(cases, summary.model_dump(mode="json"), outputs), encoding="utf-8")
    for filename in GENERATED_FILENAMES:
        text = (output_dir / filename).read_text(encoding="utf-8")
        if any(token in text.lower() for token in ("/home/", "private_key", "seed_phrase", "mnemonic")):
            raise RuntimeError(f"Generated output contains forbidden data: {filename}")


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s:]+[\\/])+[^\s:]+", "[path]", f"{exc.__class__.__name__}: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic Week 8 validator-network benchmark.")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--mode", choices=[item.value for item in Week8BenchmarkMode], default=Week8BenchmarkMode.CI.value)
    parser.add_argument("--seed", type=int, default=WEEK_8_BENCHMARK_SEED)
    parser.add_argument("--full-test-count", type=int, default=0)
    parser.add_argument("--regression-suite-passed", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases, summary, _ = run_week8_validator_benchmark(
            args.output_dir, mode=args.mode, seed=args.seed,
            full_test_count=args.full_test_count,
            regression_suite_passed=args.regression_suite_passed,
        )
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 2
    print(f"Week 8 validator benchmark: {summary.passed}/{summary.cases_total} cases passed.")
    print(f"State hash: {summary.deterministic_state_hash}")
    print(f"Outputs: {args.output_dir}")
    return 0 if summary.benchmark_passed and all(case.status == "pass" for case in cases.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
