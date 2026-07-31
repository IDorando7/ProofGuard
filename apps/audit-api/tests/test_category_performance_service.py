import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.contribution import (
    ContributionComponents,
    ContributionEligibilityStatus,
    ContributionScoreRecord,
)
from app.schemas.finding import Finding
from app.schemas.node import (
    NodeCreate,
    NodeRecord,
    NodeStatistics,
    NodeStatus,
    NodeStatusChangeRequest,
)
from app.schemas.reproduction import ReproductionStatus
from app.schemas.reputation import (
    ReputationEvent,
    ReputationEventApplicationStatus,
    ReputationEventType,
)
from app.schemas.reward import RewardEvent, RewardEventStatus, RewardUnit
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.schemas.validation import ValidationEvidence, ValidationStatus
from app.services.category_performance_service import (
    CategoryPerformanceCategoryMismatchError,
    CategoryPerformanceInputMismatchError,
    CategoryPerformanceNodeNotFoundError,
    CategoryPerformanceSourceBundle,
    CategoryPerformanceSourceNotFoundError,
    CategoryPerformanceUnknownEventTypeError,
    InvalidCategoryPerformanceIdentifierError,
    aggregate_category_performance,
    build_category_performance_source_payload,
    build_category_performance_id,
    collect_node_category_events,
    compute_category_performance_source_fingerprint,
    determine_category_performance_outcome,
    get_category_performance_path,
    list_category_performance,
    load_category_performance,
    rebuild_all_category_performance,
    rebuild_category_performance,
    rebuild_node_category_performance,
    validate_category_event_sources,
)
from app.services.contribution_scoring_service import (
    calculate_contribution_for_submission,
    load_contribution_score,
    save_contribution_score,
)
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    load_node,
)
from app.services.reproduction_service import (
    create_initial_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.reputation_service import (
    build_reputation_event,
    load_reputation_event_by_submission,
    process_submission_reputation,
    save_prepared_event_exclusively,
)
from app.services.reward_service import save_reward_event_exclusively
from app.services.submission_service import (
    create_submission,
    load_submission,
    save_submission,
    update_submission_status,
)
from app.services.subnet_registry_service import (
    create_subnet,
    load_subnet,
)
from app.schemas.subnet import SubnetCreate
from app.services.validation_service import (
    create_validation_decision,
    load_validation_decision,
)


NOW = datetime.now(timezone.utc)


def _node(node_id="node-1"):
    return NodeRecord(
        node_id=node_id,
        node_type="agent",
        display_name="node-one",
        operator_id="operator-one",
        public_key=None,
        supported_categories=["access_control", "reentrancy"],
        description=None,
        status=NodeStatus.ACTIVE,
        reputation_score=0.5,
        statistics=NodeStatistics(),
        status_reason="Node registered.",
        created_at=NOW,
        updated_at=NOW,
        status_updated_at=NOW,
    )


def _contribution(submission_id, score, eligible):
    return ContributionScoreRecord(
        score_id=f"score-{submission_id}",
        scoring_version="contribution_v0",
        submission_id=submission_id,
        project_id=f"project-{submission_id}",
        finding_id=f"finding-{submission_id}",
        node_id="node-1",
        validation_id=f"validation-{submission_id}",
        reproduction_id=f"reproduction-{submission_id}",
        total_score=score,
        eligibility_status=(
            ContributionEligibilityStatus.ELIGIBLE
            if eligible
            else ContributionEligibilityStatus.INELIGIBLE
        ),
        eligible_for_reward=eligible,
        eligibility_reasons=["Synthetic aggregate fixture."],
        components=ContributionComponents(),
        signals=[],
        reason="Synthetic aggregate fixture.",
        created_at=NOW,
        updated_at=NOW,
    )


def _event(
    submission_id,
    event_type,
    score=0,
    reproduction_status=None,
    category="access_control",
    applied_at=NOW,
):
    return ReputationEvent.model_construct(
        event_id=f"event-{submission_id}",
        reputation_version="reputation_v0",
        application_status=ReputationEventApplicationStatus.APPLIED,
        node_id="node-1",
        submission_id=submission_id,
        project_id=f"project-{submission_id}",
        finding_id=f"finding-{submission_id}",
        category=category,
        validation_id=f"validation-{submission_id}",
        reproduction_id=(
            f"reproduction-{submission_id}"
            if reproduction_status is not None
            else None
        ),
        contribution_score_id=f"score-{submission_id}",
        validation_status={
            "accepted_contribution": "accepted",
            "duplicate_finding": "duplicate",
            "rejected_finding": "rejected",
            "out_of_scope_finding": "out_of_scope",
            "insufficient_evidence": "insufficient_evidence",
            "unsafe_submission": "unsafe_poc",
            "unsupported_submission": "unsupported",
        }.get(str(event_type), "unknown"),
        reproduction_status=reproduction_status,
        contribution_score=score,
        contribution_eligibility_status=(
            "eligible" if event_type == "accepted_contribution" else "ineligible"
        ),
        normalized_severity="High",
        event_type=ReputationEventType(event_type),
        source_fingerprint="a" * 64,
        created_at=applied_at,
        applied_at=applied_at,
        updated_at=applied_at,
    )


def _bundle(
    submission_id,
    event_type,
    score=0,
    reproduction_status=None,
    category="access_control",
    applied_at=NOW,
    rewarded=False,
):
    eligible = event_type == "accepted_contribution"
    event = _event(
        submission_id,
        event_type,
        score=score,
        reproduction_status=reproduction_status,
        category=category,
        applied_at=applied_at,
    )
    contribution = _contribution(submission_id, score, eligible)
    reward = None
    if rewarded:
        reward = RewardEvent.model_construct(
            reward_event_id=f"reward-{submission_id}",
            event_status=RewardEventStatus.FINALIZED,
        )
    return CategoryPerformanceSourceBundle(
        reputation_event=event,
        submission=None,
        contribution=contribution,
        validation=None,
        reproduction=None,
        reward_event=reward,
    )


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("accepted_contribution", "accepted_unique"),
        ("duplicate_finding", "duplicate"),
        ("rejected_finding", "rejected"),
        ("out_of_scope_finding", "out_of_scope"),
        ("insufficient_evidence", "insufficient_evidence"),
        ("unsafe_submission", "unsafe"),
        ("unsupported_submission", "unsupported"),
    ],
)
def test_outcome_mapping(event_type, expected):
    assert determine_category_performance_outcome(
        _event("submission-1", event_type)
    ).value == expected


def test_unknown_outcome_fails_clearly():
    event = _event("submission-1", "accepted_contribution")
    event.event_type = "future_event"
    with pytest.raises(CategoryPerformanceUnknownEventTypeError):
        determine_category_performance_outcome(event)


@pytest.mark.parametrize(
    ("event_type", "field"),
    [
        ("accepted_contribution", "accepted_unique_submissions"),
        ("duplicate_finding", "duplicate_submissions"),
        ("rejected_finding", "rejected_submissions"),
        ("out_of_scope_finding", "out_of_scope_submissions"),
        ("insufficient_evidence", "insufficient_evidence_submissions"),
        ("unsafe_submission", "unsafe_submissions"),
        ("unsupported_submission", "unsupported_submissions"),
    ],
)
def test_each_event_increments_exactly_one_primary_outcome(event_type, field):
    record = aggregate_category_performance(
        _node(),
        "access_control",
        [_bundle("submission-1", event_type)],
    )
    counts = record.counts.model_dump()
    primary_fields = [
        name for name in counts
        if name.endswith("_submissions")
        and name not in {
            "total_finalized_submissions",
            "reproduced_submissions",
            "reward_eligible_submissions",
            "rewarded_submissions",
        }
    ]
    assert record.counts.total_finalized_submissions == 1
    assert counts[field] == 1
    assert sum(counts[name] for name in primary_fields) == 1


@pytest.mark.parametrize(
    ("status", "attempts", "reproduced"),
    [
        ("reproduced", 1, 1),
        ("failed", 1, 0),
        ("timeout", 1, 0),
        ("running", 0, 0),
        ("generated", 0, 0),
        ("not_attempted", 0, 0),
        (None, 0, 0),
    ],
)
def test_reproduction_counting_rules(status, attempts, reproduced):
    record = aggregate_category_performance(
        _node(),
        "access_control",
        [_bundle("submission-1", "accepted_contribution", 80, status)],
    )
    assert record.counts.reproduction_attempts == attempts
    assert record.counts.reproduced_submissions == reproduced


def test_reward_eligibility_and_finalized_reward_are_separate():
    eligible_only = aggregate_category_performance(
        _node(),
        "access_control",
        [_bundle("submission-1", "accepted_contribution", 80, "reproduced")],
    )
    rewarded = aggregate_category_performance(
        _node(),
        "access_control",
        [_bundle("submission-1", "accepted_contribution", 80, "reproduced", rewarded=True)],
    )
    assert eligible_only.counts.reward_eligible_submissions == 1
    assert eligible_only.counts.rewarded_submissions == 0
    assert rewarded.counts.rewarded_submissions == 1


def test_score_statistics_activity_and_source_ordering_are_deterministic():
    bundles = [
        _bundle(
            "submission-b",
            "duplicate_finding",
            20,
            "failed",
            applied_at=NOW + timedelta(hours=2),
        ),
        _bundle(
            "submission-a",
            "accepted_contribution",
            80,
            "reproduced",
            applied_at=NOW,
        ),
        _bundle(
            "submission-c",
            "accepted_contribution",
            100,
            "reproduced",
            applied_at=NOW + timedelta(hours=1),
        ),
    ]
    first = aggregate_category_performance(_node(), "access_control", bundles)
    second = aggregate_category_performance(
        _node(), "access_control", list(reversed(bundles))
    )

    assert first.contribution_stats.total_contribution_score == 200
    assert first.contribution_stats.average_contribution_score == 66.666667
    assert first.contribution_stats.minimum_contribution_score == 20
    assert first.contribution_stats.maximum_contribution_score == 100
    assert first.contribution_stats.accepted_contribution_score_total == 180
    assert first.contribution_stats.accepted_average_contribution_score == 90
    assert first.first_activity_at == NOW
    assert first.last_activity_at == NOW + timedelta(hours=2)
    assert first.source_event_ids == sorted(first.source_event_ids)
    assert first.source_submission_ids == sorted(first.source_submission_ids)
    assert first.source_fingerprint == second.source_fingerprint
    assert first.counts == second.counts
    assert first.contribution_stats == second.contribution_stats


def test_duplicate_event_or_submission_is_not_double_counted():
    bundle = _bundle("submission-1", "accepted_contribution", 80, "reproduced")
    duplicate_submission = deepcopy(bundle)
    duplicate_submission.reputation_event.event_id = "event-other"

    exact = aggregate_category_performance(
        _node(), "access_control", [bundle, bundle]
    )
    repeated_submission = aggregate_category_performance(
        _node(), "access_control", [bundle, duplicate_submission]
    )
    assert exact.counts.total_finalized_submissions == 1
    assert repeated_submission.counts.total_finalized_submissions == 1


def test_category_isolation_and_mixed_input_rejection():
    access = aggregate_category_performance(
        _node(),
        "access_control",
        [_bundle("submission-a", "accepted_contribution", 80, "reproduced")],
    )
    reentrancy = aggregate_category_performance(
        _node(),
        "reentrancy",
        [
            _bundle(
                "submission-r",
                "duplicate_finding",
                category="reentrancy",
            )
        ],
    )
    assert access.counts.accepted_unique_submissions == 1
    assert access.counts.duplicate_submissions == 0
    assert reentrancy.counts.accepted_unique_submissions == 0
    assert reentrancy.counts.duplicate_submissions == 1
    with pytest.raises(CategoryPerformanceCategoryMismatchError):
        aggregate_category_performance(
            _node(),
            "access_control",
            [_bundle("submission-r", "duplicate_finding", category="reentrancy")],
        )


def test_source_fingerprint_is_canonical_and_changes_with_source_data():
    event = _event("submission-1", "accepted_contribution", 80, "reproduced")
    contribution = _contribution("submission-1", 80, True)
    payload = build_category_performance_source_payload(
        "node-1",
        "access_control",
        [event],
        [contribution],
        ["reward-1"],
    )
    fingerprint = compute_category_performance_source_fingerprint(payload)
    assert fingerprint == compute_category_performance_source_fingerprint(
        dict(reversed(list(payload.items())))
    )
    changed = deepcopy(payload)
    changed["sources"][0]["contribution_score"] = 81
    assert fingerprint != compute_category_performance_source_fingerprint(changed)


def _finding(project_id, finding_id, category):
    return Finding(
        finding_id=finding_id,
        project_id=project_id,
        title="Missing authorization on protocol operation",
        category=category,
        severity="High",
        confidence=0.9,
        contracts=["src/Protocol.sol"],
        functions=["operate"],
        root_cause="Required authorization check is missing",
        attack_path="Unauthorized caller invokes the operation",
        impact="Protected state can be modified",
        recommended_fix="Add an explicit authorization check.",
        agent_name="category_agent",
    )


def _write_finding(workspace, finding):
    directory = workspace / "findings"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json")], indent=2),
        encoding="utf-8",
    )


def _register_node(root, name="category-node"):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=["access_control", "reentrancy"],
        ),
    )


def _finalize_case(
    root,
    audits,
    node,
    suffix,
    category="access_control",
    validation_status="accepted",
    reproduction_status="reproduced",
    score=None,
    apply_reputation=True,
):
    project_id = f"project-{suffix}"
    finding_id = f"finding-{suffix}"
    workspace = audits / project_id
    finding = _finding(project_id, finding_id, category)
    _write_finding(workspace, finding)
    submission = create_submission(
        root,
        workspace,
        SubmissionCreate(
            project_id=project_id,
            finding_id=finding_id,
            node_id=node.node_id,
        ),
    )
    reproduction = None
    if reproduction_status is not None:
        reproduction = create_initial_reproduction_result(
            project_id, finding_id, workspace
        )
        reproduction = save_reproduction_result(
            reproduction.model_copy(
                update={"status": ReproductionStatus(reproduction_status)}
            ),
            workspace,
        )
    validation = create_validation_decision(
        project_id,
        finding_id,
        workspace,
        status=ValidationStatus(validation_status),
        reason="Finalized category-performance fixture.",
        evidence=ValidationEvidence(
            has_finding=True,
            has_reproduction=reproduction is not None,
            reproduction_status=reproduction_status,
            in_scope=validation_status != "out_of_scope",
            is_duplicate=validation_status == "duplicate",
            normalized_severity="High",
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status="validation_pending",
            reason="Validation pending.",
            reproduction_id=reproduction.reproduction_id if reproduction else None,
        ),
    )
    submission = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(
            status={"unsafe_poc": "unsafe"}.get(validation_status, validation_status),
            reason="Validation finalized.",
            validation_id=validation.validation_id,
        ),
    )
    contribution = calculate_contribution_for_submission(
        root, workspace, submission.submission_id
    )
    if score is not None:
        contribution = save_contribution_score(
            root,
            contribution.model_copy(update={"total_score": score}),
        )
    if apply_reputation:
        event = process_submission_reputation(
            root, workspace, submission.submission_id
        ).event
    else:
        event = build_reputation_event(
            load_node(root, node.node_id),
            submission,
            finding,
            validation,
            reproduction,
            contribution,
        )
        event = save_prepared_event_exclusively(root, event)
    return {
        "workspace": workspace,
        "submission": submission,
        "validation": validation,
        "reproduction": reproduction,
        "contribution": contribution,
        "event": event,
    }


def _save_finalized_reward(root, case):
    event = case["event"]
    contribution = case["contribution"]
    now = datetime.now(timezone.utc)
    reward = RewardEvent(
        reward_event_id=f"reward-{event.submission_id}",
        reward_version="reward_v0",
        event_status=RewardEventStatus.FINALIZED,
        cycle_id=f"cycle-{event.submission_id}",
        project_id=event.project_id,
        submission_id=event.submission_id,
        node_id=event.node_id,
        finding_id=event.finding_id,
        category=event.category,
        contribution_score_id=contribution.score_id,
        reputation_event_id=event.event_id,
        contribution_score=contribution.total_score,
        reputation_score=event.new_reputation,
        reputation_multiplier=1,
        category_multiplier=1,
        raw_weight=contribution.total_score,
        allocation_ratio=1,
        reward_amount=100,
        reward_unit=RewardUnit.PROTOCOL_POINTS,
        source_fingerprint="b" * 64,
        reason="Finalized reward fixture.",
        created_at=now,
        finalized_at=now,
        updated_at=now,
    )
    return save_reward_event_exclusively(root, reward)


def test_real_source_validation_and_rebuild_round_trip(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    node = _register_node(root)
    case = _finalize_case(root, audits, node, "access", score=88)

    events = collect_node_category_events(root, node.node_id, "Access Control")
    assert events == [case["event"]]
    bundle = validate_category_event_sources(root, events[0])
    assert bundle.submission.submission_id == case["submission"].submission_id
    record = rebuild_category_performance(root, node.node_id, "access-control")
    path = (
        root
        / "category-performance"
        / node.node_id
        / "access_control.json"
    )
    assert path.is_file()
    assert load_category_performance(root, node.node_id, "access_control") == record
    assert record.counts.total_finalized_submissions == 1
    assert record.counts.accepted_unique_submissions == 1
    assert record.contribution_stats.average_contribution_score == 88
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n  ")
    assert text.endswith("\n")
    assert "/home/" not in text
    assert "poc" not in text.lower()
    assert "private_key" not in text


def test_strict_source_mismatch_and_missing_source_fail(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    node = _register_node(root)
    case = _finalize_case(root, audits, node, "mismatch")
    submission = load_submission(root, case["submission"].submission_id)
    save_submission(root, submission.model_copy(update={"category": "reentrancy"}))
    with pytest.raises(CategoryPerformanceCategoryMismatchError):
        validate_category_event_sources(root, case["event"])

    missing_event = case["event"].model_copy(
        update={"submission_id": "00000000-0000-0000-0000-000000000000"}
    )
    with pytest.raises(CategoryPerformanceSourceNotFoundError):
        validate_category_event_sources(root, missing_event)


def test_prepared_events_are_ignored_and_applied_events_included(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    applied_node = _register_node(root, "applied-node")
    _finalize_case(root, audits, applied_node, "applied")
    assert len(
        collect_node_category_events(root, applied_node.node_id, "access_control")
    ) == 1

    prepared_node = _register_node(root, "prepared-node")
    _finalize_case(
        root,
        audits,
        prepared_node,
        "prepared",
        apply_reputation=False,
    )
    assert collect_node_category_events(
        root, prepared_node.node_id, "access_control"
    ) == []


def test_rebuild_is_idempotent_and_changed_reward_source_rewrites(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    node = _register_node(root)
    case = _finalize_case(root, audits, node, "idempotent", score=90)
    first = rebuild_category_performance(root, node.node_id, "access_control")
    path = get_category_performance_path(root, node.node_id, "access_control")
    first_mtime = path.stat().st_mtime_ns
    second = rebuild_category_performance(root, node.node_id, "access_control")
    assert second == first
    assert path.stat().st_mtime_ns == first_mtime

    _save_finalized_reward(root, case)
    changed = rebuild_category_performance(root, node.node_id, "access_control")
    assert changed.source_fingerprint != first.source_fingerprint
    assert changed.performance_id == first.performance_id
    assert changed.created_at == first.created_at
    assert changed.updated_at > first.updated_at
    assert changed.counts.rewarded_submissions == 1


def test_node_and_global_rebuild_keep_categories_and_nodes_isolated(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    first_node = _register_node(root, "first-node")
    second_node = _register_node(root, "second-node")
    _finalize_case(root, audits, first_node, "first-access", score=80)
    _finalize_case(
        root,
        audits,
        first_node,
        "first-reentrancy",
        category="reentrancy",
        validation_status="duplicate",
        reproduction_status=None,
    )
    _finalize_case(root, audits, second_node, "second-access", score=70)

    first_records = rebuild_node_category_performance(root, first_node.node_id)
    assert [record.category.value for record in first_records] == [
        "access_control",
        "reentrancy",
    ]
    all_records = rebuild_all_category_performance(root)
    assert [(record.node_id, record.category.value) for record in all_records] == sorted(
        (record.node_id, record.category.value) for record in all_records
    )
    assert len(all_records) == 3


@pytest.mark.parametrize("status", ["inactive", "banned"])
def test_disabled_node_history_is_still_aggregated(tmp_path, status):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    node = _register_node(root)
    _finalize_case(root, audits, node, f"disabled-{status}")
    change_node_status(
        root,
        node.node_id,
        NodeStatusChangeRequest(
            status=status,
            reason="Historical aggregation test.",
        ),
    )
    record = rebuild_category_performance(root, node.node_id, "access_control")
    assert record.counts.total_finalized_submissions == 1


def test_explicit_empty_rebuild_persists_zero_record(tmp_path):
    root = tmp_path / "protocol"
    node = _register_node(root)
    record = rebuild_category_performance(root, node.node_id, "oracle_manipulation")
    assert record.counts.total_finalized_submissions == 0
    assert record.source_event_ids == []
    assert record.first_activity_at is None
    assert rebuild_node_category_performance(root, node.node_id) == []


def test_list_filters_and_rebuild_does_not_modify_source_or_subnet_records(tmp_path):
    root = tmp_path / "protocol"
    audits = tmp_path / "audits"
    node = _register_node(root)
    case = _finalize_case(root, audits, node, "preserve", score=77)
    subnet = create_subnet(root, SubnetCreate(category="access_control"))
    snapshots = {
        "node": deepcopy(load_node(root, node.node_id)),
        "submission": deepcopy(load_submission(root, case["submission"].submission_id)),
        "contribution": deepcopy(load_contribution_score(root, case["submission"].submission_id)),
        "event": deepcopy(load_reputation_event_by_submission(root, case["submission"].submission_id)),
        "validation": deepcopy(load_validation_decision(case["workspace"], case["submission"].finding_id)),
        "reproduction": deepcopy(load_reproduction_result(case["workspace"], case["submission"].finding_id)),
        "subnet": deepcopy(load_subnet(root, subnet.subnet_id)),
    }
    rebuild_category_performance(root, node.node_id, "access_control")
    rebuild_category_performance(root, node.node_id, "reentrancy")

    assert len(list_category_performance(root)) == 2
    assert len(list_category_performance(root, node_id=node.node_id)) == 2
    assert len(list_category_performance(root, category="access_control")) == 1
    assert len(list_category_performance(root, minimum_finalized_submissions=1)) == 1
    assert load_node(root, node.node_id) == snapshots["node"]
    assert load_submission(root, case["submission"].submission_id) == snapshots["submission"]
    assert load_contribution_score(root, case["submission"].submission_id) == snapshots["contribution"]
    assert load_reputation_event_by_submission(root, case["submission"].submission_id) == snapshots["event"]
    assert load_validation_decision(case["workspace"], case["submission"].finding_id) == snapshots["validation"]
    assert load_reproduction_result(case["workspace"], case["submission"].finding_id) == snapshots["reproduction"]
    assert load_subnet(root, subnet.subnet_id) == snapshots["subnet"]
    assert not list((root / "subnets").glob("*/members/*.json"))


def test_missing_node_and_unsafe_identifiers_fail_without_escape(tmp_path):
    with pytest.raises(CategoryPerformanceNodeNotFoundError):
        rebuild_category_performance(
            tmp_path, "00000000-0000-0000-0000-000000000000", "access_control"
        )
    for unsafe in ("../node", "node/name", r"node\\name", "/absolute", ".."):
        with pytest.raises(InvalidCategoryPerformanceIdentifierError):
            get_category_performance_path(tmp_path, unsafe, "access_control")
    assert build_category_performance_id("node-1", "Access Control") == (
        "performance_node-1_access_control"
    )
    assert not (tmp_path.parent / "node").exists()
