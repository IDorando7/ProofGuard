import json
from datetime import datetime, timezone
from decimal import Decimal
from inspect import signature

import pytest
from pydantic import ValidationError

from app.schemas.report_quality import (
    QualityAssessmentSource,
    QualityComponentAssessment,
    QualityComponentName,
    QualityComponentState,
    ReportQualityAssessment,
    ReportQualityAssessmentRequest,
    ReportQualityAssessmentStatus,
    ReportQualityComponentAssessments,
    ReportQualityComponents,
    ReportQualityConfig,
)
from app.services.report_quality_calculator import calculate_report_quality
from app.utils.protocol_serialization import canonical_json_bytes, protocol_fingerprint


def _component(name, score, weight):
    return QualityComponentAssessment(
        component_name=name,
        score=score,
        weight=weight,
        weighted_value=(Decimal(score) * Decimal(weight)).quantize(Decimal("0.000001")),
        assessment_source="validator_assessment",
        reason_codes=["z_reason", "a_reason"],
        evidence_references=["validation:v1", "finding:f1"],
        state="assessed",
        assessment_policy_version="report_quality_policy_v1",
    )


def _breakdown():
    return ReportQualityComponentAssessments(
        correctness=_component("correctness", "1.000000", "0.350000"),
        poc_quality=_component("poc_quality", "0.800000", "0.250000"),
        root_cause_quality=_component("root_cause_quality", "0.900000", "0.200000"),
        impact_quality=_component("impact_quality", "0.700000", "0.100000"),
        fix_quality=_component("fix_quality", "0.600000", "0.100000"),
    )


def test_default_quality_configuration_is_exact_decimal_and_deterministic():
    config = ReportQualityConfig()
    assert all(isinstance(value, Decimal) for value in config.model_dump().values())
    assert sum(config.model_dump().values(), Decimal("0")) == Decimal("1")
    assert config == ReportQualityConfig(
        correctness="0.35",
        poc_quality="0.25",
        root_cause="0.20",
        impact="0.10",
        fix="0.10",
    )
    assert canonical_json_bytes(config) == canonical_json_bytes(ReportQualityConfig())
    serialized = config.model_dump(mode="json")
    assert "llm" not in json.dumps(serialized).lower()
    assert "reward" not in serialized


@pytest.mark.parametrize(
    "overrides",
    [
        {"correctness": "-0.01", "poc_quality": ".36"},
        {"poc_quality": "1.01", "correctness": "-0.01"},
        {"correctness": ".34"},
        {"correctness": ".36"},
    ],
)
def test_invalid_quality_configuration_fails(overrides):
    values = {
        "correctness": "0.35",
        "poc_quality": "0.25",
        "root_cause": "0.20",
        "impact": "0.10",
        "fix": "0.10",
        **overrides,
    }
    with pytest.raises(ValidationError):
        ReportQualityConfig(**values)


def test_quality_formula_exact_boundaries_and_example():
    config = ReportQualityConfig()
    perfect = ReportQualityComponents(
        correctness_score="1",
        poc_quality_score="1",
        root_cause_quality_score="1",
        impact_quality_score="1",
        fix_quality_score="1",
    )
    zero = ReportQualityComponents(
        correctness_score="0",
        poc_quality_score="0",
        root_cause_quality_score="0",
        impact_quality_score="0",
        fix_quality_score="0",
    )
    example = ReportQualityComponents(
        correctness_score="1.00",
        poc_quality_score="0.80",
        root_cause_quality_score="0.90",
        impact_quality_score="0.70",
        fix_quality_score="0.60",
    )
    assert calculate_report_quality(perfect, config) == Decimal("1.000000")
    assert calculate_report_quality(zero, config) == Decimal("0.000000")
    assert calculate_report_quality(example, config) == Decimal("0.860000")
    assert calculate_report_quality(example, config) == calculate_report_quality(
        ReportQualityComponents(**dict(reversed(list(example.model_dump().items())))),
        config,
    )


@pytest.mark.parametrize(
    ("active_component", "expected"),
    [
        ("correctness_score", "0.350000"),
        ("poc_quality_score", "0.250000"),
        ("root_cause_quality_score", "0.200000"),
        ("impact_quality_score", "0.100000"),
        ("fix_quality_score", "0.100000"),
    ],
)
def test_each_quality_component_has_its_exact_policy_weight(active_component, expected):
    values = {
        "correctness_score": "0",
        "poc_quality_score": "0",
        "root_cause_quality_score": "0",
        "impact_quality_score": "0",
        "fix_quality_score": "0",
    }
    values[active_component] = "1"
    result = calculate_report_quality(
        ReportQualityComponents(**values), ReportQualityConfig()
    )
    assert result == Decimal(expected)
    assert result.as_tuple().exponent == -6


def test_quality_calculator_accepts_no_historical_or_economic_inputs():
    parameters = set(signature(calculate_report_quality).parameters)
    assert parameters == {"component_scores", "config"}
    assert parameters.isdisjoint(
        {
            "reputation",
            "category_score",
            "membership",
            "routing_rank",
            "task_reward_budget",
            "previous_rewards",
            "accepted_finding_count",
            "severity",
            "distinct_operator_count",
        }
    )


@pytest.mark.parametrize("value", ["-0.01", "1.01", "NaN", "Infinity", "-Infinity"])
def test_malformed_component_values_fail(value):
    with pytest.raises(ValidationError):
        ReportQualityComponents(
            correctness_score=value,
            poc_quality_score="0",
            root_cause_quality_score="0",
            impact_quality_score="0",
            fix_quality_score="0",
        )


def test_binary_float_and_unknown_components_fail():
    with pytest.raises(ValidationError):
        ReportQualityComponents(
            correctness_score=0.5,
            poc_quality_score="0",
            root_cause_quality_score="0",
            impact_quality_score="0",
            fix_quality_score="0",
        )
    with pytest.raises(ValidationError):
        ReportQualityAssessmentRequest(
            reason_codes={"novelty": ["not_a_quality_component"]}
        )


def test_reason_and_evidence_order_is_canonical_for_fingerprints():
    first = _component("correctness", "1", ".35")
    second = QualityComponentAssessment(
        **{
            **first.model_dump(),
            "reason_codes": list(reversed(first.reason_codes)),
            "evidence_references": list(reversed(first.evidence_references)),
        }
    )
    assert first.reason_codes == ["a_reason", "z_reason"]
    assert protocol_fingerprint(first) == protocol_fingerprint(second)


def test_draft_can_expose_missing_component_but_finalized_cannot():
    missing_fix = QualityComponentAssessment(
        component_name="fix_quality",
        score=None,
        weight="0.10",
        weighted_value=None,
        assessment_source="not_assessed",
        reason_codes=["validator_fix_assessment_required"],
        evidence_references=["finding:f1"],
        state="not_assessed",
        assessment_policy_version="report_quality_policy_v1",
    )
    components = ReportQualityComponentAssessments(
        **{**_breakdown().model_dump(), "fix_quality": missing_fix}
    )
    base = {
        "report_quality_assessment_id": "report_quality_assessment_" + "a" * 32,
        "assessment_version": 1,
        "assessment_policy_version": "report_quality_policy_v1",
        "configuration_version": "report_quality_config_v1",
        "project_id": "project-1",
        "routing_id": "routing-1",
        "finding_cluster_id": "finding_cluster_" + "b" * 64,
        "submission_id": "submission-1",
        "finding_id": "finding-1",
        "node_id": "node-1",
        "operator_id": "operator-1",
        "submitted_at": datetime.now(timezone.utc),
        "validation_id": "validation-1",
        "reproduction_id": "reproduction-1",
        "final_severity": "High",
        "components": components,
        "quality_score": None,
        "assessment_method": "deterministic",
        "cluster_source_fingerprint": "c" * 64,
        "member_source_fingerprint": "d" * 64,
        "validation_source_fingerprint": "e" * 64,
        "reproduction_source_fingerprint": "f" * 64,
        "severity_source_fingerprint": "1" * 64,
        "finding_source_fingerprint": "2" * 64,
        "source_fingerprint": "3" * 64,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    draft = ReportQualityAssessment(
        **base, assessment_status="draft", finalized_at=None
    )
    assert draft.components.fix_quality.state == QualityComponentState.NOT_ASSESSED
    with pytest.raises(ValidationError):
        ReportQualityAssessment(
            **base,
            assessment_status=ReportQualityAssessmentStatus.FINALIZED,
            finalized_at=datetime.now(timezone.utc),
        )


def test_finalized_quality_must_be_server_reconstructable():
    with pytest.raises(ValidationError):
        ReportQualityAssessmentRequest(quality_score="1.0")
    assert _breakdown().scores() == ReportQualityComponents(
        correctness_score="1",
        poc_quality_score=".8",
        root_cause_quality_score=".9",
        impact_quality_score=".7",
        fix_quality_score=".6",
    )
