from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.node_registry_service import list_nodes
from app.services.subnet_registry_service import list_subnet_members, list_subnets
from research.benchmarks.week6_subnet_benchmark import (
    FIXED_NOW,
    NODE_CATEGORIES,
    RELEVANT_CATEGORIES,
    _assert,
    execute_benchmark_once,
    run_deterministic_replay_check,
    temporary_fixture_roots,
)
from research.schemas.week6_subnet_benchmark import (
    BENCHMARK_VERSION,
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    BenchmarkAssertionResult,
    SubnetBenchmarkCaseResult,
    Week6BenchmarkSummary,
    Week6CaseResultsDocument,
)


@pytest.fixture(scope="module")
def execution(tmp_path_factory):
    root = tmp_path_factory.mktemp("week6-execution") / "fixture"
    with temporary_fixture_roots(root) as roots:
        result = execute_benchmark_once(roots)
        yield result


def test_valid_assertion_result_and_empty_id_validation():
    assertion = BenchmarkAssertionResult(
        assertion_id="bounded_score",
        description="Score is bounded.",
        passed=True,
        expected=True,
        actual=True,
    )
    assert assertion.passed
    with pytest.raises(ValidationError):
        BenchmarkAssertionResult(
            assertion_id="",
            description="Invalid.",
            passed=False,
        )


def test_valid_case_and_failed_assertion_cannot_claim_pass(execution):
    case = execution.cases.cases[0]
    assert SubnetBenchmarkCaseResult.model_validate(case.model_dump()).passed
    failed = case.assertions[0].model_copy(update={"passed": False})
    with pytest.raises(ValidationError):
        SubnetBenchmarkCaseResult.model_validate(
            {
                **case.model_dump(),
                "assertions": [failed, *case.assertions[1:]],
                "passed": True,
            }
        )


def test_case_document_requires_unique_complete_deterministic_case_set(execution):
    assert tuple(case.case_id for case in execution.cases.cases) == REQUIRED_CASE_IDS
    with pytest.raises(ValidationError):
        Week6CaseResultsDocument(
            cases=[execution.cases.cases[0]] * len(REQUIRED_CASE_IDS)
        )


def test_summary_constraints_and_decimal_strings():
    payload = {
        "benchmark_version": BENCHMARK_VERSION,
        "protocol_version": "protocol_v0",
        "week": 6,
        "benchmark_passed": True,
        "total_cases": 7,
        "passed_cases": 7,
        "failed_cases": 0,
        "total_assertions": 7,
        "passed_assertions": 7,
        "failed_assertions": 0,
        "pass_rate": 1.0,
        "generated_files": list(GENERATED_FILENAMES),
        "category_count": 2,
        "node_count": 9,
        "project_count": 3,
        "routing_count": 2,
        "routing_assignment_count": 12,
        "reward_cycle_count": 1,
        "reward_event_count": 4,
        "total_simulated_pool_points": "10000.000000",
        "total_distributed_points": "10000.000000",
        "total_undistributed_points": "0.000000",
        "deterministic_replay_passed": True,
        "failed_case_ids": [],
        "categories": list(RELEVANT_CATEGORIES),
        "membership_distribution": {},
        "routing_distribution": {},
        "reward_distribution": {},
        "idempotency_checks": {},
        "known_limitations": ["Synthetic."],
        "started_at": FIXED_NOW,
        "completed_at": FIXED_NOW,
    }
    summary = Week6BenchmarkSummary.model_validate(payload)
    assert 0 <= summary.pass_rate <= 1
    assert isinstance(summary.total_simulated_pool_points, str)
    with pytest.raises(ValidationError):
        Week6BenchmarkSummary.model_validate(
            {**payload, "total_simulated_pool_points": 10000}
        )


def test_benchmark_schemas_have_no_secret_wallet_or_poc_fields():
    schema_text = str(
        {
            **BenchmarkAssertionResult.model_fields,
            **SubnetBenchmarkCaseResult.model_fields,
            **Week6BenchmarkSummary.model_fields,
        }
    ).lower()
    for forbidden in ("wallet", "private_key", "poc_content", "poc_code"):
        assert forbidden not in schema_text


def test_synthetic_roots_nodes_subnets_and_projects_are_isolated(execution):
    roots = execution.state.roots
    assert roots.protocol_data_root.is_relative_to(roots.root)
    assert roots.project_workspace_root.is_relative_to(roots.root)
    assert roots.protocol_data_root != Path("data/protocol").resolve()
    assert {node.node_id for node in list_nodes(roots.protocol_data_root)} == set(
        NODE_CATEGORIES
    )
    assert {item.category.value for item in list_subnets(roots.protocol_data_root)}.issuperset(
        RELEVANT_CATEGORIES
    )
    for workspace in execution.state.projects.values():
        assert workspace.is_relative_to(roots.project_workspace_root)
        assert (workspace / "scope" / "scope.yaml").is_file()
        assert (workspace / "scope" / "parsed_scope.json").is_file()


def test_synthetic_history_outcomes_and_references_are_consistent(execution):
    histories = execution.state.histories
    assert any(item.outcome == "accepted_contribution" for item in histories)
    assert any(item.outcome == "duplicate_finding" for item in histories)
    assert any(item.outcome == "unsafe_submission" for item in histories)
    assert all(item.category in RELEVANT_CATEGORIES for item in histories)
    assert len({item.submission_id for item in histories}) == len(histories)
    assert len({item.reputation_event_id for item in histories}) == len(histories)
    assert FIXED_NOW.tzinfo is timezone.utc


def test_fixture_cleanup_removes_only_fixture_root(tmp_path):
    root = tmp_path / "disposable"
    with temporary_fixture_roots(root) as roots:
        marker = roots.protocol_data_root / "marker"
        marker.write_text("synthetic", encoding="utf-8")
        assert marker.exists()
    assert not root.exists()


@pytest.mark.parametrize("case_id", REQUIRED_CASE_IDS)
def test_each_required_benchmark_case_passes(execution, case_id):
    case = next(item for item in execution.cases.cases if item.case_id == case_id)
    assert case.passed
    assert case.assertions
    assert all(assertion.passed for assertion in case.assertions)


def test_category_scores_memberships_routing_and_rewards_are_real_outputs(execution):
    state = execution.state
    assert state.scores[("node_access_expert", "access_control")].category_score >= 0.8
    assert state.scores[("node_multi_category", "access_control")].category_score > (
        state.scores[("node_multi_category", "reentrancy")].category_score
    )
    assert state.members[("node_unsafe", "access_control")].status.value == "suspended"
    assert state.multi_routing.status.value == "finalized"
    assert state.reward_cycle.status.value == "finalized"
    assert state.reward_cycle.total_distributed_points + state.reward_cycle.total_undistributed_points == (
        state.reward_cycle.total_pool_points
    )


def test_all_idempotency_checks_pass_and_rank_is_never_persisted(execution):
    assert execution.state.idempotency
    assert all(execution.state.idempotency.values())
    for category in RELEVANT_CATEGORIES:
        members = list_subnet_members(
            execution.state.roots.protocol_data_root, f"subnet_{category}"
        )
        assert all(member.rank is None for member in members)


def test_normalized_logical_replay_comparison_and_negative_diff(execution):
    passed, differences = run_deterministic_replay_check(
        execution.normalized, execution.normalized
    )
    assert passed and differences == []
    changed = {**execution.normalized, "idempotency": {"changed": False}}
    passed, differences = run_deterministic_replay_check(
        execution.normalized, changed
    )
    assert not passed
    assert differences


def test_negative_controls_fail_without_changing_production_logic(execution):
    expert_weakened = _assert(
        "expert_weakened",
        "A deliberately weakened expert should fail the threshold check.",
        ">=0.8",
        0.3,
        False,
    )
    suspended_selected = _assert(
        "suspended_selected",
        "A deliberately selected suspended member is invalid.",
        False,
        True,
        False,
    )
    category_mixed = _assert(
        "category_mixed",
        "A deliberately mixed category source is invalid.",
        "access_control",
        "reentrancy",
        False,
    )
    conservation_changed = _assert(
        "conservation_changed",
        "A deliberately changed reward total is invalid.",
        "10000.000000",
        "10001.000000",
        False,
    )
    assert not any(
        item.passed
        for item in (
            expert_weakened,
            suspended_selected,
            category_mixed,
            conservation_changed,
        )
    )
