# Responsibility: ONE answer to "how much of this file's wall is left", and what a call may therefore ask for.
# Owns: `set_file_deadline`, `remaining_s`, `panel_worst_case_s`, `clip_member_timeout`, `TooLateToStart`, and the
#       file's budget scale (`set_member_scale` / `member_scale`, CRS v5 step 4).
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

import math
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
#: Seconds a panel spends outside its members' clocks (a killed worker replaced, results gathered). Added to the
#: worst case so a panel that "fits" does not finish a few seconds past the wall.
PANEL_OVERHEAD_S = 10.0

#: CRS v5 step 4: the current file's budget scale (`limits.budget_scale`), applied to the per-member timeout by
#: `assemble`. Module-level for the same reason as the deadline (panel threads do not inherit a ContextVar).
_member_scale: float = 1.0


class TooLateToStart(RuntimeError):
    """There is not enough wall left to run this call usefully. The caller skips the work and says so in the row;
    it is never a verdict about the part."""


def set_file_deadline(deadline: float | None) -> None:
    """Set (or clear, with None) the monotonic deadline for the file being repaired. Called once per file. Clearing
    it also resets the budget scale: the next file in this process must inherit neither."""
    global _deadline, _member_scale
    with _lock:
        _deadline = deadline
        if deadline is None:
            _member_scale = 1.0


def file_deadline() -> float | None:
    return _deadline


def remaining_s() -> float | None:
    """Seconds left of the file's wall, or None when no deadline is set. Never negative."""
    d = _deadline
    return None if d is None else max(0.0, d - time.monotonic())


def set_member_scale(scale: float | None) -> None:
    """Set (or reset to 1.0, with None) the file's budget scale. Called once per file, cleared with the deadline."""
    global _member_scale
    with _lock:
        _member_scale = 1.0 if scale is None or not scale > 0 else float(scale)


def member_scale() -> float:
    return _member_scale


def panel_slots(members: int, *, threads: int = 1, retry_slots: int = RETRY_SLOTS) -> int:
    """How many member timeouts `run_panel`'s worst case spans. SERIAL (threads 1): every member in turn plus the
    retry (one member at T, then again at 2T), `members + 2`. PARALLEL: the members run in ceil(members / threads)
    waves and the retried member's 2T runs in its own thread after its first T, so `waves + 2` - for a full panel of
    4 on 4 threads that is 3 slots, not 6 (lane A14 proposal (a): 1200 s serial becomes ~300 s plus the retry)."""
    waves = math.ceil(max(1, members) / max(1, min(threads, max(1, members))))
    return waves + max(0, retry_slots)


def panel_worst_case_s(members: int, timeout_s: float, *, threads: int = 1, retry_slots: int = RETRY_SLOTS) -> float:
    """Seconds a panel of `members` at `timeout_s` each can take, on `threads` parallel members."""
    return timeout_s * panel_slots(members, threads=threads, retry_slots=retry_slots) + PANEL_OVERHEAD_S


def clip_member_timeout(members: int, timeout_s: float, *, retry_slots: int = RETRY_SLOTS,
                        threads: int = 1) -> tuple[float, bool]:
    """`(timeout per member, was it clipped)` for a panel of `members` about to start on `threads` parallel members.

    Unclipped whenever the panel's whole worst case (`panel_worst_case_s`) fits in what is left - so a full-budget
    panel keeps the exact timeout it asked for, and its cache key (which carries the timeout) does not move.
    Otherwise the per-member budget is what remains (less the overhead) divided by the slots that could use it.

    Raises `TooLateToStart` when that derived budget is under `MIN_MEMBER_S`: a member under the floor cannot say
    anything about the part, and flooring it instead would start a panel that runs PAST the wall (A16's 930 s panel
    on a 900 s wall). The caller records the file as `checker_failed` on the wall; it never overruns.
    """
    left = remaining_s()
    if left is None:
        return (timeout_s, False)
    slots = panel_slots(members, threads=threads, retry_slots=retry_slots)
    if timeout_s * slots + PANEL_OVERHEAD_S <= left:
        return (timeout_s, False)
    derived = (left - PANEL_OVERHEAD_S) / slots
    if derived < MIN_MEMBER_S:
        raise TooLateToStart(
            f"{left:.1f} s left of the file wall: {slots} member slots would get {max(0.0, derived):.1f} s each, "
            f"under the {MIN_MEMBER_S:.0f} s a member needs to say anything")
    return (derived, True)


def fits(worst_case_s: float) -> bool:
    """Does a call whose worst case is `worst_case_s` fit in what is left? True when no deadline is set."""
    left = remaining_s()
    return True if left is None else worst_case_s <= left


#: The attribute a harness's file-wall kill carries (`reprobe.FileTimeout.FILE_WALL = True`). The kill is a
#: BaseException raised by SIGALRM wherever the main thread happens to be; the engine cannot import the harness's
#: class, so it recognises the kill by this marker. CRS v5 step 7: inside the tournament's thread pool the kill used
#: to escape every handler and lose the whole file; the tournament now turns it into a wall stop.
FILE_WALL_MARK = "FILE_WALL"


def is_file_wall(exc: BaseException) -> bool:
    """Is `exc` the harness's file-wall kill (a BaseException marked `FILE_WALL`)?"""
    return bool(getattr(type(exc), FILE_WALL_MARK, False))
