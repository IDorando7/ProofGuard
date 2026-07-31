import json
from pathlib import Path

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionResult
from app.schemas.validation import ValidationDecision
from app.services.scope_service import parse_scope_yaml


APP_ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = APP_ROOT / "research" / "datasets" / "protocol"
EXPECTED_CASES = [
    "accepted_high_value_001",
    "accepted_medium_value_001",
    "duplicate_no_reward_001",
    "out_of_scope_no_reward_001",
    "unsafe_penalty_001",
    "inactive_node_rejected_001",
    "reputation_growth_001",
]


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_protocol_manifest_exists_and_has_exact_dataset_and_cases():
    manifest_path = DATASET_ROOT / "manifest.json"
    assert manifest_path.is_file()
    manifest = _read_json(manifest_path)
    assert manifest["dataset"] == "week_5_protocol_synthetic"
    assert len(manifest["cases"]) == 7
    assert [entry["case_id"] for entry in manifest["cases"]] == EXPECTED_CASES
    assert len({entry["case_id"] for entry in manifest["cases"]}) == 7


def test_every_manifest_case_has_required_files_and_valid_relative_path():
    manifest = _read_json(DATASET_ROOT / "manifest.json")
    root = DATASET_ROOT.resolve()
    for entry in manifest["cases"]:
        case_path = (DATASET_ROOT / entry["path"]).resolve()
        case_path.relative_to(root)
        assert case_path.is_dir()
        assert (case_path / "case.json").is_file()
        assert (case_path / "expected_protocol.json").is_file()
        assert (case_path / "project").is_dir()


def test_all_expected_case_directories_exist():
    for case_id in EXPECTED_CASES:
        assert (DATASET_ROOT / "cases" / case_id).is_dir()


def test_every_project_fixture_validates_with_application_schemas():
    manifest = _read_json(DATASET_ROOT / "manifest.json")
    for entry in manifest["cases"]:
        case_path = DATASET_ROOT / entry["path"]
        case = _read_json(case_path / "case.json")
        project = case_path / "project"
        parse_scope_yaml((project / "scope.yaml").read_bytes())
        findings = [
            Finding.model_validate(item)
            for finding_file in sorted((project / "findings").glob("*.json"))
            for item in _read_json(finding_file)
        ]
        assert sorted(finding.finding_id for finding in findings) == sorted(case["finding_ids"])
        for finding_id in case["finding_ids"]:
            ReproductionResult.model_validate_json(
                (project / "reproductions" / finding_id / "reproduction.json").read_text(encoding="utf-8")
            )
            ValidationDecision.model_validate_json(
                (project / "validations" / finding_id / "validation.json").read_text(encoding="utf-8")
            )


def test_dataset_is_static_synthetic_and_contains_no_secrets_or_execution_requests():
    all_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(DATASET_ROOT.rglob("*"))
        if path.is_file()
    ).lower()
    assert "synthetic" in all_text
    for forbidden in (
        "private_key",
        "seed_phrase",
        "seed phrase",
        "wallet seed",
        '"command": [',
        "docker run",
        "forge test",
    ):
        assert forbidden not in all_text


def test_manifest_order_is_stable_and_matches_case_files():
    manifest = _read_json(DATASET_ROOT / "manifest.json")
    assert [entry["case_id"] for entry in manifest["cases"]] == [
        _read_json(DATASET_ROOT / entry["path"] / "case.json")["case_id"]
        for entry in manifest["cases"]
    ]
