import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.category_performance import CategoryPerformanceRecord
from app.schemas.category_score import (
    CATEGORY_SCORE_NEUTRAL_BASELINE,
    CATEGORY_SCORE_SCORING_VERSION,
    CategoryScoreBand,
    CategoryScoreBatchResult,
    CategoryScoreComponents,
    CategoryScorePenalties,
    CategoryScoreProcessingStatus,
    CategoryScoreRebuildResult,
    CategoryScoreRecord,
    CategoryScoreWeightedComponents,
    CategoryScoreWeights,
)
from app.schemas.finding import FindingCategory
from app.schemas.node import NodeRecord, normalize_category
from app.services.category_performance_service import (
    CategoryPerformanceNodeNotFoundError,
    CategoryPerformanceStorageError,
    get_node_category_performance,
    list_category_performance,
    load_category_performance,
    rebuild_all_category_performance,
    rebuild_category_performance,
    rebuild_node_category_performance,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, load_node
from app.services.subnet_registry_service import load_subnet


CATEGORY_SCORES_DIRECTORY = "category-scores"
PENALTY_FORMULA_VERSION = "category_score_penalties_v0"
CONFIDENCE_FORMULA_VERSION = "category_score_confidence_v0"
CONSISTENCY_FORMULA_VERSION = "accepted_score_range_v0"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CategoryScoreServiceError(ValueError):
    """Base error for deterministic category-score failures."""


class CategoryScoreNodeNotFoundError(CategoryScoreServiceError):
    pass


class CategoryScorePerformanceNotFoundError(CategoryScoreServiceError):
    pass


class CategoryScoreNotFoundError(CategoryScoreServiceError):
    pass


class CategoryScoreUnsupportedCategoryError(CategoryScoreServiceError):
    pass


class CategoryScoreInputMismatchError(CategoryScoreServiceError):
    pass


class CategoryScoreSourceChangedError(CategoryScoreServiceError):
    pass


class InvalidCategoryScoreIdentifierError(CategoryScoreServiceError):
    pass


class CategoryScoreCalculationError(CategoryScoreServiceError):
    pass


class CategoryScoreStorageError(CategoryScoreServiceError):
    pass


def get_category_scores_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / CATEGORY_SCORES_DIRECTORY


def get_node_category_scores_root(
    protocol_data_root: Path,
    node_id: str,
) -> Path:
    _validate_identifier(node_id, "node")
    root = get_category_scores_root(protocol_data_root)
    node_root = root / node_id
    _ensure_path_within(node_root, root)
    return node_root


def get_category_score_path(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> Path:
    normalized = _normalize_service_category(category)
    node_root = get_node_category_scores_root(protocol_data_root, node_id)
    path = node_root / f"{normalized}.json"
    _ensure_path_within(path, node_root)
    return path


def build_category_score_id(
    node_id: str,
    category: str | FindingCategory,
) -> str:
    _validate_identifier(node_id, "node")
    normalized = _normalize_service_category(category)
    return f"category_score_{node_id}_{normalized}"


def calculate_precision(performance: CategoryPerformanceRecord) -> float:
    finalized = performance.counts.total_finalized_submissions
    if finalized == 0:
        return 0.0
    accepted_valid = (
        performance.counts.accepted_unique_submissions
        + performance.counts.accepted_independent_duplicate_submissions
    )
    return _round6(accepted_valid / finalized)


def calculate_reproduction_rate(
    performance: CategoryPerformanceRecord,
) -> float:
    attempts = performance.counts.reproduction_attempts
    if attempts == 0:
        return 0.0
    return _round6(performance.counts.reproduced_submissions / attempts)


def calculate_uniqueness_rate(
    performance: CategoryPerformanceRecord,
) -> float:
    accepted = performance.counts.accepted_unique_submissions
    denominator = (
        accepted
        + performance.counts.accepted_independent_duplicate_submissions
        + performance.counts.duplicate_submissions
    )
    if denominator == 0:
        return 0.0
    return _round6(accepted / denominator)


def calculate_contribution_quality(
    performance: CategoryPerformanceRecord,
) -> float:
    if (
        performance.counts.accepted_unique_submissions
        + performance.counts.accepted_independent_duplicate_submissions
        == 0
    ):
        return 0.0
    return _round6(
        performance.contribution_stats.accepted_average_contribution_score
        / 100.0
    )


def calculate_consistency(
    performance: CategoryPerformanceRecord,
) -> float:
    accepted = (
        performance.counts.accepted_unique_submissions
        + performance.counts.accepted_independent_duplicate_submissions
    )
    if accepted == 0:
        return 0.0
    if accepted == 1:
        return 0.5

    accepted_minimum = (
        performance.contribution_stats.accepted_minimum_contribution_score
    )
    accepted_maximum = (
        performance.contribution_stats.accepted_maximum_contribution_score
    )
    if accepted_minimum is None or accepted_maximum is None:
        # Backward-compatible neutral fallback for Day 2 records created before
        # accepted-only bounds existed. Overall bounds are deliberately ignored
        # because invalid outcomes may contribute zero scores.
        return 0.5
    accepted_range_normalized = (
        accepted_maximum - accepted_minimum
    ) / 100.0
    return _round6(_clamp01(1.0 - accepted_range_normalized))


def calculate_experience_confidence(
    finalized_submissions: int,
) -> float:
    if (
        isinstance(finalized_submissions, bool)
        or not isinstance(finalized_submissions, int)
        or finalized_submissions < 0
    ):
        raise CategoryScoreCalculationError(
            "Finalized submissions must be a non-negative integer"
        )
    return _round6(min(finalized_submissions / 10.0, 1.0))


def calculate_score_components(
    performance: CategoryPerformanceRecord,
) -> CategoryScoreComponents:
    validated = _validate_performance(performance)
    return CategoryScoreComponents(
        precision=calculate_precision(validated),
        reproduction_rate=calculate_reproduction_rate(validated),
        uniqueness_rate=calculate_uniqueness_rate(validated),
        contribution_quality=calculate_contribution_quality(validated),
        consistency=calculate_consistency(validated),
        experience_confidence=calculate_experience_confidence(
            validated.counts.total_finalized_submissions
        ),
    )


def calculate_weighted_components(
    components: CategoryScoreComponents,
    weights: CategoryScoreWeights,
) -> CategoryScoreWeightedComponents:
    validated_components = CategoryScoreComponents.model_validate(
        components.model_dump()
    )
    validated_weights = CategoryScoreWeights.model_validate(weights.model_dump())
    contributions = {
        "precision_contribution": _round6(
            validated_components.precision
            * validated_weights.precision_weight
        ),
        "reproduction_contribution": _round6(
            validated_components.reproduction_rate
            * validated_weights.reproduction_weight
        ),
        "uniqueness_contribution": _round6(
            validated_components.uniqueness_rate
            * validated_weights.uniqueness_weight
        ),
        "contribution_quality_contribution": _round6(
            validated_components.contribution_quality
            * validated_weights.contribution_quality_weight
        ),
        "consistency_contribution": _round6(
            validated_components.consistency
            * validated_weights.consistency_weight
        ),
        "experience_contribution": _round6(
            validated_components.experience_confidence
            * validated_weights.experience_weight
        ),
    }
    return CategoryScoreWeightedComponents(
        **contributions,
        positive_total=_round6(math.fsum(contributions.values())),
    )


def calculate_score_penalties(
    performance: CategoryPerformanceRecord,
) -> CategoryScorePenalties:
    validated = _validate_performance(performance)
    counts = validated.counts
    finalized = counts.total_finalized_submissions
    if finalized == 0:
        return CategoryScorePenalties(
            duplicate_penalty=0,
            out_of_scope_penalty=0,
            insufficient_evidence_penalty=0,
            rejected_penalty=0,
            unsafe_penalty=0,
            unsupported_penalty=0,
            total_penalty=0,
        )

    # v0 records did not distinguish spam from root-cause duplicate outcomes.
    # Preserve their historical fallback while precise v1 counters take priority.
    duplicate_penalty_source = (
        counts.submission_duplicate_spam
        if counts.submission_duplicate_spam > 0
        else counts.duplicate_submissions
    )
    duplicate_penalty = _rate_penalty(
        duplicate_penalty_source,
        finalized,
        multiplier=0.10,
        maximum=0.10,
    )
    out_of_scope_penalty = _rate_penalty(
        counts.out_of_scope_submissions,
        finalized,
        multiplier=0.15,
        maximum=0.15,
    )
    insufficient_evidence_penalty = _rate_penalty(
        counts.insufficient_evidence_submissions,
        finalized,
        multiplier=0.10,
        maximum=0.10,
    )
    rejected_penalty = _rate_penalty(
        counts.rejected_submissions,
        finalized,
        multiplier=0.12,
        maximum=0.12,
    )
    unsafe_penalty = _rate_penalty(
        counts.unsafe_submissions,
        finalized,
        multiplier=0.30,
        maximum=0.30,
    )
    if counts.unsafe_submissions > 0:
        unsafe_penalty = _round6(max(unsafe_penalty, 0.10))
    unsupported_penalty = 0.0
    total_penalty = _round6(
        min(
            math.fsum(
                (
                    duplicate_penalty,
                    out_of_scope_penalty,
                    insufficient_evidence_penalty,
                    rejected_penalty,
                    unsafe_penalty,
                    unsupported_penalty,
                )
            ),
            0.60,
        )
    )
    return CategoryScorePenalties(
        duplicate_penalty=duplicate_penalty,
        out_of_scope_penalty=out_of_scope_penalty,
        insufficient_evidence_penalty=insufficient_evidence_penalty,
        rejected_penalty=rejected_penalty,
        unsafe_penalty=unsafe_penalty,
        unsupported_penalty=unsupported_penalty,
        total_penalty=total_penalty,
    )


def calculate_raw_score(
    weighted_components: CategoryScoreWeightedComponents,
    penalties: CategoryScorePenalties,
) -> tuple[float, float]:
    before_penalties = _round6(weighted_components.positive_total)
    after_penalties = _round6(
        _clamp01(before_penalties - penalties.total_penalty)
    )
    return before_penalties, after_penalties


def apply_confidence_adjustment(
    raw_score_after_penalties: float,
    experience_confidence: float,
    neutral_baseline: float = CATEGORY_SCORE_NEUTRAL_BASELINE,
) -> float:
    for value, label in (
        (raw_score_after_penalties, "Raw score"),
        (experience_confidence, "Experience confidence"),
        (neutral_baseline, "Neutral baseline"),
    ):
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise CategoryScoreCalculationError(
                f"{label} must be between 0 and 1"
            )
    adjusted = float(neutral_baseline) + float(
        experience_confidence
    ) * (float(raw_score_after_penalties) - float(neutral_baseline))
    return _round6(_clamp01(adjusted))


def determine_category_score_band(
    category_score: float,
    experience_confidence: float,
) -> CategoryScoreBand:
    if not 0 <= category_score <= 1 or not 0 <= experience_confidence <= 1:
        raise CategoryScoreCalculationError(
            "Score and confidence must be between 0 and 1"
        )
    if experience_confidence < 0.30:
        return CategoryScoreBand.INSUFFICIENT_DATA
    if category_score < 0.40:
        return CategoryScoreBand.WEAK
    if category_score < 0.60:
        return CategoryScoreBand.DEVELOPING
    if category_score < 0.80:
        return CategoryScoreBand.STRONG
    return CategoryScoreBand.EXPERT


def build_category_score_explanation(
    performance: CategoryPerformanceRecord,
    components: CategoryScoreComponents,
    penalties: CategoryScorePenalties,
    category_score: float,
    score_band: CategoryScoreBand,
) -> list[str]:
    counts = performance.counts
    stats = performance.contribution_stats
    accepted = (
        counts.accepted_unique_submissions
        + counts.accepted_independent_duplicate_submissions
    )
    independent_duplicate = counts.accepted_independent_duplicate_submissions
    duplicate = counts.submission_duplicate_spam
    confidence_text = (
        "full experience confidence"
        if components.experience_confidence == 1
        else "experience confidence"
    )
    if accepted == 0:
        consistency_source = "No accepted unique submissions"
    elif accepted == 1:
        consistency_source = "One accepted unique submission"
    elif (
        stats.accepted_minimum_contribution_score is None
        or stats.accepted_maximum_contribution_score is None
    ):
        consistency_source = (
            "Legacy accepted-score statistics without accepted-only bounds"
        )
    else:
        consistency_source = (
            "Accepted contribution scores ranging from "
            f"{stats.accepted_minimum_contribution_score:.6f} to "
            f"{stats.accepted_maximum_contribution_score:.6f}"
        )
    return [
        (
            f"{accepted} of {counts.total_finalized_submissions} finalized "
            "submissions were accepted and unique, producing precision "
            f"{components.precision:.6f}."
        ),
        (
            f"{counts.reproduced_submissions} of "
            f"{counts.reproduction_attempts} finalized reproduction attempts "
            "succeeded, producing reproduction rate "
            f"{components.reproduction_rate:.6f}."
        ),
        (
            f"{counts.accepted_unique_submissions} accepted unique submissions, "
            f"{independent_duplicate} valid independent duplicates, and "
            f"{counts.duplicate_submissions} legacy duplicate outcomes produced "
            "uniqueness rate "
            f"{components.uniqueness_rate:.6f}."
        ),
        (
            "Accepted findings had average contribution score "
            f"{stats.accepted_average_contribution_score:.6f}, producing "
            f"quality component {components.contribution_quality:.6f}."
        ),
        (
            f"{consistency_source} produced consistency component "
            f"{components.consistency:.6f}."
        ),
        (
            f"{counts.total_finalized_submissions} finalized submissions "
            f"produced {confidence_text} "
            f"{components.experience_confidence:.6f}."
        ),
        (
            f"{duplicate} submission-spam duplicates plus out-of-scope, "
            "insufficient-evidence, rejected, unsafe, "
            "and unsupported outcomes produced penalties "
            f"{penalties.duplicate_penalty:.6f}, "
            f"{penalties.out_of_scope_penalty:.6f}, "
            f"{penalties.insufficient_evidence_penalty:.6f}, "
            f"{penalties.rejected_penalty:.6f}, "
            f"{penalties.unsafe_penalty:.6f}, and "
            f"{penalties.unsupported_penalty:.6f}, for total penalty "
            f"{penalties.total_penalty:.6f}."
        ),
        (
            f"The final category score is {category_score:.6f} with score "
            f"band {score_band.value}."
        ),
    ]


def build_category_score_source_payload(
    performance: CategoryPerformanceRecord,
    scoring_version: str,
    weights: CategoryScoreWeights,
) -> dict[str, Any]:
    validated = _validate_performance(performance)
    validated_weights = CategoryScoreWeights.model_validate(weights.model_dump())
    return {
        "performance_id": validated.performance_id,
        "performance_version": validated.performance_version,
        "node_id": validated.node_id,
        "category": validated.category.value,
        "performance_source_fingerprint": validated.source_fingerprint,
        "counts": validated.counts.model_dump(mode="json"),
        "contribution_statistics": (
            validated.contribution_stats.model_dump(mode="json")
        ),
        "scoring_version": scoring_version,
        "weights": validated_weights.model_dump(mode="json"),
        "neutral_baseline": CATEGORY_SCORE_NEUTRAL_BASELINE,
        "penalty_formula_version": PENALTY_FORMULA_VERSION,
        "confidence_formula_version": CONFIDENCE_FORMULA_VERSION,
        "consistency_formula_version": CONSISTENCY_FORMULA_VERSION,
    }


def compute_category_score_source_fingerprint(
    performance_or_payload: CategoryPerformanceRecord | dict[str, Any],
    scoring_version: str = CATEGORY_SCORE_SCORING_VERSION,
    weights: CategoryScoreWeights | None = None,
) -> str:
    if isinstance(performance_or_payload, CategoryPerformanceRecord):
        payload = build_category_score_source_payload(
            performance_or_payload,
            scoring_version,
            weights or CategoryScoreWeights(),
        )
    else:
        payload = performance_or_payload
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def calculate_category_score(
    performance: CategoryPerformanceRecord,
) -> CategoryScoreRecord:
    validated = _validate_performance(performance)
    weights = CategoryScoreWeights()
    components = calculate_score_components(validated)
    weighted_components = calculate_weighted_components(components, weights)
    penalties = calculate_score_penalties(validated)
    raw_before, raw_after = calculate_raw_score(
        weighted_components,
        penalties,
    )
    confidence_adjusted = apply_confidence_adjustment(
        raw_after,
        components.experience_confidence,
    )
    category_score = _round6(_clamp01(confidence_adjusted))
    score_band = determine_category_score_band(
        category_score,
        components.experience_confidence,
    )
    explanation = build_category_score_explanation(
        validated,
        components,
        penalties,
        category_score,
        score_band,
    )
    source_fingerprint = compute_category_score_source_fingerprint(
        validated,
        CATEGORY_SCORE_SCORING_VERSION,
        weights,
    )
    now = _utc_now()
    try:
        return CategoryScoreRecord(
            score_id=build_category_score_id(
                validated.node_id,
                validated.category,
            ),
            scoring_version=CATEGORY_SCORE_SCORING_VERSION,
            node_id=validated.node_id,
            category=validated.category,
            performance_id=validated.performance_id,
            performance_source_fingerprint=validated.source_fingerprint,
            components=components,
            weights=weights,
            weighted_components=weighted_components,
            penalties=penalties,
            raw_score_before_penalties=raw_before,
            raw_score_after_penalties=raw_after,
            neutral_baseline=CATEGORY_SCORE_NEUTRAL_BASELINE,
            confidence_adjusted_score=confidence_adjusted,
            category_score=category_score,
            score_band=score_band,
            finalized_submissions=(
                validated.counts.total_finalized_submissions
            ),
            accepted_unique_submissions=(
                validated.counts.accepted_unique_submissions
            ),
            source_fingerprint=source_fingerprint,
            explanation=explanation,
            calculated_at=now,
            created_at=now,
            updated_at=now,
        )
    except ValidationError as exc:
        raise CategoryScoreCalculationError(
            "Unable to construct a valid category-score record"
        ) from exc


def rebuild_category_score(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
    rebuild_performance: bool = False,
) -> CategoryScoreRebuildResult:
    _load_required_node(protocol_data_root, node_id)
    normalized = _normalize_service_category(category)
    if rebuild_performance:
        try:
            performance = rebuild_category_performance(
                protocol_data_root,
                node_id,
                normalized,
            )
        except CategoryPerformanceNodeNotFoundError as exc:
            raise CategoryScoreNodeNotFoundError("Node not found") from exc
    else:
        try:
            performance = load_category_performance(
                protocol_data_root,
                node_id,
                normalized,
            )
        except CategoryPerformanceStorageError as exc:
            raise CategoryScoreCalculationError(
                "Stored category-performance record is malformed"
            ) from exc
    if performance is None:
        raise CategoryScorePerformanceNotFoundError(
            "Category-performance record not found"
        )
    if (
        performance.node_id != node_id
        or performance.category.value != normalized
    ):
        raise CategoryScoreInputMismatchError(
            "Category-performance identity does not match the requested score"
        )

    candidate = calculate_category_score(performance)
    existing = load_category_score(protocol_data_root, node_id, normalized)
    previous_fingerprint = (
        existing.source_fingerprint if existing is not None else None
    )
    if (
        existing is not None
        and existing.source_fingerprint == candidate.source_fingerprint
    ):
        return CategoryScoreRebuildResult(
            node_id=node_id,
            category=normalized,
            status=CategoryScoreProcessingStatus.UNCHANGED,
            previous_source_fingerprint=previous_fingerprint,
            current_source_fingerprint=existing.source_fingerprint,
            record=existing,
        )

    now = _utc_now()
    candidate = CategoryScoreRecord.model_validate(
        {
            **candidate.model_dump(),
            "score_id": (
                existing.score_id
                if existing is not None
                else candidate.score_id
            ),
            "created_at": (
                existing.created_at if existing is not None else now
            ),
            "calculated_at": now,
            "updated_at": now,
        }
    )
    saved = save_category_score(protocol_data_root, candidate)
    return CategoryScoreRebuildResult(
        node_id=node_id,
        category=normalized,
        status=CategoryScoreProcessingStatus.CALCULATED,
        previous_source_fingerprint=previous_fingerprint,
        current_source_fingerprint=saved.source_fingerprint,
        record=saved,
    )


def rebuild_node_category_scores(
    protocol_data_root: Path,
    node_id: str,
    rebuild_performance: bool = False,
) -> list[CategoryScoreRebuildResult]:
    _load_required_node(protocol_data_root, node_id)
    if rebuild_performance:
        try:
            rebuild_node_category_performance(protocol_data_root, node_id)
        except CategoryPerformanceNodeNotFoundError as exc:
            raise CategoryScoreNodeNotFoundError("Node not found") from exc
    try:
        performances = get_node_category_performance(
            protocol_data_root,
            node_id,
        )
    except CategoryPerformanceNodeNotFoundError as exc:
        raise CategoryScoreNodeNotFoundError("Node not found") from exc
    return [
        rebuild_category_score(
            protocol_data_root,
            node_id,
            performance.category,
            rebuild_performance=False,
        )
        for performance in sorted(
            performances,
            key=lambda item: item.category.value,
        )
    ]


def rebuild_all_category_scores(
    protocol_data_root: Path,
    rebuild_performance: bool = False,
) -> CategoryScoreBatchResult:
    if rebuild_performance:
        rebuild_all_category_performance(protocol_data_root)
    performances = list_category_performance(protocol_data_root)
    results: list[CategoryScoreRebuildResult] = []
    errors: list[str] = []
    for performance in sorted(
        performances,
        key=lambda item: (item.node_id, item.category.value),
    ):
        try:
            results.append(
                rebuild_category_score(
                    protocol_data_root,
                    performance.node_id,
                    performance.category,
                    rebuild_performance=False,
                )
            )
        except CategoryScoreServiceError as exc:
            errors.append(
                f"{performance.node_id}/{performance.category.value}: {exc}"
            )
    calculated = sum(
        result.status == CategoryScoreProcessingStatus.CALCULATED
        for result in results
    )
    unchanged = sum(
        result.status == CategoryScoreProcessingStatus.UNCHANGED
        for result in results
    )
    return CategoryScoreBatchResult(
        total_records=len(performances),
        calculated_records=calculated,
        unchanged_records=unchanged,
        failed_records=len(errors),
        results=results,
        errors=errors,
    )


def save_category_score(
    protocol_data_root: Path,
    record: CategoryScoreRecord,
) -> CategoryScoreRecord:
    validated = CategoryScoreRecord.model_validate(record.model_dump())
    _load_required_node(protocol_data_root, validated.node_id)
    performance = load_category_performance(
        protocol_data_root,
        validated.node_id,
        validated.category,
    )
    if performance is None:
        raise CategoryScorePerformanceNotFoundError(
            "Category-performance record not found"
        )
    if (
        performance.performance_id != validated.performance_id
        or performance.node_id != validated.node_id
        or performance.category != validated.category
    ):
        raise CategoryScoreInputMismatchError(
            "Score does not match its category-performance record"
        )
    if (
        performance.source_fingerprint
        != validated.performance_source_fingerprint
    ):
        raise CategoryScoreSourceChangedError(
            "Category-performance source changed before score persistence"
        )
    current_source_fingerprint = (
        compute_category_score_source_fingerprint(
            performance,
            validated.scoring_version,
            validated.weights,
        )
    )
    if current_source_fingerprint != validated.source_fingerprint:
        raise CategoryScoreSourceChangedError(
            "Category-score source fingerprint does not match current inputs"
        )

    existing = load_category_score(
        protocol_data_root,
        validated.node_id,
        validated.category,
    )
    values = validated.model_dump()
    if existing is not None:
        values.update(
            {
                "score_id": existing.score_id,
                "node_id": existing.node_id,
                "category": existing.category,
                "created_at": existing.created_at,
            }
        )
    values["updated_at"] = _utc_now()
    saved = CategoryScoreRecord.model_validate(values)
    _write_category_score(protocol_data_root, saved)
    return saved


def load_category_score(
    protocol_data_root: Path,
    node_id: str,
    category: str | FindingCategory,
) -> CategoryScoreRecord | None:
    normalized = _normalize_service_category(category)
    path = get_category_score_path(protocol_data_root, node_id, normalized)
    if not path.exists():
        return None
    if not path.is_file():
        raise CategoryScoreStorageError(
            "Stored category-score record is not a regular file"
        )
    try:
        record = CategoryScoreRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise CategoryScoreStorageError(
            "Stored category-score record is malformed"
        ) from exc
    if record.node_id != node_id or record.category.value != normalized:
        raise CategoryScoreStorageError(
            "Stored category-score identity does not match its path"
        )
    return record


def list_category_scores(
    protocol_data_root: Path,
    node_id: str | None = None,
    category: str | None = None,
    minimum_score: float | None = None,
    minimum_confidence: float | None = None,
    score_band: CategoryScoreBand | None = None,
) -> list[CategoryScoreRecord]:
    if node_id is not None:
        _validate_identifier(node_id, "node")
    normalized = (
        _normalize_service_category(category)
        if category is not None
        else None
    )
    _validate_optional_unit_interval(minimum_score, "Minimum score")
    _validate_optional_unit_interval(
        minimum_confidence,
        "Minimum confidence",
    )
    if score_band is not None and not isinstance(score_band, CategoryScoreBand):
        try:
            score_band = CategoryScoreBand(score_band)
        except (TypeError, ValueError) as exc:
            raise CategoryScoreCalculationError(
                "Invalid category score band"
            ) from exc

    root = get_category_scores_root(protocol_data_root)
    if not root.exists():
        return []
    records: list[CategoryScoreRecord] = []
    for path in sorted(root.glob("*/*.json")):
        record = load_category_score(
            protocol_data_root,
            path.parent.name,
            path.stem,
        )
        if record is None:
            continue
        if node_id is not None and record.node_id != node_id:
            continue
        if normalized is not None and record.category.value != normalized:
            continue
        if minimum_score is not None and record.category_score < minimum_score:
            continue
        if (
            minimum_confidence is not None
            and record.components.experience_confidence
            < minimum_confidence
        ):
            continue
        if score_band is not None and record.score_band != score_band:
            continue
        records.append(record)
    return sorted(
        records,
        key=lambda record: (
            -record.category_score,
            -record.components.experience_confidence,
            -record.finalized_submissions,
            record.node_id,
            record.category.value,
        ),
    )


def get_node_category_scores(
    protocol_data_root: Path,
    node_id: str,
) -> list[CategoryScoreRecord]:
    _load_required_node(protocol_data_root, node_id)
    return list_category_scores(protocol_data_root, node_id=node_id)


def list_scores_for_subnet(
    protocol_data_root: Path,
    subnet_id: str,
) -> list[CategoryScoreRecord]:
    subnet = load_subnet(protocol_data_root, subnet_id)
    if subnet is None:
        from app.services.subnet_registry_service import SubnetNotFoundError

        raise SubnetNotFoundError("Subnet not found")
    return list_category_scores(
        protocol_data_root,
        category=subnet.category.value,
    )


def _validate_performance(
    performance: CategoryPerformanceRecord,
) -> CategoryPerformanceRecord:
    try:
        return CategoryPerformanceRecord.model_validate(
            performance.model_dump()
        )
    except (AttributeError, ValidationError) as exc:
        raise CategoryScoreCalculationError(
            "Invalid category-performance input"
        ) from exc


def _load_required_node(
    protocol_data_root: Path,
    node_id: str,
) -> NodeRecord:
    _validate_identifier(node_id, "node")
    try:
        node = load_node(protocol_data_root, node_id)
    except InvalidNodeIdentifierError as exc:
        raise InvalidCategoryScoreIdentifierError(
            "Invalid node identifier"
        ) from exc
    if node is None:
        raise CategoryScoreNodeNotFoundError("Node not found")
    return node


def _normalize_service_category(
    category: str | FindingCategory,
) -> str:
    try:
        return normalize_category(category)
    except (TypeError, ValueError) as exc:
        raise CategoryScoreUnsupportedCategoryError(str(exc)) from exc


def _validate_identifier(identifier: str, label: str) -> None:
    if (
        not isinstance(identifier, str)
        or not SAFE_IDENTIFIER.fullmatch(identifier)
        or identifier in {".", ".."}
        or "/" in identifier
        or "\\" in identifier
        or Path(identifier).is_absolute()
    ):
        raise InvalidCategoryScoreIdentifierError(
            f"Invalid {label} identifier"
        )


def _ensure_path_within(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidCategoryScoreIdentifierError(
            "Invalid category-score storage path"
        ) from exc


def _validate_optional_unit_interval(
    value: float | None,
    label: str,
) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= float(value) <= 1
    ):
        raise CategoryScoreCalculationError(
            f"{label} must be between 0 and 1"
        )


def _rate_penalty(
    count: int,
    finalized: int,
    multiplier: float,
    maximum: float,
) -> float:
    if finalized == 0:
        return 0.0
    return _round6(min((count / finalized) * multiplier, maximum))


def _write_category_score(
    protocol_data_root: Path,
    record: CategoryScoreRecord,
) -> None:
    output_path = get_category_score_path(
        protocol_data_root,
        record.node_id,
        record.category,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        record.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=".category-score-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(output_path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise CategoryScoreStorageError(
            "Unable to persist category-score record"
        ) from exc


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _round6(value: float) -> float:
    return round(float(value), 6)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
