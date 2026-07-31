import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.finding import FindingCategory
from app.schemas.node import NodeStatus, normalize_category
from app.schemas.subnet import SubnetMemberStatus, SubnetStatus


ROUTING_VERSION = "subnet_router_v0"
SAFE_ROUTING_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class RoutingStatus(str, Enum):
    CALCULATED = "calculated"
    FINALIZED = "finalized"
    SUPERSEDED = "superseded"


class RoutingSelectionType(str, Enum):
    RANKED = "ranked"
    EXPLORATION = "exploration"


class RoutingAssignmentMode(str, Enum):
    PRODUCTION = "production"
    SHADOW = "shadow"


class RoutingShortageReason(str, Enum):
    NO_SUBNET = "no_subnet"
    SUBNET_NOT_ACTIVE = "subnet_not_active"
    NO_RANKED_MEMBERS = "no_ranked_members"
    INSUFFICIENT_RANKED_MEMBERS = "insufficient_ranked_members"
    NO_EXPLORATION_MEMBERS = "no_exploration_members"
    INSUFFICIENT_EXPLORATION_MEMBERS = "insufficient_exploration_members"
    CATEGORY_NOT_SUPPORTED = "category_not_supported"
    PROJECT_SCOPE_MISSING = "project_scope_missing"
    PROJECT_CATEGORY_MISSING = "project_category_missing"
    NO_ELIGIBLE_NODES = "no_eligible_nodes"


class RoutingProcessingStatus(str, Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


class ProjectRoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[FindingCategory] | None = None
    nodes_per_category: int = Field(default=4, ge=1, le=100)
    include_exploration: bool = True
    allow_partial: bool = True
    description: str | None = Field(default=None, max_length=500)

    @field_validator("categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: Any) -> Any:
        if value is None or not isinstance(value, list):
            return value
        normalized = [normalize_category(category) for category in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Routing categories must be unique")
        return normalized

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class RoutingCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=128)
    subnet_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    node_status: NodeStatus
    membership_status: SubnetMemberStatus
    category_score_id: str | None = Field(default=None, min_length=1, max_length=256)
    category_score: float = Field(..., ge=0.0, le=1.0)
    experience_confidence: float = Field(..., ge=0.0, le=1.0)
    finalized_submissions: int = Field(..., ge=0)
    accepted_unique_submissions: int = Field(..., ge=0)
    membership_source_fingerprint: str
    score_source_fingerprint: str | None = None
    exploration_assignments_before: int = Field(..., ge=0)
    last_exploration_assignment_at: datetime | None = None

    @field_validator(
        "node_id",
        "subnet_id",
        "category_score_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is not None and (
            not SAFE_ROUTING_IDENTIFIER.fullmatch(value) or value in {".", ".."}
        ):
            raise ValueError("Invalid routing snapshot identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_snapshot_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator(
        "membership_source_fingerprint",
        "score_source_fingerprint",
    )
    @classmethod
    def validate_fingerprints(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_HEX.fullmatch(value):
            raise ValueError("Fingerprint must be a lowercase SHA-256 digest")
        return value

    @field_validator("last_exploration_assignment_at")
    @classmethod
    def require_timezone_aware_datetime(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Routing snapshot timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_category_identity(self) -> "RoutingCandidateSnapshot":
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Candidate category must match its subnet")
        return self


class RoutingAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(..., min_length=1, max_length=256)
    routing_id: str = Field(..., min_length=1, max_length=256)
    project_id: str = Field(..., min_length=1, max_length=128)
    subnet_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    node_id: str = Field(..., min_length=1, max_length=128)
    selection_type: RoutingSelectionType
    assignment_mode: RoutingAssignmentMode
    membership_status: SubnetMemberStatus
    category_score: float = Field(..., ge=0.0, le=1.0)
    experience_confidence: float = Field(..., ge=0.0, le=1.0)
    position: int = Field(..., ge=1)
    selection_reasons: list[str] = Field(..., min_length=1)
    candidate_snapshot: RoutingCandidateSnapshot
    qualification_reference: str | None = None
    created_at: datetime

    @field_validator(
        "assignment_id",
        "routing_id",
        "project_id",
        "subnet_id",
        "node_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not SAFE_ROUTING_IDENTIFIER.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid routing assignment identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_assignment_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("selection_reasons")
    @classmethod
    def validate_selection_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("Selection reasons must not be empty")
        return cleaned

    @field_validator("created_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Routing assignment timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_assignment_mapping(self) -> "RoutingAssignment":
        if self.selection_type == RoutingSelectionType.RANKED:
            if self.assignment_mode != RoutingAssignmentMode.PRODUCTION:
                raise ValueError("Ranked assignments must use production mode")
            if self.membership_status not in {
                SubnetMemberStatus.ACTIVE,
                SubnetMemberStatus.EXPERT,
            }:
                raise ValueError("Ranked assignments require active or expert membership")
        else:
            if self.assignment_mode != RoutingAssignmentMode.SHADOW:
                raise ValueError("Exploration assignments must use shadow mode")
            if self.membership_status not in {
                SubnetMemberStatus.CANDIDATE,
                SubnetMemberStatus.PROBATION,
            }:
                raise ValueError(
                    "Exploration assignments require candidate or probation membership"
                )
        if self.qualification_reference is not None:
            raise ValueError("Router v0 does not support qualification references")
        snapshot = self.candidate_snapshot
        if (
            snapshot.node_id != self.node_id
            or snapshot.subnet_id != self.subnet_id
            or snapshot.category != self.category
            or snapshot.membership_status != self.membership_status
            or snapshot.category_score != self.category_score
            or snapshot.experience_confidence != self.experience_confidence
        ):
            raise ValueError("Assignment does not match its candidate snapshot")
        return self


class CategoryRoutingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: FindingCategory
    subnet_id: str | None = Field(default=None, max_length=128)
    subnet_status: SubnetStatus | None = None
    requested_assignments: int = Field(..., ge=1, le=100)
    ranked_target: int = Field(..., ge=0)
    exploration_target: int = Field(..., ge=0)
    ranked_selected: int = Field(..., ge=0)
    exploration_selected: int = Field(..., ge=0)
    total_selected: int = Field(..., ge=0)
    complete: bool
    assignments: list[RoutingAssignment]
    shortage_reasons: list[RoutingShortageReason]
    warnings: list[str]

    @field_validator("category", mode="before")
    @classmethod
    def normalize_result_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("subnet_id")
    @classmethod
    def validate_subnet_id(cls, value: str | None) -> str | None:
        if value is not None and (
            not SAFE_ROUTING_IDENTIFIER.fullmatch(value) or value in {".", ".."}
        ):
            raise ValueError("Invalid routing subnet identifier")
        return value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Routing warnings must not be empty")
        return value

    @model_validator(mode="after")
    def validate_result_totals(self) -> "CategoryRoutingResult":
        if self.ranked_target + self.exploration_target != self.requested_assignments:
            raise ValueError("Routing targets must equal requested assignments")
        if self.ranked_selected + self.exploration_selected != self.total_selected:
            raise ValueError("Selected routing totals do not match")
        if self.total_selected != len(self.assignments):
            raise ValueError("Total selected must equal assignment count")
        if self.total_selected > self.requested_assignments:
            raise ValueError("Routing result exceeds requested assignments")
        ranked = sum(
            item.selection_type == RoutingSelectionType.RANKED
            for item in self.assignments
        )
        exploration = len(self.assignments) - ranked
        if ranked != self.ranked_selected or exploration != self.exploration_selected:
            raise ValueError("Assignment selection types do not match result totals")
        if self.complete and self.shortage_reasons:
            raise ValueError("Complete routing results cannot contain shortages")
        if not self.complete and not self.shortage_reasons:
            raise ValueError("Incomplete routing results require a shortage reason")
        if self.complete != (self.total_selected == self.requested_assignments):
            raise ValueError("Routing completeness does not match selected total")
        node_ids = [assignment.node_id for assignment in self.assignments]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("A node may appear only once within a category")
        assignment_ids = [assignment.assignment_id for assignment in self.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("Assignment identifiers must be unique")
        positions = [assignment.position for assignment in self.assignments]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError("Assignment positions must be contiguous from one")
        for assignment in self.assignments:
            if assignment.category != self.category:
                raise ValueError("Assignment category does not match category result")
            if self.subnet_id is None or assignment.subnet_id != self.subnet_id:
                raise ValueError("Assignment subnet does not match category result")
        return self


class ProjectRoutingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routing_id: str = Field(..., min_length=1, max_length=256)
    routing_version: Literal["subnet_router_v0"] = ROUTING_VERSION
    project_id: str = Field(..., min_length=1, max_length=128)
    project_scope_fingerprint: str
    requested_categories: list[FindingCategory] = Field(..., min_length=1)
    nodes_per_category: int = Field(..., ge=1, le=100)
    include_exploration: bool
    allow_partial: bool
    status: RoutingStatus
    results: list[CategoryRoutingResult] = Field(..., min_length=1)
    total_assignments: int = Field(..., ge=0)
    production_assignments: int = Field(..., ge=0)
    shadow_assignments: int = Field(..., ge=0)
    complete_categories: int = Field(..., ge=0)
    partial_categories: int = Field(..., ge=0)
    failed_categories: int = Field(..., ge=0)
    source_fingerprint: str
    request_fingerprint: str
    supersedes_routing_id: str | None = Field(default=None, min_length=1)
    superseded_by_routing_id: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=500)
    calculated_at: datetime
    finalized_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "routing_id",
        "project_id",
        "supersedes_routing_id",
        "superseded_by_routing_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is not None and (
            not SAFE_ROUTING_IDENTIFIER.fullmatch(value) or value in {".", ".."}
        ):
            raise ValueError("Invalid routing identifier")
        return value

    @field_validator(
        "project_scope_fingerprint",
        "source_fingerprint",
        "request_fingerprint",
    )
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Routing fingerprints must be lowercase SHA-256")
        return value

    @field_validator("requested_categories", mode="before")
    @classmethod
    def normalize_requested_categories(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = [normalize_category(category) for category in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Requested routing categories must be unique")
        if normalized != sorted(normalized):
            raise ValueError("Requested routing categories must be sorted")
        return normalized

    @field_validator(
        "calculated_at",
        "finalized_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def require_timezone_aware_datetime(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Routing timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_record_totals_and_lifecycle(self) -> "ProjectRoutingRecord":
        if self.status == RoutingStatus.FINALIZED and self.finalized_at is None:
            raise ValueError("Finalized routing records require finalized_at")
        if self.status != RoutingStatus.FINALIZED and self.finalized_at is not None:
            raise ValueError("Non-finalized routing records cannot have finalized_at")
        if (
            self.status == RoutingStatus.SUPERSEDED
            and self.superseded_by_routing_id is None
        ):
            raise ValueError("Superseded routing records require their replacement")
        if (
            self.status != RoutingStatus.SUPERSEDED
            and self.superseded_by_routing_id is not None
        ):
            raise ValueError("Only superseded records may reference a replacement")
        if self.supersedes_routing_id == self.routing_id:
            raise ValueError("A routing record cannot supersede itself")
        categories = [result.category for result in self.results]
        if categories != self.requested_categories:
            raise ValueError("Routing results must match requested category order")
        assignments = [
            assignment
            for result in self.results
            for assignment in result.assignments
        ]
        if self.total_assignments != len(assignments):
            raise ValueError("Total assignments do not match category results")
        production = sum(
            item.assignment_mode == RoutingAssignmentMode.PRODUCTION
            for item in assignments
        )
        if self.production_assignments != production:
            raise ValueError("Production assignment total does not match")
        if self.shadow_assignments != len(assignments) - production:
            raise ValueError("Shadow assignment total does not match")
        complete = sum(result.complete for result in self.results)
        failed = sum(
            not result.complete and result.total_selected == 0
            for result in self.results
        )
        partial = len(self.results) - complete - failed
        if (
            self.complete_categories != complete
            or self.partial_categories != partial
            or self.failed_categories != failed
        ):
            raise ValueError("Category completion totals do not match results")
        assignment_ids = [item.assignment_id for item in assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("Assignment identifiers must be globally unique per route")
        for assignment in assignments:
            if (
                assignment.routing_id != self.routing_id
                or assignment.project_id != self.project_id
            ):
                raise ValueError("Assignment identity does not match routing record")
        return self


class RoutingUsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage_event_id: str = Field(..., min_length=1, max_length=256)
    routing_id: str = Field(..., min_length=1, max_length=256)
    assignment_id: str = Field(..., min_length=1, max_length=256)
    project_id: str = Field(..., min_length=1, max_length=128)
    subnet_id: str = Field(..., min_length=1, max_length=128)
    category: FindingCategory
    node_id: str = Field(..., min_length=1, max_length=128)
    selection_type: RoutingSelectionType
    assignment_mode: RoutingAssignmentMode
    source_fingerprint: str
    applied_at: datetime

    @field_validator(
        "usage_event_id",
        "routing_id",
        "assignment_id",
        "project_id",
        "subnet_id",
        "node_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not SAFE_ROUTING_IDENTIFIER.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid routing usage identifier")
        return value

    @field_validator("category", mode="before")
    @classmethod
    def normalize_usage_category(cls, value: Any) -> str:
        return normalize_category(value)

    @field_validator("source_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("Usage fingerprint must be a lowercase SHA-256")
        return value

    @field_validator("applied_at")
    @classmethod
    def require_timezone_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Routing usage timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_usage_mapping(self) -> "RoutingUsageEvent":
        if self.selection_type == RoutingSelectionType.RANKED:
            if self.assignment_mode != RoutingAssignmentMode.PRODUCTION:
                raise ValueError("Ranked usage requires production mode")
        elif self.assignment_mode != RoutingAssignmentMode.SHADOW:
            raise ValueError("Exploration usage requires shadow mode")
        if self.subnet_id != f"subnet_{self.category.value}":
            raise ValueError("Usage category must match subnet")
        return self


class RoutingUsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_assignments: int = Field(..., ge=0)
    ranked_assignments: int = Field(..., ge=0)
    exploration_assignments: int = Field(..., ge=0)
    last_assignment_at: datetime | None = None
    last_exploration_assignment_at: datetime | None = None

    @field_validator("last_assignment_at", "last_exploration_assignment_at")
    @classmethod
    def require_timezone_aware_datetime(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Routing usage timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_usage_totals(self) -> "RoutingUsageSummary":
        if (
            self.ranked_assignments + self.exploration_assignments
            != self.total_assignments
        ):
            raise ValueError("Routing usage totals do not match")
        if (
            self.last_exploration_assignment_at is not None
            and self.last_assignment_at is None
        ):
            raise ValueError("Exploration usage requires a last assignment time")
        return self


class RoutingCalculationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RoutingProcessingStatus
    record: ProjectRoutingRecord
    message: str = Field(..., min_length=1)


class RoutingFinalizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RoutingProcessingStatus
    record: ProjectRoutingRecord
    usage_events_created: int = Field(..., ge=0)
    usage_events_existing: int = Field(..., ge=0)
    message: str = Field(..., min_length=1)


class ProjectRoutingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1)
    total: int = Field(..., ge=0)
    records: list[ProjectRoutingRecord]

    @model_validator(mode="after")
    def validate_total(self) -> "ProjectRoutingListResponse":
        if self.total != len(self.records):
            raise ValueError("Routing list total does not match records")
        return self


class RoutingFinalizeRequest(BaseModel):
    """Empty body that rejects client-controlled finalization values."""

    model_config = ConfigDict(extra="forbid")
