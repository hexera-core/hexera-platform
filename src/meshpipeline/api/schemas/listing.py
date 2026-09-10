# src/meshpipeline/api/schemas/listing.py
# Responsibility: Give every collection route one envelope, so a client writes one pager.
# Boundaries: shape only - what a row contains belongs to the route that read it.
from __future__ import annotations

import uuid
from datetime import datetime

from meshpipeline.api import pagination


def page(items: list[dict], *, limit: int, last_key: tuple[datetime, uuid.UUID] | None) -> dict:
    """The one response shape every list route answers with.

    A cursor is minted ONLY for a page that came back full. A short page cannot have more behind
    it, and offering a cursor there costs the caller a round trip that returns nothing.
    """
    full = len(items) == limit
    return {
        "items": items,
        "next_cursor": (pagination.encode_cursor(*last_key)
                        if full and last_key is not None else None),
    }
