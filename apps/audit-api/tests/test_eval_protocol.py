import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest


APP_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = APP_ROOT / "research" / "evals"
DATASET_ROOT = APP_ROOT / "research" / "datasets" / "protocol"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

import eval_protocol  # noqa: E402


def _snapshot(root: Path):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _logical(case_document, leaderboard):
    cases = copy.deepcopy(case_document["cases"])
    comparison = copy.deepcopy(leaderboard["reward_comparison"])
    comparison.pop("high_submission_id", None)
    comparison.pop("medium_submission_id", None)
    logical_leaderboard = copy.deepcopy(leaderboard)
    logical_leaderboard["reward_comparison"] = comparison
    return cases, logical_leaderboard


def test_manifest_loader_and_validator_work():
    manifest = eval_protocol.load_protocol_manifest()
    assert manifest["dataset"] == "week_5_protocol_synthetic"
    assert len(manifest["cases"]) == 7
    with pytest.raises(ValueError, match="dataset"):
        eval_protocol.validate_protocol_manifest({"dataset": "wrong", "cases": []})


def test_benchmark_writes_all_outputs_without_mutating_source(tmp_path):
    before = _snapshot(DATASET_ROOT)
    cases, leaderboard = eval_protocol.run_protocol_benchmark(results_dir=tmp_path)
    after = _snapshot(DATASET_ROOT)
    assert before == after
    assert len(cases["cases"]) == 7
    assert leaderboard["success"] is True
    for filename in (
        "protocol_case_results.json",
        "protocol_leaderboard.json",
        "week_5_report.md",
        "week_5_summary.json",
    ):
        assert (tmp_path / filename).is_file()


def test_failed_case_is_recorded_and_does_not_stop_remaining_cases(tmp_path):
    dataset_copy = tmp_path / "dataset"
    shutil.copytree(DATASET_ROOT, dataset_copy)
    expected_path = dataset_copy / "cases" / "accepted_high_value_001" / "expected_protocol.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    expected["expected"]["expected_contribution_score"] = 1.0
    expected_path.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    output = tmp_path / "results"
    cases, leaderboard = eval_protocol.run_protocol_benchmark(dataset_copy / "manifest.json", output)
    assert len(cases["cases"]) == 7
    assert cases["cases"][0]["passed"] is False
    assert all(result["passed"] for result in cases["cases"][1:])
    assert leaderboard["failed_cases"] == 1
    assert leaderboard["success"] is False


def test_output_order_and_logical_results_are_deterministic(tmp_path):
    first_cases, first_leaderboard = eval_protocol.run_protocol_benchmark(results_dir=tmp_path / "first")
    second_cases, second_leaderboard = eval_protocol.run_protocol_benchmark(results_dir=tmp_path / "second")
    manifest = eval_protocol.load_protocol_manifest()
    assert [item["case_id"] for item in first_cases["cases"]] == [item["case_id"] for item in manifest["cases"]]
    assert _logical(first_cases, first_leaderboard) == _logical(second_cases, second_leaderboard)


def test_results_expose_no_temporary_paths_or_secrets(tmp_path):
    eval_protocol.run_protocol_benchmark(results_dir=tmp_path)
    output = "\n".join(path.read_text(encoding="utf-8") for path in sorted(tmp_path.iterdir())).lower()
    assert "/tmp/" not in output
    assert "/home/" not in output
    for forbidden in ("private_key", "seed_phrase", "wallet_address", "token_address"):
        assert forbidden not in output


def test_evaluator_has_no_execution_or_subprocess_dependency():
    source = Path(eval_protocol.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source
    assert "sandbox_runner" not in source
    assert "foundry_service" not in source
    assert "poc_service" not in source
    assert "validator_pipeline" not in source


def test_main_returns_success_and_failure_status(tmp_path):
    assert eval_protocol.main(["--results-dir", str(tmp_path / "success")]) == 0
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"dataset":"wrong","cases":[]}', encoding="utf-8")
    assert eval_protocol.main(["--dataset", str(invalid), "--results-dir", str(tmp_path / "failure")]) != 0
