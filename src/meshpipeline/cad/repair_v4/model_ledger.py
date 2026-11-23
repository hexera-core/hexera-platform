# Responsibility: The per-wave record of every model call the v4 AI proposer makes, and the spending cap it enforces.
# Owns: WaveLedger - reserve before a call, settle after it, release if it never ran; the wave and per-file totals.
# Boundaries: money and bookkeeping only. It knows no price table (a call arrives already priced by the router),
#             no model and no conversation; the proposer decides what to do with a refusal.
"""The wave ledger.

THE BOUND, AND WHAT IT RESTS ON. A wave runs as N shards on SEPARATE HOSTS, so no host-local lock (flock, a
thread lock) can make one wave cap bind across them, and GCS FUSE has no flock at all. This module therefore
guarantees a bound that needs NO shared state: each shard owns a static slice, `WaveLedger.for_shard(wave_cap,
n_shards, shard_index, path)`, whose cap is `wave_cap / n_shards`. Shards cannot overspend each other's slice, so
the wave total is at most `wave_cap` however the shards are placed, restarted or requeued - at the price of idle
budget in a shard that finishes early. Within a shard, all files share the slice through the locked log below
(`flock` is correct between the processes of ONE host, which is all a shard is).

The slice only holds if the shard's log survives a restart of its process: pass a `path` on durable local disk,
never rely on the in-memory form for a real wave (it exists for tests and single-process runs). A requeue of a
FILE onto another shard starts that file at $0 on the new shard, so the per-file ceiling is best-effort across
shards; the wave bound is not affected.

A shared store (Cloud SQL: one transaction per `admit` under `pg_advisory_xact_lock(hash(wave_id))`, summing open
reservations plus settled spend) would replace the slices and make both caps exact. It plugs in as a `LedgerStore`
(below); it is not built here (it needs a table and a migration and cannot be verified without a database).

TWO-PHASE. `admit` appends a *reservation* (the caller's estimate of the call it is about to make) if, and only
if, slice total + open reservations + the estimate fits the cap and the file's total + its own open reservations +
the estimate fits the file ceiling. `settle` replaces the reservation with the call's real cost; `release` drops
it when the call never ran or failed unbilled. In-flight calls count against the cap while they run. The
proposer's estimate is an upper bound by construction (see proposer.estimate_call_usd), so a settled call does not
exceed its reservation; if one ever did, the overrun is recorded in full and the next `admit` refuses sooner.

A ledger REFUSES a non-finite cap: a hard cap that can be forgotten is not one.

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
from typing import Any, Literal, Protocol

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


class LedgerStore(Protocol):
    """Where the log lives. `transact(fn)` runs `fn(rows, append)` with the log held exclusively across EVERY
    writer that shares it - the guarantee a Cloud SQL implementation would give with an advisory lock."""

    def transact(self, fn): ...


class MemoryStore:
    """One process. Unit tests and single-process runs only."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def transact(self, fn):
        with self._lock:
            return fn(self._rows, self._rows.append)


class FileStore:
    """An append-only JSONL log under `flock`: exclusive among the processes of ONE host."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def transact(self, fn):
        with open(self._path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows: list[dict[str, Any]] = []
                for line in handle:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue          # a torn last line from a killed process is not a row

                def append(row: dict[str, Any]) -> None:
                    handle.seek(0, os.SEEK_END)
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    rows.append(row)

                return fn(rows, append)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class WaveLedger:
    def __init__(self, cap_usd: float, path: str | Path | None = None, *,
                 store: LedgerStore | None = None) -> None:
        if not math.isfinite(cap_usd) or cap_usd <= 0:
            raise ValueError(f"a wave cap must be a finite, positive number of dollars, got {cap_usd!r}")
        self._cap = _micro(cap_usd)
        self._store: LedgerStore = store if store is not None else (
            FileStore(path) if path is not None else MemoryStore())

    @classmethod
    def for_shard(cls, wave_cap_usd: float, n_shards: int, shard_index: int,
                  path: str | Path) -> WaveLedger:
        """This shard's slice of the wave: `wave_cap / n_shards`, in a log at `path` (durable local disk)."""
        if n_shards < 1 or not 0 <= shard_index < n_shards:
            raise ValueError(f"shard {shard_index} of {n_shards} is not a valid shard")
        return cls(wave_cap_usd / n_shards, path)

    @property
    def cap_usd(self) -> float:
        return self._cap / _MICRO

    def _transact(self, fn):
        return self._store.transact(fn)

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

    def mark(self, file_id: str, *, stop_reason: str | None, ai_capped: bool) -> None:
        """Record why the AI stopped in a file, in the ledger itself (latest mark per file wins)."""
        def go(rows, append):
            append({"t": "status", "file": file_id, "stop": stop_reason, "capped": bool(ai_capped),
                    "at": time.time()})
        self._transact(go)

    def file_statuses(self) -> dict[str, dict[str, Any]]:
        def go(rows, _a):
            out: dict[str, dict[str, Any]] = {}
            for r in rows:
                if r["t"] == "status":
                    out[r["file"]] = {"stop_reason": r["stop"], "ai_capped": r["capped"]}
            return out
        return self._transact(go)

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
