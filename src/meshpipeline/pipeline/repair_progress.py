# Responsibility: Say where the CAD repair is, live, in plain words: map the CRS v5 engine's events to
#                 `repair_progress` events on the job's stream (CRS v5 beta spec, "Contract"), and
#                 relay them from the repair's thread onto the stream as it runs.
# Owns: the mapping (which engine event becomes which stage and line), the line templates, the
#       lane's publication of each line, and `ProgressRelay`.
# Boundaries: words only. It decides nothing about the repair, never fails a run (the user stream
#             is never load-bearing) and NEVER forwards an engine string: every line is built from
#             this module's own templates and the plain tables in contracts/repair_plain, and is
#             checked by its leak guard before it is published.
# Collaborates with: cad/repair_v5/result.ProgressMapper (the engine's own plain events, handed to
#                    `ProgressRelay.push` from the repair's thread), pipeline/repair_attempt.py (which
#                    opens and closes the lane), events/__init__.py (`repair_progress`),
#                    contracts/repair_plain.py, contracts/narration.py (the lane's step rows).
#
# WHAT THE LANE SAYS (spec "User-facing language"):
#   checking        "Checking your CAD" / "Found 12 problems to fix"
#   repairing       "Repairing a thin gap — problem 3 of 12" / "Fixed" /
#                   "Couldn't fix this one — left as is"
#   broader_repair  "Trying a broader repair"
#   verifying       "Checking the repaired part"
#   done            "Repair finished: 10 of 12 fixed"
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.contracts.repair_plain import a_or_an, leaks, type_key, type_words

logger = logging.getLogger(__name__)

#: The lane's stages (events.REPAIR_PROGRESS_STAGES).
CHECKING = "checking"
REPAIRING = "repairing"
BROADER = "broader_repair"
VERIFYING = "verifying"
DONE = "done"

#: The fixed lines. Every line the lane can say is one of these or `_repairing_line`/`_done_line`.
CHECKING_TEXT = "Checking your CAD"
FIXED_TEXT = "Fixed"
NOT_FIXED_TEXT = "Couldn't fix this one — left as is"
BROADER_TEXT = "Trying a broader repair"
VERIFYING_TEXT = "Checking the repaired part"
NOTHING_FOUND_TEXT = "No problems found that need fixing"

#: Keys an engine event may carry the number of problems the check found under.
_TOTAL_KEYS = ("problem_total", "defects_found", "defects_total", "found", "defects")


def _problems(n: int) -> str:
    return f"{n} problem" + ("" if n == 1 else "s")


def found_line(total: int) -> str:
    return NOTHING_FOUND_TEXT if total <= 0 else f"Found {_problems(total)} to fix"


def repairing_line(kind: object, index: int | None, total: int | None) -> str:
    where = ""
    if index:
        where = f" — problem {index}" + (f" of {total}" if total else "")
    return f"Repairing {a_or_an(type_words(kind))}{where}"


def done_line(fixed: int | None, total: int | None) -> str:
    if total is None or fixed is None:
        return "Repair finished"
    if total <= 0:
        return "Repair finished: nothing needed fixing"
    return f"Repair finished: {min(fixed, total)} of {total} fixed"


def progress(stage: str, text: str, *, index: int | None = None, total: int | None = None,
             kind: object = None, result: str | None = None,
             fixed: int | None = None) -> dict:
    """One `repair_progress` payload (spec "Contract"); `problem_type` is the class KEY (the
    console says it from the plain table), never free text. `result` (fixed | left_as_is) ends a
    problem's turn; `problems_fixed` rides the done line."""
    out = {"stage": stage, "problem_index": index, "problem_total": total,
           "problem_type": None if kind is None else type_key(kind), "text": text}
    if result is not None:
        out["result"] = result
    if fixed is not None:
        out["problems_fixed"] = fixed
    return out


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value)
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


@dataclass
class ProgressMapper:
    """The engine's event log -> the lane's lines. STATEFUL: problem numbers are counted as the
    engine takes them up, and a problem's turn that ends without a "gone" says it was left.

    Feed it every event dict, in order; it returns the lines to publish (possibly none). Engine
    strings never pass through: only the event's `event`/`phase` names, its defect id and class,
    its result word and its counts are READ, and the line is built from templates."""

    total: int | None = None
    _index: dict[str, int] = field(default_factory=dict)
    _kind: dict[str, Any] = field(default_factory=dict)
    _current: str | None = None
    _current_fixed: bool = False
    _last: tuple | None = None
    _ready_index: int | None = None

    def feed(self, event: Mapping[str, Any]) -> list[dict]:
        try:
            out = self._map(event)
        except Exception:  # noqa: BLE001 - a line nobody can read is no line, never a failure
            logger.debug("repair progress: an engine event could not be mapped", exc_info=True)
            out = []
        lines: list[dict] = []
        for line in out:
            if leaks(line["text"]):
                continue                    # THE HARD RULE: a leaking line is dropped, not shown
            key = (line["stage"], line["text"], line["problem_index"])
            if key == self._last:
                continue                    # the same line twice in a row says nothing new
            self._last = key
            lines.append(line)
        return lines

    def _close_turn(self) -> list[dict]:
        """The problem in hand ended its turn: said as left when nothing said it was fixed."""
        if self._current is None:
            return []
        current, fixed = self._current, self._current_fixed
        kind = self._kind.get(current)
        self._current, self._current_fixed = None, False
        if fixed:
            return []
        return [progress(REPAIRING, NOT_FIXED_TEXT, index=self._index.get(current),
                         total=self.total, kind=kind, result="left_as_is")]

    def _map(self, ev: Mapping[str, Any]) -> list[dict]:
        name = str(ev.get("event") or "")
        for key in _TOTAL_KEYS:
            found = _int(ev.get(key)) if key in ev else None
            if found is not None and name != "repair_progress":
                self.total = found
                return [progress(CHECKING, found_line(found), total=found)]
        if name == "repair_progress":
            return self._ready(ev)
        if name == "file_start":
            return [progress(CHECKING, CHECKING_TEXT)]
        if name == "phase":
            phase = str(ev.get("phase") or "")
            if phase == "diagnose":
                return [*self._close_turn(), progress(CHECKING, CHECKING_TEXT, total=self.total)]
            if phase == "tournament":
                return self._take_up(ev)
            if phase == "fallback":
                return [*self._close_turn(), progress(BROADER, BROADER_TEXT, total=self.total)]
            if phase == "round_panel":
                return [*self._close_turn(), progress(VERIFYING, VERIFYING_TEXT, total=self.total)]
            return []
        if name == "local_verify" and str(ev.get("result") or "") == "gone":
            defect = str(ev.get("defect_id") or self._current or "")
            if defect and defect == self._current and not self._current_fixed:
                self._current_fixed = True
                return [progress(REPAIRING, FIXED_TEXT, index=self._index.get(defect),
                                 total=self.total,
                                 kind=ev.get("defect_class") or self._kind.get(defect),
                                 result="fixed")]
            return []
        if name == "file_end":
            return self._close_turn()
        return []

    def _take_up(self, ev: Mapping[str, Any]) -> list[dict]:
        defect = str(ev.get("defect_id") or "")
        if not defect:
            return []
        out: list[dict] = []
        if defect != self._current:
            out.extend(self._close_turn())
            self._current, self._current_fixed = defect, False
        index = self._index.setdefault(defect, len(self._index) + 1)
        if self.total is not None and index > self.total:
            self.total = index      # a problem found on a later look: the count grows, never lies
        kind = ev.get("defect_class")
        self._kind[defect] = kind
        out.append(progress(REPAIRING, repairing_line(kind, index, self.total), index=index,
                            total=self.total, kind=kind))
        return out

    def _ready(self, ev: Mapping[str, Any]) -> list[dict]:
        """A `repair_progress` the ENGINE wrote itself: its numbers are taken, its words are not -
        the line is said again from this module's templates."""
        stage = str(ev.get("stage") or "")
        index, total = _int(ev.get("problem_index")), _int(ev.get("problem_total"))
        if total is not None:
            self.total = total
        kind = ev.get("problem_type")
        status = str(ev.get("status") or ev.get("result") or "")
        said = " ".join(str(ev.get("text") or "").split())
        if not status and said and not leaks(said):
            # the engine's own plain result line (cad/repair_v5/result.ProgressMapper says "Fixed",
            # "Couldn't fix ..." or "Left as is: ...") carries the result its fields do not
            if said.startswith("Fixed"):
                status = "fixed"
            elif said.startswith(("Couldn't fix", "Left as is")):
                status = "left_as_is"
        if stage == CHECKING:
            return [progress(CHECKING, found_line(total) if total is not None else CHECKING_TEXT,
                             total=total)]
        if stage == REPAIRING:
            if status in ("fixed", "not_fixed", "left_as_is"):
                # A RESULT LINE ends the turn of the problem in hand: the engine does not repeat
                # its number, so the lane carries the one it took up (found on the slug)
                index = index if index is not None else self._ready_index
                text = FIXED_TEXT if status == "fixed" else NOT_FIXED_TEXT
                return [progress(REPAIRING, text, index=index, total=self.total, kind=kind,
                                 result=status)]
            if index is not None:
                self._ready_index = index
            return [progress(REPAIRING, repairing_line(kind, index, self.total), index=index,
                             total=self.total, kind=kind)]
        if stage == BROADER:
            return [progress(BROADER, BROADER_TEXT, total=self.total)]
        if stage == VERIFYING:
            return [progress(VERIFYING, VERIFYING_TEXT, total=self.total)]
        if stage == DONE:
            fixed = _int(ev.get("fixed") or ev.get("problems_fixed"))
            return [progress(DONE, done_line(fixed, self.total), total=self.total, fixed=fixed)]
        return []


def map_events(events: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Every line a sequence of engine events says (a fresh mapper)."""
    mapper = ProgressMapper()
    out: list[dict] = []
    for ev in events:
        out.extend(mapper.feed(ev))
    return out


async def apublish(job_id: str, line: Mapping[str, Any], *, seq: int) -> None:
    """One line on the job's stream, on the repair lane. Never raises but for a lost claim."""
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        pub = execution_publisher(job_id, agent="repair")
        await pub.arepair_progress(dict(line), op_id=f"repair:progress:{seq}")
    except StaleExecutionPublish:
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        logger.debug("repair progress: a line was not published", exc_info=True)


class Lane:
    """The repair lane for one run: say lines in order, each with its own identity (so a replay
    dedupes), whether the node says them or the engine does (`feed`, from `ProgressRelay`)."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.mapper = ProgressMapper()
        self._seq = 0
        self.said: list[dict] = []

    async def say(self, line: Mapping[str, Any]) -> None:
        if leaks(str(line.get("text") or "")):
            return
        key = (line.get("stage"), line.get("text"), line.get("problem_index"))
        if self.said and key == (self.said[-1].get("stage"), self.said[-1].get("text"),
                                 self.said[-1].get("problem_index")):
            return              # the node and the engine both said it: once is enough
        self.mapper._last = key
        self._seq += 1
        self.said.append(dict(line))
        await apublish(self.job_id, line, seq=self._seq)

    async def feed(self, event: Mapping[str, Any]) -> None:
        for line in self.mapper.feed(event):
            await self.say(line)


#: the narration kind each progress stage shows as (contracts/narration.KINDS)
STAGE_KIND = {"checking": "check", "repairing": "refine", "broader_repair": "refine", "verifying": "check",
              "done": "check"}
#: the op id the lines carry, so a reader of the stream can pick out the repair's progress
OP_ID = "repair:progress"


class ProgressRelay:
    """`push` is called from the repair's worker thread; `run` (a task on the node's loop) publishes each event in
    order as a narration step row (text, `n` = problem index, `of` = problem total) and - with a `lane` - as the typed
    `repair_progress` event the repair view reads, re-said by the lane from its own templates. Each event is also
    kept (`events`) so the run's record can carry them."""

    def __init__(self, job_id: str, loop: asyncio.AbstractEventLoop | None = None,
                 lane: Lane | None = None) -> None:
        self.job_id = job_id
        self.loop = loop or asyncio.get_running_loop()
        self.lane = lane
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.events: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    def push(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(RuntimeError):     # the loop has closed: the line is dropped
            self.loop.call_soon_threadsafe(self.queue.put_nowait, dict(event))

    def start(self) -> ProgressRelay:
        self._task = self.loop.create_task(self._run())
        return self

    async def stop(self) -> None:
        if self._task is None:
            return
        self.queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=10)

    async def _run(self) -> None:
        from meshpipeline.contracts.event_stream import StaleExecutionPublish
        from meshpipeline.contracts.narration import astep
        from meshpipeline.pipeline.repair_words import AGENT

        publisher: Any = None
        while True:
            event = await self.queue.get()
            if event is None:
                return
            self.events.append(event)
            try:
                if publisher is None:
                    from meshpipeline.application.execution_publisher import execution_publisher
                    publisher = execution_publisher(self.job_id, agent=AGENT)
                await astep(publisher, STAGE_KIND.get(str(event.get("stage")), "check"), str(event.get("text") or ""),
                            n=event.get("problem_index"), of=event.get("problem_total"), op_id=OP_ID)
                if self.lane is not None:
                    await self.lane.feed({**event, "event": "repair_progress"})
            except StaleExecutionPublish:
                return          # a superseded generation stops publishing; the repair itself is not this task's
            except Exception:  # noqa: BLE001 - the user stream is never load-bearing
                logger.debug("repair progress line not published", exc_info=True)


__all__ = ["BROADER", "CHECKING", "CHECKING_TEXT", "DONE", "Lane", "OP_ID", "ProgressMapper",
           "ProgressRelay", "REPAIRING", "STAGE_KIND", "VERIFYING", "apublish", "done_line",
           "found_line", "map_events", "progress", "repairing_line"]
