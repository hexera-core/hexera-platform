# Responsibility: The per-file record of one v4 search - every decision the driver made and why.
# Owns: `Trace`, an append-only list of JSON-safe events with a sequence number and elapsed time.
# Boundaries: knows nothing about geometry or the search's types; the driver decides what to record
#             (search.py documents the event kinds). Whoever debugs a bad repair reads this file.
"""The search trace.

A human asking "why did the search ship this, or give up here?" needs: what the start scored, which
state was expanded and what it looked like, every candidate with where it came from and what
happened to it (rejected by validation with the messages, declined, failed, a duplicate, skipped
for budget, or evaluated with its score), which states were confirmed by the full panel, when the
best improved, and exactly why the search stopped with the budget it had spent. Events are plain
dicts so the file can be diffed, grepped and loaded without this package.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from meshpipeline.cad.repair_v5 import events as EV


def jsonable(value: Any) -> Any:
    """`value` with tuples as lists, non-finite floats as strings, and unknown objects as `repr`."""
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return repr(value)


class Trace:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._t0 = clock()
        self.events: list[dict] = []

    def record(self, kind: str, **fields: Any) -> None:
        event = {
            "seq": len(self.events),
            "t": round(self._clock() - self._t0, 4),
            "kind": kind,
            **{k: jsonable(v) for k, v in fields.items()},
        }
        self.events.append(event)
        # CRS v5 observability: every trace event also streams to the event log (when one is configured), so a file
        # killed before `write` still leaves its trace up to the kill
        EV.emit(f"trace.{kind}", **{k: v for k, v in event.items() if k not in ("seq", "kind")})

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]

    def to_dict(self) -> dict:
        return {"events": list(self.events)}

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1))


__all__ = ["Trace", "jsonable"]
