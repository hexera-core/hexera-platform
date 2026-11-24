# Responsibility: ONE answer to "how much of this file's wall is left", and what a call may therefore ask for.
# Owns: `set_file_deadline`, `remaining_s`, `clip_member_timeout`, `TooLateToStart`.
# Boundaries: it holds no policy about what to do when time runs out - a caller decides whether to clip, skip, or
#             refuse. It never reads a clock the caller cannot see: everything is `time.monotonic`.
"""The file wall, readable from wherever work is actually about to start.

A16. Measured cause: on 263d5e2d5bab ONE full panel took 929.95 s against a 900 s search wall, and the file ran
2050 s against a 1200 s file wall, because `run_panel` runs members at a FIXED 300 s each (worst case
`(len(members) + 2) * timeout_s` = 1800 s with the retry) and the search only checks its wall BETWEEN expansions.
A call longer than the time left cannot be stopped by a check that happens after it.

WHY A MODULE-LEVEL DEADLINE AND NOT A CONTEXTVAR. The work that must read this runs in `ThreadPoolExecutor`
threads (D22's candidate pool, D23.2's parallel panel members), and `concurrent.futures` does NOT propagate a
`contextvars.Context` into its workers - a `ContextVar` set on the main thread reads as unset in exactly the
parallel paths that matter. A module global is visible to every thread in the process, and one engine process
handles one file, which is the lifetime this value has.
"""
from __future__ import annotations

import threading
import time

#: the file's deadline (`time.monotonic`), or None when nothing set one - then every call runs as it always did
_deadline: float | None = None
_lock = threading.Lock()

#: a member timeout is never clipped below this: under it, a mesher cannot even read the file, so a "fail" would be
#: a statement about the clock rather than about the part (`panel.MIN_USEFUL_MEMBER_S` if that is ever defined).
MIN_MEMBER_S = 20.0
#: `run_panel`'s own worst case: every member, plus the one retry the file is allowed (its docstring's
#: `(len(members) + 2) * timeout_s`).
RETRY_SLOTS = 2


class TooLateToStart(RuntimeError):
    """There is not enough wall left to run this call usefully. The caller skips the work and says so in the row;
    it is never a verdict about the part."""


def set_file_deadline(deadline: float | None) -> None:
    """Set (or clear, with None) the monotonic deadline for the file being repaired. Called once per file."""
    global _deadline
    with _lock:
        _deadline = deadline


def file_deadline() -> float | None:
    return _deadline


def remaining_s() -> float | None:
    """Seconds left of the file's wall, or None when no deadline is set. Never negative."""
    d = _deadline
    return None if d is None else max(0.0, d - time.monotonic())


def clip_member_timeout(members: int, timeout_s: float, *, retry_slots: int = RETRY_SLOTS) -> tuple[float, bool]:
    """`(timeout per member, was it clipped)` for a panel of `members` about to start.

    Unclipped whenever the panel's whole worst case fits in what is left - so a full-budget panel keeps the exact
    300 s it has always had, and its cache key (which carries the timeout) does not move. Otherwise the per-member
    budget is what remains divided by the slots that could use it, floored at `MIN_MEMBER_S`.

    Raises `TooLateToStart` when even one floored member does not fit: starting then guarantees an overrun and
    buys nothing, which is the whole point of A16.
    """
    left = remaining_s()
    if left is None:
        return (timeout_s, False)
    slots = max(1, members) + max(0, retry_slots)
    if timeout_s * slots <= left:
        return (timeout_s, False)
    if left < MIN_MEMBER_S:
        raise TooLateToStart(
            f"{left:.1f} s left of the file wall, under the {MIN_MEMBER_S:.0f} s a member needs to say anything")
    return (max(MIN_MEMBER_S, left / slots), True)


def fits(worst_case_s: float) -> bool:
    """Does a call whose worst case is `worst_case_s` fit in what is left? True when no deadline is set."""
    left = remaining_s()
    return True if left is None else worst_case_s <= left
