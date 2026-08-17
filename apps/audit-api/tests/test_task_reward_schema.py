from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.schemas.subnet_reward import MAX_PROTOCOL_POINTS
from app.schemas.task_reward import (
    RewardDomain,
    TaskRewardBudget,
    TaskRewardBudgetCreateRequest,
    TaskRewardBudgetStatus,
    TaskRewardPoolConfig,
)
from app.services.task_reward_budget_service import split_task_reward_budget


NOW = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def _budget_values(status=TaskRewardBudgetStatus.DRAFT):
    finalized_at = NOW if status == TaskRewardBudgetStatus.FINALIZED else None
    return {
        "task_reward_budget_id": "task_reward_budget_123",
        "project_id": "project-1",
        "routing_id": "routing-1",
        "routing_source_fingerprint": FINGERPRINT,
        "total_budget_points": "100.000000",
        "miner_pool_points": "70.000000",
        "validator_pool_points": "20.000000",
        "protocol_pool_points": "10.000000",
        "miner_share": "0.70",
        "validator_share": "0.20",
        "protocol_share": "0.10",
        "status": status,
        "configuration_version": "task_reward_config_v1",
        "request_fingerprint": FINGERPRINT,
        "source_fingerprint": FINGERPRINT,
        "created_at": NOW,
        "updated_at": NOW,
        "finalized_at": finalized_at,
    }


def test_default_pool_configuration_is_exact_decimal_and_safe():
    config = TaskRewardPoolConfig()
    assert config == TaskRewardPoolConfig(
        miner_share="0.70",
        validator_share="0.20",
        protocol_share="0.10",
    )
    assert all(
        isinstance(value, Decimal)
        for value in (
            config.miner_share,
            config.validator_share,
            config.protocol_share,
        )
    )
    assert sum(
        (config.miner_share, config.validator_share, config.protocol_share),
        Decimal("0"),
    ) == Decimal("1")
    assert not {
        "token_address",
        "wallet_address",
        "private_key",
    } & TaskRewardPoolConfig.model_fields.keys()
    assert Settings().task_reward_pool == config


@pytest.mark.parametrize(
    "values",
    [
        {"miner_share": "-0.1", "validator_share": "0.2", "protocol_share": "0.9"},
        {"miner_share": "0.7", "validator_share": "-0.1", "protocol_share": "0.4"},
        {"miner_share": "0.7", "validator_share": "0.4", "protocol_share": "-0.1"},
        {"miner_share": "1.1", "validator_share": "0", "protocol_share": "-0.1"},
        {"miner_share": "0.6", "validator_share": "0.2", "protocol_share": "0.1"},
        {"miner_share": "0.8", "validator_share": "0.3", "protocol_share": "0.1"},
    ],
)
def test_invalid_pool_configurations_fail_without_normalization(values):
    with pytest.raises(ValidationError):
        TaskRewardPoolConfig(**values)


def test_binary_float_pool_configuration_is_rejected():
    with pytest.raises(ValidationError, match="decimal strings"):
        TaskRewardPoolConfig(
            miner_share=0.7,
            validator_share=0.2,
            protocol_share=0.1,
        )


def test_invalid_environment_pool_configuration_fails_fast(monkeypatch):
    monkeypatch.setenv("AUDIT_API_TASK_REWARD_MINER_SHARE", "0.8")
    monkeypatch.setenv("AUDIT_API_TASK_REWARD_VALIDATOR_SHARE", "0.3")
    monkeypatch.setenv("AUDIT_API_TASK_REWARD_PROTOCOL_SHARE", "0.1")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="sum exactly to 1"):
            get_settings()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        ("100.000000", ("70.000000", "20.000000", "10.000000")),
        ("10000.000000", ("7000.000000", "2000.000000", "1000.000000")),
        ("0.123457", ("0.086420", "0.024691", "0.012346")),
        ("0.000001", ("0.000001", "0.000000", "0.000000")),
        ("0.000003", ("0.000002", "0.000001", "0.000000")),
    ],
)
def test_pool_split_is_exact_and_deterministic(total, expected):
    split = split_task_reward_budget(Decimal(total), TaskRewardPoolConfig())
    actual = (
        split.miner_pool_points,
        split.validator_pool_points,
        split.protocol_pool_points,
    )
    assert actual == tuple(Decimal(value) for value in expected)
    assert sum(actual, Decimal("0")) == split.total_budget_points


def test_pool_split_is_independent_of_config_input_key_order():
    first = TaskRewardPoolConfig.model_validate(
        {"miner_share": "0.5", "validator_share": "0.3", "protocol_share": "0.2"}
    )
    second = TaskRewardPoolConfig.model_validate(
        {"protocol_share": "0.2", "miner_share": "0.5", "validator_share": "0.3"}
    )
    assert split_task_reward_budget(Decimal("0.000007"), first) == split_task_reward_budget(
        Decimal("0.000007"), second
    )


@pytest.mark.parametrize("value", ["0", "-1.000000"])
def test_zero_and_negative_task_budgets_fail(value):
    with pytest.raises(ValueError):
        split_task_reward_budget(Decimal(value), TaskRewardPoolConfig())


def test_excessive_and_overprecision_task_budgets_fail():
    with pytest.raises(ValueError, match="cannot exceed"):
        split_task_reward_budget(MAX_PROTOCOL_POINTS + Decimal("0.000001"), TaskRewardPoolConfig())
    with pytest.raises(ValueError, match="six decimal"):
        split_task_reward_budget(Decimal("1.0000001"), TaskRewardPoolConfig())


def test_valid_draft_and_finalized_budget_schemas():
    draft = TaskRewardBudget(**_budget_values())
    finalized = TaskRewardBudget(**_budget_values(TaskRewardBudgetStatus.FINALIZED))
    assert draft.reward_domain == RewardDomain.CLIENT_TASK
    assert draft.finalized_at is None
    assert finalized.finalized_at == NOW
    assert draft.miner_pool_points + draft.validator_pool_points + draft.protocol_pool_points == draft.total_budget_points


def test_budget_rejects_wrong_domain_pool_total_ids_and_fingerprint():
    changes = [
        {"reward_domain": RewardDomain.NETWORK_PROTOCOL},
        {"miner_pool_points": "69.000000"},
        {"project_id": "../project"},
        {"routing_id": "routing/escape"},
        {"source_fingerprint": "not-a-fingerprint"},
    ]
    for change in changes:
        with pytest.raises(ValidationError):
            TaskRewardBudget(**{**_budget_values(), **change})


def test_budget_lifecycle_and_immutability_are_enforced():
    with pytest.raises(ValidationError, match="require finalized_at"):
        TaskRewardBudget(
            **{
                **_budget_values(TaskRewardBudgetStatus.FINALIZED),
                "finalized_at": None,
            }
        )
    with pytest.raises(ValidationError, match="cannot have finalized_at"):
        TaskRewardBudget(**{**_budget_values(), "finalized_at": NOW})
    budget = TaskRewardBudget(**_budget_values(TaskRewardBudgetStatus.FINALIZED))
    with pytest.raises(ValidationError, match="frozen"):
        budget.total_budget_points = Decimal("50.000000")


@pytest.mark.parametrize(
    "forged_field",
    [
        "task_reward_budget_id",
        "miner_pool_points",
        "validator_pool_points",
        "protocol_pool_points",
        "source_fingerprint",
        "finalized_at",
        "reward_events",
    ],
)
def test_public_request_rejects_server_derived_fields(forged_field):
    with pytest.raises(ValidationError):
        TaskRewardBudgetCreateRequest.model_validate(
            {
                "routing_id": "routing-1",
                "total_budget_points": "100.000000",
                forged_field: "forged",
            }
        )
