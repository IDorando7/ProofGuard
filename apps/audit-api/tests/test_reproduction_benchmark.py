import json
import sys
from pathlib import Path

from app.services.foundry_service import detect_foundry_project
from app.services.safety_preflight_service import run_safety_preflight


APP_ROOT = Path(__file__).resolve().parents[1]
REPRO_DATASET = APP_ROOT / "research" / "datasets" / "reproduction"
EVALS_ROOT = APP_ROOT / "research" / "evals"
if str(EVALS_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALS_ROOT))

import eval_reproduction  # noqa: E402


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _expected(case_id: str):
    return _read_json(REPRO_DATASET / "cases" / case_id / "expected_reproduction.json")


def test_reproduction_manifest_exists_and_has_four_cases():
    manifest = _read_json(REPRO_DATASET / "manifest.json")

    assert manifest["dataset"] == "week_3_reproduction_synthetic"
    assert len(manifest["cases"]) == 4


def test_every_reproduction_case_has_expected_reproduction_json():
    manifest = _read_json(REPRO_DATASET / "manifest.json")

    for case in manifest["cases"]:
        assert (REPRO_DATASET / case["path"] / "expected_reproduction.json").is_file()


def test_access_control_case_expected_status_is_reproduced():
    assert _expected("access_control_poc_001")["expected_status"] == "reproduced"


def test_reentrancy_case_expected_status_is_reproduced():
    assert _expected("reentrancy_poc_001")["expected_status"] == "reproduced"


def test_unsafe_case_expected_status_is_rejected_unsafe():
    assert _expected("unsafe_poc_001")["expected_status"] == "rejected_unsafe"


def test_unsupported_case_expected_status_is_unsupported():
    assert _expected("unsupported_non_foundry_001")["expected_status"] == "unsupported"


def test_unsafe_case_contains_ffi_and_safety_preflight_rejects_it():
    case_path = REPRO_DATASET / "cases" / "unsafe_poc_001"
    poc_text = (case_path / "test" / "PoC.t.sol").read_text(encoding="utf-8")
    foundry_text = (case_path / "foundry.toml").read_text(encoding="utf-8")

    result = run_safety_preflight(
        repo_path=case_path,
        poc_file="test/PoC.t.sol",
        test_name="testUnsafeFfi",
        command=["forge", "test", "--match-test", "testUnsafeFfi"],
    )

    assert "vm.ffi" in poc_text or "ffi = true" in foundry_text
    assert result.passed is False


def test_unsupported_case_is_detected_as_non_foundry():
    case_path = REPRO_DATASET / "cases" / "unsupported_non_foundry_001"

    assert detect_foundry_project(case_path).is_foundry_project is False


def test_eval_reproduction_mock_mode_writes_case_results(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        eval_reproduction.sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Docker should not be called in mock mode")),
    )

    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)
    case_results_path, _ = eval_reproduction.write_results(case_results, leaderboard, results_dir=tmp_path)

    assert case_results_path.is_file()
    saved = _read_json(case_results_path)
    assert len(saved["cases"]) == 4
    assert all(case["mock_sandbox"] is True for case in saved["cases"])


def test_eval_reproduction_mock_mode_writes_leaderboard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        eval_reproduction.sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Docker should not be called in mock mode")),
    )

    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)
    _, leaderboard_path = eval_reproduction.write_results(case_results, leaderboard, results_dir=tmp_path)

    assert leaderboard_path.is_file()
    saved = _read_json(leaderboard_path)
    assert saved["total_cases"] == 4


def test_reproduction_leaderboard_contains_required_fields():
    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)

    assert leaderboard["total_cases"] == 4
    assert leaderboard["reproduced_count"] == 2
    assert leaderboard["rejected_unsafe_count"] == 1
    assert leaderboard["unsupported_count"] == 1
    assert leaderboard["accuracy"] == 1.0


def test_reproduction_report_file_is_generated(tmp_path: Path):
    case_results, leaderboard = eval_reproduction.run_evaluation(mock_sandbox=True)
    report_path = eval_reproduction.write_report(
        case_results,
        leaderboard,
        mock_sandbox=True,
        report_path=tmp_path / "week_3_reproduction_report.md",
    )

    assert report_path.is_file()
    assert "Week 3 Reproduction Benchmark Report" in report_path.read_text(encoding="utf-8")


def test_mock_mode_does_not_call_docker_or_subprocess_for_sandbox_execution(monkeypatch):
    called = False

    def fake_run_in_sandbox(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("Docker should not be called in mock mode")

    monkeypatch.setattr(eval_reproduction.sandbox_runner, "run_in_sandbox", fake_run_in_sandbox)

    eval_reproduction.run_evaluation(mock_sandbox=True)

    assert called is False

