from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.reward import RewardPoolKind
from app.schemas.validator_reward import RewardPoolConsumption
from app.utils.protocol_serialization import atomic_create_json, protocol_fingerprint


class RewardPoolConsumptionError(ValueError):
    pass


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


def _identifier(value: str, label: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value) or value in {".", ".."} or "/" in value or "\\" in value:
        raise RewardPoolConsumptionError(f"Invalid {label} identifier")
    return value


def get_reward_pool_consumption_path(
    protocol_data_root: Path, task_reward_budget_id: str, pool_kind: RewardPoolKind
) -> Path:
    _identifier(task_reward_budget_id, "task reward budget")
    pool_kind = RewardPoolKind(pool_kind)
    return (
        protocol_data_root
        / "task-rewards"
        / "pool-consumptions"
        / task_reward_budget_id
        / f"{pool_kind.value}.json"
    )


def load_reward_pool_consumption(
    protocol_data_root: Path, task_reward_budget_id: str, pool_kind: RewardPoolKind
) -> RewardPoolConsumption | None:
    pool_kind = RewardPoolKind(pool_kind)
    path = get_reward_pool_consumption_path(
        protocol_data_root, task_reward_budget_id, pool_kind
    )
    if not path.exists():
        return None
    try:
        value = RewardPoolConsumption.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise RewardPoolConsumptionError("Stored reward-pool consumption is malformed") from exc
    if value.task_reward_budget_id != task_reward_budget_id or value.pool_kind != pool_kind:
        raise RewardPoolConsumptionError("Reward-pool consumption identity is corrupt")
    return value


def consume_reward_pool(
    protocol_data_root: Path,
    *,
    task_reward_budget_id: str,
    pool_kind: RewardPoolKind,
    reward_cycle_id: str,
    consumed_points,
    finalization_source_fingerprint: str,
) -> tuple[RewardPoolConsumption, bool]:
    pool_kind = RewardPoolKind(pool_kind)
    payload = {
        "consumption_version": "reward_pool_consumption_v1",
        "task_reward_budget_id": task_reward_budget_id,
        "pool_kind": pool_kind.value,
        "reward_cycle_id": reward_cycle_id,
        "consumed_points": consumed_points,
        "finalization_source_fingerprint": finalization_source_fingerprint,
    }
    value = RewardPoolConsumption(
        reward_pool_consumption_id="reward_pool_consumption_" + protocol_fingerprint(payload),
        task_reward_budget_id=task_reward_budget_id,
        pool_kind=pool_kind,
        reward_cycle_id=reward_cycle_id,
        consumed_points=consumed_points,
        source_fingerprint=protocol_fingerprint(payload),
        finalized_at=datetime.now(timezone.utc),
    )
    path = get_reward_pool_consumption_path(
        protocol_data_root, task_reward_budget_id, pool_kind
    )
    if atomic_create_json(path, value, temporary_prefix=".reward-pool-consumption-"):
        return value, True
    existing = load_reward_pool_consumption(
        protocol_data_root, task_reward_budget_id, pool_kind
    )
    if existing is None:
        raise RewardPoolConsumptionError("Reward-pool consumption disappeared")
    if (
        existing.reward_cycle_id != reward_cycle_id
        or existing.consumed_points != value.consumed_points
        or existing.source_fingerprint != value.source_fingerprint
    ):
        raise RewardPoolConsumptionError(
            f"The {pool_kind.value} pool has already been consumed by another reward cycle"
        )
    return existing, False
