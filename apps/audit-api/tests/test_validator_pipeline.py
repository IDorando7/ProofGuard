import inspect

from app.schemas.finding import Finding
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.validation import ValidationStatus
from app.services.finding_service import list_project_findings
from app.services.reproduction_service import save_reproduction_result
from app.services.validation_service import load_validation_decision
from app.services.validator_pipeline import validate_all_findings, validate_finding


def _finding(
    finding_id="finding-1",
    category="access_control",
    contract="src/Vault.sol",
    function="setTreasury",
    severity="High",
    root_cause="missing onlyOwner authorization",
    attack_path="attacker calls setTreasury directly",
    impact="unauthorized privileged action can change treasury",
):
    return Finding(
        finding_id=finding_id,
        project_id="project-1",
        title="Missing access control on treasury setter",
        category=category,
        severity=severity,
        confidence=0.8,
        contracts=[contract],
        functions=[function],
        root_cause=root_cause,
        attack_path=attack_path,
        impact=impact,
        recommended_fix="Add onlyOwner.",
        agent_name="access_control_agent",
    )


def _setup_workspace(tmp_path, findings=None, scope=None):
    workspace = tmp_path / "project-1"
    (workspace / "findings").mkdir(parents=True)
    (workspace / "scope").mkdir()
    if scope is None:
        scope = {
            "contracts_in_scope": ["src/Vault.sol"],
            "contracts_out_of_scope": [],
            "attack_categories": ["access_control", "reentrancy"],
            "assets_at_risk": ["vault funds"],
        }
    (workspace / "scope" / "parsed_scope.json").write_text(__import__("json").dumps(scope), encoding="utf-8")
    findings = findings or [_finding()]
    (workspace / "findings" / "findings.json").write_text(
        __import__("json").dumps([finding.model_dump(mode="json") for finding in findings], indent=2),
        encoding="utf-8",
    )
    return workspace


def _save_reproduction(workspace, finding_id="finding-1", status=ReproductionStatus.REPRODUCED):
    result = ReproductionResult(
        reproduction_id=f"repr-{finding_id}",
        finding_id=finding_id,
        project_id="project-1",
        status=status,
        poc_file="test/PoC.t.sol",
        test_name="testName",
        command=["forge", "test", "--match-test", "testName"],
        stdout="stdout",
        stderr=None,
        duration_ms=123,
        error_message=None,
        safety_notes=[],
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    return save_reproduction_result(result, workspace)


def test_in_scope_reproduced_unique_finding_is_accepted(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.ACCEPTED


def test_out_of_scope_finding_returns_out_of_scope(tmp_path):
    workspace = _setup_workspace(tmp_path, findings=[_finding(contract="src/Other.sol")])
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.OUT_OF_SCOPE
    assert decision.confidence == 0.95


def test_duplicate_finding_returns_duplicate(tmp_path):
    findings = [_finding("finding-1"), _finding("finding-2")]
    workspace = _setup_workspace(tmp_path, findings=findings)
    _save_reproduction(workspace, "finding-2")

    decision = validate_finding("project-1", "finding-2", workspace)

    assert decision.status == ValidationStatus.DUPLICATE


def test_possible_duplicate_with_reproduced_is_not_duplicate(tmp_path):
    findings = [
        _finding("finding-1"),
        _finding(
            "finding-2",
            function="setOwner",
            root_cause="missing onlyOwner authorization",
            attack_path="attacker calls setOwner directly",
        ),
    ]
    workspace = _setup_workspace(tmp_path, findings=findings)
    _save_reproduction(workspace, "finding-2")

    decision = validate_finding("project-1", "finding-2", workspace)

    assert decision.status != ValidationStatus.DUPLICATE
    assert decision.evidence.is_duplicate is False
    assert "possible duplicate" in " ".join(decision.evidence.notes).lower()


def test_no_reproduction_result_returns_needs_review(tmp_path):
    workspace = _setup_workspace(tmp_path)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.NEEDS_REVIEW
    assert decision.reason == "Finding has no reproduction result yet."


def test_failed_reproduction_returns_insufficient_evidence(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace, status=ReproductionStatus.FAILED)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.INSUFFICIENT_EVIDENCE


def test_rejected_unsafe_reproduction_returns_unsafe_poc(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace, status=ReproductionStatus.REJECTED_UNSAFE)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.UNSAFE_POC


def test_unsupported_reproduction_returns_unsupported(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace, status=ReproductionStatus.UNSUPPORTED)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.UNSUPPORTED


def test_timeout_reproduction_returns_needs_review(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace, status=ReproductionStatus.TIMEOUT)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.NEEDS_REVIEW
    assert decision.reason == "Reproduction timed out."


def test_sandbox_error_reproduction_returns_needs_review(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace, status=ReproductionStatus.SANDBOX_ERROR)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.NEEDS_REVIEW
    assert decision.reason == "Sandbox execution failed."


def test_invalid_missing_scope_returns_needs_review(tmp_path):
    workspace = _setup_workspace(tmp_path)
    (workspace / "scope" / "parsed_scope.json").unlink()
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.status == ValidationStatus.NEEDS_REVIEW
    assert decision.confidence == 0.30


def test_accepted_finding_has_confidence_at_least_point_eight(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.confidence >= 0.80


def test_duplicate_finding_has_duplicate_of_evidence(tmp_path):
    workspace = _setup_workspace(tmp_path, findings=[_finding("finding-1"), _finding("finding-2")])
    _save_reproduction(workspace, "finding-2")

    decision = validate_finding("project-1", "finding-2", workspace)

    assert decision.evidence.duplicate_of == "finding-1"


def test_accepted_finding_has_reproduction_evidence(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.evidence.has_reproduction is True
    assert decision.evidence.reproduction_status == "reproduced"


def test_severity_normalization_is_included_in_evidence(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert decision.evidence.original_severity == "High"
    assert decision.evidence.normalized_severity in {"High", "Critical", "Medium", "Low", "Informational"}


def test_validation_json_is_saved(tmp_path):
    workspace = _setup_workspace(tmp_path)
    _save_reproduction(workspace)

    decision = validate_finding("project-1", "finding-1", workspace)

    assert (workspace / "validations" / "finding-1" / "validation.json").is_file()
    assert load_validation_decision(workspace, "finding-1").validation_id == decision.validation_id


def test_validate_all_findings_validates_multiple_findings(tmp_path):
    findings = [_finding("finding-1"), _finding("finding-2", contract="src/Vault.sol", function="mint")]
    workspace = _setup_workspace(tmp_path, findings=findings)
    _save_reproduction(workspace, "finding-1")
    _save_reproduction(workspace, "finding-2")

    decisions = validate_all_findings("project-1", workspace)

    assert len(decisions) == 2


def test_validate_all_findings_identifies_duplicates(tmp_path):
    findings = [_finding("finding-1"), _finding("finding-2")]
    workspace = _setup_workspace(tmp_path, findings=findings)
    _save_reproduction(workspace, "finding-1")
    _save_reproduction(workspace, "finding-2")

    decisions = validate_all_findings("project-1", workspace)

    assert decisions[0].status == ValidationStatus.ACCEPTED
    assert decisions[1].status == ValidationStatus.DUPLICATE
    assert decisions[1].evidence.duplicate_of == "finding-1"


def test_pipeline_does_not_call_docker_forge_or_subprocess():
    import app.services.validator_pipeline as validator_pipeline

    source = inspect.getsource(validator_pipeline)

    assert "subprocess" not in source
    assert "docker" not in source
    assert "forge" not in source
    assert "run_in_sandbox" not in source


def test_list_project_findings_loads_saved_findings(tmp_path):
    workspace = _setup_workspace(tmp_path)

    findings = list_project_findings(workspace)

    assert len(findings) == 1
    assert findings[0].finding_id == "finding-1"
