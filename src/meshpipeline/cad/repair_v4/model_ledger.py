# Responsibility: The per-wave record of every model call the v4 AI proposer makes, and the spending cap it enforces.
# Owns: WaveLedger - reserve before a call, settle after it, release if it never ran; the wave and per-file totals.
# Boundaries: money and bookkeeping only. It knows no price table (a call arrives already priced by the router),
#             no model and no conversation; the proposer decides what to do with a refusal.
"""The wave ledger.

THE BOUND, AND WHAT IT RESTS ON. A wave runs as N shards on SEPARATE HOSTS, so no host-local lock (flock, a
thread lock) can make one wave cap bind across them, and GCS FUSE has no flock at all. This module therefore
guarantees a bound that needs NO shared state: each shard owns a static slice, `WaveLedger.for_shard(wave,
wave_id=, n_shards=, shard_index=, path=, markers=)`, whose cap is `WAVE_CAPS_USD[wave] / n_shards`. Shards cannot overspend each other's slice, so
the wave total is at most the wave's cap however the shards are placed, restarted or requeued - at the price of idle
budget in a shard that finishes early. Within a shard, all files share the slice through the locked log below
(`flock` is correct between the processes of ONE host, which is all a shard is).

The slice only holds if the shard's log survives a restart of its process: pass a `path` on durable local disk,
never rely on the in-memory form for a real wave (it exists for tests and single-process runs). That assumption is
CHECKED, not hoped for: the first start of a shard writes a marker to a shared `MarkerStore` (the wave bucket);
a shard that finds its marker but no local log raises `LostShardLog` instead of starting again at $0 (a lost log
would otherwise let a wave with R retries per shard cost (1+R) x its cap). The wave's own marker records the wave
type and shard count N; a shard configured with a different N, wave type or a duplicate/foreign log identity
raises `ShardArithmeticError`. The launcher's N is the single source of truth: shard_index is part of the log's
identity (an `init` row), so two shards cannot share a log or an index silently.

Caps are named, not typed: `WAVE_CAPS_USD` holds the owner's limits and `for_shard` takes the wave TYPE.
The raw constructor is closed: `for_shard` is the only production way to get a ledger (it applies the marker, log and
type checks), and `for_test` - which refuses to run outside pytest - is the only other. `AIProposer` refuses a
ledger that came from neither. (How MANY checkpoint waves may run - at most
3 - or that the held-out wave runs once is not enforceable here and belongs to the launcher.)

A requeue of a FILE onto another shard starts that file at $0 on the new shard, so the per-file ceiling is
best-effort across shards; the wave bound is not affected.

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

ROLLBACK AND TRUNCATION. After every settle the shard writes its settled total to a spend marker; at start it
refuses a log that shows LESS than the marker claims (truncated, emptied or rolled back) and a log with no `init`
row when the shard's marker exists. Starting a shard twice under one index loses the marker create race and raises
`ShardArithmeticError`.

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

from meshpipeline.settings.cad_repair import running_under_pytest

_MICRO = 1_000_000
_FACTORY = object()      # the private key to the constructor; only for_shard and for_test hold it

WaveType = Literal["normal", "checkpoint", "held_out"]
#: The owner's wave limits in USD: $50 a normal wave, $300 a checkpoint wave (at most 3), $250 the held-out wave (once).
WAVE_CAPS_USD: dict[str, float] = {"normal": 50.0, "checkpoint": 300.0, "held_out": 250.0}
MAX_WAVE_CAP_USD = max(WAVE_CAPS_USD.values())


class LostShardLog(RuntimeError):
    """This shard has started before (its marker exists) but its local log is gone, so its spend is unknown.
    Refusing is the point: starting again at $0 would let the wave overspend its cap."""


class ShardArithmeticError(RuntimeError):
    """The shard's identity (wave id, wave type, N, index) disagrees with the wave marker or with its own log."""


class MarkerStore(Protocol):
    """A small shared key-value store (the wave bucket). `create` is create-if-absent and MUST be atomic: True if
    this call wrote it, False if the key already existed. `put` overwrites."""

    def get(self, key: str) -> str | None: ...

    def create(self, key: str, value: str) -> bool: ...

    def put(self, key: str, value: str) -> None: ...


class FileMarkerStore:
    """Markers as files under a directory on a shared filesystem (and in tests); `create` is O_EXCL-atomic."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key.replace("/", "__")

    def get(self, key: str) -> str | None:
        try:
            return self._path(key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def create(self, key: str, value: str) -> bool:
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        return True

    def put(self, key: str, value: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, self._path(key))


class GcsObjectStore:
    """The wave bucket through the native GCS client and the caller's Application Default Credentials (on a
    wave VM, the VM service account - the same identity `gcloud` uses there). `create_object_if_absent` is
    `upload_from_string(..., if_generation_match=0)`: the server refuses with HTTP 412 when the object exists,
    which is the atomic create the model ledger needs. No HMAC keys, no secret, no private client methods.
    `bucket` is a client Bucket-like (tests inject one); omit it and `bucket_name` is opened lazily."""

    def __init__(self, bucket_name: str, *, bucket: Any = None) -> None:
        if not bucket_name:
            raise ValueError("GcsObjectStore needs a bucket name")
        self.bucket_name = bucket_name
        self._bucket = bucket

    @property
    def backend_id(self) -> str:
        return f"gcs/{self.bucket_name}"

    def _b(self) -> Any:
        if self._bucket is None:
            from google.cloud import storage  # in the mesh image via requirements/runtime.txt
            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def exists(self, *, object_key: str) -> bool:
        return bool(self._b().blob(object_key).exists())

    def get_bytes(self, *, object_key: str) -> bytes:
        return bytes(self._b().blob(object_key).download_as_bytes())

    def upload_file(self, *, local_path: Path, object_key: str, content_type: str | None = None) -> None:
        self._b().blob(object_key).upload_from_string(
            Path(local_path).read_bytes(), content_type=content_type or "application/octet-stream")

    def create_object_if_absent(self, *, local_path: Path, object_key: str,
                                content_type: str | None = None) -> bool:
        """True if THIS call created the object, False if it already existed (HTTP 412). Any other error
        propagates: an unknown outcome is never reported as a win."""
        from google.api_core.exceptions import PreconditionFailed
        try:
            self._b().blob(object_key).upload_from_string(
                Path(local_path).read_bytes(), content_type=content_type or "application/octet-stream",
                if_generation_match=0)
        except PreconditionFailed:
            return False
        return True

    def delete_object(self, *, object_key: str, missing_ok: bool = True) -> bool:
        """True if this call deleted the object. NOT part of `MarkerStore`, deliberately: the ledger must never
        be able to delete a marker it relies on (that is what `LostShardLog` exists to catch). It is here for
        devtools/validation/v4_eval/marker_probe.py, which creates throwaway `_probe/` keys against the real wave
        bucket and has to take them away again."""
        from google.api_core.exceptions import NotFound
        try:
            self._b().blob(object_key).delete()
        except NotFound:
            if missing_ok:
                return False
            raise
        return True


class ObjectMarkerStore:
    """Markers as objects in the wave bucket. `create` is a server-side PRECONDITION write
    (`create_object_if_absent` on the object store, e.g. the MinIO adapter's If-None-Match / generation-match
    write), so two shards starting the same index at once cannot both win. A store without that method is
    refused outright rather than used with a weaker exists-then-write."""

    def __init__(self, store: Any, prefix: str = "model-ledger") -> None:
        if not callable(getattr(store, "create_object_if_absent", None)):
            raise TypeError("ObjectMarkerStore needs an object store with an atomic create_object_if_absent")
        self._store, self._prefix = store, prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{key}"

    @property
    def store(self) -> Any:
        """The object store underneath. Exposed for the marker probe, which has to clean up the throwaway keys
        it writes to the real wave bucket; `MarkerStore` itself has no delete, on purpose."""
        return self._store

    def object_key(self, key: str) -> str:
        """The object key a marker `key` is stored under, so a caller that must address the object (the probe's
        cleanup) does not have to rebuild the prefix."""
        return self._key(key)

    def get(self, key: str) -> str | None:
        if not self._store.exists(object_key=self._key(key)):
            return None
        return self._store.get_bytes(object_key=self._key(key)).decode("utf-8")

    def _write(self, key: str, value: str, *, only_if_absent: bool) -> bool:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "marker"
            local.write_text(value, encoding="utf-8")
            if only_if_absent:
                return bool(self._store.create_object_if_absent(
                    local_path=local, object_key=self._key(key), content_type="application/json"))
            self._store.upload_file(local_path=local, object_key=self._key(key),
                                    content_type="application/json")
            return True

    def create(self, key: str, value: str) -> bool:
        return self._write(key, value, only_if_absent=True)

    def put(self, key: str, value: str) -> None:
        self._write(key, value, only_if_absent=False)


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
                 store: LedgerStore | None = None, _factory: object = None) -> None:
        if _factory is not _FACTORY:
            raise TypeError("a WaveLedger is built with WaveLedger.for_shard (production) or for_test (tests); "
                            "the raw constructor would skip the marker, log and wave-type checks")
        if not math.isfinite(cap_usd) or cap_usd <= 0:
            raise ValueError(f"a wave cap must be a finite, positive number of dollars, got {cap_usd!r}")
        if cap_usd > MAX_WAVE_CAP_USD:
            raise ValueError(f"a wave cap of ${cap_usd:,.2f} exceeds the largest wave limit "
                             f"(${MAX_WAVE_CAP_USD:,.0f}); use for_shard with a named wave type")
        self._cap = _micro(cap_usd)
        self._store: LedgerStore = store if store is not None else (
            FileStore(path) if path is not None else MemoryStore())
        self._markers: MarkerStore | None = None
        self._spend_key = ""
        self.provenance: str = "unset"

    @property
    def markers(self) -> MarkerStore | None:
        """The wave bucket's marker store this shard's ledger was built on (None for a test ledger). Read-only: the
        wave-level 429 guard shares it; nothing here prices or caps."""
        return self._markers

    @classmethod
    def for_test(cls, cap_usd: float, path: str | Path | None = None, *,
                 store: LedgerStore | None = None) -> WaveLedger:
        """A ledger of any cap up to the largest wave limit, for tests. REFUSES to run outside pytest, so it is
        not a second production door."""
        if not running_under_pytest():
            raise RuntimeError("WaveLedger.for_test is for tests; production builds ledgers with for_shard")
        ledger = cls(cap_usd, path, store=store, _factory=_FACTORY)
        ledger.provenance = "test"
        return ledger

    @classmethod
    def for_file(cls, cap_usd: float) -> WaveLedger:
        """An in-memory ledger for ONE file on the engine path (`repair_v4()`), capped at that file's own model
        budget. There is no wave here (a service request has no shard, no marker bucket and no restart to survive),
        so the bound is the per-call reservation of `admit` against `cap_usd`; a wave shard keeps using `for_shard`."""
        ledger = cls(cap_usd, None, _factory=_FACTORY)
        ledger.provenance = "file"
        return ledger

    @classmethod
    def for_shard(cls, wave: WaveType, *, wave_id: str, n_shards: int, shard_index: int,
                  path: str | Path, markers: MarkerStore) -> WaveLedger:
        """This shard's slice of a wave: `WAVE_CAPS_USD[wave] / n_shards`, in a log at `path` (durable local
        disk). Refuses (LostShardLog / ShardArithmeticError) rather than start at an unknown or inconsistent
        spend; see the module docstring."""
        if wave not in WAVE_CAPS_USD:
            raise ValueError(f"unknown wave type {wave!r}; the wave types are {sorted(WAVE_CAPS_USD)}")
        if n_shards < 1 or not 0 <= shard_index < n_shards:
            raise ValueError(f"shard {shard_index} of {n_shards} is not a valid shard")
        identity = {"wave_id": wave_id, "wave": wave, "n_shards": n_shards}
        wave_marker = json.dumps(identity, sort_keys=True)
        if not markers.create(f"{wave_id}/wave", wave_marker):
            existing = markers.get(f"{wave_id}/wave")
            if existing != wave_marker:
                raise ShardArithmeticError(
                    f"wave {wave_id!r} was started as {existing} but this shard says {wave_marker}: the "
                    "launcher's N and wave type must be the same for every shard")
        path = Path(path)
        had_log = path.exists()
        shard_key = f"{wave_id}/shard-{shard_index}"
        spend_key = f"{shard_key}-spent-micro"
        marker_before = markers.get(shard_key)
        if marker_before is not None and not had_log:
            raise LostShardLog(
                f"shard {shard_index} of wave {wave_id!r} has started before (marker {shard_key}) but its "
                f"local log {path} is missing, so what it already spent is unknown. Refusing to start it at "
                "$0; restore the log, or abandon this shard's slice and launch a replacement wave.")
        ledger = cls(WAVE_CAPS_USD[wave] / n_shards, path, _factory=_FACTORY)
        me = {**identity, "shard_index": shard_index}

        def check(rows, append):
            inits = [r for r in rows if r["t"] == "init"]
            if not inits:
                if marker_before is not None:
                    raise LostShardLog(f"the log {path} has no init row but shard {shard_index}'s marker "
                                       "exists: the log was truncated or replaced")
                append({"t": "init", **me, "at": time.time()})
            elif {k: inits[0].get(k) for k in me} != me:
                raise ShardArithmeticError(f"the log {path} belongs to {inits[0]}, not to {me}")
            return cls._totals(rows)[0]
        settled = ledger._transact(check)
        claimed = int(markers.get(spend_key) or 0)
        if settled < claimed:
            raise LostShardLog(
                f"shard {shard_index} of wave {wave_id!r} had spent ${claimed / _MICRO:,.4f} (its spend marker) "
                f"but its log {path} shows ${settled / _MICRO:,.4f}: it was truncated or rolled back. Refusing "
                "to continue from a lower figure.")
        if marker_before is None and not markers.create(shard_key, json.dumps(me, sort_keys=True)):
            raise ShardArithmeticError(
                f"shard {shard_index} of wave {wave_id!r} was claimed by another process while this one "
                "started: two shards were given the same index. The launcher must assign unique indexes.")
        ledger._markers, ledger._spend_key = markers, spend_key
        ledger.provenance = "shard"
        return ledger

    @property
    def cap_usd(self) -> float:
        return self._cap / _MICRO

    def cap_to(self, usd: float) -> None:
        """LOWER this ledger's cap to `usd` (an operator's tighter limit for one run, e.g. a $5 smoke inside a $50
        normal wave). It never raises: a cap that a flag could lift is not the owner's cap. In-process only; the
        shard's slice (`for_shard`) stays the hard ceiling and a restart must pass the same flag again."""
        micro = _micro(usd)
        if micro <= 0:
            raise ValueError(f"a cap must be positive, got {usd!r}")
        if micro > self._cap:
            raise ValueError(f"cap_to can only lower the cap: ${usd:,.2f} is above ${self.cap_usd:,.2f}")
        self._cap = micro

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
               kind: str = "repair_v4_propose", cache_write_tokens: int = 0,
               cache_write_30m_tokens: int = 0) -> None:
        cost = _micro(cost_usd)

        def go(rows, append):
            if not any(r["t"] == "reserve" and r["id"] == reservation for r in rows):
                raise KeyError(f"no such reservation {reservation!r}")
            if any(r["t"] in ("settle", "release") and r["id"] == reservation for r in rows):
                raise KeyError(f"reservation {reservation!r} is already closed")
            append({"t": "settle", "id": reservation, "file": file_id, "job": job_id, "provider": provider,
                    "model": model, "in": int(input_tokens), "cached": int(cached_input_tokens),
                    "out": int(output_tokens), "cw": int(cache_write_tokens),
                    "cw30": int(cache_write_30m_tokens), "u": cost, "kind": kind, "at": time.time()})
        self._transact(go)
        if self._markers is not None:
            # the spend marker lets a restart notice a log that now shows LESS than was spent
            self._markers.put(self._spend_key, str(self._transact(lambda rows, _a: self._totals(rows)[0])))

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

    def headroom_usd(self, file_id: str, *, file_ceiling_usd: float) -> float:
        """What one more reservation for `file_id` may be: the smaller of the file's room under its ceiling and the
        slice's room under its cap (settled + held counted on both). A read; `admit` stays the transaction."""
        ceiling = _micro(file_ceiling_usd)

        def go(rows, _a):
            f_settled, f_held = self._totals(rows, file_id)
            w_settled, w_held = self._totals(rows)
            return min(ceiling - f_settled - f_held, self._cap - w_settled - w_held)
        return max(0, self._transact(go)) / _MICRO

    def file_spent_usd(self, file_id: str) -> float:
        return self._transact(lambda rows, _a: self._totals(rows, file_id)[0]) / _MICRO

    def entries(self) -> list[dict[str, Any]]:
        """Every settled call, oldest first, in dollars."""
        def go(rows, _a):
            return [{"file_id": r["file"], "job_id": r["job"], "provider": r["provider"],
                     "model": r["model"], "input_tokens": r["in"], "cached_input_tokens": r["cached"],
                     "output_tokens": r["out"],
                     "cache_write_tokens": r.get("cw", 0), "cache_write_30m_tokens": r.get("cw30", 0), "usd": r["u"] / _MICRO, "kind": r["kind"], "at": r["at"]}
                    for r in rows if r["t"] == "settle"]
        return self._transact(go)
