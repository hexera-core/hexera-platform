# Responsibility: Say what a run decided about its mesh before it built it - the same record for every route - as one receipt for the run box's "Designing the mesh" row.
# Owns: which decision-record stages are design decisions, how a decision becomes a plain row, and the one publisher every mesher start calls.
# Boundaries: words only. It reads the run's own records (the decision record, the planned size the engine wrote beside its case, the notes and the remarks steps left on them) and decides nothing; a row that fails the narration leak guard is dropped, and a failure never touches the run.
# Collaborates with: contracts/decision_record (the decisions), engines/live_estimate (the planned size), contracts/user_notes (the notes and remarks), events.receipt (the event), ui/js/core/run_rows.js (draws it in row 2).
"""THE MESH DESIGN, as one receipt.

Every route designs its mesh before it builds it: a size to aim for, a base size, where it refines,
the smallest sizes it allows, what it did with the user's notes, what it set aside. Some routes said
part of that as commentary, some said none of it, so the run box showed decision-making on one route
and not on another. This module is the one place that reads it back - from records every route keeps
or can keep - and publishes it the same way for all of them:

    {type: "receipt", stage: "builder", title: "The mesh design", rows: [{label, value, why}]}

A route adds its decisions by citing them in the decision record under one of DESIGN_STAGES (or by
adding its own stage name there). Nothing here is engine-specific.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from meshpipeline.contracts.event_stream import EventStreamError

logger = logging.getLogger(__name__)

#: The receipt's title, as the run box shows it.
TITLE = "The mesh design"

#: THE DESIGN STAGES of the decision record: the stages whose decisions are about the mesh's design
#: (sizes, refinement, minimums, what was set aside, what the notes changed). Run plumbing - the
#: machine, the clocks, the retry ladder, the engine pick, the verdict - is not design and stays out.
#: A new route that cites its design under a stage of its own adds that stage here, and only here.
DESIGN_STAGES: tuple[str, ...] = (
    "fea_plan",      # engines/fea_plan: element, base size, zones, set-asides, the element limit
    "fea_ai",        # engines/fea_ai: what the AI read from the brief and the notes, and did
    "plan",          # engines/resolution_policy: narrow spots sized locally
    "builder",       # agents/builder/invoke + engines/plan_guard: which plan configured the mesh
)

#: The most decision rows one receipt carries (the receipt itself is capped at 12).
MAX_DECISIONS = 9

#: The runs whose current builder attempt has said its design already (in this process), so the
#: attempt's catch-all at its end does not say a second, different version of it. Bounded.
_SAID: OrderedDict[str, bool] = OrderedDict()
_SAID_MAX = 256


def begin(job_id: str) -> None:
    """A builder attempt starts: its design has not been said yet."""
    _SAID.pop(str(job_id), None)


def _mark_said(job_id: str) -> None:
    _SAID[str(job_id)] = True
    _SAID.move_to_end(str(job_id))
    while len(_SAID) > _SAID_MAX:
        _SAID.popitem(last=False)


def _plain(text: Any) -> str | None:
    from meshpipeline.contracts.narration import plain, soften
    return plain(soften(" ".join(str(text or "").split())))


def _label(decided: Any) -> str:
    """A decision's name as a row label: "base size" -> "Base size". A name that is not plain
    words (an id, a pair of face names) reads "Decision" instead."""
    words = _plain(str(decided or "").replace("_", " "))
    if not words or len(words) > 40:
        return "Decision"
    return words[:1].upper() + words[1:]


def _row(label: str, why: str) -> dict:
    return {"label": label, "value": "", "why": why}


def _size_row(planned: tuple | None) -> dict | None:
    """The planned size, from the (count, noun) the engine recorded beside its case
    (engines/live_estimate.planned_live_count, read by the caller: contracts sit below engines)."""
    from meshpipeline.events import cells_words

    of, unit = (tuple(planned or ()) + (None, ""))[:2]
    try:
        of = int(of or 0)
    except (TypeError, ValueError):
        of = 0
    if of <= 0:
        return None
    words = _plain(f"About {cells_words(int(of))} {unit or 'cells'} planned.")
    return _row("Size", words) if words else None


def _decision_rows(job_id: str) -> list[dict]:
    from meshpipeline.contracts.decision_record import cited_for

    latest: dict[tuple[str, str], dict] = {}
    for e in cited_for(job_id):
        if str(e.get("stage") or "") in DESIGN_STAGES:
            # the latest word on each decision: a later pass re-plans, and its plan is the one meshed
            latest.pop((str(e.get("stage")), str(e.get("decided"))), None)
            latest[(str(e.get("stage")), str(e.get("decided")))] = e
    rows: list[dict] = []
    seen: set[str] = set()
    for e in latest.values():
        words = _plain(e.get("words"))
        if not words or words in seen:
            continue
        seen.add(words)
        rows.append(_row(_label(e.get("decided")), words))
        if len(rows) >= MAX_DECISIONS:
            break
    return rows


def _notes_rows(workspace: Any) -> list[dict]:
    from meshpipeline.contracts.user_notes import NOTES_FILE, read_remarks

    if not workspace:
        return []
    rows = []
    for r in read_remarks(workspace):
        # said as the result says it ("From your notes: ..."), so the row reads alone
        words = _plain(f"From your notes: {r}")
        if words:
            rows.append(_row("Your notes", words))
    if rows:
        return rows
    try:
        has_notes = bool((Path(workspace) / NOTES_FILE).read_text(encoding="utf-8").strip())
    except OSError:
        has_notes = False
    said = "Your notes were read as guidance; no step reported a change for them."
    return [_row("Your notes", said)] if has_notes else []


def design_rows(job_id: str, workspace: Any, planned: tuple | None = None) -> list[dict]:
    """The design receipt's rows for this run, in reading order: the planned size, the decisions,
    then the notes. [] when there is nothing on record. Never raises."""
    rows: list[dict] = []
    try:
        size = _size_row(planned)
        if size:
            rows.append(size)
        rows += _decision_rows(job_id) if job_id else []
        rows += _notes_rows(workspace)
    except Exception:  # noqa: BLE001 - the design receipt is a courtesy, never a reason to fail
        logger.debug("mesh design: rows not read", exc_info=True)
    return rows[:12]


async def apublish(publish: Any, *, job_id: str, workspace: Any, planned: tuple | None = None,
                   attempt: int = 0, unsaid_only: bool = False) -> bool:
    """Publish the design receipt (once per distinct content per attempt). `planned` is the
    engine's own (count, noun) for the case, when it recorded one; `unsaid_only` is the builder
    attempt's catch-all, which says nothing when the engine's own path already said the design
    since the attempt began. False when nothing was published. A lost claim (EventStreamError) is
    raised, as at every fenced boundary; any other failure is swallowed."""
    send = getattr(publish, "areceipt", None) if publish is not None else None
    if send is None or (unsaid_only and _SAID.get(str(job_id))):
        return False
    rows = design_rows(job_id, workspace, planned)
    if not rows:
        return False
    digest = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:10]
    try:
        await send(TITLE, rows, op_id=f"design:{int(attempt or 0)}:{digest}")
    except EventStreamError:
        raise
    except Exception:  # noqa: BLE001 - the design receipt is a courtesy
        return False
    _mark_said(job_id)
    return True


__all__ = ["DESIGN_STAGES", "MAX_DECISIONS", "TITLE", "apublish", "begin", "design_rows"]
