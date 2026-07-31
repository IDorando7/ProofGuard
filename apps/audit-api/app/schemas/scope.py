from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


AttackCategory = Literal[
    "access_control",
    "reentrancy",
    "oracle_manipulation",
    "accounting",
    "upgradeability",
    "mev",
    "governance",
    "cross_chain",
    "liquidation",
    "dos",
    "other",
]


class ScopeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(min_length=1)
    language: str = Field(min_length=1)
    framework: str = Field(min_length=1)
    chain: str | None = None
    commit_hash: str | None = None
    contracts_in_scope: list[str] = Field(min_length=1)
    contracts_out_of_scope: list[str] = Field(default_factory=list)
    assets_at_risk: list[str] = Field(default_factory=list)
    attack_categories: list[AttackCategory] = Field(min_length=1)
    known_issues: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)


class ScopeSummary(BaseModel):
    language: str
    framework: str
    contracts_in_scope_count: int
    attack_categories: list[str]

