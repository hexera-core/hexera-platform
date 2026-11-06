# Responsibility: Say, as rows of measured fact rather than prose, exactly what the repair did -
#                 and what it REFUSED to do, with the reason it recorded.
# Owns: the Repair receipt's rows, read from what node_repair_inspect and node_repair_attempt
#       recorded, the plain words for a defect, a rollback and a refusal, and the one publication
#       that sends them.
# Boundaries: words and numbers only - it decides nothing, measures nothing, invents nothing, and
#             never fails a run: the user stream is never load-bearing. A fact that was not
#             measured is omitted; it is never guessed.
# Collaborates with: pipeline/repair_words.py (the lane's one line, and the vocabulary for what a
#                    kept operation did), pipeline/repair_attempt.py (records it, publishes this),
#                    events (the RECEIPT type), settings/cad_repair.py (the caps a change is read
#                    against).
from __future__ import annotations

import re
from collections.abc import Mapping

from meshpipeline.pipeline.repair_words import _DID, _FIXED, AGENT, _plural

#: The receipt's heading on the Repair lane.
TITLE = "Repair"

#: What a defect IS, in the words a user knows it by. The internal code is a snake_case key the
#: narration leak guard drops on sight, and rightly: "wire_gap" is our label, not an account.
_DEFECT_WORDS: dict[str, str] = {
    "invalid_brep": "topology the kernel calls invalid",
    "open_shell": "an opening in the surface",
    "wire_gap": "a gap where two edges did not meet",
    "small_edge": "an edge far shorter than the part's scale",
    "small_face": "a sliver face",
    "degenerate_edge": "a collapsed edge",
    "curve_inconsistency": "an edge curve that did not follow its surface",
    "self_intersection": "faces that cross each other",
    "non_manifold_surface": "a surface shared by more than two faces",
    "bad_orientation": "a face pointing the wrong way",
    "tolerance_outlier": "a tolerance far above the rest of the part",
    "duplicate_surface_data": "a surface stored twice",
    "engine_staging_failure": "geometry the mesh machine could not take",
}

#: WHY AN ATTEMPT WAS UNDONE, by the prefix the session records. The recorded reason is a code or
#: a kernel message; neither is something a customer should read.
_UNDONE: tuple[tuple[str, str], ...] = (
    ("untargetable", "what it aimed at could not be pinned down"),
    ("target_absent", "what it aimed at was no longer there"),
    ("held_for_review", "it would have changed something held for an engineer"),
    ("cap:", "the result moved the shape further than a repair is allowed to"),
    ("declined:", "the operation itself declined the work"),
)
_UNDONE_OTHERWISE = "the result did not hold up when it was measured"

#: WHAT WAS DELIBERATELY NOT ATTEMPTED, by the reason the planner recorded. The paired-rim entry
#: is the one this whole receipt exists for: a through-hole whose wall is missing has two flat
#: rims, and patching both hands the customer back a part with no bolt hole.
_REFUSED: dict[str, tuple[str, str]] = {
    "paired_rim": ("Did not fill an opening",
                   "it pairs with another as the two ends of one passage - a missing tube wall - "
                   "and filling both would seal a through-hole"),
    "untargetable": ("Left a defect alone",
                     "it has no measurable extent, so nothing could be aimed at it"),
    "no_operation_claims_it": ("Left a defect to an engineer",
                               "it needs a judgement no bounded operation makes"),
    "directive_unplayable": ("Did not play a requested fix",
                             "the run ended before it was reached"),
    "not_reached": ("Did not play a requested fix",
                    "the run ended before it was reached"),
}
_REFUSED_OTHERWISE = ("Refused a repair", "")

#: WHY THE LOOP STOPPED. "Every error cleared" and "I ran out of operations" leave the same
#: geometry and mean opposite things.
_STOPPED: dict[str, str] = {
    "resolved": "every error the scan found was cleared",
    "no_candidate": "nothing was left that a bounded operation claims",
    "op_budget": "the repair reached its limit on operations",
    "time_budget": "the repair reached its time limit",
    "stalled": "several attempts in a row changed nothing",
    "scan_incomplete": "the defect scan did not finish, so nothing can be called absent",
}

#: The measured lengths an inspection records against a located defect, worst-first by usefulness.
_SPAN_KEYS = ("boundary_length", "length")


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _safe(words: str) -> str:
    """`words` if a user may read it, else "". The guard is applied here as well as in the event
    constructor, because a row whose `why` leaks is dropped WHOLE there - and a refusal without
    its reason is still worth showing."""
    from meshpipeline.contracts.narration import leaks
    line = _text(words)
    return "" if not line or leaks(line) else line


def _row(label: str, value: str = "", why: str = "") -> dict:
    row = {"label": _text(label), "value": _text(value)}
    if why:
        row["why"] = _text(why)
    return row


def _sentence(words: str) -> str:
    return words[:1].upper() + words[1:] if words else ""


def _num(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n


def _records(out: Mapping) -> dict:
    record = out.get("repair_attempt")
    return dict(record) if isinstance(record, Mapping) else {}


def _key(name: str, code: str) -> tuple[str, str]:
    """The `_DID` entry a kept operation is counted under - the same lookup repair_words makes."""
    return next((k for k in ((name, code), (name, ""), ("", code)) if k in _DID), ("", ""))


def _without_count(words: str, n: int) -> str:
    """"closed 3 gaps" -> "closed gaps". The count is the row's VALUE; a label that repeats it
    says the same thing twice."""
    return _text(re.sub(rf"\b{n}\s+", "", words, count=1))


def _span_words(targets, code: str, entities: set[str]) -> str:
    """The spans the inspection MEASURED on the targets THIS ROW'S OPERATIONS ACTUALLY TOOK, in
    millimetres, or "" when none of them carries one.

    `entities` is what the kept operations named, and the span may be read from nothing else. It
    used to be the largest span over every target of the same CODE in `advice.targets` - what the
    repair was ADVISED to look at. A part with a 2 mm wire gap that was filled and a 40 mm
    paired-rim opening of the same code that was REFUSED printed "Closed 1 gap - largest measured
    span 40 mm" one line above the refusal of that very opening: the row claimed credit for a
    defect still in the customer's part.

    NOR IS IT CALLED THE LARGEST OF ITS KIND. `triage._targets` keeps the worst twenty defects, so
    a span can be measured and never forwarded; this says only what it is - the largest among the
    ones measured here.
    """
    best: float | None = None
    for target in targets or ():
        if not isinstance(target, Mapping) or str(target.get("code") or "") != code:
            continue
        if str(target.get("entity") or "") not in entities:
            continue
        measured = target.get("measurements")
        if not isinstance(measured, Mapping):
            continue
        for key in _SPAN_KEYS:
            value = _num(measured.get(key))
            if value is not None and value > 0 and (best is None or value > best):
                best = value
    return "" if best is None else f"spans up to {best:.3g} mm among those measured"


def _kept_rows(record: Mapping) -> list[dict]:
    """One row per kind of operation that was KEPT, grouped exactly as repair_words groups them."""
    counts: dict[tuple[str, str], int] = {}
    codes: dict[tuple[str, str], str] = {}
    took: dict[tuple[str, str], set[str]] = {}
    for op in record.get("operations") or ():
        if not isinstance(op, Mapping) or op.get("status") != "kept":
            continue
        name, code = str(op.get("name") or op.get("op") or ""), str(op.get("defect_code") or "")
        key = _key(name, code)
        counts[key] = counts.get(key, 0) + (len(op.get("targets") or ()) or 1)
        codes.setdefault(key, code)
        # WHAT THIS ROW MAY SPEAK FOR: the identifiers the kept operations themselves named. A
        # defect the repair was advised to look at and did not touch is not this row's to measure.
        took.setdefault(key, set()).update(
            str(t) for t in (op.get("targets") or ()) if isinstance(t, str))

    rows = []
    targets = record.get("targets") or ()
    for key, n in counts.items():
        code = codes.get(key, "")
        label = _sentence(_without_count(_plural(n, *_DID.get(key, _FIXED)), n))
        because = _DEFECT_WORDS.get(code, "")
        span = _span_words(targets, code, took.get(key) or set())
        why = "; ".join(x for x in (f"for {because}" if because else "", span) if x)
        rows.append(_row(label, str(n), _safe(why)))
    return rows


def _undone_rows(record: Mapping) -> list[dict]:
    """What was tried and did not stay. Named by the defect it aimed at: the operation's own label
    is internal, and its recorded reason is a code or a kernel message.

    A ROLLED-BACK operation ran, was measured, and was put back. A FAILED one raised while running
    (cad/repair/session.py marks it so) and may have produced no after-measurement at all. Saying
    "rolled back - the result did not hold up when it was measured" about the second one reports a
    measurement that was never taken, and dresses a tool fault as a judgement about the customer's
    part. They are different outcomes and the receipt says which it was.
    """
    seen: set[tuple[str, str]] = set()
    rows = []
    for op in record.get("operations") or ():
        if not isinstance(op, Mapping) or op.get("status") == "kept":
            continue
        code = str(op.get("defect_code") or "")
        because = _DEFECT_WORDS.get(code, "")
        label = f"Tried to fix {because}" if because else "Tried a repair"
        if str(op.get("status") or "") == "failed":
            # No reason is read from the record here on purpose: a failure's reason is whatever the
            # kernel said as it stopped, which is the one string on this path most likely to carry
            # a path, a tool name or a type nobody outside this codebase can act on.
            value, why = "Could not run", "the tool stopped while it was working on it"
        else:
            reason = str(op.get("reason") or "")
            value = "Rolled back"
            why = next((w for prefix, w in _UNDONE if reason.startswith(prefix)), _UNDONE_OTHERWISE)
        if (label, why) in seen:
            continue
        seen.add((label, why))
        rows.append(_row(label, value, _safe(why)))
    return rows


def _refusal_rows(record: Mapping) -> list[dict]:
    """What the repair DELIBERATELY did not do, with the reason it recorded. Identical rows are
    collapsed: a part with four paired rims refuses four times for one reason, and four copies of
    one line is a wall, not a receipt."""
    seen: set[tuple[str, str]] = set()
    rows = []
    for refusal in record.get("refusals") or ():
        if not isinstance(refusal, Mapping):
            continue
        reason = str(refusal.get("reason") or "")
        kind = str(refusal.get("refusal") or "")
        label, why = next((_REFUSED[k] for k in (reason, kind) if k in _REFUSED),
                          _REFUSED_OTHERWISE)
        why = why or _safe(str(refusal.get("detail") or ""))
        if (label, why) in seen:
            continue
        seen.add((label, why))
        rows.append(_row(label, "Refused", _safe(why)))
    return rows


#: DECLINED OUTCOMES WITH SOMETHING MEASURED TO ACCOUNT FOR. `repair_attempt._declined` records
#: `attempted: False` for every decline, but the refused one is the only one that carries the
#: loop it ran - its attempts, its refusals and its caps evidence - and that run is the one a
#: customer most needs an account of: we changed your part too far and threw the result away. The
#: rest (no_report, no_geometry, off, format, triage, timeout, unavailable, failed, not_promoted)
#: measured nothing a row could state, and the lane's one line already says which it was.
_ACCOUNTABLE_DECLINES = ("refused",)


def _view(record: Mapping) -> dict:
    """ONE SHAPE FOR BOTH PATHS, so the rows below read the repair loop rather than the path it
    took. A completed repair records its loop at the top of the attempt record; a REFUSED one
    returns a decline and carries the same loop inside `detail.measurements`
    (repair_attempt._attempt). `undone` marks the second: that loop's result reached no customer,
    so nothing of it may be reported as kept.
    """
    view = dict(record)
    if record.get("attempted") or str(record.get("outcome") or "") not in _ACCOUNTABLE_DECLINES:
        return view
    detail = record.get("detail")
    measured = detail.get("measurements") if isinstance(detail, Mapping) else None
    if not isinstance(measured, Mapping):
        return view
    view["undone"] = True
    view["measurements"] = dict(measured)
    view["operations"] = [dict(op) for op in (measured.get("operations") or ())
                          if isinstance(op, Mapping)]
    view["refusals"] = [dict(r) for r in (measured.get("refusals") or ())
                        if isinstance(r, Mapping)]
    return view


def _undone_whole_rows(view: Mapping) -> list[dict]:
    """The one row a refused run owes the customer: nothing of it reached your part.

    The two refusals read differently and must not be collapsed. `result_discarded` is a finished
    result the after-checks threw away WHOLE - operations the session had kept included, which is
    why `undone` suppresses every kept row above. Without it, the loop simply never kept anything.
    Neither says the part changed, and neither reports the recorded reason, which is a caps
    message full of ratios and kernel measurements. Nor does either name WHICH check refused: the
    after-checks refuse a result for losing a solid, deleting faces or moving the shape too far,
    and the row says the result did not hold up rather than guessing which of those it was. The
    shape rows beside it carry the figure when the figure is what refused it.
    """
    measured = view.get("measurements")
    discarded = bool(measured.get("result_discarded")) if isinstance(measured, Mapping) else False
    if discarded:
        return [_row("The repair was undone", "Nothing changed",
                     "the finished result did not hold up when the whole part was measured, "
                     "so your original file was used")]
    return [_row("Nothing was repaired", "Nothing changed",
                 "no attempt held up when it was measured, so your original file was used")]


def _measurements(record: Mapping) -> dict:
    """The repair's own before/after measurement, as `_view` normalised it. Empty when the run did
    not take one: this receipt reconstructs a measurement from nothing else.

    ONE PLACE LOOKS FOR IT. The refused path's copy lives in `detail.measurements` and is lifted
    into this key by `_view`, so there is no second search here to fall out of step with it.
    """
    measured = record.get("measurements")
    if isinstance(measured, Mapping) and isinstance(measured.get("before"), Mapping) \
            and isinstance(measured.get("after"), Mapping):
        return dict(measured)
    return {}


def _percent(ratio: float) -> str:
    pct = abs(ratio) * 100.0
    return f"{pct:.2g}%" if pct >= 0.01 else "under 0.01%"


def _shape_rows(record: Mapping, *, undone: bool = False) -> list[dict]:
    """How far the shape MOVED, and how much of that move no operation claimed.

    `undone` is the refused run, whose before/after describe a result that was thrown away. The
    number is still the evidence the refusal was made on - often the whole reason for it - but the
    label has to say it is the undone result that moved, not the customer's part.

    THE CAP IS NOT ON THE TOTAL. `cad/repair/caps._accounted` subtracts what the kept operations
    DECLARED they would add before judging: a fill that says it adds 5% of the area and adds 5% is
    fully accounted for and passes a 2% residual. Printing the total beside that 2% made an
    approved repair read as though it had broken its own limit - the receipt accused the run of
    something the guard had explicitly allowed. The total is still shown, because it is the honest
    answer to "how much did my part change"; the cap is shown against the unexplained part, which
    is the number it actually bounds.

    Omitted entirely when the run did not measure it: a fact that cannot be measured is None,
    never a guess.
    """
    import meshpipeline.settings.cad_repair as rcfg

    measured = _measurements(record)
    if not measured:
        return []
    before, after = dict(measured["before"]), dict(measured["after"])
    rows = []
    for what, key, declared_key, cap in (
            ("The undone repair changed the surface area" if undone else "Surface area changed",
             "surface_area_mm2", "declared_area_mm2",
             rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO),
            ("The undone repair changed the volume" if undone else "Volume changed",
             "volume_mm3", "declared_volume_mm3",
             rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO)):
        was, now = _num(before.get(key)), _num(after.get(key))
        if was is None or now is None or was <= 0:
            continue
        if key == "volume_mm3" and not _volume_was_compared(before, after):
            # THE GUARD SKIPS THIS COMPARISON, so the receipt may not imply it was made. An open
            # shell has no inside: OpenCASCADE still returns a number for it, and that number is
            # not a volume. A body that CLOSED is new volume no operation could have declared.
            rows.append(_row(what, _percent((now - was) / was),
                             "not checked against a limit here: the part has no closed inside to "
                             "measure, or a body closed that no repair could have predicted"))
            continue
        declared = _num(measured.get(declared_key)) or 0.0
        unexplained = abs(now - (was + declared)) / was
        why = (f"{_percent(unexplained)} of that is not accounted for by the repairs, and the "
               f"limit on that is {_percent(float(cap))}")
        if declared:
            why = f"the repairs declared {_percent(declared / was)} of it; " + why
        rows.append(_row(what, _percent((now - was) / was), why))
    return rows


def _volume_was_compared(before: Mapping, after: Mapping) -> bool:
    """Whether `cad/repair/caps.cumulative_refusal` actually judged the volume, by its own rule:
    both sides must have an inside, and a body must not have closed during the run."""
    was_solids, now_solids = _num(before.get("solids")), _num(after.get("solids"))
    if was_solids is None or now_solids is None:
        return False
    return was_solids > 0 and now_solids > 0 and now_solids <= was_solids


def _outcome_row(record: Mapping) -> list[dict]:
    """Whether the loop finished the job, and why it stopped - both from what it recorded."""
    if not record.get("attempted"):
        return []
    resolved = bool(record.get("resolved", False))
    why = _STOPPED.get(str(record.get("stopped_because") or ""), "")
    return [_row("Everything found was fixed", "Yes" if resolved else "No", _safe(why))]


def repair_receipt_rows(state: Mapping, out: Mapping) -> list[dict]:
    """The Repair receipt's rows, from the inspection in `state` and the attempt's `out`.

    [] when there is nothing true to say: no inspection, nothing to attempt, and nothing refused.
    The caller must not publish an empty receipt - a heading with no rows claims a record exists.

    A REFUSED RUN GETS ITS RECEIPT TOO (see `_view`), and it is the run that most needs one. What
    it does NOT get is a kept row or an "everything found was fixed" row: its result was thrown
    away whole, so the operations the session kept changed nothing the customer has. The declines
    that measured nothing still return [] - including `no_report` and `no_geometry`, which carry
    no refusals and no detail and so fall out of the same test as every other one.
    """
    state = state or {}
    record = _records(out or {})
    if not dict(state.get("repair_report") or {}):
        return []
    view = _view(record)

    if view.get("undone"):
        # The restatement row is worth sending only beside something measured: on its own it says
        # no more than the lane's line already did, under a heading claiming a record.
        measured = (_undone_rows(view) + _refusal_rows(view) + _shape_rows(view, undone=True))
        rows = measured + _undone_whole_rows(view) if measured else []
    elif not view.get("attempted") and not (view.get("refusals") or ()):
        return []
    else:
        rows = (_kept_rows(view) + _undone_rows(view) + _refusal_rows(view)
                + _shape_rows(view) + _outcome_row(view))
    return [r for r in rows if r["label"]]


async def apublish(job_id: str, rows: list[dict] | None) -> None:
    """Send the receipt. Nothing when there are no rows: see repair_receipt_rows."""
    if not rows:
        return
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        _pub = execution_publisher(job_id, agent=AGENT)
        await _pub.areceipt(TITLE, list(rows), op_id="repair:receipt")
    except StaleExecutionPublish:
        # a lost claim is not a stream hiccup: a superseded generation must stop here
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


__all__ = ["AGENT", "TITLE", "apublish", "repair_receipt_rows"]
