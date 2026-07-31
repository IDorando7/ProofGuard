from app.schemas.severity import NormalizedSeverity, SeverityChangeType, SeveritySignal
from app.schemas.validation import ValidationEvidence
from app.services.severity_service import (
    apply_severity_result_to_validation_evidence,
    calculate_severity_score,
    detect_severity_signals,
    determine_change_type,
    extract_assets_at_risk,
    extract_category_from_finding,
    extract_original_severity_from_finding,
    extract_reproduction_status,
    extract_text_from_finding,
    normalize_finding_severity,
    normalize_severity_value,
    score_to_severity,
    severity_to_score,
)


def _finding(
    severity="High",
    category="access_control",
    title="Missing onlyOwner",
    root_cause="missing onlyOwner on privileged setter",
    attack_path="attacker calls setTreasury directly",
    impact="unauthorized privileged action can change treasury",
):
    return {
        "title": title,
        "category": category,
        "severity": severity,
        "root_cause": root_cause,
        "attack_path": attack_path,
        "impact": impact,
        "conditions": None,
        "recommended_fix": "Add authorization checks.",
    }


def _codes(signals):
    return {signal.code for signal in signals}


def test_normalize_severity_value_accepts_critical():
    assert normalize_severity_value("critical") == NormalizedSeverity.CRITICAL


def test_normalize_severity_value_accepts_crit():
    assert normalize_severity_value("crit") == NormalizedSeverity.CRITICAL


def test_normalize_severity_value_accepts_high_case_insensitive():
    assert normalize_severity_value("HIGH") == NormalizedSeverity.HIGH


def test_normalize_severity_value_accepts_med():
    assert normalize_severity_value("med") == NormalizedSeverity.MEDIUM


def test_normalize_severity_value_accepts_info():
    assert normalize_severity_value("info") == NormalizedSeverity.INFORMATIONAL


def test_unknown_severity_defaults_to_informational():
    assert normalize_severity_value("severe-ish") == NormalizedSeverity.INFORMATIONAL


def test_severity_to_score_critical():
    assert severity_to_score(NormalizedSeverity.CRITICAL) == 1.0


def test_severity_to_score_high():
    assert severity_to_score(NormalizedSeverity.HIGH) == 0.8


def test_score_to_severity_critical():
    assert score_to_severity(0.95) == NormalizedSeverity.CRITICAL


def test_score_to_severity_high():
    assert score_to_severity(0.75) == NormalizedSeverity.HIGH


def test_score_to_severity_medium():
    assert score_to_severity(0.50) == NormalizedSeverity.MEDIUM


def test_score_to_severity_low():
    assert score_to_severity(0.25) == NormalizedSeverity.LOW


def test_score_to_severity_informational():
    assert score_to_severity(0.05) == NormalizedSeverity.INFORMATIONAL


def test_direct_theft_of_funds_creates_signal():
    signals = detect_severity_signals(_finding(impact="direct theft of funds from vault"))

    assert "DIRECT_FUND_THEFT" in _codes(signals)


def test_arbitrary_mint_creates_signal():
    signals = detect_severity_signals(_finding(impact="arbitrary mint can inflate token supply"))

    assert "ARBITRARY_MINT" in _codes(signals)


def test_missing_only_owner_creates_privilege_signal():
    signals = detect_severity_signals(_finding(root_cause="missing onlyOwner"))

    assert "PRIVILEGE_ESCALATION" in _codes(signals)


def test_withdraw_before_state_update_creates_reentrancy_signal():
    signals = detect_severity_signals(
        _finding(category="reentrancy", root_cause="withdraw before state update")
    )

    assert "REENTRANCY_DRAIN" in _codes(signals)


def test_denial_of_service_creates_signal():
    signals = detect_severity_signals(_finding(impact="denial of service for liquidations"))

    assert "DENIAL_OF_SERVICE" in _codes(signals)


def test_no_direct_loss_creates_negative_signal():
    signals = detect_severity_signals(_finding(impact="no direct loss is possible"))

    assert "LOW_IMPACT_INDICATOR" in _codes(signals)
    assert next(signal.weight for signal in signals if signal.code == "LOW_IMPACT_INDICATOR") < 0


def test_gas_optimization_creates_downgrade_signal():
    signals = detect_severity_signals(_finding(title="gas optimization recommendation only"))

    assert "LOW_IMPACT_INDICATOR" in _codes(signals)


def test_reproduced_reproduction_result_creates_signal():
    signals = detect_severity_signals(_finding(), reproduction_result={"status": "reproduced"})

    assert "REPRODUCED_EVIDENCE" in _codes(signals)


def test_failed_reproduction_result_creates_signal():
    signals = detect_severity_signals(_finding(), reproduction_result={"status": "failed"})

    assert "FAILED_REPRODUCTION" in _codes(signals)


def test_rejected_unsafe_reproduction_result_creates_signal():
    signals = detect_severity_signals(_finding(), reproduction_result={"status": "rejected_unsafe"})

    assert "UNSAFE_POC" in _codes(signals)


def test_assets_at_risk_with_vault_funds_creates_signal():
    signals = detect_severity_signals(_finding(), scope={"assets_at_risk": ["vault funds"]})

    assert "HIGH_VALUE_ASSET_AT_RISK" in _codes(signals)


def test_category_reentrancy_adds_baseline_signal():
    signals = detect_severity_signals(_finding(category="reentrancy"))

    assert "REENTRANCY_BASELINE" in _codes(signals)


def test_calculate_severity_score_clamps_to_one():
    score = calculate_severity_score(
        NormalizedSeverity.HIGH,
        [SeveritySignal(code="A", weight=0.8, message="large positive signal")],
    )

    assert score == 1.0


def test_calculate_severity_score_clamps_to_zero():
    score = calculate_severity_score(
        NormalizedSeverity.LOW,
        [SeveritySignal(code="A", weight=-0.8, message="large negative signal")],
    )

    assert score == 0.0


def test_positive_signals_increase_score():
    score = calculate_severity_score(
        NormalizedSeverity.LOW,
        [SeveritySignal(code="A", weight=0.2, message="positive")],
    )

    assert score > severity_to_score(NormalizedSeverity.LOW)


def test_negative_signals_decrease_score():
    score = calculate_severity_score(
        NormalizedSeverity.HIGH,
        [SeveritySignal(code="A", weight=-0.2, message="negative")],
    )

    assert score < severity_to_score(NormalizedSeverity.HIGH)


def test_critical_with_low_impact_can_downgrade():
    result = normalize_finding_severity(
        _finding(
            severity="Critical",
            category="other",
            title="Theoretical issue",
            root_cause="theoretical code quality concern",
            attack_path="requires trusted admin",
            impact="no direct loss",
        )
    )

    assert result.normalized_severity in {NormalizedSeverity.HIGH, NormalizedSeverity.MEDIUM}
    assert result.change_type == SeverityChangeType.DOWNGRADED


def test_medium_with_fund_theft_and_reproduced_evidence_upgrades():
    result = normalize_finding_severity(
        _finding(
            severity="Medium",
            category="reentrancy",
            impact="direct theft of funds from vault",
            root_cause="withdraw before state update",
        ),
        scope={"assets_at_risk": ["vault funds"]},
        reproduction_result={"status": "reproduced"},
    )

    assert result.normalized_severity in {NormalizedSeverity.HIGH, NormalizedSeverity.CRITICAL}
    assert result.change_type == SeverityChangeType.UPGRADED


def test_high_unauthorized_access_control_with_reproduced_evidence_stays_high():
    result = normalize_finding_severity(
        _finding(severity="High", category="access_control"),
        reproduction_result={"status": "reproduced"},
    )

    assert result.normalized_severity == NormalizedSeverity.HIGH
    assert result.change_type == SeverityChangeType.UNCHANGED


def test_high_with_failed_reproduction_and_limited_impact_can_downgrade():
    result = normalize_finding_severity(
        _finding(
            severity="High",
            category="other",
            title="Limited misconfiguration",
            root_cause="limited misconfiguration",
            attack_path="requires owner",
            impact="no direct loss",
        ),
        reproduction_result={"status": "failed"},
    )

    assert result.normalized_severity == NormalizedSeverity.MEDIUM
    assert result.change_type == SeverityChangeType.DOWNGRADED


def test_informational_gas_optimization_stays_informational_or_low():
    result = normalize_finding_severity(
        _finding(
            severity="Informational",
            category="other",
            title="gas optimization",
            root_cause="code quality",
            attack_path="recommendation only",
            impact="no direct loss",
        )
    )

    assert result.normalized_severity in {NormalizedSeverity.INFORMATIONAL, NormalizedSeverity.LOW}


def test_result_includes_reason():
    result = normalize_finding_severity(_finding())

    assert result.reason


def test_result_includes_deterministic_notes():
    result = normalize_finding_severity(_finding())

    assert "Severity Normalizer v0 uses deterministic heuristics." in result.notes
    assert "This does not replace human security review." in result.notes


def test_change_type_is_upgraded():
    assert determine_change_type(NormalizedSeverity.MEDIUM, NormalizedSeverity.HIGH) == SeverityChangeType.UPGRADED


def test_change_type_is_downgraded():
    assert determine_change_type(NormalizedSeverity.HIGH, NormalizedSeverity.MEDIUM) == SeverityChangeType.DOWNGRADED


def test_change_type_is_unchanged():
    assert determine_change_type(NormalizedSeverity.HIGH, NormalizedSeverity.HIGH) == SeverityChangeType.UNCHANGED


def test_extract_text_from_finding_returns_lowercase_text():
    text = extract_text_from_finding(_finding(title="Unauthorized Setter"))

    assert "unauthorized setter" in text


def test_extract_category_from_finding_normalizes_category():
    assert extract_category_from_finding({"category": "Access Control"}) == "access_control"


def test_extract_original_severity_from_finding_supports_original_severity():
    assert extract_original_severity_from_finding({"original_severity": "crit"}) == NormalizedSeverity.CRITICAL


def test_extract_assets_at_risk_returns_lowercase_strings():
    assert extract_assets_at_risk({"assets_at_risk": ["Vault Funds"]}) == ["vault funds"]


def test_extract_reproduction_status_supports_reproduction_status_field():
    assert extract_reproduction_status({"reproduction_status": "REPRODUCED"}) == "reproduced"


def test_apply_severity_result_to_validation_evidence_sets_fields_and_notes():
    result = normalize_finding_severity(_finding(severity="Medium"))
    evidence = apply_severity_result_to_validation_evidence(ValidationEvidence(notes=["existing"]), result)

    assert evidence.original_severity == "Medium"
    assert evidence.normalized_severity == result.normalized_severity.value
    assert "existing" in evidence.notes
    assert "Severity Normalizer v0 uses deterministic heuristics." in evidence.notes
