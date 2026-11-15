"""Shared helpers: time, ISO formatting, opaque keyset cursors."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import UUID

from sg_commons.errors import ValidationError


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, rid = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(at), UUID(rid)
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid cursor") from exc
