import json
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.node import NodeCreate, NodeStatusChangeRequest, NodeUpdate
from app.schemas.reputation import (
    NodeStatisticsDelta,
    ReputationDeltaComponents,
    ReputationEvent,
)
from app.schemas.subnet import (
    SubnetCreate,
    SubnetMemberRecord,
    SubnetStatusChangeRequest,
)
from app.services.category_performance_service import save_category_performance
from app.services.category_scoring_service import (
    calculate_category_score,
    rebuild_category_score,
)
from app.services.node_registry_service import (
    change_node_status,
    create_node,
    update_node,
)
from app.services.reputation_service import get_reputation_event_dir
from app.services.subnet_membership_service import (
    MembershipSourceBundle,
    SubnetMembershipPerformanceNotFoundError,
    administratively_remove_member,
    administratively_suspend_member,
    build_membership_source_payload,
    calculate_recommended_membership_status,
    collect_recent_unsafe_events,
    compute_membership_source_fingerprint,
    determine_membership_decision,
    evaluate_node_membership,
    get_node_membership_events_root,
    list_membership_events,
    load_membership_source_bundle,
    refresh_subnet_memberships,
)
from app.services.subnet_registry_service import (
    change_subnet_status,
    create_subnet,
    load_subnet_member,
    save_subnet_member,
)
from tests.test_category_scoring_service import _performance


NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)


def _node(root, name, categories=None):
    return create_node(
        root,
        NodeCreate(
            node_type="agent",
            display_name=name,
            operator_id=f"operator-{name}",
            supported_categories=categories or ["access_control"],
        ),
    )


def _subnet(root, **updates):
    return create_subnet(
        root,
        SubnetCreate(category="access_control", **updates),
    )


def _store_sources(root, node, **performance_kwargs):
    performance = save_category_performance(
        root,
        _performance(
            node_id=node.node_id,
            category="access_control",
            **performance_kwargs,
        ),
    )
    score = rebuild_category_score(
        root, node.node_id, "access_control"
    ).record
    return performance, score


def _store_reputation_event(
    root,
    *,
    event_id,
    node_id,
    applied_at,
    category="access_control",
    event_type="unsafe_submission",
    application_status="applied",
):
    event = ReputationEvent(
        event_id=event_id,
        reputation_version="reputation_v0",
        application_status=application_status,
        node_id=node_id,
        submission_id=f"submission-{event_id}",
        project_id="project-1",
        finding_id=f"finding-{event_id}",
        category=category,
        validation_id="validation-1",
        reproduction_id=None,
        contribution_score_id=f"contribution-{event_id}",
        validation_status="unsafe_poc",
        reproduction_status="rejected_unsafe",
        contribution_score=0,
        contribution_eligibility_status="ineligible",
        normalized_severity="High",
        event_type=event_type,
        previous_reputation=0.5,
        delta_components=ReputationDeltaComponents(
            base_delta=-0.08,
            total_delta=-0.08,
        ),
        new_reputation=0.42,
        statistics_delta=NodeStatisticsDelta(unsafe_submissions=1),
        previous_statistics={},
        new_statistics={"unsafe_submissions": 1},
        signals=[],
        reason="Deterministic test event.",
        source_fingerprint=("a" if event_id != "event-b" else "b") * 64,
        created_at=applied_at,
        applied_at=applied_at if application_status == "applied" else None,
        updated_at=applied_at,
    )
    directory = get_reputation_event_dir(root, event.submission_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "event.json").write_text(
        json.dumps(event.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return event


def _member(subnet, node, status, score, performance):
    return save_subnet_member(
        subnet[0],
        SubnetMemberRecord(
            subnet_id=subnet[1].subnet_id,
            node_id=node.node_id,
            category="access_control",
            status=status,
            category_score=score.category_score,
            rank=None,
            finalized_submissions=performance.counts.total_finalized_submissions,
            accepted_unique_submissions=performance.counts.accepted_unique_submissions,
            exploration_assignments=4,
            last_assigned_at=NOW - timedelta(days=1),
            joined_at=NOW - timedelta(days=10),
            updated_at=NOW - timedelta(days=1),
            status_reason="Existing member.",
        ),
    )


def test_base_candidate_probation_active_and_expert_rules(tmp_path):
    subnet = _subnet(
        tmp_path,
        minimum_category_score=0.6,
        minimum_finalized_submissions=5,
    )
    cases = [
        (_performance(accepted_scores=[100]), "candidate"),
        (_performance(accepted_scores=[80] * 3), "probation"),
        (_performance(accepted_scores=[80] * 5), "active"),
        (_performance(accepted_scores=[100] * 10), "expert"),
    ]
    for performance, expected in cases:
        score = calculate_category_score(performance)
        status, _ = calculate_recommended_membership_status(
            subnet, performance, score, None, False
        )
        assert status.value == expected


def test_active_and_expert_hysteresis_have_distinct_demotion_thresholds(tmp_path):
    subnet = _subnet(
        tmp_path,
        minimum_category_score=0.8,
        minimum_finalized_submissions=5,
    )
    node = _node(tmp_path, "hysteresis-node")
    active_performance = _performance(
        node_id=node.node_id, accepted_scores=[80] * 6
    )
    active_score = calculate_category_score(active_performance)
    existing = SubnetMemberRecord(
        subnet_id=subnet.subnet_id,
        node_id=node.node_id,
        category="access_control",
        status="active",
        category_score=0.8,
        finalized_submissions=6,
        accepted_unique_submissions=6,
        exploration_assignments=0,
        joined_at=NOW,
        updated_at=NOW,
    )
    status, reasons = calculate_recommended_membership_status(
        subnet, active_performance, active_score, existing, False
    )
    assert status.value == "active"
    assert "hysteresis_preserved_active" in [code.value for code in reasons]

    expert_performance = _performance(
        node_id=node.node_id,
        accepted_scores=[100] * 6,
        rejected=4,
    )
    expert_score = calculate_category_score(expert_performance)
    expert_existing = existing.model_copy(update={"status": "expert"})
    status, reasons = calculate_recommended_membership_status(
        subnet.model_copy(update={"minimum_category_score": 0.6}),
        expert_performance,
        expert_score,
        expert_existing,
        False,
    )
    assert status.value == "expert"
    assert "hysteresis_preserved_expert" in [code.value for code in reasons]


def test_recent_and_historical_unsafe_override_high_performance(tmp_path):
    subnet = _subnet(tmp_path)
    performance = _performance(accepted_scores=[100] * 10)
    score = calculate_category_score(performance)
    status, reasons = calculate_recommended_membership_status(
        subnet, performance, score, None, True
    )
    assert status.value == "suspended"
    assert reasons == [reasons[0].RECENT_UNSAFE_SUBMISSION]

    historical = _performance(accepted_scores=[100] * 10, unsafe=1)
    historical_score = calculate_category_score(historical)
    status, reasons = calculate_recommended_membership_status(
        subnet, historical, historical_score, None, False
    )
    assert status.value != "expert"
    assert "historical_unsafe_prevents_expert" in [
        code.value for code in reasons
    ]


def test_recent_unsafe_collection_is_exact_inclusive_and_utc(tmp_path):
    node = _node(tmp_path, "unsafe-event-node")
    boundary = NOW - timedelta(days=30)
    _store_reputation_event(
        tmp_path,
        event_id="event-b",
        node_id=node.node_id,
        applied_at=NOW - timedelta(days=1),
    )
    _store_reputation_event(
        tmp_path,
        event_id="event-a",
        node_id=node.node_id,
        applied_at=boundary,
    )
    _store_reputation_event(
        tmp_path,
        event_id="event-old",
        node_id=node.node_id,
        applied_at=boundary - timedelta(microseconds=1),
    )
    _store_reputation_event(
        tmp_path,
        event_id="event-prepared",
        node_id=node.node_id,
        applied_at=NOW,
        application_status="prepared",
    )
    _store_reputation_event(
        tmp_path,
        event_id="event-category",
        node_id=node.node_id,
        applied_at=NOW,
        category="reentrancy",
    )
    _store_reputation_event(
        tmp_path,
        event_id="event-other-type",
        node_id=node.node_id,
        applied_at=NOW,
        event_type="duplicate_finding",
    )
    events = collect_recent_unsafe_events(
        tmp_path,
        node.node_id,
        "access_control",
        NOW.astimezone(timezone(timedelta(hours=3))),
    )
    assert [event.event_id for event in events] == ["event-a", "event-b"]


def test_missing_dependencies_create_virtual_zero_candidate_on_refresh(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "new-node")
    result = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW
    )
    assert result.created_members == 1
    member = result.results[0].current_member
    assert member.node_id == node.node_id
    assert member.status.value == "candidate"
    assert member.finalized_submissions == 0
    assert member.category_score == 0
    assert not (tmp_path / "category-performance").exists()
    assert not (tmp_path / "category-scores").exists()
    with pytest.raises(SubnetMembershipPerformanceNotFoundError):
        load_membership_source_bundle(
            tmp_path, subnet.subnet_id, node.node_id, NOW
        )


def test_refresh_tiers_capacity_and_idempotency(tmp_path):
    subnet = _subnet(
        tmp_path,
        maximum_active_nodes=2,
        minimum_category_score=0.6,
        minimum_finalized_submissions=5,
    )
    nodes = [
        _node(tmp_path, "capacity-expert"),
        _node(tmp_path, "capacity-active-a"),
        _node(tmp_path, "capacity-active-b"),
    ]
    _store_sources(tmp_path, nodes[0], accepted_scores=[100] * 10)
    _store_sources(tmp_path, nodes[1], accepted_scores=[90] * 6)
    _store_sources(tmp_path, nodes[2], accepted_scores=[80] * 5)

    first = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW
    )
    assert first.active_members + first.expert_members == 2
    overflow = [
        result
        for result in first.results
        if result.decision.capacity_adjusted
    ]
    assert len(overflow) == 1
    assert overflow[0].current_member.status.value == "probation"
    assert "active_capacity_reached" in overflow[0].current_member.status_reason_codes
    assert all(result.current_member.rank is None for result in first.results)

    member_paths = sorted(
        (tmp_path / "subnets" / subnet.subnet_id / "members").glob("*.json")
    )
    before = {path: path.read_bytes() for path in member_paths}
    event_count = len(list_membership_events(tmp_path, subnet.subnet_id))
    second = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(days=1)
    )
    assert second.unchanged_members == 3
    assert len(list_membership_events(tmp_path, subnet.subnet_id)) == event_count
    assert {path: path.read_bytes() for path in member_paths} == before


def test_single_node_evaluation_uses_complete_capacity(tmp_path):
    subnet = _subnet(tmp_path, maximum_active_nodes=1)
    stronger = _node(tmp_path, "node-a")
    requested = _node(tmp_path, "node-z")
    _store_sources(tmp_path, stronger, accepted_scores=[100] * 10)
    _store_sources(tmp_path, requested, accepted_scores=[80] * 5)
    result = evaluate_node_membership(
        tmp_path, subnet.subnet_id, requested.node_id, evaluated_at=NOW
    )
    assert result.current_member.status.value == "probation"
    assert result.decision.capacity_adjusted


def test_node_subnet_category_and_removed_overrides(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "override-node")
    _store_sources(tmp_path, node, accepted_scores=[100] * 10)
    refresh_subnet_memberships(tmp_path, subnet.subnet_id, evaluated_at=NOW)

    change_node_status(
        tmp_path,
        node.node_id,
        NodeStatusChangeRequest(status="banned", reason="Unsafe operator."),
    )
    banned = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(hours=1)
    )
    assert banned.results[0].current_member.status.value == "suspended"

    change_subnet_status(
        tmp_path,
        subnet.subnet_id,
        SubnetStatusChangeRequest(status="archived", reason="Archived."),
    )
    archived = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(hours=2)
    )
    assert archived.results[0].current_member.status.value == "removed"
    preserved = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(hours=3)
    )
    assert preserved.results[0].current_member.status.value == "removed"


def test_unsupported_existing_member_is_removed_and_new_unrelated_node_is_skipped(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "supported-node")
    performance, score = _store_sources(
        tmp_path, node, accepted_scores=[80] * 5
    )
    refresh_subnet_memberships(tmp_path, subnet.subnet_id, evaluated_at=NOW)
    update_node(
        tmp_path,
        node.node_id,
        NodeUpdate(supported_categories=["reentrancy"]),
    )
    unrelated = _node(tmp_path, "unrelated-node", ["reentrancy"])
    result = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(days=1)
    )
    assert [item.node_id for item in result.results] == [node.node_id]
    assert result.results[0].current_member.status.value == "removed"
    assert load_subnet_member(
        tmp_path, subnet.subnet_id, unrelated.node_id
    ) is None


def test_source_fingerprint_is_canonical_and_excludes_evaluation_time(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "fingerprint-node")
    performance, score = _store_sources(
        tmp_path, node, accepted_scores=[90] * 5
    )
    bundle = load_membership_source_bundle(
        tmp_path, subnet.subnet_id, node.node_id, NOW
    )
    payload = build_membership_source_payload(
        node, subnet, performance, score, [], None, "membership_policy_v0"
    )
    assert compute_membership_source_fingerprint(payload) == (
        compute_membership_source_fingerprint(json.loads(json.dumps(payload)))
    )
    one = determine_membership_decision(bundle, NOW)
    two = determine_membership_decision(bundle, NOW + timedelta(days=10))
    assert one.source_fingerprint == two.source_fingerprint
    assert one.created_at != two.created_at


def test_administrative_locks_are_idempotent_and_survive_refresh(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "admin-node")
    _store_sources(tmp_path, node, accepted_scores=[100] * 10)
    initial = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW
    ).results[0].current_member
    joined_at = initial.joined_at
    last_assigned_at = initial.model_copy(
        update={"last_assigned_at": NOW, "exploration_assignments": 7}
    )
    save_subnet_member(tmp_path, last_assigned_at)

    suspended = administratively_suspend_member(
        tmp_path, subnet.subnet_id, node.node_id, "Manual security review."
    )
    repeat = administratively_suspend_member(
        tmp_path, subnet.subnet_id, node.node_id, "Manual security review."
    )
    assert suspended.current_member.status.value == "suspended"
    assert repeat.status.value == "unchanged"
    history_count = len(
        list_membership_events(tmp_path, subnet.subnet_id, node.node_id)
    )
    refreshed = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW + timedelta(days=2)
    ).results[0]
    assert refreshed.current_member.status.value == "suspended"
    assert refreshed.current_member.administrative_lock
    assert refreshed.current_member.joined_at == joined_at
    assert refreshed.current_member.exploration_assignments == 7
    assert refreshed.current_member.last_assigned_at == NOW
    assert len(
        list_membership_events(tmp_path, subnet.subnet_id, node.node_id)
    ) == history_count

    removed = administratively_remove_member(
        tmp_path,
        subnet.subnet_id,
        node.node_id,
        "Node no longer participates.",
    )
    assert removed.current_member.status.value == "removed"
    assert removed.current_member.administrative_lock


def test_event_and_member_storage_paths_are_safe_and_human_readable(tmp_path):
    subnet = _subnet(tmp_path)
    node = _node(tmp_path, "storage-node")
    _store_sources(tmp_path, node, accepted_scores=[80] * 5)
    result = refresh_subnet_memberships(
        tmp_path, subnet.subnet_id, evaluated_at=NOW
    ).results[0]
    member_path = (
        tmp_path
        / "subnets"
        / subnet.subnet_id
        / "members"
        / f"{node.node_id}.json"
    )
    event_path = (
        get_node_membership_events_root(
            tmp_path, subnet.subnet_id, node.node_id
        )
        / f"{result.decision.decision_id}.json"
    )
    assert member_path.is_file() and event_path.is_file()
    assert member_path.read_text().startswith("{\n  ")
    text = event_path.read_text().lower()
    assert "/home/" not in text
    assert "private_key" not in text
    assert '"rank"' not in text
