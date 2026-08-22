import json
from decimal import Decimal

from research.benchmarks.week8_validator_benchmark import (
    SCALE_PROFILES,
    run_week8_validator_benchmark,
)
from research.reports.week8_validator_report import REQUIRED_REPORT_SECTIONS
from research.schemas.week8_validator_benchmark import (
    GENERATED_FILENAMES,
    REQUIRED_CASE_IDS,
    WEEK_8_BENCHMARK_SEED,
    Week8BenchmarkMode,
)


def test_week8_real_service_benchmark_generates_passing_auditable_outputs(tmp_path):
    cases, summary, outputs = run_week8_validator_benchmark(tmp_path / "week8")

    assert tuple(case.case_id for case in cases.cases) == REQUIRED_CASE_IDS
    assert len(cases.cases) >= 50
    assert all(case.status == "pass" for case in cases.cases)
    assert summary.benchmark_passed
    assert summary.seed == WEEK_8_BENCHMARK_SEED == 8008
    assert summary.mode == Week8BenchmarkMode.CI
    assert summary.determinism_pass
    assert summary.conflict_of_interest_pass
    assert summary.operator_diversity_pass
    assert summary.consensus_pass
    assert summary.escalation_pass
    assert summary.minority_correctness_pass
    assert summary.accounting_pass
    assert summary.pool_isolation_pass
    assert summary.idempotency_pass
    assert summary.recovery_pass

    accounting = outputs["accounting"]
    assert Decimal(accounting["validator_distributed"]) + Decimal(
        accounting["validator_undistributed"]
    ) == Decimal(accounting["validator_pool"])
    assert Decimal(accounting["reward_event_total"]) == Decimal(
        accounting["validator_distributed"]
    )
    assert accounting["pool_isolation_pass"]
    assert accounting["double_spend_pass"]

    consensus = {item["case"]: item for item in outputs["consensus"]}
    assert consensus["three_two"]["outcome"] == "disputed"
    assert consensus["no_quorum"]["outcome"] == "no_quorum"
    assert consensus["original_minority_later_correct"]["outcome"] == "rejected"
    assert consensus["original_minority_later_correct"]["target_n"] == 9
    assert consensus["high_assurance_escalation"]["target_n"] == 11
    assert consensus["terminal_out_of_scope"]["outcome"] == "out_of_scope"
    assert consensus["terminal_insufficient"]["outcome"] == "insufficient_evidence"
    assert consensus["terminal_unsafe"]["outcome"] == "unsafe"
    assert consensus["terminal_unsupported"]["outcome"] == "unsupported"

    assert len(outputs["reproductions"]) == 6
    assert len({item["record_id"] for item in outputs["reproductions"]}) == 6
    assert any(item["role"] == "shadow" for item in outputs["quality"])
    assert all(item["assignment_id"] not in {
        allocation["assignment_id"] for allocation in outputs["rewards"]["allocations"]
    } for item in outputs["quality"] if item["role"] == "shadow")

    for filename in GENERATED_FILENAMES:
        path = tmp_path / "week8" / filename
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text
        assert "private_key" not in text.lower()

    report = (tmp_path / "week8" / "week_8_report.md").read_text(encoding="utf-8")
    assert all(section in report for section in REQUIRED_REPORT_SECTIONS)
    assert summary.deterministic_state_hash in report
    assert "3 ACCEPT / 2 REJECT" in report
    assert "3 ACCEPT / 6 REJECT" in report
    assert "ValidatorCategoryScore" in report


def test_week8_outputs_are_canonical_and_replay_hashes_match(tmp_path):
    _, summary, outputs = run_week8_validator_benchmark(tmp_path / "week8")
    determinism = json.loads(
        (tmp_path / "week8" / "week_8_determinism_report.json").read_text()
    )
    assert determinism == outputs["determinism"]
    assert determinism["identical"]
    assert determinism["insertion_order_independent"]
    assert determinism["first_state_hash"] == determinism["replay_state_hash"]
    assert determinism["first_state_hash"] == summary.deterministic_state_hash

    cases = json.loads(
        (tmp_path / "week8" / "week_8_case_results.json").read_text()
    )["cases"]
    assert [item["case_id"] for item in cases] == list(REQUIRED_CASE_IDS)
    assert all(item["assertions"] and item["invariant_ids"] for item in cases)


def test_week8_scale_modes_are_explicit_and_full_meets_requested_shape():
    assert SCALE_PROFILES[Week8BenchmarkMode.CI] == {
        "projects": 1,
        "routings": 1,
        "clusters": 8,
        "reporting_operators": 20,
        "validator_operators": 20,
        "validator_nodes": 30,
    }
    full = SCALE_PROFILES[Week8BenchmarkMode.FULL]
    assert full["projects"] == 2
    assert full["routings"] == 4
    assert full["clusters"] == 40
    assert full["validator_operators"] == 100
    assert full["validator_nodes"] == 150
