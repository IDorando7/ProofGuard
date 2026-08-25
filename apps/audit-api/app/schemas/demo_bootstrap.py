from pydantic import BaseModel, Field

from app.schemas.finding import FindingCategory
from app.schemas.subnet import SubnetMemberStatus


class DemoBootstrapNodeResult(BaseModel):
    node_id: str
    operator_id: str
    category: FindingCategory
    runtime_type: str
    history_profile: str
    category_score: float = Field(..., ge=0.0, le=1.0)
    finalized_submissions: int = Field(..., ge=0)
    membership_status: SubnetMemberStatus


class DemoBootstrapCategoryResult(BaseModel):
    category: FindingCategory
    active_members: int = Field(..., ge=0)
    candidate_members: int = Field(..., ge=0)
    probation_members: int = Field(..., ge=0)
    nodes: list[DemoBootstrapNodeResult]


class DemoBootstrapResult(BaseModel):
    bootstrap_version: str
    historical_submission_count: int = Field(..., ge=0)
    runtime_node_ids: list[str]
    categories: list[DemoBootstrapCategoryResult]

