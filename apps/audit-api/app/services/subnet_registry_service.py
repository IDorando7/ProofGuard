import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.schemas.finding import FindingCategory
from app.schemas.node import normalize_category
from app.schemas.subnet import (
    SUBNET_MEMBERSHIP_VERSION,
    SUBNET_REGISTRY_VERSION,
    SubnetCreate,
    SubnetMemberRecord,
    SubnetMemberStatus,
    SubnetRecord,
    SubnetStatus,
    SubnetStatusChangeRequest,
    SubnetUpdate,
)
from app.services.node_registry_service import InvalidNodeIdentifierError, load_node


SUBNET_FILENAME = "subnet.json"
MEMBERS_DIRECTORY = "members"
SAFE_SUBNET_ID = re.compile(r"^subnet_[a-z0-9]+(?:_[a-z0-9]+)*$")
SAFE_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class SubnetRegistryError(ValueError):
    """Base error for subnet registry domain failures."""


class SubnetNotFoundError(SubnetRegistryError):
    pass


class SubnetAlreadyExistsError(SubnetRegistryError):
    pass


class SubnetCategoryAlreadyRegisteredError(SubnetAlreadyExistsError):
    pass


class UnsupportedSubnetCategoryError(SubnetRegistryError):
    pass


class InvalidSubnetIdentifierError(SubnetRegistryError):
    pass


class InvalidSubnetStatusTransitionError(SubnetRegistryError):
    pass


class SubnetInactiveError(SubnetRegistryError):
    pass


class SubnetArchivedError(SubnetRegistryError):
    pass


class SubnetMemberNotFoundError(SubnetRegistryError):
    pass


class SubnetMemberCategoryMismatchError(SubnetRegistryError):
    pass


class SubnetNodeNotFoundError(SubnetRegistryError):
    pass


class SubnetStorageError(SubnetRegistryError):
    pass


def get_subnets_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "subnets"


def get_subnet_dir(protocol_data_root: Path, subnet_id: str) -> Path:
    _validate_subnet_id(subnet_id)
    subnets_root = get_subnets_root(protocol_data_root)
    subnet_dir = subnets_root / subnet_id
    _ensure_path_within(subnet_dir, subnets_root, "Invalid subnet identifier")
    return subnet_dir


def get_subnet_members_root(protocol_data_root: Path, subnet_id: str) -> Path:
    subnet_dir = get_subnet_dir(protocol_data_root, subnet_id)
    members_root = subnet_dir / MEMBERS_DIRECTORY
    _ensure_path_within(members_root, subnet_dir, "Invalid subnet identifier")
    return members_root


def build_subnet_id(category: str | FindingCategory) -> str:
    normalized = _normalize_service_category(category)
    subnet_id = f"subnet_{normalized}"
    _validate_subnet_id(subnet_id)
    return subnet_id


def create_subnet(protocol_data_root: Path, payload: SubnetCreate) -> SubnetRecord:
    category = _normalize_service_category(payload.category)
    subnet_id = build_subnet_id(category)
    if load_subnet(protocol_data_root, subnet_id) is not None:
        raise SubnetCategoryAlreadyRegisteredError(
            f"A subnet is already registered for category '{category}'"
        )

    now = _utc_now()
    subnet = SubnetRecord(
        subnet_id=subnet_id,
        registry_version=SUBNET_REGISTRY_VERSION,
        category=category,
        name=payload.name or _default_subnet_name(category),
        description=payload.description,
        status=SubnetStatus.ACTIVE,
        minimum_category_score=payload.minimum_category_score,
        minimum_finalized_submissions=payload.minimum_finalized_submissions,
        maximum_active_nodes=payload.maximum_active_nodes,
        exploration_ratio=payload.exploration_ratio,
        status_reason="Subnet registered.",
        created_at=now,
        updated_at=now,
        status_updated_at=now,
    )
    _write_subnet(protocol_data_root, subnet)
    return subnet


def save_subnet(protocol_data_root: Path, subnet: SubnetRecord) -> SubnetRecord:
    existing = load_subnet(protocol_data_root, subnet.subnet_id)
    values = subnet.model_dump()
    if existing is not None:
        values.update(
            {
                "subnet_id": existing.subnet_id,
                "category": existing.category,
                "registry_version": existing.registry_version,
                "created_at": existing.created_at,
            }
        )
    values["updated_at"] = _utc_now()
    saved = SubnetRecord.model_validate(values)
    _write_subnet(protocol_data_root, saved)
    return saved


def load_subnet(protocol_data_root: Path, subnet_id: str) -> SubnetRecord | None:
    path = get_subnet_dir(protocol_data_root, subnet_id) / SUBNET_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise SubnetStorageError("Stored subnet record is not a regular file")
    try:
        subnet = SubnetRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise SubnetStorageError(f"Stored subnet record is malformed for subnet '{subnet_id}'") from exc
    if subnet.subnet_id != subnet_id:
        raise SubnetStorageError("Stored subnet identifier does not match its directory")
    return subnet


def find_subnet_by_category(
    protocol_data_root: Path,
    category: str | FindingCategory,
) -> SubnetRecord | None:
    return load_subnet(protocol_data_root, build_subnet_id(category))


def list_subnets(
    protocol_data_root: Path,
    category: str | None = None,
    status: SubnetStatus | None = None,
) -> list[SubnetRecord]:
    normalized_category = _normalize_service_category(category) if category is not None else None
    subnets_root = get_subnets_root(protocol_data_root)
    if not subnets_root.exists():
        return []

    subnets: list[SubnetRecord] = []
    for path in sorted(subnets_root.glob(f"*/{SUBNET_FILENAME}")):
        subnet = load_subnet(protocol_data_root, path.parent.name)
        if subnet is None:
            continue
        if normalized_category is not None and subnet.category.value != normalized_category:
            continue
        if status is not None and subnet.status != status:
            continue
        subnets.append(subnet)
    return sorted(subnets, key=lambda subnet: (subnet.category.value, subnet.subnet_id))


def update_subnet(
    protocol_data_root: Path,
    subnet_id: str,
    update: SubnetUpdate,
) -> SubnetRecord:
    existing = load_subnet(protocol_data_root, subnet_id)
    if existing is None:
        raise SubnetNotFoundError("Subnet not found")
    if existing.status == SubnetStatus.ARCHIVED:
        raise SubnetArchivedError("Archived subnet configuration cannot be modified")

    update_data = update.model_dump(exclude_unset=True)
    if update_data.get("name", existing.name) is None:
        raise SubnetRegistryError("Subnet name cannot be null")
    non_nullable_fields = {
        "minimum_category_score",
        "minimum_finalized_submissions",
        "maximum_active_nodes",
        "exploration_ratio",
    }
    if any(field in update_data and update_data[field] is None for field in non_nullable_fields):
        raise SubnetRegistryError("Subnet configuration values cannot be null")

    updated = SubnetRecord.model_validate({**existing.model_dump(), **update_data})
    return save_subnet(protocol_data_root, updated)


def change_subnet_status(
    protocol_data_root: Path,
    subnet_id: str,
    request: SubnetStatusChangeRequest,
) -> SubnetRecord:
    existing = load_subnet(protocol_data_root, subnet_id)
    if existing is None:
        raise SubnetNotFoundError("Subnet not found")
    if existing.status == request.status:
        return existing
    if existing.status == SubnetStatus.ARCHIVED:
        raise InvalidSubnetStatusTransitionError("Archived subnets cannot change status")

    now = _utc_now()
    updated = SubnetRecord.model_validate(
        {
            **existing.model_dump(),
            "status": request.status,
            "status_reason": request.reason,
            "status_updated_at": now,
            "updated_at": now,
        }
    )
    return save_subnet(protocol_data_root, updated)


def is_subnet_active(protocol_data_root: Path, subnet_id: str) -> bool:
    subnet = load_subnet(protocol_data_root, subnet_id)
    return subnet is not None and subnet.status == SubnetStatus.ACTIVE


def ensure_subnet_active(protocol_data_root: Path, subnet_id: str) -> SubnetRecord:
    subnet = load_subnet(protocol_data_root, subnet_id)
    if subnet is None:
        raise SubnetNotFoundError("Subnet not found")
    if subnet.status == SubnetStatus.ARCHIVED:
        raise SubnetArchivedError("Subnet is archived")
    if subnet.status != SubnetStatus.ACTIVE:
        raise SubnetInactiveError(f"Subnet is not active (status: {subnet.status.value})")
    return subnet


def bootstrap_default_subnets(protocol_data_root: Path) -> list[SubnetRecord]:
    for category in sorted(FindingCategory, key=lambda item: item.value):
        if find_subnet_by_category(protocol_data_root, category) is None:
            create_subnet(protocol_data_root, SubnetCreate(category=category))
    return list_subnets(protocol_data_root)


def list_subnet_members(
    protocol_data_root: Path,
    subnet_id: str,
    status: SubnetMemberStatus | None = None,
    minimum_category_score: float | None = None,
    minimum_finalized_submissions: int | None = None,
) -> list[SubnetMemberRecord]:
    subnet = load_subnet(protocol_data_root, subnet_id)
    if subnet is None:
        raise SubnetNotFoundError("Subnet not found")
    members_root = get_subnet_members_root(protocol_data_root, subnet_id)
    if not members_root.exists():
        return []

    members: list[SubnetMemberRecord] = []
    for path in sorted(members_root.glob("*.json")):
        member = load_subnet_member(protocol_data_root, subnet_id, path.stem)
        if member is None:
            continue
        if status is not None and member.status != status:
            continue
        if (
            minimum_category_score is not None
            and member.category_score < minimum_category_score
        ):
            continue
        if (
            minimum_finalized_submissions is not None
            and member.finalized_submissions < minimum_finalized_submissions
        ):
            continue
        members.append(member)
    if any(member.rank is not None for member in members):
        return sorted(
            members,
            key=lambda member: (
                member.rank is None,
                member.rank if member.rank is not None else 0,
                -member.category_score,
                member.node_id,
            ),
        )
    tier_order = {
        SubnetMemberStatus.EXPERT: 0,
        SubnetMemberStatus.ACTIVE: 1,
        SubnetMemberStatus.PROBATION: 2,
        SubnetMemberStatus.CANDIDATE: 3,
        SubnetMemberStatus.SUSPENDED: 4,
        SubnetMemberStatus.REMOVED: 5,
    }
    return sorted(
        members,
        key=lambda member: (
            tier_order[member.status],
            -member.category_score,
            -member.finalized_submissions,
            member.node_id,
        ),
    )


def load_subnet_member(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
) -> SubnetMemberRecord | None:
    subnet = load_subnet(protocol_data_root, subnet_id)
    if subnet is None:
        raise SubnetNotFoundError("Subnet not found")
    path = _get_subnet_member_path(protocol_data_root, subnet_id, node_id)
    if not path.exists():
        return None
    if not path.is_file():
        raise SubnetStorageError("Stored subnet member record is not a regular file")
    try:
        raw_member = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise SubnetStorageError(
            f"Stored member record is malformed for node '{node_id}'"
        ) from exc
    if not isinstance(raw_member, dict):
        raise SubnetStorageError(f"Stored member record is malformed for node '{node_id}'")
    if raw_member.get("subnet_id") != subnet_id:
        raise SubnetMemberCategoryMismatchError("Member subnet does not match the requested subnet")
    try:
        stored_category = _normalize_service_category(raw_member.get("category"))
    except UnsupportedSubnetCategoryError as exc:
        raise SubnetStorageError(
            f"Stored member record is malformed for node '{node_id}'"
        ) from exc
    if stored_category != subnet.category.value:
        raise SubnetMemberCategoryMismatchError("Member category does not match the subnet category")
    try:
        member = SubnetMemberRecord.model_validate(raw_member)
    except ValidationError as exc:
        raise SubnetStorageError(
            f"Stored member record is malformed for node '{node_id}'"
        ) from exc
    if member.subnet_id != subnet_id:
        raise SubnetMemberCategoryMismatchError("Member subnet does not match the requested subnet")
    if member.category != subnet.category:
        raise SubnetMemberCategoryMismatchError("Member category does not match the subnet category")
    if member.node_id != node_id:
        raise SubnetStorageError("Stored member node identifier does not match its filename")
    return member


def save_subnet_member(
    protocol_data_root: Path,
    member: SubnetMemberRecord,
    evaluated_at: datetime | None = None,
    preserve_assignment_state: bool = False,
) -> SubnetMemberRecord:
    subnet = load_subnet(protocol_data_root, member.subnet_id)
    if subnet is None:
        raise SubnetNotFoundError("Subnet not found")
    if member.category != subnet.category:
        raise SubnetMemberCategoryMismatchError("Member category does not match the subnet category")

    try:
        node = load_node(protocol_data_root, member.node_id)
    except InvalidNodeIdentifierError as exc:
        raise InvalidSubnetIdentifierError("Invalid node identifier") from exc
    if node is None:
        raise SubnetNodeNotFoundError("Node not found")

    existing = load_subnet_member(protocol_data_root, member.subnet_id, member.node_id)
    values = member.model_dump()
    now = evaluated_at or _utc_now()
    if existing is not None:
        values["joined_at"] = existing.joined_at
        if preserve_assignment_state:
            values.update(
                {
                    "exploration_assignments": existing.exploration_assignments,
                    "last_assigned_at": existing.last_assigned_at,
                }
            )
        values["status_updated_at"] = (
            existing.status_updated_at or existing.updated_at
            if existing.status == member.status
            else member.status_updated_at or now
        )
    values.update(
        {
            "subnet_id": subnet.subnet_id,
            "category": subnet.category,
            "membership_version": SUBNET_MEMBERSHIP_VERSION,
            "updated_at": now,
        }
    )
    saved = SubnetMemberRecord.model_validate(values)
    _write_subnet_member(protocol_data_root, saved)
    return saved


def _default_subnet_name(category: str) -> str:
    return f"{category.replace('_', ' ').title()} Subnet"


def _normalize_service_category(category: str | FindingCategory) -> str:
    try:
        return normalize_category(category)
    except (TypeError, ValueError) as exc:
        raise UnsupportedSubnetCategoryError(str(exc)) from exc


def _validate_subnet_id(subnet_id: str) -> None:
    if (
        not isinstance(subnet_id, str)
        or len(subnet_id) > 128
        or not SAFE_SUBNET_ID.fullmatch(subnet_id)
        or subnet_id in {".", ".."}
        or "/" in subnet_id
        or "\\" in subnet_id
        or Path(subnet_id).is_absolute()
        or ".." in subnet_id
    ):
        raise InvalidSubnetIdentifierError("Invalid subnet identifier")


def _validate_member_node_id(node_id: str) -> None:
    if (
        not isinstance(node_id, str)
        or not SAFE_NODE_ID.fullmatch(node_id)
        or node_id in {".", ".."}
        or "/" in node_id
        or "\\" in node_id
        or Path(node_id).is_absolute()
    ):
        raise InvalidSubnetIdentifierError("Invalid node identifier")


def _get_subnet_member_path(
    protocol_data_root: Path,
    subnet_id: str,
    node_id: str,
) -> Path:
    _validate_member_node_id(node_id)
    members_root = get_subnet_members_root(protocol_data_root, subnet_id)
    path = members_root / f"{node_id}.json"
    _ensure_path_within(path, members_root, "Invalid node identifier")
    return path


def _ensure_path_within(path: Path, root: Path, message: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidSubnetIdentifierError(message) from exc


def _write_subnet(protocol_data_root: Path, subnet: SubnetRecord) -> None:
    subnet_dir = get_subnet_dir(protocol_data_root, subnet.subnet_id)
    subnet_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(subnet_dir / SUBNET_FILENAME, subnet.model_dump(mode="json"))


def _write_subnet_member(protocol_data_root: Path, member: SubnetMemberRecord) -> None:
    members_root = get_subnet_members_root(protocol_data_root, member.subnet_id)
    members_root.mkdir(parents=True, exist_ok=True)
    output_path = _get_subnet_member_path(protocol_data_root, member.subnet_id, member.node_id)
    _write_json_atomically(output_path, member.model_dump(mode="json"))


def _write_json_atomically(output_path: Path, payload: dict[str, object]) -> None:
    serialized = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.stem}-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(output_path)
    except OSError as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise SubnetStorageError("Unable to persist subnet registry record") from exc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
