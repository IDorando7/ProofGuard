import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.finding import Finding
from app.schemas.report import ReportFindingSummary
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.report_service import (
    build_final_audit_report,
    build_report_finding_summary,
    compute_severity_breakdown,
    compute_validation_breakdown,
    get_reports_dir,
    group_summaries_by_validation_status,
    load_final_audit_report_json,
    load_final_audit_report_markdown,
    render_markdown_report,
    save_final_audit_report,
)
from app.services.reproduction_service import save_reproduction_result
from app.services.validation_service import create_validation_decision


def _finding(
    finding_id="finding-1",
    title="Missing access control",
    category="access_control",
    severity="High",
    contract="src/Vault.sol",
    function="setTreasury",
):
    return Finding(
        finding_id=finding_id,
        project_id="project-1",
        title=title,
        category=category,
        severity=severity,
        confidence=0.8,
        contracts=[contract],
        functions=[function],
        root_cause="missing onlyOwner authorization",
        attack_path="attacker calls setTreasury directly",
        impact="unauthorized privileged action can change treasury",
        recommended_fix="Add onlyOwner.",
        agent_name="access_control_agent",
    )


def _workspace(tmp_path: Path, findings: list[Finding] | None = None) -> Path:
    workspace = tmp_path / "project-1"
    (workspace / "findings").mkdir(parents=True)
    (workspace / "scope").mkdir()
    (workspace / "reports").mkdir()
    scope = {
        "project_name": "MiniLendingProtocol",
        "contracts_in_scope": ["src/Vault.sol"],
        "contracts_out_of_scope": ["src/Legacy.sol"],
        "attack_categories": ["access_control", "reentrancy"],
        "assets_at_risk": ["vault funds"],
    }
    (workspace / "scope" / "parsed_scope.json").write_text(json.dumps(scope), encoding="utf-8")
    (workspace / "metadata.json").write_text(
        json.dumps({"project_id": "project-1", "project_name": "MetadataName"}, indent=2),
        encoding="utf-8",
    )
    findings = findings or [_finding()]
    (workspace / "findings" / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json") for finding in findings], indent=2),
        encoding="utf-8",
    )
    return workspace


def _reproduction(workspace: Path, finding_id="finding-1", status=ReproductionStatus.REPRODUCED):
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
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    return save_reproduction_result(result, workspace)


def _validation(
    workspace: Path,
    finding_id="finding-1",
    status=ValidationStatus.ACCEPTED,
    normalized_severity="High",
):
    return create_validation_decision(
        project_id="project-1",
        finding_id=finding_id,
        project_workspace=workspace,
        status=status,
        reason=f"Decision reason for {status.value}.",
        confidence=0.9,
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=True,
            reproduction_status="reproduced",
            original_severity="High",
            normalized_severity=normalized_severity,
        ),
        validator_name="validator_pipeline_v0",
    )


def test_get_reports_dir_returns_workspace_reports(tmp_path):
    workspace = tmp_path / "project-1"

    assert get_reports_dir(workspace) == workspace / "reports"


def test_build_report_finding_summary_extracts_finding_fields(tmp_path):
    workspace = _workspace(tmp_path)
    finding = _finding()

    summary = build_report_finding_summary(finding, None, None)

    assert summary.finding_id == "finding-1"
    assert summary.title == "Missing access control"
    assert summary.category == "access_control"
    assert summary.original_severity == "High"
    assert summary.contract == "src/Vault.sol"
    assert summary.function == "setTreasury"


def test_build_report_finding_summary_includes_validation_status_and_reason(tmp_path):
    workspace = _workspace(tmp_path)
    decision = _validation(workspace, status=ValidationStatus.ACCEPTED)

    summary = build_report_finding_summary(_finding(), decision, None)

    assert summary.validation_status == "accepted"
    assert summary.reason == "Decision reason for accepted."


def test_build_report_finding_summary_includes_reproduction_fields(tmp_path):
    workspace = _workspace(tmp_path)
    reproduction = _reproduction(workspace)

    summary = build_report_finding_summary(_finding(), None, reproduction)

    assert summary.reproduction_status == "reproduced"
    assert summary.poc_file == "test/PoC.t.sol"
    assert summary.test_name == "testName"


def test_compute_severity_breakdown_counts_normalized_severity():
    summaries = [
        ReportFindingSummary(finding_id="f1", validation_status="accepted", normalized_severity="Critical"),
        ReportFindingSummary(finding_id="f2", validation_status="accepted", normalized_severity="High"),
    ]

    breakdown = compute_severity_breakdown(summaries)

    assert breakdown.Critical == 1
    assert breakdown.High == 1


def test_compute_severity_breakdown_falls_back_to_original_severity():
    summaries = [ReportFindingSummary(finding_id="f1", validation_status="accepted", original_severity="Medium")]

    assert compute_severity_breakdown(summaries).Medium == 1


def test_compute_validation_breakdown_counts_all_statuses():
    summaries = [
        ReportFindingSummary(finding_id=status, validation_status=status)
        for status in (
            "accepted",
            "rejected",
            "duplicate",
            "needs_review",
            "out_of_scope",
            "insufficient_evidence",
            "unsafe_poc",
            "unsupported",
        )
    ]

    breakdown = compute_validation_breakdown(summaries)

    assert breakdown.accepted == 1
    assert breakdown.rejected == 1
    assert breakdown.duplicate == 1
    assert breakdown.needs_review == 1
    assert breakdown.out_of_scope == 1
    assert breakdown.insufficient_evidence == 1
    assert breakdown.unsafe_poc == 1
    assert breakdown.unsupported == 1


def test_group_summaries_by_validation_status_groups_expected_statuses():
    groups = group_summaries_by_validation_status(
        [
            ReportFindingSummary(finding_id="f1", validation_status="accepted"),
            ReportFindingSummary(finding_id="f2", validation_status="duplicate"),
            ReportFindingSummary(finding_id="f3", validation_status="unknown"),
        ]
    )

    assert [item.finding_id for item in groups["accepted"]] == ["f1"]
    assert [item.finding_id for item in groups["duplicate"]] == ["f2"]
    assert [item.finding_id for item in groups["needs_review"]] == ["f3"]


def test_build_final_audit_report_creates_report_with_total_findings(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)
    _reproduction(workspace)

    report = build_final_audit_report("project-1", workspace)

    assert report.total_findings == 1
    assert report.project_name == "MiniLendingProtocol"


def test_build_final_audit_report_includes_accepted_findings(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace, status=ValidationStatus.ACCEPTED)

    assert len(build_final_audit_report("project-1", workspace).accepted_findings) == 1


def test_build_final_audit_report_includes_duplicate_findings(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace, status=ValidationStatus.DUPLICATE)

    assert len(build_final_audit_report("project-1", workspace).duplicate_findings) == 1


def test_build_final_audit_report_includes_insufficient_evidence_findings(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace, status=ValidationStatus.INSUFFICIENT_EVIDENCE)

    assert len(build_final_audit_report("project-1", workspace).insufficient_evidence_findings) == 1


def test_build_final_audit_report_includes_unsafe_poc_findings(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace, status=ValidationStatus.UNSAFE_POC)

    assert len(build_final_audit_report("project-1", workspace).unsafe_poc_findings) == 1


def test_render_markdown_report_contains_required_sections(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)
    report = build_final_audit_report("project-1", workspace)

    markdown = render_markdown_report(report)

    assert "# ProofGuard Audit Report" in markdown
    assert "## Project" in markdown
    assert "## Scope" in markdown
    assert "## Executive Summary" in markdown
    assert "## Severity Breakdown" in markdown
    assert "## Validation Breakdown" in markdown
    assert "## Accepted Findings" in markdown
    assert "## Limitations" in markdown


def test_render_markdown_report_writes_none_for_empty_sections(tmp_path):
    workspace = _workspace(tmp_path, findings=[])
    report = build_final_audit_report("project-1", workspace)

    markdown = render_markdown_report(report)

    assert "None." in markdown


def test_save_final_audit_report_writes_markdown_and_json(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)

    response = save_final_audit_report("project-1", workspace)

    assert (workspace / "reports" / "final_report.md").is_file()
    assert (workspace / "reports" / "final_report.json").is_file()
    assert response.markdown_path == "reports/final_report.md"
    assert response.json_path == "reports/final_report.json"


def test_load_final_audit_report_json_loads_saved_report(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)
    save_final_audit_report("project-1", workspace)

    assert load_final_audit_report_json(workspace).project_id == "project-1"


def test_load_final_audit_report_markdown_loads_saved_markdown(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)
    save_final_audit_report("project-1", workspace)

    assert "# ProofGuard Audit Report" in load_final_audit_report_markdown(workspace)


def test_output_paths_are_relative(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)

    response = save_final_audit_report("project-1", workspace)

    assert not Path(response.markdown_path).is_absolute()
    assert not Path(response.json_path).is_absolute()


def test_report_json_is_human_readable(tmp_path):
    workspace = _workspace(tmp_path)
    _validation(workspace)
    save_final_audit_report("project-1", workspace)

    content = (workspace / "reports" / "final_report.json").read_text(encoding="utf-8")

    assert "\n  " in content
    assert '"project_id": "project-1"' in content
