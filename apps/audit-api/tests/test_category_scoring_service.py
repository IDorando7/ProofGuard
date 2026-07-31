import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.schemas.category_performance import (
    CategoryPerformanceContributionStats,
    CategoryPerformanceCounts,
    CategoryPerformanceRecord,
)
from app.schemas.category_score import CategoryScoreBand, CategoryScoreWeights
from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.subnet import SubnetCreate
from app.services.category_performance_service import (
    get_category_performance_path,
    load_category_performance,
    save_category_performance,
)
from app.services.category_scoring_service import (
    CategoryScorePerformanceNotFoundError,
    InvalidCategoryScoreIdentifierError,
    apply_confidence_adjustment,
    build_category_score_source_payload,
    calculate_category_score,
    calculate_consistency,
    calculate_contribution_quality,
    calculate_experience_confidence,
    calculate_precision,
    calculate_reproduction_rate,
    calculate_score_penalties,
    calculate_uniqueness_rate,
    compute_category_score_source_fingerprint,
    determine_category_score_band,
    get_category_score_path,
    list_category_scores,
    list_scores_for_subnet,
    load_category_score,
    rebuild_all_category_scores,
    rebuild_category_score,
    rebuild_node_category_scores,
)
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    load_node,
)
from app.services.subnet_registry_service import (
    create_subnet,
    load_subnet,
)


NOW = datetime.now(timezone.utc)


def _performance(
    node_id="node-1",
    category="access_control",
    accepted_scores=None,
    rejected=0,
    duplicates=0,
    out_of_scope=0,
    insufficient=0,
    unsafe=0,
    unsupported=0,
    reproduction_attempts=None,
    reproduced=None,
    fingerprint_seed=None,
):
    accepted_scores = list(accepted_scores or [])
    accepted = len(accepted_scores)
    finalized = (
        accepted
        + rejected
        + duplicates
        + out_of_scope
        + insufficient
        + unsafe
        + unsupported
    )
    reproduction_attempts = (
        finalized if reproduction_attempts is None else reproduction_attempts
    )
    reproduced = accepted if reproduced is None else reproduced
    all_scores = accepted_scores + [0.0] * (finalized - accepted)
    accepted_total = round(sum(accepted_scores), 6)
    total = round(sum(all_scores), 6)
    source_ids = [f"source-{index:03d}" for index in range(finalized)]
    fingerprint_payload = {
        "node_id": node_id,
        "category": category,
        "accepted_scores": accepted_scores,
        "rejected": rejected,
        "duplicates": duplicates,
        "out_of_scope": out_of_scope,
        "insufficient": insufficient,
        "unsafe": unsafe,
        "unsupported": unsupported,
        "seed": fingerprint_seed,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode()
    ).hexdigest()
    return CategoryPerformanceRecord(
        performance_id=f"performance_{node_id}_{category}",
        performance_version="category_performance_v0",
        node_id=node_id,
        category=category,
        counts=CategoryPerformanceCounts(
            total_finalized_submissions=finalized,
            accepted_unique_submissions=accepted,
            rejected_submissions=rejected,
            duplicate_submissions=duplicates,
            out_of_scope_submissions=out_of_scope,
            insufficient_evidence_submissions=insufficient,
            unsafe_submissions=unsafe,
            unsupported_submissions=unsupported,
            reproduced_submissions=reproduced,
            reproduction_attempts=reproduction_attempts,
            reward_eligible_submissions=accepted,
            rewarded_submissions=0,
        ),
        contribution_stats=CategoryPerformanceContributionStats(
            total_contribution_score=total,
            average_contribution_score=(
                round(total / finalized, 6) if finalized else 0
            ),
            minimum_contribution_score=min(all_scores) if all_scores else None,
            maximum_contribution_score=max(all_scores) if all_scores else None,
            accepted_contribution_score_total=accepted_total,
            accepted_average_contribution_score=(
                round(accepted_total / accepted, 6) if accepted else 0
            ),
            accepted_minimum_contribution_score=(
                min(accepted_scores) if accepted_scores else None
            ),
            accepted_maximum_contribution_score=(
                max(accepted_scores) if accepted_scores else None
            ),
        ),
        source_event_ids=source_ids,
        source_submission_ids=[
            f"submission-{index:03d}" for index in range(finalized)
        ],
        source_fingerprint=fingerprint,
        first_activity_at=NOW if finalized else None,
        last_activity_at=NOW if finalized else None,
        rebuilt_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _node(root, name="score-node"):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control", "reentrancy"],
        ),
    )


def _store_performance(root, node_id, category="access_control", **kwargs):
    return save_category_performance(
        root,
        _performance(node_id=node_id, category=category, **kwargs),
    )


def test_component_formulas_and_zero_denominators():
    empty = _performance()
    assert calculate_precision(empty) == 0
    assert calculate_reproduction_rate(empty) == 0
    assert calculate_uniqueness_rate(empty) == 0
    assert calculate_contribution_quality(empty) == 0
    assert calculate_consistency(empty) == 0

    example = _performance(
        accepted_scores=[80] * 7,
        duplicates=1,
        out_of_scope=1,
        insufficient=1,
        reproduction_attempts=8,
        reproduced=7,
    )
    assert calculate_precision(example) == 0.7
    assert calculate_reproduction_rate(example) == 0.875
    assert calculate_uniqueness_rate(example) == 0.875
    assert calculate_contribution_quality(example) == 0.8


def test_consistency_uses_accepted_only_range():
    assert calculate_consistency(_performance(accepted_scores=[82])) == 0.5
    stable = _performance(accepted_scores=[80, 80, 80])
    variable = _performance(accepted_scores=[20, 50, 100])
    assert calculate_consistency(stable) == 1
    assert calculate_consistency(variable) == 0.2

    legacy_values = stable.model_dump()
    legacy_values["contribution_stats"][
        "accepted_minimum_contribution_score"
    ] = None
    legacy_values["contribution_stats"][
        "accepted_maximum_contribution_score"
    ] = None
    legacy = CategoryPerformanceRecord.model_validate(legacy_values)
    assert calculate_consistency(legacy) == 0.5


@pytest.mark.parametrize(
    ("finalized", "expected"),
    [(0, 0), (1, 0.1), (5, 0.5), (10, 1), (20, 1)],
)
def test_experience_confidence_formula(finalized, expected):
    assert calculate_experience_confidence(finalized) == expected


def test_penalty_rates_unsafe_floor_and_unsupported_behavior():
    duplicate = calculate_score_penalties(
        _performance(accepted_scores=[80] * 9, duplicates=1)
    )
    unsafe = calculate_score_penalties(
        _performance(accepted_scores=[80] * 9, unsafe=1)
    )
    all_unsafe = calculate_score_penalties(_performance(unsafe=10))
    unsupported = calculate_score_penalties(_performance(unsupported=10))
    assert duplicate.duplicate_penalty == 0.01
    assert unsafe.unsafe_penalty == 0.1
    assert unsafe.unsafe_penalty > duplicate.duplicate_penalty
    assert all_unsafe.unsafe_penalty == 0.3
    assert unsupported.unsupported_penalty == 0
    assert unsupported.total_penalty == 0


@pytest.mark.parametrize(
    ("raw", "confidence", "expected"),
    [
        (0.95, 0, 0.5),
        (0.95, 0.1, 0.545),
        (0.8, 0.5, 0.65),
        (0.8, 1, 0.8),
        (0.1, 0.5, 0.3),
    ],
)
def test_confidence_adjustment(raw, confidence, expected):
    assert apply_confidence_adjustment(raw, confidence) == expected


@pytest.mark.parametrize(
    ("score", "confidence", "expected"),
    [
        (1, 0.29, "insufficient_data"),
        (0.39, 0.3, "weak"),
        (0.4, 0.3, "developing"),
        (0.59, 1, "developing"),
        (0.6, 1, "strong"),
        (0.79, 1, "strong"),
        (0.8, 1, "expert"),
        (1, 1, "expert"),
    ],
)
def test_score_band_boundaries(score, confidence, expected):
    assert determine_category_score_band(score, confidence).value == expected


def test_calculation_quality_penalties_confidence_and_explanation():
    perfect_one = calculate_category_score(
        _performance(accepted_scores=[100], reproduction_attempts=1, reproduced=1)
    )
    perfect_ten = calculate_category_score(
        _performance(accepted_scores=[100] * 10)
    )
    duplicate_heavy = calculate_category_score(
        _performance(accepted_scores=[100] * 5, duplicates=5)
    )
    unsafe = calculate_category_score(
        _performance(accepted_scores=[100] * 9, unsafe=1)
    )
    assert perfect_one.score_band == CategoryScoreBand.INSUFFICIENT_DATA
    assert perfect_one.category_score < 0.60
    assert perfect_ten.category_score == 1
    assert perfect_ten.score_band == CategoryScoreBand.EXPERT
    assert duplicate_heavy.category_score < perfect_ten.category_score
    assert unsafe.penalties.unsafe_penalty == 0.1
    assert 0 <= unsafe.category_score <= 1
    assert f"{unsafe.category_score:.6f}" in unsafe.explanation[-1]
    assert any("precision" in line for line in unsafe.explanation)


def test_fingerprint_is_canonical_deterministic_and_category_isolated():
    access = _performance(accepted_scores=[80], category="access_control")
    same = deepcopy(access)
    reentrancy = _performance(
        accepted_scores=[80],
        category="reentrancy",
    )
    payload = build_category_score_source_payload(
        access,
        "category_score_v0",
        CategoryScoreWeights(),
    )
    assert compute_category_score_source_fingerprint(
        payload
    ) == compute_category_score_source_fingerprint(
        dict(reversed(list(payload.items())))
    )
    assert calculate_category_score(access).source_fingerprint == (
        calculate_category_score(same).source_fingerprint
    )
    assert calculate_category_score(access).source_fingerprint != (
        calculate_category_score(reentrancy).source_fingerprint
    )


def test_rebuild_storage_idempotency_and_source_change(tmp_path):
    root = tmp_path / "protocol"
    node = _node(root)
    performance = _store_performance(
        root,
        node.node_id,
        accepted_scores=[80, 90, 100],
    )
    performance_snapshot = deepcopy(performance)
    node_snapshot = deepcopy(load_node(root, node.node_id))

    first = rebuild_category_score(root, node.node_id, "access_control")
    path = get_category_score_path(root, node.node_id, "access_control")
    first_mtime = path.stat().st_mtime_ns
    second = rebuild_category_score(root, node.node_id, "Access Control")
    assert first.status.value == "calculated"
    assert second.status.value == "unchanged"
    assert second.record == first.record
    assert path.stat().st_mtime_ns == first_mtime
    assert load_category_score(root, node.node_id, "access_control") == first.record
    assert path == root / "category-scores" / node.node_id / "access_control.json"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n  ")
    assert text.endswith("\n")
    for forbidden in ("/home/", "poc", "private_key", "secret"):
        assert forbidden not in text.lower()
    assert load_category_performance(
        root,
        node.node_id,
        "access_control",
    ) == performance_snapshot
    assert load_node(root, node.node_id) == node_snapshot

    changed = performance.model_copy(
        update={"source_fingerprint": "f" * 64}
    )
    save_category_performance(root, changed)
    recalculated = rebuild_category_score(
        root,
        node.node_id,
        "access_control",
    )
    assert recalculated.status.value == "calculated"
    assert recalculated.record.score_id == first.record.score_id
    assert recalculated.record.created_at == first.record.created_at
    assert recalculated.current_source_fingerprint != (
        first.current_source_fingerprint
    )


def test_missing_performance_and_unsafe_paths_fail(tmp_path):
    root = tmp_path / "protocol"
    node = _node(root)
    with pytest.raises(CategoryScorePerformanceNotFoundError):
        rebuild_category_score(root, node.node_id, "access_control")
    assert load_category_score(root, node.node_id, "access_control") is None
    for unsafe in ("../node", "node/name", r"node\\name", "/absolute", ".."):
        with pytest.raises(InvalidCategoryScoreIdentifierError):
            get_category_score_path(root, unsafe, "access_control")
    assert not (tmp_path / "node").exists()


def test_node_global_filters_sorting_and_subnet_are_read_only(tmp_path):
    root = tmp_path / "protocol"
    first_node = _node(root, "first-score-node")
    second_node = _node(root, "second-score-node")
    _store_performance(
        root,
        first_node.node_id,
        accepted_scores=[100] * 10,
    )
    _store_performance(
        root,
        first_node.node_id,
        category="reentrancy",
        accepted_scores=[50] * 5,
    )
    _store_performance(
        root,
        second_node.node_id,
        accepted_scores=[80] * 8,
    )
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    subnet_snapshot = deepcopy(load_subnet(root, subnet.subnet_id))

    node_results = rebuild_node_category_scores(root, first_node.node_id)
    assert [result.category for result in node_results] == [
        "access_control",
        "reentrancy",
    ]
    batch = rebuild_all_category_scores(root)
    assert batch.total_records == 3
    assert batch.calculated_records == 1
    assert batch.unchanged_records == 2
    scores = list_category_scores(root)
    assert scores == sorted(
        scores,
        key=lambda record: (
            -record.category_score,
            -record.components.experience_confidence,
            -record.finalized_submissions,
            record.node_id,
            record.category.value,
        ),
    )
    assert len(list_category_scores(root, node_id=first_node.node_id)) == 2
    assert len(list_category_scores(root, category="reentrancy")) == 1
    assert all(
        score.category_score >= 0.8
        for score in list_category_scores(root, minimum_score=0.8)
    )
    assert all(
        score.components.experience_confidence >= 0.8
        for score in list_category_scores(root, minimum_confidence=0.8)
    )
    experts = list_category_scores(root, score_band=CategoryScoreBand.EXPERT)
    assert all(score.score_band == CategoryScoreBand.EXPERT for score in experts)
    subnet_scores = list_scores_for_subnet(root, subnet.subnet_id)
    assert all(score.category.value == "access_control" for score in subnet_scores)
    assert load_subnet(root, subnet.subnet_id) == subnet_snapshot
    assert not list((root / "subnets").glob("*/members/*.json"))


@pytest.mark.parametrize("status", ["inactive", "banned"])
def test_disabled_node_history_can_still_be_scored(tmp_path, status):
    root = tmp_path / "protocol"
    node = _node(root)
    _store_performance(root, node.node_id, accepted_scores=[90] * 5)
    change_node_status(
        root,
        node.node_id,
        NodeStatusChangeRequest(
            status=status,
            reason="Historical category-score test.",
        ),
    )
    result = rebuild_category_score(root, node.node_id, "access_control")
    assert result.record.finalized_submissions == 5
    assert get_category_performance_path(
        root,
        node.node_id,
        "access_control",
    ).is_file()
