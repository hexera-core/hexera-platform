# Responsibility: The per-wave record of every model call the v4 AI proposer makes, and the spending cap it enforces.
# Owns: WaveLedger - reserve before a call, settle after it, release if it never ran; the wave and per-file totals.
# Boundaries: money and bookkeeping only. It knows no price table (a call arrives already priced by the router),
#             no model and no conversation; the proposer decides what to do with a refusal.
"""The wave ledger.

SHARED STATE. Every shard of a wave opens a `WaveLedger` on the SAME `path`. The file is an append-only
JSONL log and every operation - read the totals, decide, append - happens under an exclusive
`fcntl.flock` on it, so two shards can never both be told "there is room" for the last dollar. With no
`path` the ledger is private to one process (guarded by a thread lock): that is the unit-test and
single-process form, and a wave that runs in several processes MUST pass a path.

TWO-PHASE. `admit` appends a *reservation* (the caller's estimate of the call it is about to make) if,
and only if, wave total + open reservations + the estimate fits the wave cap and the file's total +
its own open reservations + the estimate fits the file ceiling. `settle` replaces the reservation with
the call's real cost; `release` drops it when the call never ran or failed unbilled. In-flight calls
are therefore counted against the cap while they run, which is what bounds the overshoot to the gap
between an estimate and a real cost for the calls in flight - not to "one call per shard, unbounded".
A settled call may cost more than its estimate; the overrun is recorded in full and simply makes the
next `admit` refuse sooner.

Money is held as integer micro-dollars so that ten admissions of $0.10 fill a $1.00 cap exactly.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_MICRO = 1_000_000


def _micro(usd: float) -> int:
    if not math.isfinite(usd) or usd < 0:
        raise ValueError(f"a cost must be a finite, non-negative number of dollars, got {usd!r}")
    return round(usd * _MICRO)


@dataclass(frozen=True)
class Admission:
    ok: bool
    #: "wave_cap" or "file_ceiling" when refused
    reason: Literal["wave_cap", "file_ceiling"] | None = None
    #: the id to pass to settle()/release(); None when refused
    reservation: str | None = None


class WaveLedger:
    def __init__(self, cap_usd: float, path: str | Path | None = None) -> None:
        self._cap = _micro(cap_usd) if math.isfinite(cap_usd) else None   # inf = no wave cap
        self._path = Path(path) if path is not None else None
        self._mem: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)

    # --- the locked read-modify-append cycle -----------------------------------------------------

    def _transact(self, fn):
        """Run fn(rows, append) with the log held exclusively; `append` writes one row durably."""
        if self._path is None:
            with self._lock:
                return fn(self._mem, self._mem.append)
        with open(self._path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = []
                for line in handle:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue          # a torn last line from a killed shard is not a row

                def append(row: dict[str, Any]) -> None:
                    handle.seek(0, os.SEEK_END)
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    rows.append(row)

                return fn(rows, append)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _totals(rows: list[dict[str, Any]], file_id: str | None = None) -> tuple[int, int]:
        """(settled, open reservations), in micro-dollars, for the wave or for one file."""
        closed = {r["id"] for r in rows if r["t"] in ("settle", "release")}
        settled = sum(r["u"] for r in rows if r["t"] == "settle" and file_id in (None, r["file"]))
        held = sum(r["u"] for r in rows
                   if r["t"] == "reserve" and r["id"] not in closed and file_id in (None, r["file"]))
        return settled, held

    # --- the three operations --------------------------------------------------------------------

    def admit(self, file_id: str, estimate_usd: float, *, file_ceiling_usd: float) -> Admission:
        est = _micro(estimate_usd)
        ceiling = _micro(file_ceiling_usd)

        def go(rows, append):
            f_settled, f_held = self._totals(rows, file_id)
            if f_settled + f_held + est > ceiling:
                return Admission(False, "file_ceiling")
            if self._cap is not None:
                w_settled, w_held = self._totals(rows)
                if w_settled + w_held + est > self._cap:
                    return Admission(False, "wave_cap")
            rid = uuid.uuid4().hex
            append({"t": "reserve", "id": rid, "file": file_id, "u": est, "at": time.time()})
            return Admission(True, None, rid)
        return self._transact(go)

    def settle(self, reservation: str, *, file_id: str, job_id: str, provider: str, model: str,
               input_tokens: int, cached_input_tokens: int, output_tokens: int, cost_usd: float,
               kind: str = "repair_v4_propose") -> None:
        cost = _micro(cost_usd)

        def go(rows, append):
            if not any(r["t"] == "reserve" and r["id"] == reservation for r in rows):
                raise KeyError(f"no such reservation {reservation!r}")
            if any(r["t"] in ("settle", "release") and r["id"] == reservation for r in rows):
                raise KeyError(f"reservation {reservation!r} is already closed")
            append({"t": "settle", "id": reservation, "file": file_id, "job": job_id, "provider": provider,
                    "model": model, "in": int(input_tokens), "cached": int(cached_input_tokens),
                    "out": int(output_tokens), "u": cost, "kind": kind, "at": time.time()})
        self._transact(go)

    def release(self, reservation: str) -> None:
        def go(rows, append):
            if any(r["t"] in ("settle", "release") and r["id"] == reservation for r in rows):
                return
            append({"t": "release", "id": reservation, "at": time.time()})
        self._transact(go)

    # --- reading ---------------------------------------------------------------------------------

    def spent_usd(self) -> float:
        return self._transact(lambda rows, _a: self._totals(rows)[0]) / _MICRO

    def reserved_usd(self) -> float:
        return self._transact(lambda rows, _a: self._totals(rows)[1]) / _MICRO

    def file_spent_usd(self, file_id: str) -> float:
        return self._transact(lambda rows, _a: self._totals(rows, file_id)[0]) / _MICRO

    def entries(self) -> list[dict[str, Any]]:
        """Every settled call, oldest first, in dollars."""
        def go(rows, _a):
            return [{"file_id": r["file"], "job_id": r["job"], "provider": r["provider"],
                     "model": r["model"], "input_tokens": r["in"], "cached_input_tokens": r["cached"],
                     "output_tokens": r["out"], "usd": r["u"] / _MICRO, "kind": r["kind"], "at": r["at"]}
                    for r in rows if r["t"] == "settle"]
        return self._transact(go)
