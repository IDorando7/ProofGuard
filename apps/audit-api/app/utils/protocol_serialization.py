from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def canonical_value(value: Any) -> Any:
    """Convert protocol values to their deterministic JSON representation."""
    if isinstance(value, BaseModel):
        return canonical_value(value.model_dump())
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key.value if isinstance(key, Enum) else key): canonical_value(nested)
            for key, nested in sorted(
                value.items(),
                key=lambda item: str(
                    item[0].value if isinstance(item[0], Enum) else item[0]
                ),
            )
        }
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        canonical_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def protocol_fingerprint(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    temporary_prefix: str = ".protocol-",
) -> None:
    """Write human-readable UTF-8 JSON with fsync and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = human_json(payload)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=temporary_prefix,
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        Path(temporary_name).replace(path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def atomic_create_json(
    path: Path,
    payload: Any,
    *,
    temporary_prefix: str = ".protocol-create-",
) -> bool:
    """Create JSON without replacing an existing record, returning creation status."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=temporary_prefix,
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(human_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def human_json(payload: Any) -> str:
    json_payload = (
        payload.model_dump(mode="json")
        if isinstance(payload, BaseModel)
        else payload
    )
    return json.dumps(
        json_payload,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
