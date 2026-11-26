# Responsibility: Read the repair engine's result (`RepairResultV5`, CRS v5 beta spec "Contract")
#                 off what the repair recorded, decide whether it waits for a person, and render the
#                 job API's `repair` section from it - in plain words only.
# Owns: where the engine's result is looked for, the review decision (needs review vs the bypass),
#       the review items a waiting repair parks with, and the section's shape.
# Boundaries: PURE over dicts. It repairs nothing, reads no storage and never raises out of its
#             public functions: a result it cannot read is "no result", and the section says what
#             is known. Every string a customer reads passes contracts/repair_plain.
# Collaborates with: cad/repair/conservative.py (`repair_step_file`, whose report carries the
#                    result), pipeline/repair_attempt.py (the decision's caller),
#                    application/final_result.py (where the section is kept), api/v1/simulation.py
#                    (where it is served), application/repair_review_park.py (the park).
from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.repair_plain import (
    FLAG_WORDS,
    OUTCOMES,
    STATUS_WORDS,
    STATUSES,
    leaks,
    plain,
    type_key,
    type_words,
)

logger = logging.getLogger(__name__)

#: Where the engine's result may sit in the repair's measurements. PR 1 of the beta defines the
#: exact key; every spelling it might take is read, the first that holds a result wins.
RESULT_KEYS = ("repair_result_v5", "repair_result", "result_v5", "v5_result")
#: ...and the attribute names it may hang off the report object itself.
RESULT_ATTRS = ("repair_result_v5", "result_v5", "repair_result", "v5")

#: The op a person's acceptance of a waiting change travels as on the review re-run
#: (api/v1/repair_review.py turns an accepted item into a directive carrying its proposal's op).
#: It is NEVER handed to the repair planner: repair_attempt filters it out and reads it here.
ACCEPT_OP = "accept_change"

#: ...and the op a waiting change the person LEFT (or did not answer) travels as on the re-run.
LEAVE_OP = "leave_change"

#: The review item origin a waiting repair's items carry, so the review screen and the re-run can
#: tell them from the rules planner's.
REVIEW_ORIGIN = "change_review"


def _sub(obj: Any, key: str) -> Mapping[str, Any]:
    """`obj[key]` when it is a mapping, else {} - a record read defensively, never trusted."""
    value = obj.get(key) if isinstance(obj, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _as_dict(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            out = to_dict()
            return dict(out) if isinstance(out, Mapping) else None
        except Exception:  # noqa: BLE001
            return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return dataclasses.asdict(value)
        except Exception:  # noqa: BLE001
            return None
    return None


def extract(measured: Mapping | None = None, report: Any = None) -> dict | None:
    """The engine's result as a dict, or None (today's v3 run, or nothing readable)."""
    try:
        for attr in RESULT_ATTRS:
            got = _as_dict(getattr(report, attr, None)) if report is not None else None
            if got:
                return got
        # PR 1 (cad/repair/conservative.py) carries it in the report's `diagnostics`
        diagnostics = getattr(report, "diagnostics", None) if report is not None else None
        for key in RESULT_KEYS:
            got = _as_dict(diagnostics.get(key)) if isinstance(diagnostics, Mapping) else None
            if got:
                return got
        for key in RESULT_KEYS:
            got = _as_dict((measured or {}).get(key))
            if got:
                return got
    except Exception:  # noqa: BLE001 - an unreadable result is no result
        logger.debug("repair result: unreadable", exc_info=True)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else max(int(number), 0)


def over_threshold(problem: Mapping, allowed_mm: float | None) -> bool:
    change = _num(problem.get("change_mm"))
    return (change is not None and allowed_mm is not None and change > allowed_mm
            and str(problem.get("status") or "") == "fixed")


def needs_review(result: Mapping | None) -> bool:
    """Whether the engine's result changed the part past what is accepted without a person: its
    own `needs_review`, or - when it does not say - any fixed problem moved past the allowance."""
    if not result:
        return False
    said = result.get("needs_review")
    if isinstance(said, bool):
        return said
    allowed = _num(result.get("allowed_change_mm"))
    biggest = _num(result.get("max_change_mm"))
    if biggest is not None and allowed is not None and biggest > allowed:
        return True
    return any(over_threshold(p, allowed) for p in result.get("problems") or ()
               if isinstance(p, Mapping))


#: The review item that stands for a change to the whole part, when the engine needs a review the
#: per-problem movements do not explain (a broader repair, a volume or size change).
WHOLE_PART_ID = "whole_part"


def accepted_ids(directives) -> frozenset[str]:
    """The waiting changes a person accepted on the review re-run (each `accept_change`'s targets)."""
    out: set[str] = set()
    for d in directives or ():
        if isinstance(d, Mapping) and d.get("op") == ACCEPT_OP:
            out.update(str(t) for t in (d.get("targets") or ()) if t)
            if d.get("defect_fingerprint"):
                out.add(str(d["defect_fingerprint"]))
    return frozenset(out)


def left_by_person(directives) -> bool:
    """Whether, on this review re-run, a person left (or never answered) a waiting change."""
    return any(isinstance(d, Mapping) and d.get("op") == LEAVE_OP for d in directives or ())


def accepted_by_person(directives, result: Mapping | None = None) -> bool:
    """Whether a person accepted EVERY change waiting for them - never "some acceptance means all".

    The review re-run carries an `accept_change` for each change accepted and a `leave_change` for
    each one left or unanswered (api/v1/repair_review.run_review). Any leave and the repair is not
    applied. With no leave, every waiting change was accepted - the person approved the repair,
    whatever numbering the re-run's own result gives its problems (a re-run can find them in
    another order). A directive set with acceptances but no leave marker at all (an older park)
    is read item by item against this result's waiting items."""
    accepted = accepted_ids(directives)
    if left_by_person(directives) or not accepted:
        return False
    if result is None or any(isinstance(d, Mapping) and d.get("op") == ACCEPT_OP
                             and d.get("item_id") for d in directives or ()):
        return True
    waiting = {str(item["fingerprint"]) for item in review_items(result)}
    return bool(waiting) and waiting <= accepted


def without_acceptances(directives) -> tuple:
    """The directives the repair planner may play: a person's accept or leave is not an operation."""
    return tuple(d for d in directives or () if not (isinstance(d, Mapping)
                                                      and d.get("op") in (ACCEPT_OP, LEAVE_OP)))


def review_items(result: Mapping) -> list[dict]:
    """One review item per change that waits for a person: what it was, where, how far it moved
    the part, and the proposal a person accepts (`ACCEPT_OP`). The repair review screen shows
    them with the before/after the repair viewer serves."""
    allowed = _num(result.get("allowed_change_mm"))
    items: list[dict] = []
    for raw in result.get("problems") or ():
        if not isinstance(raw, Mapping) or not over_threshold(raw, allowed):
            continue
        pid = str(raw.get("id") or f"p{len(items) + 1}")
        location = _sub(raw, "location")
        faces = [f for f in (location.get("faces") or ()) if f is not None]
        kind = type_key(raw.get("type"))
        change = _num(raw.get("change_mm"))
        items.append({
            "group_key": f"{REVIEW_ORIGIN}:{kind}",
            "defect_code": kind,
            "severity": "warning",
            "entity_type": "face",
            "entity_index": _face_index(faces[0]) if faces else 0,
            "fingerprint": pid,
            "measurements": {"change_mm": change, "allowed_change_mm": allowed,
                             "faces": [str(f) for f in faces][:50],
                             "points": [list(p) for p in (location.get("points") or ())
                                        if isinstance(p, (list, tuple))][:20]},
            "message": (f"Fixing this {type_words(kind)} moved the part by "
                        f"{_mm(change)}, more than the {_mm(allowed)} allowed without your "
                        "approval"),
            "origin": REVIEW_ORIGIN,
            "proposal": {"op": ACCEPT_OP, "targets": [pid], "source": "repair"},
            "camera": {},
        })
    if not items and needs_review(result):
        # A REVIEW NO SINGLE PROBLEM EXPLAINS (a broader repair, the part's volume or size moved):
        # one item for the whole part, so the park always has something to approve
        biggest = _num(result.get("max_change_mm"))
        items.append({
            "group_key": f"{REVIEW_ORIGIN}:{WHOLE_PART_ID}",
            "defect_code": WHOLE_PART_ID,
            "severity": "warning",
            "entity_type": "part",
            "entity_index": 0,
            "fingerprint": WHOLE_PART_ID,
            "measurements": {"change_mm": biggest, "allowed_change_mm": allowed,
                             "volume_ratio": _num(result.get("volume_ratio")),
                             "size_ratio": _num(result.get("size_ratio"))},
            "message": (f"The repair changed your part by up to {_mm(biggest)} overall, more "
                        "than is applied without your approval"),
            "origin": REVIEW_ORIGIN,
            "proposal": {"op": ACCEPT_OP, "targets": [WHOLE_PART_ID], "source": "repair"},
            "camera": {},
        })
    return items


def _face_index(face: Any) -> int:
    digits = "".join(ch for ch in str(face) if ch.isdigit())
    return int(digits) if digits else 0


def _mm(value: float | None) -> str:
    if value is None:
        return "an unmeasured amount"
    return f"{value:.3g} mm"


def _problem(raw: Mapping, allowed_mm: float | None) -> dict:
    status = str(raw.get("status") or "")
    status = status if status in STATUSES else "not_fixed"
    kind = type_key(raw.get("type"))
    location = _sub(raw, "location")
    change = _num(raw.get("change_mm"))
    return {
        "id": str(raw.get("id") or ""),
        "type": kind,
        "type_text": type_words(kind),
        "location": {"faces": [str(f) for f in (location.get("faces") or ())][:200],
                     "bodies": [str(b) for b in (location.get("bodies") or ())][:50],
                     "points": [[float(c) for c in p][:3] for p in (location.get("points") or ())
                                if isinstance(p, (list, tuple)) and len(p) >= 3
                                and all(_num(c) is not None for c in p[:3])][:50]},
        "status": status,
        "reason_text": plain(raw.get("reason_text"), STATUS_WORDS[status]),
        "change_mm": change,
        "over_allowed": change is not None and allowed_mm is not None and change > allowed_mm,
    }


def _flag(value: Any) -> str | None:
    """A flag the customer reads: the engine's flag key in plain words (`FLAG_WORDS`), a flag that
    already is plain words, or None (never shown by its key)."""
    text = " ".join(str(value or "").split())
    text = FLAG_WORDS.get(text, text)
    return text if text and not leaks(text) else None


def _counts(problems: list[dict]) -> dict:
    out = {"found": len(problems), "fixed": 0, "not_fixed": 0, "left_as_is": 0}
    for p in problems:
        out[p["status"]] = out.get(p["status"], 0) + 1
    return out


def _v3_section(attempt: Mapping) -> dict:
    """What today's (pre-v5) repair can say in the section's shape: no per-problem list - its
    defects are not the classes the spec names - but the outcome, the counts and the time."""
    ops = [o for o in attempt.get("operations") or () if isinstance(o, Mapping)]
    kept = sum(1 for o in ops if o.get("status") == "kept")
    remaining = len([d for d in attempt.get("defects_remaining") or () if isinstance(d, Mapping)])
    if not attempt.get("attempted"):
        outcome = "unrepairable" if attempt.get("outcome") == "refused" else "unchanged"
    elif attempt.get("resolved"):
        outcome = "repaired"
    else:
        outcome = "partly_repaired" if kept else "unchanged"
    # COUNTS ONLY FROM A REPAIR THAT RAN: a declined one records no defect list of its own (its
    # findings went to review items), and "0 found" beside "30 need an engineer's look" is false
    counts = ({"found": kept + remaining, "fixed": kept, "not_fixed": remaining, "left_as_is": 0}
              if attempt.get("attempted") else {})
    return {"engine": "v3", "outcome": outcome, "problems": [], "counts": counts,
            "meshers_passing": None, "meshers_total": None, "max_change_mm": None,
            "allowed_change_mm": None, "volume_ratio": None, "size_ratio": None, "flags": [],
            "seconds": None, "spend_usd": None}


def section(state: Mapping, *, choice: str) -> dict:
    """The job API's `repair` section for a finished run: the engine's result fields (spec
    "Contract") plus `choice`, `auto_accepted`, `needs_review` - plain words throughout.

    NEVER RAISES: a run whose repair recorded nothing readable gets the choice and `ran: False`."""
    try:
        return _section(state, choice=choice)
    except Exception:  # noqa: BLE001 - the section is a view; a broken one is a smaller one
        logger.warning("repair result: the section could not be built", exc_info=True)
        return {"choice": choice, "ran": False, "needs_review": False, "auto_accepted": False}


def _section(state: Mapping, *, choice: str) -> dict:
    from meshpipeline.pipeline.repair_goal import bypass_review
    # THE RUN'S OWN BYPASS, whatever the repair came to: a repair that declined records no review
    # decision, and the job must still say it was dispatched with the bypass
    base: dict[str, Any] = {"choice": choice, "ran": False, "needs_review": False,
                            "auto_accepted": False, "review_bypass": bypass_review(state)}
    if choice == "off":
        return base
    attempt = _sub(state, "repair_attempt")
    if not attempt:
        return base
    record = _sub(attempt, "result")
    # the review decision's copy, else the engine's own on the attempt record (PR 1 writes it there
    # on every exit, a refused repair included - `repair_attempt.repair_result_v5`)
    result: Mapping[str, Any] | None = _sub(record, "engine_result") or None
    for key in RESULT_KEYS:
        if result:
            break
        result = _sub(attempt, key) or None
    if not record and result:
        record = {"needs_review": needs_review(result),
                  "auto_accepted": bool(result.get("auto_accepted"))}
    out: dict[str, Any] = {**base, "ran": True,
                           "needs_review": bool(record.get("needs_review")),
                           "auto_accepted": bool(record.get("auto_accepted")),
                           "review_bypass": bool(record.get("review_bypass")
                                                 or base["review_bypass"]),
                           "waiting_for_review": bool(record.get("waiting_for_review")),
                           # whether the change was applied, and whether a person approved it
                           # (a review re-run) - a change that needed review is NOT "approved"
                           # merely because it was reviewed for
                           "applied": bool(record.get("applied", True)),
                           "accepted_by_person": bool(record.get("accepted_by_person"))}
    if not result:
        return {**out, **_v3_section(attempt)}
    allowed = _num(result.get("allowed_change_mm"))
    problems = [_problem(p, allowed) for p in result.get("problems") or ()
                if isinstance(p, Mapping)]
    outcome = str(result.get("outcome") or "")
    engine = str(result.get("engine") or "v5")
    out.update({
        "engine": engine if engine in ("v5", "v3") else "v5",
        "outcome": outcome if outcome in OUTCOMES else ("repaired" if problems and all(
            p["status"] == "fixed" for p in problems) else "unchanged"),
        "problems": problems,
        # COUNTS ONLY WHERE THE ENGINE LISTED THE PROBLEMS: v5 lists every one it found (an empty
        # list from v5 is a clean part); its v3 fallback lists none, and "0 found" from it would
        # say "nothing needed fixing" about a part v3 refused (found on the personal slug)
        "counts": _counts(problems) if problems or engine == "v5" else {},
        "meshers_passing": _count(result.get("meshers_passing")),
        "meshers_total": _count(result.get("meshers_total")),
        "max_change_mm": _num(result.get("max_change_mm")),
        "allowed_change_mm": allowed,
        "volume_ratio": _num(result.get("volume_ratio")),
        "size_ratio": _num(result.get("size_ratio")),
        "flags": [f for f in (_flag(x) for x in result.get("flags") or ()) if f],
        "seconds": _num(result.get("seconds")),
        "spend_usd": _num(result.get("spend_usd")),
    })
    return out


def summary_text(sec: Mapping) -> str:
    """The section's one line, as the lane's last line says it."""
    from meshpipeline.pipeline.repair_progress import done_line
    counts = _sub(sec, "counts")
    if not sec.get("ran"):
        return "Repair was not run" if sec.get("choice") == "off" else "Repair finished"
    return done_line(_count(counts.get("fixed")), _count(counts.get("found")))


__all__ = ["ACCEPT_OP", "REVIEW_ORIGIN", "accepted_by_person", "extract", "needs_review",
           "over_threshold", "review_items", "section", "summary_text", "without_acceptances"]
