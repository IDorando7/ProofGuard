from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class FindingCategory(str, Enum):
    ACCESS_CONTROL = "access_control"
    REENTRANCY = "reentrancy"
    ORACLE_MANIPULATION = "oracle_manipulation"
    ACCOUNTING = "accounting"
    UPGRADEABILITY = "upgradeability"
    MEV = "mev"
    GOVERNANCE = "governance"
    CROSS_CHAIN = "cross_chain"
    LIQUIDATION = "liquidation"
    DOS = "dos"
    OTHER = "other"


class FindingSeverity(str, Enum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INFORMATIONAL = "Informational"


class FindingStatus(str, Enum):
    CANDIDATE = "candidate"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


class FindingCreate(BaseModel):
    title: str = Field(..., min_length=5)
    category: FindingCategory
    severity: FindingSeverity = FindingSeverity.INFORMATIONAL
    confidence: float = Field(..., ge=0.0, le=1.0)

    contracts: List[str] = Field(default_factory=list)
    functions: List[str] = Field(default_factory=list)

    root_cause: str = Field(..., min_length=5)
    attack_path: str = Field(..., min_length=5)
    impact: str = Field(..., min_length=5)
    conditions: Optional[str] = None

    reproduction_steps: List[str] = Field(default_factory=list)
    poc_type: str = "none"
    poc_file: Optional[str] = None

    recommended_fix: str = Field(..., min_length=5)

    status: FindingStatus = FindingStatus.CANDIDATE
    agent_name: str


class Finding(FindingCreate):
    finding_id: str
    project_id: str