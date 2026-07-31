import json
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_DATASET = APP_ROOT / "research" / "datasets" / "validation"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _expected(case_id: str):
    return _read_json(VALIDATION_DATASET / "cases" / case_id / "expected_validation.json")


def test_validation_manifest_exists():
    assert (VALIDATION_DATASET / "manifest.json").is_file()


def test_validation_manifest_contains_seven_cases():
    manifest = _read_json(VALIDATION_DATASET / "manifest.json")

    assert manifest["dataset"] == "week_4_validation_synthetic"
    assert len(manifest["cases"]) == 7


def test_every_validation_case_has_expected_validation_json():
    manifest = _read_json(VALIDATION_DATASET / "manifest.json")

    for case in manifest["cases"]:
        assert (VALIDATION_DATASET / case["path"] / "expected_validation.json").is_file()


def test_accepted_reproduced_expected_status_is_accepted():
    expected = _expected("accepted_reproduced_001")

    assert expected["expected_statuses"]["finding_accepted_001"] == "accepted"


def test_duplicate_finding_case_contains_at_least_two_findings():
    findings = _read_json(VALIDATION_DATASET / "cases" / "duplicate_finding_001" / "findings.json")

    assert len(findings) >= 2


def test_out_of_scope_expected_status_is_out_of_scope():
    expected = _expected("out_of_scope_001")

    assert expected["expected_statuses"]["finding_oos_001"] == "out_of_scope"


def test_insufficient_evidence_expected_status_is_insufficient_evidence():
    expected = _expected("insufficient_evidence_001")

    assert expected["expected_statuses"]["finding_failed_repro_001"] == "insufficient_evidence"


def test_unsafe_poc_expected_status_is_unsafe_poc():
    expected = _expected("unsafe_poc_001")

    assert expected["expected_statuses"]["finding_unsafe_poc_001"] == "unsafe_poc"


def test_unsupported_expected_status_is_unsupported():
    expected = _expected("unsupported_001")

    assert expected["expected_statuses"]["finding_unsupported_001"] == "unsupported"


def test_needs_review_no_reproduction_expected_status_is_needs_review():
    expected = _expected("needs_review_no_reproduction_001")

    assert expected["expected_statuses"]["finding_needs_review_001"] == "needs_review"
