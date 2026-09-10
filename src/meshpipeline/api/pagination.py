# src/meshpipeline/api/pagination.py
# Responsibility: Encode and decode the opaque cursor every collection route paginates on.
# Owns: the rule that a cursor this module did not mint reads as "no cursor", never as an error.
# Boundaries: strings and tuples; which table is being paged belongs to the repository.
from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

#: The page size a caller gets without asking, and the ceiling it cannot exceed. The ceiling is
#: what stops one request scanning an entire tenant's history.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_SEPARATOR = "|"


@dataclass(frozen=True)
class Page:
    """One page of rows plus the cursor that reaches the next one.

    `next_cursor` is None on the last page. It is set only when the page came back FULL: a short
    page cannot have more behind it, and minting a cursor there would give the caller one more
    round trip that returns nothing.
    """

    items: list
    next_cursor: str | None


def clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}{_SEPARATOR}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """The values a cursor names, or None for anything this module did not mint.

    FAILS TO None, NEVER TO AN EXCEPTION. A cursor arrives in a query string, so any caller can
    send any bytes; raising here would turn a typo in a URL into a 500 for a proven caller. Reading
    a bad cursor as "start at the beginning" narrows rather than widens - the caller sees their own
    first page, never somebody else's rows, because the tenant predicate is applied independently.
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    head, separator, tail = raw.partition(_SEPARATOR)
    if not separator:
        return None
    try:
        return datetime.fromisoformat(head), uuid.UUID(tail)
    except (ValueError, AttributeError):
        return None
