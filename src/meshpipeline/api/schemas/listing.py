# Responsibility: Give every collection route one envelope, so a client writes one pager.
# Boundaries: shape only - what a row contains belongs to the route that read it.
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from meshpipeline.api import pagination


def look_ahead(limit: int) -> int:
    """How many rows to ASK the repository for, to page a `limit`-sized window.

    ONE MORE THAN THE PAGE. A full page is not evidence that anything follows it: when the rows
    that remain happen to number exactly `limit`, "the page came back full" and "there is another
    page" are indistinguishable, and minting a cursor on that basis hands the user an "Older"
    link that opens an empty table. The extra row is the only cheap way to tell the two apart,
    and it is discarded by `page()` rather than rendered.
    """
    return limit + 1


def page(rows: list, *, limit: int, item: Callable[[Any], dict],
         key: Callable[[Any], tuple[datetime, uuid.UUID]]) -> dict:
    """The one response shape every list route answers with.

    `rows` is what the repository returned for `look_ahead(limit)` - so at most one more than the
    caller asked for. `item` renders a row for the wire. `key` maps a row to its
    `(created_at, id)` cursor pair, and is applied to the LAST ROW OF THE PAGE, never to the
    look-ahead row, which is where the NEXT page begins and which is discarded here.

    Taking both callables rather than a pre-rendered list is what keeps the trim in one place:
    a route that rendered its own items would have to remember to drop the look-ahead row first,
    and forgetting is silent - one extra row on every page.
    """
    has_more = len(rows) > limit
    window = rows[:limit]
    return {
        "items": [item(row) for row in window],
        "next_cursor": (pagination.encode_cursor(*key(window[-1]))
                        if has_more and window else None),
    }
