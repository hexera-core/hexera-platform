# Responsibility: Keep a wave of AI-proposer shards inside the model route's rate limits: a per-wave ceiling on
#                 in-flight model calls, requests per minute and tokens per minute, enforced as a static per-shard
#                 slice, plus a cool-down after the provider says 429.
# Owns: WaveThrottle, the measured route limits (`ROUTE_LIMITS`) and the safety share of them a wave may use.
# Boundaries: pacing only. It prices nothing (the ledger does), retries nothing (the router does) and holds no
#             shared state across hosts: like the ledger's cap, a wave limit binds because each shard owns 1/N of it.
"""The wave throttle.

WHY A SLICE, NOT A SHARED COUNTER. A wave runs N shards on N hosts (about 96 for a dev wave). No host-local lock can
count calls across them, and a shared store would put a network round trip in front of every model call. So each shard
gets `1/N` of the wave's allowance - the same construction `model_ledger.WaveLedger.for_shard` uses for money - and
the wave total can never exceed the wave limit however shards are scheduled. The price is idle allowance in a shard
that is meshing rather than calling, which at the measured limits costs nothing (see ROUTE_LIMITS).

WHAT IS LIMITED, per shard:
  * in-flight calls (a bounded semaphore of `max_in_flight // N`, at least 1 - and a wave whose N exceeds its
    in-flight ceiling is REFUSED, because a slice cannot be less than one call);
  * requests and input tokens per rolling 60 s (`rpm / N`, `tpm / N`); a call is charged its ESTIMATED input
    tokens up front (the proposer's upper-bound estimate), so the bucket never under-counts;
  * a cool-down: after a call ends rate-limited, no new call starts on this shard until `retry_after` (or
    `COOLDOWN_S`) has passed. The router has already retried that call; the cool-down stops the NEXT call from
    walking straight into the same limit.

`acquire` blocks up to `wait_s` and returns False rather than waiting forever: the proposer then treats the round as a
failed model round (never a crash), and three in a row stop the AI for the file as `model_unavailable`.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: Limits of the repair_v4 routes on the hexera-dev `openai-api-key`, per minute: (requests, tokens). Measured
#: 2026-11-23 from the `x-ratelimit-limit-requests` / `x-ratelimit-limit-tokens` headers of one gpt-6-astra call
#: (15000, 40000000), matching the "Grow" tier on developers.openai.com/api/docs/guides/rate-limits, where Astra,
#: Sol and Terra SHARE one limit. Production's Sol/Terra traffic on the same key draws on it too.
ROUTE_LIMITS: dict[str, tuple[int, int]] = {
    "openai:gpt-6-astra": (15_000, 40_000_000),
    "openai:gpt-5.6-terra": (15_000, 40_000_000),
}
#: The share of a route's limit a wave may take. Half, because the limit is shared with production on the same key.
WAVE_SHARE = 0.5
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


class WaveThrottle:
    """One shard's slice of the wave's model-call allowance. Thread-safe; `acquire`/`release` bracket each call."""

    def __init__(self, slice_: Slice, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None) -> None:
        if slice_.max_in_flight < 1 or slice_.rpm <= 0 or slice_.tpm <= 0:
            raise ThrottleConfigError(f"an empty throttle slice {slice_!r}")
        self.slice = slice_
        self._clock = clock
        self._cond = threading.Condition()
        self._in_flight = 0
        self._starts: deque[tuple[float, int]] = deque()     # (start time, tokens charged) within the window
        self._cool_until = 0.0
        self._sleep = sleep
        #: the most calls ever in flight at once on this shard (for tests and the shard's provenance)
        self.peak_in_flight = 0
        self.rate_limited = 0

    @classmethod
    def for_shard(cls, route: str, *, n_shards: int, max_in_flight: int | None = None,
                  share: float = WAVE_SHARE, **kw) -> WaveThrottle:
        """This shard's slice: `max_in_flight // n_shards` calls (refused below one), `share x rpm / n_shards`
        requests and `share x tpm / n_shards` input tokens per minute."""
        if n_shards < 1:
            raise ThrottleConfigError(f"n_shards must be at least 1, got {n_shards}")
        if route not in ROUTE_LIMITS:
            raise ThrottleConfigError(f"no measured rate limit for {route!r}; add it to ROUTE_LIMITS from a header probe")
        wave_in_flight = max_in_flight if max_in_flight is not None else wave_max_in_flight(route, share=share)
        per_shard = wave_in_flight // n_shards
        if per_shard < 1:
            raise ThrottleConfigError(
                f"{n_shards} shards cannot share {wave_in_flight} in-flight model calls: a shard's slice would be "
                "below one call. Use fewer shards for the AI arm, or raise the wave's in-flight ceiling.")
        rpm, tpm = ROUTE_LIMITS[route]
        return cls(Slice(per_shard, share * rpm / n_shards, share * tpm / n_shards), **kw)

    # --- the two operations ---------------------------------------------------------------------------------------

    def acquire(self, tokens: int, *, wait_s: float = DEFAULT_WAIT_S) -> bool:
        """Block until a call charging `tokens` input tokens may start on this shard, or `wait_s` passes (False).
        A call larger than the whole per-minute token slice is admitted alone, into an empty window, rather than
        refused for ever."""
        deadline = self._clock() + wait_s
        with self._cond:
            while True:
                now = self._clock()
                hold = self._hold_for(now, tokens)
                if hold <= 0:
                    self._in_flight += 1
                    self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
                    self._starts.append((now, int(tokens)))
                    return True
                remaining = deadline - now
                if remaining <= 0:
                    return False
                self._wait(min(hold, remaining))

    def release(self, *, rate_limited: bool = False, retry_after_s: float | None = None) -> None:
        """The call ended. `rate_limited` (the router's final word was RATE_LIMIT) starts this shard's cool-down."""
        with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            if rate_limited:
                self.rate_limited += 1
                pause = retry_after_s if retry_after_s and retry_after_s > 0 else COOLDOWN_S
                self._cool_until = max(self._cool_until, self._clock() + pause)
            self._cond.notify_all()

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


__all__ = ["COOLDOWN_S", "ROUTE_LIMITS", "Slice", "ThrottleConfigError", "WAVE_SHARE", "WaveThrottle",
           "wave_max_in_flight"]
