import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.task_reward import (
    RewardDomain,
    TaskRewardBudgetCreateRequest,
    TaskRewardBudgetStatus,
    TaskRewardPoolConfig,
)
from app.services.task_reward_budget_service import (
    InvalidTaskRewardIdentifierError,
    TaskRewardBudgetAlreadyFinalizedError,
    TaskRewardBudgetConflictError,
    TaskRewardProjectMismatchError,
    TaskRewardProjectNotFoundError,
    TaskRewardRoutingNotFoundError,
    build_task_reward_request_payload,
    build_task_reward_source_payload,
    compute_task_reward_fingerprint,
    create_task_reward_budget,
    finalize_task_reward_budget,
    get_task_reward_budget_path,
    get_task_rewards_root,
    list_project_task_reward_budgets,
    load_task_reward_budget,
    load_task_reward_budget_by_routing,
    save_task_reward_budget,
    split_task_reward_budget,
)
from tests.test_subnet_reward_allocation_service import _setup


def _request(routing_id, total="10000.000000", **kwargs):
    return TaskRewardBudgetCreateRequest(
        routing_id=routing_id,
        total_budget_points=total,
        **kwargs,
    )


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "task-rewards" not in path.parts
    }


def test_create_load_find_list_finalize_and_idempotency(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    request = _request(routing.routing_id)
    budget, created = create_task_reward_budget(
        root, workspace, "project-1", request
    )
    assert created
    assert budget.status == TaskRewardBudgetStatus.DRAFT
    assert budget.reward_domain == RewardDomain.CLIENT_TASK
    assert budget.total_budget_points == Decimal("10000.000000")
    assert budget.miner_pool_points == Decimal("7000.000000")
    assert budget.validator_pool_points == Decimal("2000.000000")
    assert budget.protocol_pool_points == Decimal("1000.000000")

    repeated, repeated_created = create_task_reward_budget(
        root, workspace, "project-1", request
    )
    assert not repeated_created
    assert repeated == budget
    assert load_task_reward_budget_by_routing(root, routing.routing_id) == budget
    assert load_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    ) == budget
    assert list_project_task_reward_budgets(root, "project-1") == [budget]

    finalized, changed = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    assert changed
    assert finalized.status == TaskRewardBudgetStatus.FINALIZED
    assert finalized.finalized_at is not None
    repeated_finalization, changed = finalize_task_reward_budget(
        root, "project-1", budget.task_reward_budget_id
    )
    assert not changed
    assert repeated_finalization == finalized


def test_conflicting_second_budget_for_same_routing_fails(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    create_task_reward_budget(root, workspace, "project-1", _request(routing.routing_id))
    with pytest.raises(TaskRewardBudgetConflictError):
        create_task_reward_budget(
            root,
            workspace,
            "project-1",
            _request(routing.routing_id, total="9999.000000"),
        )
    with pytest.raises(TaskRewardBudgetConflictError):
        create_task_reward_budget(
            root,
            workspace,
            "project-1",
            _request(
                routing.routing_id,
                pool_split=TaskRewardPoolConfig(
                    miner_share="0.6",
                    validator_share="0.3",
                    protocol_share="0.1",
                ),
            ),
        )


def test_missing_project_routing_and_project_mismatch_fail(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    with pytest.raises(TaskRewardProjectNotFoundError):
        create_task_reward_budget(
            root,
            tmp_path / "missing",
            "project-1",
            _request(routing.routing_id),
        )
    with pytest.raises(TaskRewardRoutingNotFoundError):
        create_task_reward_budget(
            root, workspace, "project-1", _request("routing-missing")
        )
    other = tmp_path / "other-workspace"
    other.mkdir()
    (other / "metadata.json").write_text(
        json.dumps({"project_id": "project-2"}), encoding="utf-8"
    )
    with pytest.raises(TaskRewardProjectMismatchError):
        create_task_reward_budget(
            root, other, "project-2", _request(routing.routing_id)
        )


def test_finalized_budget_economic_inputs_are_immutable(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    draft, _ = create_task_reward_budget(
        root, workspace, "project-1", _request(routing.routing_id)
    )
    finalized, _ = finalize_task_reward_budget(
        root, "project-1", draft.task_reward_budget_id
    )
    for changed in (
        finalized.model_copy(update={"total_budget_points": Decimal("1.000000")}),
        finalized.model_copy(update={"miner_share": Decimal("0.60")}),
    ):
        with pytest.raises((TaskRewardBudgetAlreadyFinalizedError, ValueError)):
            save_task_reward_budget(root, changed)


def test_storage_is_decimal_safe_atomic_and_contains_no_host_path(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1", _request(routing.routing_id)
    )
    path = get_task_reward_budget_path(root, routing.routing_id)
    assert path == (
        root
        / "task-rewards"
        / "budgets"
        / "routing"
        / routing.routing_id
        / "budget.json"
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["total_budget_points"] == "10000.000000"
    assert stored["miner_pool_points"] == "7000.000000"
    assert "/home/" not in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))
    assert stored["task_reward_budget_id"] == budget.task_reward_budget_id


def test_budget_fingerprint_is_deterministic_and_covers_every_economic_input():
    config = TaskRewardPoolConfig()
    split = split_task_reward_budget(Decimal("100.000000"), config)
    request_payload = build_task_reward_request_payload(
        "project-1",
        "routing-1",
        "a" * 64,
        Decimal("100.000000"),
        config,
        "task_reward_config_v1",
    )
    first = compute_task_reward_fingerprint(
        build_task_reward_source_payload(request_payload, split)
    )
    second = compute_task_reward_fingerprint(
        build_task_reward_source_payload(dict(reversed(list(request_payload.items()))), split)
    )
    assert first == second
    assert len(first) == 64
    variants = [
        {**request_payload, "project_id": "project-2"},
        {**request_payload, "routing_id": "routing-2"},
        {**request_payload, "total_budget_points": Decimal("99.000000")},
        {**request_payload, "miner_share": Decimal("0.69")},
        {**request_payload, "configuration_version": "task_reward_config_v2"},
        {**request_payload, "protocol_quantum": Decimal("0.01")},
    ]
    assert all(
        compute_task_reward_fingerprint(build_task_reward_source_payload(item, split)) != first
        for item in variants
    )


def test_budget_creation_has_no_performance_routing_or_payout_side_effects(tmp_path):
    root, workspace, _, routing, _ = _setup(tmp_path)
    assert not get_task_rewards_root(root).exists()
    before = _snapshot_files(root)
    budget, _ = create_task_reward_budget(
        root, workspace, "project-1", _request(routing.routing_id)
    )
    after = _snapshot_files(root)
    assert after == before
    assert budget.reward_domain == RewardDomain.CLIENT_TASK
    assert not (root / "network-rewards").exists()
    reward_event_files = list((root / "rewards" / "events").glob("**/*.json"))
    subnet_event_files = list((root / "subnet-rewards" / "events").glob("**/*.json"))
    assert not reward_event_files
    assert not subnet_event_files


@pytest.mark.parametrize(
    "unsafe",
    ["../routing", "routing/escape", "routing\\escape", "/absolute"],
)
def test_unsafe_identifiers_cannot_escape_protocol_root(tmp_path, unsafe):
    with pytest.raises(InvalidTaskRewardIdentifierError):
        load_task_reward_budget_by_routing(tmp_path / "protocol", unsafe)
