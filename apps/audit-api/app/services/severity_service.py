import re
from enum import Enum
from pathlib import Path
from typing import Any

from app.schemas.severity import (
    NormalizedSeverity,
    SeverityChangeType,
    SeverityNormalizationResult,
    SeveritySignal,
)
from app.schemas.validation import ValidationEvidence
from app.services.scope_validation_service import load_scope_from_project_workspace


NOTES = [
    "Severity Normalizer v0 uses deterministic heuristics.",
    "This does not replace human security review.",
]

SEVERITY_ORDER = {
    NormalizedSeverity.INFORMATIONAL: 0,
    NormalizedSeverity.LOW: 1,
    NormalizedSeverity.MEDIUM: 2,
    NormalizedSeverity.HIGH: 3,
    NormalizedSeverity.CRITICAL: 4,
}

CATEGORY_BASELINES = {
    "access_control": ("ACCESS_CONTROL_BASELINE", 0.10, "Access-control findings have privileged-action risk."),
    "reentrancy": ("REENTRANCY_BASELINE", 0.15, "Reentrancy findings have a high-risk category baseline."),
    "accounting": ("ACCOUNTING_BASELINE", 0.15, "Accounting findings can affect protocol balances."),
    "oracle_manipulation": ("ORACLE_MANIPULATION_BASELINE", 0.15, "Oracle manipulation can affect pricing decisions."),
    "dos": ("DOS_BASELINE", 0.05, "Denial-of-service findings have an availability baseline."),
    "other": ("OTHER_BASELINE", 0.00, "No category-specific baseline was applied."),
}

CRITICAL_PATTERNS = [
    ("DIRECT_FUND_THEFT", 0.35, "Finding text indicates direct theft of funds.", [
        "direct theft of funds",
        "drain funds",
        "steal funds",
        "loss of all funds",
        "protocol insolvency",
        "insolvency",
        "permanent loss",
        "seize all collateral",
    ]),
    ("ARBITRARY_MINT", 0.35, "Finding text indicates arbitrary or unlimited token minting.", [
        "arbitrary mint",
        "unlimited mint",
    ]),
    ("ARBITRARY_BURN", 0.35, "Finding text indicates arbitrary token burning.", [
        "arbitrary burn",
    ]),
]

HIGH_PATTERNS = [
    ("PRIVILEGE_ESCALATION", 0.25, "Finding text indicates unauthorized privileged action.", [
        "unauthorized privileged action",
        "access control bypass",
        "onlyowner missing",
        "missing onlyowner",
        "privilege escalation",
    ]),
    ("REENTRANCY_DRAIN", 0.25, "Finding text indicates a reentrancy drain pattern.", [
        "reentrancy drain",
        "withdraw before state update",
    ]),
    ("ACCOUNTING_CORRUPTION", 0.25, "Finding text indicates accounting corruption.", [
        "accounting corruption",
        "incorrect collateral accounting",
    ]),
    ("ORACLE_MANIPULATION", 0.25, "Finding text indicates oracle or price manipulation.", [
        "oracle manipulation",
        "price manipulation",
    ]),
    ("LIQUIDATION_ABUSE", 0.25, "Finding text indicates liquidation abuse.", [
        "liquidation abuse",
    ]),
]

MEDIUM_PATTERNS = [
    ("DENIAL_OF_SERVICE", 0.15, "Finding text indicates denial of service.", [
        "denial of service",
        "dos",
        "temporary lock",
        "funds locked temporarily",
    ]),
    ("GRIEFING", 0.15, "Finding text indicates griefing or limited disruption.", [
        "griefing",
        "incorrect event",
        "limited misconfiguration",
        "limited admin misuse",
    ]),
]

LOW_PATTERNS = [
    ("LOW_IMPACT_INDICATOR", -0.20, "Finding text indicates limited or theoretical impact.", [
        "no direct loss",
        "theoretical",
        "requires owner",
        "requires trusted admin",
        "informational",
        "code quality",
        "missing event",
        "naming issue",
        "gas optimization",
        "recommendation only",
    ]),
]

CRITICAL_SIGNAL_CODES = {code for code, _, _, _ in CRITICAL_PATTERNS}

HIGH_VALUE_ASSET_KEYWORDS = (
    "vault",
    "funds",
    "collateral",
    "stablecoin",
    "treasury",
    "backing",
    "token supply",
    "user balances",
)


def normalize_severity_value(value: str | None) -> NormalizedSeverity:
    if value is None:
        return NormalizedSeverity.INFORMATIONAL
    normalized = str(value).strip().lower()
    aliases = {
        "critical": NormalizedSeverity.CRITICAL,
        "crit": NormalizedSeverity.CRITICAL,
        "high": NormalizedSeverity.HIGH,
        "medium": NormalizedSeverity.MEDIUM,
        "med": NormalizedSeverity.MEDIUM,
        "low": NormalizedSeverity.LOW,
        "informational": NormalizedSeverity.INFORMATIONAL,
        "info": NormalizedSeverity.INFORMATIONAL,
        "informative": NormalizedSeverity.INFORMATIONAL,
    }
    return aliases.get(normalized, NormalizedSeverity.INFORMATIONAL)


def severity_to_score(severity: NormalizedSeverity) -> float:
    return {
        NormalizedSeverity.CRITICAL: 1.0,
        NormalizedSeverity.HIGH: 0.8,
        NormalizedSeverity.MEDIUM: 0.55,
        NormalizedSeverity.LOW: 0.3,
        NormalizedSeverity.INFORMATIONAL: 0.1,
    }[severity]


def score_to_severity(score: float) -> NormalizedSeverity:
    if score >= 0.90:
        return NormalizedSeverity.CRITICAL
    if score >= 0.70:
        return NormalizedSeverity.HIGH
    if score >= 0.45:
        return NormalizedSeverity.MEDIUM
    if score >= 0.20:
        return NormalizedSeverity.LOW
    return NormalizedSeverity.INFORMATIONAL


def extract_text_from_finding(finding: Any) -> str:
    fields = [
        "title",
        "root_cause",
        "attack_path",
        "impact",
        "conditions",
        "recommended_fix",
        "category",
        "severity",
    ]
    values: list[str] = []
    for field in fields:
        value = _get_value(finding, field)
        if isinstance(value, list):
            values.extend(_string_value(item) for item in value if item is not None)
        elif value is not None:
            values.append(_string_value(value))
    return _normalize_text(" ".join(values))


def extract_category_from_finding(finding: Any) -> str:
    for key in ("category", "categories", "attack_categories"):
        value = _get_value(finding, key)
        if isinstance(value, str):
            return _normalize_category(value)
        if isinstance(value, list) and value:
            first = next((item for item in value if isinstance(item, str)), None)
            if first:
                return _normalize_category(first)
    return ""


def extract_original_severity_from_finding(finding: Any) -> NormalizedSeverity:
    return normalize_severity_value(
        _optional_str(_get_value(finding, "severity"))
        or _optional_str(_get_value(finding, "original_severity"))
    )


def extract_assets_at_risk(scope: dict | None) -> list[str]:
    if not scope:
        return []
    assets = scope.get("assets_at_risk", []) or []
    if isinstance(assets, str):
        return [assets.lower()]
    if isinstance(assets, list):
        return [str(asset).lower() for asset in assets if asset is not None]
    return []


def extract_reproduction_status(reproduction_result: Any | None) -> str | None:
    if reproduction_result is None:
        return None
    value = _get_value(reproduction_result, "status") or _get_value(reproduction_result, "reproduction_status")
    return str(value).strip().lower() if value is not None else None


def detect_severity_signals(
    finding: Any,
    scope: dict | None = None,
    reproduction_result: Any | None = None,
) -> list[SeveritySignal]:
    text = extract_text_from_finding(finding)
    category = extract_category_from_finding(finding)
    signals: list[SeveritySignal] = []

    for code, weight, message, patterns in [*CRITICAL_PATTERNS, *HIGH_PATTERNS, *MEDIUM_PATTERNS, *LOW_PATTERNS]:
        matched = _first_match(text, patterns)
        if matched:
            signals.append(SeveritySignal(code=code, weight=weight, message=message, matched_text=matched))

    status = extract_reproduction_status(reproduction_result)
    if status == "reproduced":
        signals.append(
            SeveritySignal(
                code="REPRODUCED_EVIDENCE",
                weight=0.10,
                message="Reproduction result indicates the issue was reproduced.",
                matched_text=status,
            )
        )
    elif status == "failed":
        signals.append(
            SeveritySignal(
                code="FAILED_REPRODUCTION",
                weight=-0.20,
                message="Reproduction ran but did not reproduce the issue.",
                matched_text=status,
            )
        )
    elif status == "rejected_unsafe":
        signals.append(
            SeveritySignal(
                code="UNSAFE_POC",
                weight=-0.25,
                message="Reproduction artifact was rejected as unsafe.",
                matched_text=status,
            )
        )
    elif reproduction_result is None:
        signals.append(
            SeveritySignal(
                code="NO_REPRODUCTION",
                weight=-0.10,
                message="No reproduction result is available yet.",
            )
        )

    assets = extract_assets_at_risk(scope)
    matched_asset = _first_match(" ".join(assets), HIGH_VALUE_ASSET_KEYWORDS)
    if matched_asset:
        signals.append(
            SeveritySignal(
                code="HIGH_VALUE_ASSET_AT_RISK",
                weight=0.10,
                message="Scope assets_at_risk includes high-value protocol assets.",
                matched_text=matched_asset,
            )
        )

    baseline = CATEGORY_BASELINES.get(category)
    if baseline and baseline[1] != 0.0:
        code, weight, message = baseline
        signals.append(
            SeveritySignal(
                code=code,
                weight=weight,
                message=message,
                matched_text=category,
            )
        )

    return signals


def calculate_severity_score(
    original_severity: NormalizedSeverity,
    signals: list[SeveritySignal],
) -> float:
    score = severity_to_score(original_severity) + sum(signal.weight for signal in signals)
    return round(max(0.0, min(1.0, score)), 4)


def determine_change_type(
    original: NormalizedSeverity,
    normalized: NormalizedSeverity,
) -> SeverityChangeType:
    if SEVERITY_ORDER[normalized] > SEVERITY_ORDER[original]:
        return SeverityChangeType.UPGRADED
    if SEVERITY_ORDER[normalized] < SEVERITY_ORDER[original]:
        return SeverityChangeType.DOWNGRADED
    return SeverityChangeType.UNCHANGED


def normalize_finding_severity(
    finding: Any,
    scope: dict | None = None,
    reproduction_result: Any | None = None,
) -> SeverityNormalizationResult:
    original = extract_original_severity_from_finding(finding)
    signals = detect_severity_signals(finding, scope, reproduction_result)
    score = calculate_severity_score(original, signals)
    if (
        score >= 0.90
        and original != NormalizedSeverity.CRITICAL
        and not any(signal.code in CRITICAL_SIGNAL_CODES for signal in signals)
    ):
        score = 0.89
    normalized = score_to_severity(score)
    change_type = determine_change_type(original, normalized)

    return SeverityNormalizationResult(
        original_severity=original,
        normalized_severity=normalized,
        change_type=change_type,
        score=score,
        reason=_build_reason(original, normalized, change_type, signals),
        signals=signals,
        notes=NOTES.copy(),
    )


def load_scope_for_severity(project_workspace: Path) -> dict:
    return load_scope_from_project_workspace(project_workspace)


def normalize_finding_severity_from_workspace(
    project_workspace: Path,
    finding: Any,
    reproduction_result: Any | None = None,
) -> SeverityNormalizationResult:
    scope = load_scope_for_severity(project_workspace)
    return normalize_finding_severity(finding, scope, reproduction_result)


def apply_severity_result_to_validation_evidence(
    evidence: ValidationEvidence,
    severity_result: SeverityNormalizationResult,
) -> ValidationEvidence:
    return evidence.model_copy(
        update={
            "original_severity": severity_result.original_severity.value,
            "normalized_severity": severity_result.normalized_severity.value,
            "notes": [*evidence.notes, *severity_result.notes],
        }
    )


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _optional_str(value: Any) -> str | None:
    return _string_value(value) if value is not None else None


def _string_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _normalize_text(value: str) -> str:
    normalized = value.lower().strip()
    normalized = normalized.replace("-", " ").replace("_", " ")
    normalized = re.sub(r"[^\w\s./]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _normalize_category(value: str) -> str:
    normalized = value.strip().lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", "_", normalized)


def _first_match(text: str, patterns: tuple[str, ...] | list[str]) -> str | None:
    normalized_text = _normalize_text(text)
    for pattern in patterns:
        normalized_pattern = _normalize_text(pattern)
        if re.search(rf"\b{re.escape(normalized_pattern)}\b", normalized_text):
            return pattern
    return None


def _build_reason(
    original: NormalizedSeverity,
    normalized: NormalizedSeverity,
    change_type: SeverityChangeType,
    signals: list[SeveritySignal],
) -> str:
    signal_codes = [signal.code for signal in signals if signal.weight != 0.0]
    if signal_codes:
        evidence = ", ".join(signal_codes[:3])
    else:
        evidence = "no strong severity adjustment signals"

    if change_type == SeverityChangeType.UPGRADED:
        return f"Severity upgraded from {original.value} to {normalized.value} based on {evidence}."
    if change_type == SeverityChangeType.DOWNGRADED:
        return f"Severity downgraded from {original.value} to {normalized.value} based on {evidence}."
    return f"Severity kept as {normalized.value} based on {evidence}."
