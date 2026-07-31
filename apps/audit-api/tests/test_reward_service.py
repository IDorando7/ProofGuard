import json

import pytest

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate
from app.schemas.reproduction import ReproductionStatus
from app.schemas.reward import RewardCycleCreate, RewardCycleStatus, RewardEligibilityReasonCode
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.contribution_scoring_service import calculate_contribution_for_submission, save_contribution_score
from app.services.node_registry_service import create_node, load_node, save_node
from app.services.reproduction_service import create_initial_reproduction_result, save_reproduction_result
from app.services.reward_service import (
    RewardCycleAlreadyFinalizedError,
    RewardCycleSourceChangedError,
    calculate_category_multiplier,
    calculate_reputation_multiplier,
    calculate_reward_cycle,
    calculate_reward_weight,
    create_reward_cycle,
    determine_reward_eligibility,
    finalize_reward_cycle,
    get_node_reward_summary,
    list_reward_events,
    load_reward_event_by_submission,
)
from app.services.submission_service import create_submission, load_submission, update_submission_status
from app.services.validation_service import create_validation_decision


def prepare_case(tmp_path, *, project_id="project-1", finding_id="finding-1", validation_status="accepted", reproduction_status="reproduced"):
    root = tmp_path / "protocol"
    workspace = tmp_path / "audits" / project_id
    finding = Finding(
        finding_id=finding_id,
        project_id=project_id,
        title="Missing access control on treasury setter",
        category="access_control",
        severity="High",
        confidence=0.8,
        contracts=["src/Treasury.sol"],
        functions=["setTreasury"],
        root_cause="Missing onlyOwner authorization check",
        attack_path="Attacker calls setTreasury directly",
        impact="Unauthorized treasury modification",
        recommended_fix="Add onlyOwner authorization.",
        agent_name="access_control_agent",
    )
    findings_dir = workspace / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    findings_path = findings_dir / "findings.json"
    existing_findings = json.loads(findings_path.read_text(encoding="utf-8")) if findings_path.exists() else []
    findings_path.write_text(json.dumps(existing_findings + [finding.model_dump(mode="json")], indent=2), encoding="utf-8")
    node = create_node(root, NodeCreate(node_type="agent", display_name=f"agent-{finding_id}", operator_id=f"operator-{finding_id}", supported_categories=["access_control"]))
    submission = create_submission(root, workspace, SubmissionCreate(project_id=project_id, finding_id=finding_id, node_id=node.node_id))
    reproduction = create_initial_reproduction_result(project_id, finding_id, workspace)
    reproduction = save_reproduction_result(reproduction.model_copy(update={"status": ReproductionStatus(reproduction_status)}), workspace)
    validation = create_validation_decision(
        project_id,
        finding_id,
        workspace,
        status=ValidationStatus(validation_status),
        reason="Deterministic validation.",
        evidence=ValidationEvidence(has_finding=True, has_reproduction=True, reproduction_status=reproduction_status, in_scope=validation_status != "out_of_scope", is_duplicate=validation_status == "duplicate", normalized_severity="High"),
    )
    update_submission_status(root, submission.submission_id, SubmissionStatusUpdate(status="validation_pending", reason="Pending.", reproduction_id=reproduction.reproduction_id))
    status = "unsafe" if validation_status == "unsafe_poc" else validation_status
    submission = update_submission_status(root, submission.submission_id, SubmissionStatusUpdate(status=status, reason="Finalized.", validation_id=validation.validation_id))
    contribution = calculate_contribution_for_submission(root, workspace, submission.submission_id)
    return root, workspace, node, submission, validation, reproduction, contribution


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0, .5), (.29, .5), (.30, .8), (.49, .8), (.50, 1), (.69, 1), (.70, 1.15), (.89, 1.15), (.90, 1.25), (1, 1.25)],
)
def test_reputation_multiplier_boundaries(score, expected):
    assert calculate_reputation_multiplier(score) == expected


@pytest.mark.parametrize("score", [-.01, 1.01])
def test_reputation_multiplier_rejects_corrupt_scores(score):
    with pytest.raises(ValueError):
        calculate_reputation_multiplier(score)


def test_weight_formula_and_category_multiplier():
    weight = calculate_reward_weight(90, .8, "access_control")
    assert weight.raw_weight == 103.5
    assert weight.reputation_multiplier == 1.15
    assert calculate_category_multiplier("reentrancy") == 1
    with pytest.raises(ValueError):
        calculate_category_multiplier("not-a-category")


def test_eligibility_and_hard_denials(tmp_path):
    root, workspace, _, submission, validation, reproduction, contribution = prepare_case(tmp_path)
    result = determine_reward_eligibility(submission, contribution, validation, reproduction, None)
    assert result.eligible is True
    assert result.reason_code == RewardEligibilityReasonCode.ELIGIBLE
    assert determine_reward_eligibility(submission, None, validation, reproduction, None).reason_code == RewardEligibilityReasonCode.CONTRIBUTION_MISSING

    _, _, _, unsafe_submission, unsafe_validation, unsafe_reproduction, unsafe_contribution = prepare_case(
        tmp_path / "unsafe", validation_status="unsafe_poc", reproduction_status="rejected_unsafe"
    )
    unsafe = determine_reward_eligibility(unsafe_submission, unsafe_contribution, unsafe_validation, unsafe_reproduction, None)
    assert unsafe.reason_code == RewardEligibilityReasonCode.UNSAFE
    assert not unsafe.eligible


def test_cycle_calculation_distribution_finalization_and_idempotency(tmp_path):
    root, workspace, node_a, submission_a, *_ = prepare_case(tmp_path, finding_id="finding-a")
    _, _, node_b, submission_b, *_ = prepare_case(tmp_path, finding_id="finding-b")
    node_a = save_node(root, load_node(root, node_a.node_id).model_copy(update={"reputation_score": .8}))
    node_b = save_node(root, load_node(root, node_b.node_id).model_copy(update={"reputation_score": .55}))

    draft = create_reward_cycle(root, workspace, RewardCycleCreate(project_id="project-1", reward_pool=1000))
    assert draft.status == RewardCycleStatus.DRAFT
    assert draft.submission_ids == sorted([submission_a.submission_id, submission_b.submission_id])
    calculated = calculate_reward_cycle(root, workspace, draft.cycle_id)
    assert calculated.status == RewardCycleStatus.CALCULATED
    assert calculated.total_allocated == 1000
    assert sum(item.reward_amount for item in calculated.allocations) == 1000
    again = calculate_reward_cycle(root, workspace, draft.cycle_id)
    assert again.allocations == calculated.allocations
    assert again.source_fingerprint == calculated.source_fingerprint

    reputation_before = {node_a.node_id: load_node(root, node_a.node_id).reputation_score, node_b.node_id: load_node(root, node_b.node_id).reputation_score}
    finalized = finalize_reward_cycle(root, workspace, draft.cycle_id)
    assert finalized.status == RewardCycleStatus.FINALIZED
    assert len(list_reward_events(root, project_id="project-1")) == 2
    assert load_submission(root, submission_a.submission_id).status.value == "rewarded"
    assert finalize_reward_cycle(root, workspace, draft.cycle_id) == finalized
    assert len(list_reward_events(root)) == 2
    assert {node_id: load_node(root, node_id).reputation_score for node_id in reputation_before} == reputation_before
    with pytest.raises(RewardCycleAlreadyFinalizedError):
        calculate_reward_cycle(root, workspace, draft.cycle_id)
    summary = get_node_reward_summary(root, node_a.node_id)
    assert summary.total_reward_events == 1
    assert summary.total_protocol_points > 0


def test_ineligible_submission_gets_zero_and_source_change_blocks_finalization(tmp_path):
    root, workspace, node, submission, _, _, contribution = prepare_case(tmp_path)
    cycle = create_reward_cycle(root, workspace, RewardCycleCreate(project_id="project-1"))
    calculated = calculate_reward_cycle(root, workspace, cycle.cycle_id)
    changed = contribution.model_copy(update={"total_score": contribution.total_score - 1})
    save_contribution_score(root, changed)
    with pytest.raises(RewardCycleSourceChangedError):
        finalize_reward_cycle(root, workspace, calculated.cycle_id)
    assert load_reward_event_by_submission(root, submission.submission_id) is None
    assert load_node(root, node.node_id).reputation_score == .5
