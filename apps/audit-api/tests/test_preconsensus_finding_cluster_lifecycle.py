from decimal import Decimal

import pytest

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionStatus
from app.schemas.submission import SubmissionRewardStatus, SubmissionStatus
from app.schemas.task_finding_reward import TaskFindingRewardCalculationRequest
from app.schemas.task_reward import TaskRewardBudgetCreateRequest
from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
)
from app.services.finding_cluster_service import (
    finalize_finding_clusters_for_task,
    load_finding_cluster,
    rebuild_finding_clusters_for_task,
)
from app.services.node_registry_service import load_node
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    save_reproduction_result,
)
from app.services.report_quality_assessment_service import (
    rebuild_task_report_quality_assessments,
)
from app.services.submission_service import save_submission
from app.services.task_finding_reward_service import calculate_task_finding_rewards
from app.services.task_reward_budget_service import (
    create_task_reward_budget,
    finalize_task_reward_budget,
)
from app.services.validation_attestation_service import create_validation_attestation
from app.services.validator_committee_service import (
    finalize_validator_committee,
    plan_validator_committee,
)
from app.services.validator_consensus_service import (
    calculate_validation_consensus,
    finalize_validation_consensus,
)
from app.services.validator_pipeline import validate_all_findings
from tests import test_subnet_reward_allocation_service as legacy_fixture
from tests.test_validator_committee_service import NOW, _validator
from tests.test_validator_reproduction_execution_service import _execute, _fake_executor


def _same_root_finding(project_id, finding_id, category="access_control"):
    return Finding(
        finding_id=finding_id,
        project_id=project_id,
        title=f"Independent candidate report {finding_id}",
        category=category,
        severity="High",
        confidence=0.8,
        contracts=["src/Vault.sol"],
        functions=["withdraw"],
        root_cause="Missing authorization on the withdrawal path.",
        attack_path="An unprivileged caller invokes the same withdrawal path.",
        impact="Unauthorized value movement from the synthetic vault.",
        recommended_fix="Add authorization before changing value ownership.",
        agent_name="access_control_agent",
    )


def _pending_context(tmp_path, monkeypatch, *, validator_count=6):
    monkeypatch.setattr(legacy_fixture, "_finding", _same_root_finding)
    root, workspace, _, routing, submission_rows = legacy_fixture._setup(
        tmp_path, include_candidate=True
    )
    submissions = []
    for submission, _contribution in submission_rows:
        reproduction_path = (
            workspace
            / "reproductions"
            / submission.finding_id
            / "reproduction.json"
        )
        reproduction_path.unlink()
        submissions.append(
            save_submission(
                root,
                submission.model_copy(
                    update={
                        "status": SubmissionStatus.SUBMITTED,
                        "reproduction_id": None,
                        "validation_id": None,
                        "reward_status": SubmissionRewardStatus.PENDING,
                        "status_reason": "Candidate report awaiting network validation.",
                    }
                ),
            )
        )

    decisions = validate_all_findings("project-1", workspace)
    assert {decision.status for decision in decisions} == {
        ValidationStatus.NEEDS_REVIEW
    }
    assert all(
        decision.reason == "Finding has no reproduction result yet."
        for decision in decisions
    )

    first = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    repeated = rebuild_finding_clusters_for_task(
        root, workspace, "project-1", routing.routing_id
    )
    assert first.total_clusters == 1
    assert repeated.unchanged_clusters == 1
    assert repeated.clusters == first.clusters
    cluster = first.clusters[0]
    assert [member.submission_id for member in cluster.members] == [
        submission.submission_id
        for submission in sorted(
            submissions, key=lambda item: (item.submitted_at, item.submission_id)
        )
    ]
    assert cluster.report_count == cluster.distinct_operator_count == 2
    assert cluster.validation_authority == "pending_validator_consensus"
    assert cluster.final_validation_status is None
    assert cluster.final_severity is None
    assert cluster.claimed_severity.value == "High"

    cluster = finalize_finding_clusters_for_task(
        root, "project-1", routing.routing_id
    )[0]
    assert cluster.status == "finalized"
    assert cluster.final_validation_status is None

    validators = [_validator(root, index) for index in range(validator_count)]
    committee = plan_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
        calculated_at=NOW,
    )
    reporter_operators = {
        load_node(root, member.node_id).operator_id for member in cluster.members
    }
    assert reporter_operators.isdisjoint(
        {seat.operator_id for seat in committee.authoritative_seats}
    )
    committee = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    ).committee
    return root, workspace, routing, cluster, committee, decisions, validators


def _install_generated_poc(workspace, cluster):
    (workspace / "repo" / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "repo" / "test").mkdir(parents=True, exist_ok=True)
    (workspace / "repo" / "foundry.toml").write_text(
        "[profile.default]\nffi = false\nfs_permissions = []\n",
        encoding="utf-8",
    )
    (workspace / "repo" / "src" / "Vault.sol").write_text(
        "contract Vault {}\n", encoding="utf-8"
    )
    (workspace / "repo" / "test" / "CandidateClaim.t.sol").write_text(
        "contract CandidateClaimTest { function testCandidateClaim() public {} }\n",
        encoding="utf-8",
    )
    result = create_initial_reproduction_result(
        "project-1", cluster.canonical_finding_id, workspace
    )
    save_reproduction_result(
        result.model_copy(
            update={
                "status": ReproductionStatus.GENERATED,
                "poc_file": "test/CandidateClaim.t.sol",
                "test_name": "testCandidateClaim",
            }
        ),
        workspace,
    )


def _vote(root, workspace, routing, committee, monkeypatch, decisions):
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat, validity in zip(
        committee.authoritative_seats, decisions, strict=False
    ):
        current["value"] = seat.validator_node_id
        reproduction = _execute(root, workspace, routing, seat)
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
                root_cause_decision=(
                    RootCauseDecision.CONFIRMED
                    if validity == ValidationStatus.ACCEPTED
                    else RootCauseDecision.MISMATCH
                ),
                normalized_severity=(
                    "High" if validity == ValidationStatus.ACCEPTED else None
                ),
                impact_decision=(
                    ImpactDecision.VALIDATED
                    if validity == ValidationStatus.ACCEPTED
                    else ImpactDecision.REJECTED
                ),
            ),
        )


def _miner_calculation(root, workspace, routing):
    budget, _ = create_task_reward_budget(
        root,
        workspace,
        "project-1",
        TaskRewardBudgetCreateRequest(
            routing_id=routing.routing_id,
            total_budget_points="10000.000000",
        ),
    )
    budget, _ = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    calculation, _ = calculate_task_finding_rewards(
        root,
        "project-1",
        routing.routing_id,
        TaskFindingRewardCalculationRequest(
            task_reward_budget_id=budget.task_reward_budget_id,
            allocation_scope="global",
        ),
    )
    return budget, calculation


def test_needs_review_reports_build_frozen_cluster_and_conflict_free_committee(
    tmp_path, monkeypatch
):
    root, workspace, routing, cluster, committee, decisions, _ = _pending_context(
        tmp_path, monkeypatch
    )
    assert all(decision.status == ValidationStatus.NEEDS_REVIEW for decision in decisions)
    assert len(committee.authoritative_seats) == 5
    assert load_finding_cluster(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    ) == cluster
    assert not (workspace / "reproductions" / cluster.canonical_finding_id / "reproduction.json").exists()


def test_frozen_preconsensus_cluster_has_zero_miner_reward_eligibility(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, _, _, _ = _pending_context(tmp_path, monkeypatch)
    quality = rebuild_task_report_quality_assessments(
        root, workspace, "project-1", routing.routing_id
    )
    assert quality.total_assessments == 0
    budget, calculation = _miner_calculation(root, workspace, routing)
    assert calculation.outcome == "no_eligible_findings"
    assert calculation.cluster_allocations == []
    assert calculation.distributed_cluster_points == Decimal("0.000000")
    assert calculation.undistributed_cluster_points == budget.miner_pool_points


def test_confirmed_consensus_is_linked_and_enables_existing_miner_valuation(
    tmp_path, monkeypatch
):
    root, workspace, routing, cluster, committee, _, _ = _pending_context(
        tmp_path, monkeypatch
    )
    _install_generated_poc(workspace, cluster)
    _vote(
        root,
        workspace,
        routing,
        committee,
        monkeypatch,
        [ValidationStatus.ACCEPTED] * 4 + [ValidationStatus.REJECTED],
    )
    consensus = calculate_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalized = finalize_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    resolved = load_finding_cluster(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    )
    assert finalized.consensus_outcome == "confirmed"
    assert resolved.final_validation_status == ValidationStatus.ACCEPTED
    assert resolved.final_validation_consensus_id == finalized.validation_consensus_id
    assert resolved.validation_resolution_source_fingerprint == finalized.source_fingerprint
    assert resolved.final_severity.value == "High"
    _, calculation = _miner_calculation(root, workspace, routing)
    assert [item.finding_cluster_id for item in calculation.cluster_allocations] == [
        cluster.finding_cluster_id
    ]


def test_rejected_consensus_remains_auditable_but_miner_ineligible(
    tmp_path, monkeypatch
):
    root, workspace, routing, cluster, committee, _, _ = _pending_context(
        tmp_path, monkeypatch
    )
    _install_generated_poc(workspace, cluster)
    _vote(
        root,
        workspace,
        routing,
        committee,
        monkeypatch,
        [ValidationStatus.REJECTED] * 4 + [ValidationStatus.ACCEPTED],
    )
    consensus = calculate_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalized = finalize_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    resolved = load_finding_cluster(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    )
    assert finalized.consensus_outcome == "rejected"
    assert resolved.final_validation_status == ValidationStatus.REJECTED
    assert resolved.final_validation_consensus_id == finalized.validation_consensus_id
    assert resolved.members == cluster.members
    _, calculation = _miner_calculation(root, workspace, routing)
    assert calculation.cluster_allocations == []


@pytest.mark.parametrize(
    ("votes", "expected"),
    [
        (
            [ValidationStatus.ACCEPTED] * 3 + [ValidationStatus.REJECTED] * 2,
            "disputed",
        ),
        ([ValidationStatus.ACCEPTED] * 3, "no_quorum"),
    ],
)
def test_disputed_and_no_quorum_stay_unresolved_and_miner_ineligible(
    tmp_path, monkeypatch, votes, expected
):
    root, workspace, routing, cluster, committee, _, _ = _pending_context(
        tmp_path, monkeypatch
    )
    _install_generated_poc(workspace, cluster)
    _vote(root, workspace, routing, committee, monkeypatch, votes)
    consensus = calculate_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        finding_cluster_id=cluster.finding_cluster_id,
    )
    finalized = finalize_validation_consensus(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validation_consensus_id=consensus.validation_consensus_id,
    )
    unresolved = load_finding_cluster(
        root, "project-1", routing.routing_id, cluster.finding_cluster_id
    )
    assert finalized.consensus_outcome == expected
    assert unresolved.validation_authority == "pending_validator_consensus"
    assert unresolved.final_validation_status is None
    assert unresolved.final_validation_consensus_id is None
    _, calculation = _miner_calculation(root, workspace, routing)
    assert calculation.cluster_allocations == []
