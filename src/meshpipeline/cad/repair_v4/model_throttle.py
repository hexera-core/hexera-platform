# Responsibility: Keep a wave of AI-proposer shards inside the model route's rate limits: a per-wave ceiling on
#                 in-flight model calls, requests per minute and tokens per minute, enforced as a static per-shard
#                 slice, plus a cool-down after the provider says 429.
# Owns: WaveThrottle, the measured route limits (`ROUTE_LIMITS`) and the safety share of them a wave may use.
#       WaveBackoffGuard, the wave-level 429 brake coordinated through the wave bucket's marker store.
# Boundaries: pacing only. It prices nothing (the ledger does), retries nothing (the router does) and holds no
#             shared state across hosts except the 429 guard's trip markers: like the ledger's cap, a wave limit binds
#             because each shard owns 1/N of it.
"""The wave throttle.

WHY A SLICE, NOT A SHARED COUNTER. A wave runs N shards on N hosts (about 96 for a dev wave). No host-local lock can
count calls across them, and a shared store would put a network round trip in front of every model call. So each shard
gets `1/N` of the wave's allowance - the same construction `model_ledger.WaveLedger.for_shard` uses for money - and
the wave total can never exceed the wave limit however shards are scheduled. The price is idle allowance in a shard
that is meshing rather than calling, which at the measured limits costs nothing (see ROUTE_LIMITS).

WHAT IS LIMITED, per shard:
  * in-flight calls (a bounded semaphore of `max_in_flight // N`). A wave whose N exceeds its in-flight ceiling is
    REFUSED unless the launch passes an explicit override (`allow_floor`, run_arm `--allow-in-flight-floor`), which
    floors each slice to one call and is recorded (`Slice.floored`, the shard's provenance);
  * requests and input tokens per rolling 60 s (`rpm / N`, `tpm / N`); a call is charged its ESTIMATED input
    tokens up front (the proposer's upper-bound estimate), so the bucket never under-counts;
  * a cool-down: after a call ends rate-limited, no new call starts on this shard until `retry_after` (or
    `COOLDOWN_S`) has passed. The router has already retried that call; the cool-down stops the NEXT call from
    walking straight into the same limit.

`acquire` blocks up to `wait_s` of time - whatever holds it, the shard's own limits or the wave's 429 back-off - and
returns False rather than waiting forever: the proposer then treats the round as a
failed model round (never a crash), and three in a row stop the AI for the file as `model_unavailable`.
"""

from __future__ import annotations

import json
import math
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Limits of the repair_v4 routes on the hexera-dev `openai-api-key`, per minute: (requests, tokens). Measured
#: 2026-11-23 from the `x-ratelimit-limit-requests` / `x-ratelimit-limit-tokens` headers of one gpt-6-astra call
#: (15000, 40000000), matching the "Grow" tier on developers.openai.com/api/docs/guides/rate-limits, where Astra,
#: Sol and Terra SHARE one limit. Production's Sol/Terra traffic on the same key draws on it too.
ROUTE_LIMITS: dict[str, tuple[int, int]] = {
    "openai:gpt-6-astra": (15_000, 40_000_000),
    "openai:gpt-5.6-terra": (15_000, 40_000_000),
}
#: The share of a route's limit a wave may take. Five per cent, because the limit is shared with production on the
#: same key (Astra, Sol and Terra draw on one 15,000 RPM / 40M TPM org limit) and the downside is asymmetric: a
#: throttled wave is slow, a starved production is an incident. About 750 RPM / 2M TPM. A 96-shard wave sustains
#: roughly 118-361 RPM (2 turns a file, files at 31.9 s median / 97.9 s mean), so this fits with headroom.
WAVE_SHARE = 0.05
#: Latency of one proposer round, seconds: the FASTEST measured (3.0-3.3 s on astra, 4-6k input tokens). Used only to
#: turn a requests-per-minute limit into an in-flight ceiling (Little's law: in-flight = rate x latency); the low
#: end is the conservative one, since a shorter call means more requests per in-flight slot.
CALL_LATENCY_S = 3.0
#: Pause after a rate-limited call when the provider gave no Retry-After.
COOLDOWN_S = 30.0
#: How long a call may wait for its slot before the round is given up (and counted as a failed model round).
DEFAULT_WAIT_S = 120.0
_WINDOW_S = 60.0


class ThrottleConfigError(ValueError):
    """The wave cannot be sliced inside the limit (more shards than in-flight calls allowed, or no limit known)."""


def wave_max_in_flight(route: str, *, share: float = WAVE_SHARE, latency_s: float = CALL_LATENCY_S) -> int:
    """The most model calls a whole wave may have in flight on `route`: `share x rpm x latency / 60`."""
    if route not in ROUTE_LIMITS:
        raise ThrottleConfigError(f"no measured rate limit for {route!r}; add it to ROUTE_LIMITS from a header probe")
    rpm, _tpm = ROUTE_LIMITS[route]
    return max(1, math.floor(share * rpm * latency_s / 60.0))


@dataclass(frozen=True)
class Slice:
    max_in_flight: int
    rpm: float
    tpm: float
    #: True when the shard's in-flight slice was raised to the floor of one, i.e. the wave as a whole may have more
    #: calls in flight than `wave_max_in_flight` (recorded in provenance so the report can say so)
    floored: bool = False


class WaveThrottle:
    """One shard's slice of the wave's model-call allowance. Thread-safe; `acquire`/`release` bracket each call."""

    def __init__(self, slice_: Slice, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None, guard: WaveBackoffGuard | None = None) -> None:
        if slice_.max_in_flight < 1 or slice_.rpm <= 0 or slice_.tpm <= 0:
            raise ThrottleConfigError(f"an empty throttle slice {slice_!r}")
        self.slice = slice_
        self._clock = clock
        self._cond = threading.Condition()
        self._in_flight = 0
        self._starts: deque[tuple[float, int]] = deque()     # (start time, tokens charged) within the window
        self._cool_until = 0.0
        self._sleep = sleep
        #: the wave-level 429 guard (None: this shard only has its own cool-down)
        self.guard = guard
        #: the most calls ever in flight at once on this shard (for tests and the shard's provenance)
        self.peak_in_flight = 0
        self.rate_limited = 0
        #: acquires that gave up after wait_s (each one a failed model round for the proposer)
        self.waits_timed_out = 0

    @classmethod
    def for_shard(cls, route: str, *, n_shards: int, max_in_flight: int | None = None,
                  share: float = WAVE_SHARE, allow_floor: bool = False, **kw) -> WaveThrottle:
        """This shard's slice: `max_in_flight // n_shards` calls, `share x rpm / n_shards` requests and
        `share x tpm / n_shards` input tokens per minute. When `max_in_flight // n_shards` is below one call the wave
        would exceed its in-flight ceiling: that is REFUSED (`ThrottleConfigError`) unless `allow_floor` - the
        launch's explicit, recorded override - floors the slice to one call."""
        if n_shards < 1:
            raise ThrottleConfigError(f"n_shards must be at least 1, got {n_shards}")
        if route not in ROUTE_LIMITS:
            raise ThrottleConfigError(f"no measured rate limit for {route!r}; add it to ROUTE_LIMITS from a header probe")
        wave_in_flight = max_in_flight if max_in_flight is not None else wave_max_in_flight(route, share=share)
        # WHY A FLOOR OF ONE MAY BE ALLOWED, EVEN THOUGH IT EXCEEDS THE DERIVED CEILING - BUT ONLY WHEN THE LAUNCH SAYS
        # SO (B11: the floor used to apply silently; it is now refused unless `allow_floor` is passed and recorded). `wave_in_flight` is `share x rpm x latency / 60`:
        # the in-flight count that share-of-RPM implies UNDER SATURATION (every slot re-calling the instant it frees).
        # At WAVE_SHARE 0.05 that is 37, below the 96 shards of a dev wave, so `37 // 96 == 0` and without a floor the
        # wave would be refused. A shard is not saturated, though: it runs one file at a time, a file is two model
        # turns (Lane B3) between long stretches of meshing and verification (files take 31.9 s median / 97.9 s mean,
        # Task 43), so 96 shards sustain about 118-361 RPM against the 750 the share allows. Concurrency can touch 96
        # for a moment; the provider meters the SUSTAINED rate. So the ceiling over-constrains by assuming a
        # saturation that does not occur, and the per-shard RPM and TPM slices below - not this count - are the
        # binding protection of production's 95%. A floor of one violates the derived ceiling (the coordinator
        # accepted 96 > 37 for dev waves), so it is an explicit decision of the launch, recorded in `Slice.floored` and
        # the shard's provenance - never a silent default.
        floored = wave_in_flight // n_shards < 1
        if floored and not allow_floor:
            raise ThrottleConfigError(
                f"{n_shards} shards cannot share {wave_in_flight} in-flight model calls: each slice would be below one "
                f"call, so the wave could have {n_shards} in flight against a ceiling of {wave_in_flight}. Launch with "
                f"<= {wave_in_flight} shards, or pass --allow-in-flight-floor (allow_floor=True) to floor each slice "
                "to one call; the override is recorded in the shard's provenance.")
        per_shard = max(1, wave_in_flight // n_shards)
        rpm, tpm = ROUTE_LIMITS[route]
        return cls(Slice(per_shard, share * rpm / n_shards, share * tpm / n_shards, floored), **kw)

    # --- the two operations ---------------------------------------------------------------------------------------

    def acquire(self, tokens: int, *, wait_s: float = DEFAULT_WAIT_S) -> bool:
        """Block until a call charging `tokens` input tokens may start on this shard, or `wait_s` passes (False).
        A call larger than the whole per-minute token slice is admitted alone, into an empty window, rather than
        refused for ever."""
        deadline = self._clock() + wait_s                  # NEVER extended: wait_s of time is the bound (B11)
        while True:
            # the wave guard is consulted OUTSIDE the shard's lock: it may read the marker store
            wave_hold = self.guard.hold_for() if self.guard is not None else 0.0
            with self._cond:
                now = self._clock()
                own_hold = self._hold_for(now, tokens)
                hold = max(own_hold, wave_hold)
                if hold <= 0:
                    self._in_flight += 1
                    self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
                    self._starts.append((now, int(tokens)))
                    return True
                remaining = deadline - now
                if remaining <= 0:
                    self.waits_timed_out += 1
                    return False
                wait = min(hold, remaining)
                if wave_hold > 0 and wave_hold >= own_hold and self.guard is not None:
                    # a wave-level back-off is recorded (the report says the wave was throttled). It used to EXTEND the
                    # deadline as well, so `remaining` never shrank and acquire() blocked past wait_s - unbounded while
                    # trips kept re-firing, outside the router's own deadline. It no longer does: a call held longer
                    # than wait_s is a failed round like any other; three in a row end the AI for the file as
                    # model_unavailable, which the row records as inconclusive.
                    self.guard.note_held(wait)
                self._wait(wait)

    def release(self, *, rate_limited: bool = False, retry_after_s: float | None = None) -> None:
        """The call ended. `rate_limited` (the router's final word was RATE_LIMIT) starts this shard's cool-down and
        is reported to the wave guard, which may back the whole wave off."""
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            if rate_limited:
                self.rate_limited += 1
                pause = retry_after_s if retry_after_s and retry_after_s > 0 else COOLDOWN_S
                self._cool_until = max(self._cool_until, self._clock() + pause)
            self._cond.notify_all()
        if rate_limited and self.guard is not None:
            self.guard.report_429()                   # a marker-store write, so outside the lock

    # --- internals --------------------------------------------------------------------------------------------------

    def _hold_for(self, now: float, tokens: int) -> float:
        """Seconds until a call of `tokens` may start (<= 0: now)."""
        while self._starts and now - self._starts[0][0] >= _WINDOW_S:
            self._starts.popleft()
        holds = [self._cool_until - now]
        if self._in_flight >= self.slice.max_in_flight:
            holds.append(0.05)                       # woken by release(); the poll is only a backstop
        if len(self._starts) + 1 > self.slice.rpm and self._starts:
            holds.append(self._starts[0][0] + _WINDOW_S - now)
        used = sum(t for _s, t in self._starts)
        if self._starts and used + tokens > self.slice.tpm:
            holds.append(self._starts[0][0] + _WINDOW_S - now)
        return max(holds)

    def _wait(self, seconds: float) -> None:
        if self._sleep is not None:              # tests drive a fake clock
            self._cond.release()
            try:
                self._sleep(seconds)
            finally:
                self._cond.acquire()
        else:
            self._cond.wait(timeout=seconds)


# --- the wave-level guard ---------------------------------------------------------------------------------------------

#: The 429 window the guard counts in, and how long a tripped wave holds all new calls.
GUARD_WINDOW_S = 300.0
#: More than this many calls ending rate-limited, wave-wide, inside one window trips the back-off.
GUARD_THRESHOLD = 10
#: How often a shard re-reads the trip markers (it reads two small objects, not one per shard).
GUARD_CHECK_INTERVAL_S = 10.0
#: Shards resume spread over this many seconds after a trip, so 96 do not return in the same second.
GUARD_RESUME_SPREAD_S = 30.0


class WaveBackoffGuard:
    """Backs the WHOLE wave off, not one shard, once 429s pile up. State lives in the wave bucket's marker store (the
    same `MarkerStore` the `WaveLedger` shares a wave's spend through), because shards are separate processes on
    separate VMs.

    HOW. Time is cut into fixed `window_s` buckets of wall-clock time. In bucket B the wave owns `threshold + 1`
    claim keys `<wave>/429/B/slot-0 ... slot-<threshold>`. A shard whose call ended rate-limited claims the first
    free slot with `create` (atomic create-if-absent: two shards cannot hold one slot). Whoever claims the LAST slot
    - the `threshold + 1`-th 429 - writes `<wave>/429/B/tripped` (`until = now + window_s`). Every shard re-reads
    the trip markers of the current and previous bucket at most every `check_interval_s` (two GETs, not 96) and, while
    a trip is in force, holds every new call until `until` plus a per-shard spread. Nothing is listed or summed, so no
    read depends on list consistency.

    WHAT IT DOES NOT GUARANTEE.
      * Lag. A shard learns of a trip up to `check_interval_s` late (plus the time the claiming shard takes to write
        the marker), so calls issued after the threshold was crossed but before the reader looked still go out.
        In-flight calls are never cancelled.
      * It counts calls that ENDED rate-limited, i.e. after the router's own retries, not every raw 429 the provider
        sent: it under-counts raw 429s and so trips later than a raw count would.
      * Fixed buckets, not a sliding window: a burst split across a boundary can put `threshold` 429s on each side
        and trip neither. A trip does carry into the next bucket (it is read from the previous one too).
      * It uses wall-clock time across hosts, so host clock skew (seconds, on GCE) moves bucket edges and `until`.
      * A marker-store error is FAIL-OPEN: the guard cannot see or raise a trip, the shard keeps only its own
        cool-down, and `store_errors` in `describe()` says so. Pacing is advisory; the ledger is what caps spend.
      * It is a brake on new calls, not a rate limiter: it does not bound the rate while untripped. The per-shard
        RPM/TPM slices do that.
    """

    def __init__(self, markers: Any, *, wave_id: str, shard_id: str, threshold: int = GUARD_THRESHOLD,
                 window_s: float = GUARD_WINDOW_S, check_interval_s: float = GUARD_CHECK_INTERVAL_S,
                 resume_spread_s: float = GUARD_RESUME_SPREAD_S, wall: Callable[[], float] = time.time) -> None:
        if threshold < 1 or window_s <= 0:
            raise ThrottleConfigError(f"a guard needs threshold >= 1 and a positive window, got {threshold}, {window_s}")
        self._m, self._wave, self._shard = markers, wave_id, shard_id
        self.threshold, self.window_s = threshold, window_s
        self._check, self._spread, self._wall = check_interval_s, resume_spread_s, wall
        self._lock = threading.Lock()
        self._checked_at = -math.inf
        self._trips: dict[str, dict[str, Any]] = {}          # marker key -> trip record (this shard's view)
        # per-shard stagger, stable for a shard
        self._jitter = (zlib.crc32(shard_id.encode()) % 1000) / 1000.0 * resume_spread_s
        # what this shard did, for provenance
        self.reported = 0
        self.tripped_here = 0
        self.held_s = 0.0
        self.holds = 0
        self.store_errors = 0
        #: trip markers ignored because they were not JSON objects with a finite numeric `until`
        self.malformed_markers = 0

    def _bucket(self, t: float) -> int:
        return int(t // self.window_s)

    def _key(self, bucket: int, leaf: str) -> str:
        return f"{self._wave}/429/{bucket}/{leaf}"

    def report_429(self) -> bool:
        """A call on this shard ended rate-limited. Claim a slot; claim the last one and the wave trips. True if this
        shard's report tripped the wave. Never raises."""
        now = self._wall()
        bucket = self._bucket(now)
        try:
            claimed = None
            for i in range(self.threshold + 1):
                if self._m.create(self._key(bucket, f"slot-{i}"), json.dumps({"shard": self._shard, "at": now})):
                    claimed = i
                    break
            with self._lock:
                self.reported += 1
            if claimed is None or claimed < self.threshold:
                return False                                  # not the one that crosses it
            trip = {"bucket": bucket, "at": now, "until": now + self.window_s, "by_shard": self._shard,
                    "threshold": self.threshold, "window_s": self.window_s}
            self._m.create(self._key(bucket, "tripped"), json.dumps(trip, sort_keys=True))
            with self._lock:
                self._trips[self._key(bucket, "tripped")] = trip
                self.tripped_here += 1
            return True
        except Exception:  # noqa: BLE001 - pacing is advisory; a store outage must not fail a model round
            with self._lock:
                self.store_errors += 1
            return False

    def _refresh(self, now: float) -> None:
        """Read the current and previous buckets' trip markers. Only a JSON object with a finite numeric `until` is
        kept; anything else is counted in `malformed_markers` and ignored, so `hold_for` can never raise on it."""
        bucket = self._bucket(now)
        for b in (bucket - 1, bucket):
            key = self._key(b, "tripped")
            if key in self._trips:
                continue
            try:
                raw = self._m.get(key)
            except Exception:  # noqa: BLE001 - fail open, and say so (once per refresh: the store is unreachable)
                self.store_errors += 1
                break
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            until = record.get("until") if isinstance(record, dict) else None
            if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
                self.malformed_markers += 1
                continue
            self._trips[key] = record

    def hold_for(self) -> float:
        """Seconds this shard must hold new calls for the wave's back-off (0: go). Reads the store at most every
        `check_interval_s`."""
        now = self._wall()
        with self._lock:
            if now - self._checked_at >= self._check:
                self._checked_at = now
                self._refresh(now)
            until = max((t["until"] for t in self._trips.values()), default=0.0)
        return max(0.0, until + self._jitter - now) if until > now else 0.0

    def note_held(self, seconds: float) -> None:
        with self._lock:
            self.held_s += seconds
            self.holds += 1

    def describe(self) -> dict[str, Any]:
        """This shard's account of the wave back-off, for PROVENANCE.json (the report merges every shard's)."""
        with self._lock:
            return {"guard": "wave_backoff", "threshold": self.threshold, "window_s": self.window_s,
                    "reported_429": self.reported, "tripped_here": self.tripped_here,
                    "trips_seen": sorted(self._trips.values(), key=lambda t: t["at"]),
                    "held_s": round(self.held_s, 3), "holds": self.holds, "store_errors": self.store_errors,
                    "malformed_markers": self.malformed_markers}


__all__ = ["COOLDOWN_S", "GUARD_THRESHOLD", "GUARD_WINDOW_S", "WaveBackoffGuard", "ROUTE_LIMITS", "Slice", "ThrottleConfigError", "WAVE_SHARE", "WaveThrottle",
           "wave_max_in_flight"]
