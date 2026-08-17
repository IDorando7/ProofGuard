from decimal import Decimal, ROUND_HALF_EVEN

from app.schemas.report_quality import (
    REPORT_QUALITY_QUANTUM,
    ReportQualityComponents,
    ReportQualityConfig,
)


def calculate_report_quality(
    component_scores: ReportQualityComponents,
    config: ReportQualityConfig,
) -> Decimal:
    """Calculate Q with exact Decimal arithmetic and one final quantization."""
    scores = ReportQualityComponents.model_validate(component_scores)
    policy = ReportQualityConfig.model_validate(config)
    raw = (
        scores.correctness_score * policy.correctness
        + scores.poc_quality_score * policy.poc_quality
        + scores.root_cause_quality_score * policy.root_cause
        + scores.impact_quality_score * policy.impact
        + scores.fix_quality_score * policy.fix
    )
    if not raw.is_finite() or raw < 0 or raw > 1:
        raise ValueError("Calculated report quality must be between 0 and 1")
    return raw.quantize(REPORT_QUALITY_QUANTUM, rounding=ROUND_HALF_EVEN)
