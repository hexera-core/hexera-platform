# Responsibility: Say on the run's live stream what the repair check found and did, in one plain line.
# Owns: the "Repair check" lane's sentence, read from what node_repair_inspect and node_repair_attempt
#       recorded, and the two publications that open the lane and say it.
# Boundaries: words only - it decides nothing, counts only what the nodes recorded, and never fails a
#             run: the user stream is never load-bearing.
# Collaborates with: pipeline/repair_inspect.py (opens the lane), pipeline/repair_attempt.py (says the
#                    line), ui/js/render/stage.js (draws it on the lane's row).
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

#: The lane's name on the stream (events.STAGES) and in the console (core/events.js LANE_LABELS).
AGENT = "repair"

#: The tail every "we did not change your file" line ends on, so the user knows what is meshed.
_ORIGINAL = "meshing your original file"

#: WHAT TO SAY FOR EACH WAY A RUN ENDED WITH NO FILE (`cad/repair/ending.py`). The lane used to
#: say "Nothing could be repaired safely" for all five, which is false for four of them and the
#: reason the owner saw a corpus report of 66 refusals where one part had been refused.
#:
#: NONE OF THESE IS GOOD NEWS and none may read as one. The caller appends "meshing your original
#: file" to every line, so each says plainly that the customer is getting what they uploaded. A
#: gate refusal (`refused`) is the one that names a decision we made, and it keeps the harshest
#: wording on purpose - it is a safety signal, not a status.
#:
#: `escalated` IS DELIBERATELY ABSENT: a run that queued a region still kept the rest and reaches
#: the `attempted` branch above, which already says what was repaired and what needs a look.
_ENDING_WORDS = {
    "nothing_to_repair": "The repair check found nothing that needed fixing",
    "no_operation_claims_it": "We have no automatic fix for what is in your part",
    "attempted_none_held": "Nothing could be repaired safely",
    "scan_incomplete": "The repair check could not see all of your part, so nothing was changed",
    "unmeshable": "Your part's geometry checks out, but it still cannot be meshed",
    "refused": "The repair was stopped by our safety checks",
}

#: What a kept operation did, by the operation and, where one operation answers several defects,
#: by the defect it aimed at. (singular, plural) after the count. An operation this table does not
#: know is still counted, as a fixed defect - never named by its internal label.
_DID: dict[tuple[str, str], tuple[str, str]] = {
    ("fix_face_boundary", "wire_gap"): ("closed {n} gap", "closed {n} gaps"),
    ("fix_face_boundary", "curve_inconsistency"): ("re-fitted {n} edge curve", "re-fitted {n} edge curves"),
    ("fix_small_edges", "degenerate_edge"): ("removed {n} collapsed edge", "removed {n} collapsed edges"),
    ("fix_small_edges", ""): ("removed {n} tiny edge", "removed {n} tiny edges"),
    ("fix_small_faces", ""): ("removed {n} sliver face", "removed {n} sliver faces"),
    ("sew_faces", ""): ("sewed {n} open seam", "sewed {n} open seams"),
    ("fill_planar_hole", ""): ("filled {n} hole", "filled {n} holes"),
    ("fill_nonplanar_hole", ""): ("filled {n} hole", "filled {n} holes"),
    ("heal_self_intersections", ""): ("fixed {n} overlap", "fixed {n} overlaps"),
    ("fix_shape_global", "bad_orientation"): ("flipped {n} face", "flipped {n} faces"),
    ("fix_shape_global", "tolerance_outlier"): ("tightened {n} loose tolerance",
                                                "tightened {n} loose tolerances"),
    ("fix_shape_global", "duplicate_surface_data"): ("removed {n} duplicate surface",
                                                     "removed {n} duplicate surfaces"),
    ("", "wire_gap"): ("closed {n} gap", "closed {n} gaps"),
    ("", "bad_orientation"): ("flipped {n} face", "flipped {n} faces"),
}
_FIXED = ("fixed {n} defect", "fixed {n} defects")


def _plural(n: int, one: str, many: str) -> str:
    return (one if n == 1 else many).format(n=n)


def _defects(n: int) -> str:
    return f"{n} defect" + ("" if n == 1 else "s")


def _inner(report: Mapping) -> Mapping:
    nested = report.get("report")
    return nested if isinstance(nested, Mapping) else report


def _counted(report: Mapping | None, keep) -> dict[str, int]:
    """The inspection's findings whose severity `keep` accepts, by defect code: its located
    entities, else its defect counts."""
    inner = _inner(dict(report or {}))
    out: dict[str, int] = {}
    entities = [e for e in (inner.get("entities") or ()) if isinstance(e, Mapping)]
    if entities:
        for e in entities:
            if keep(str(e.get("severity", ""))):
                code = str(e.get("code") or "")
                out[code] = out.get(code, 0) + 1
        return out
    for d in inner.get("defects") or ():
        if isinstance(d, Mapping) and keep(str(d.get("severity", ""))):
            try:
                n = max(int(d.get("count", 1) or 1), 1)
            except (TypeError, ValueError):
                n = 1
            code = str(d.get("code") or "")
            out[code] = out.get(code, 0) + n
    return out


def found_count(report: Mapping | None) -> int:
    """How many defects the inspection recorded: its located entities, else its defect counts.
    `info` remarks are not defects."""
    return sum(_counted(report, lambda s: s != "info").values())


def _small_details(report: Mapping | None) -> dict[str, int]:
    """The findings that are only WARNINGS - a tiny edge, a sliver face: details that may force small
    cells, not a part that is broken."""
    return _counted(report, lambda s: s == "warning")


#: What a warning-only finding is, in the words a user knows it by (singular, plural).
_DETAIL_WORDS = {"small_edge": ("tiny edge", "tiny edges"), "small_face": ("sliver face", "sliver faces")}


def _details_words(details: dict[str, int]) -> str:
    """'36 tiny edges and 6 sliver faces'."""
    parts = []
    other = 0
    for code, n in details.items():
        if code in _DETAIL_WORDS:
            parts.append(_plural(n, "{n} " + _DETAIL_WORDS[code][0], "{n} " + _DETAIL_WORDS[code][1]))
        else:
            other += n
    if other:
        parts.append(_plural(other, "{n} other small detail", "{n} other small details"))
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def clean_with_details(state: Mapping, report: Mapping) -> str:
    """ONE CLEAN VERDICT, THE STAGE'S. The inspection found only small CAD details (warnings, never
    errors): the line says the repair check found nothing to fix and names the details - never
    "Found 42 defects" (the stud punch of the 2 Nov dry run), and never a "clean" of its own: the
    Confirmed card carries the part's clean verdict, and a second one here contradicted it (16 Nov:
    "Not clean - we'll still try" on the card, "Your part is clean" in the run box). '' when the
    inspection found more than details."""
    if str(state.get("repair_status") or "") != "clean":
        return ""
    total = found_count(report)
    details = _small_details(report)
    if not total or sum(details.values()) != total:
        return ""
    return (f"The repair check found nothing to fix; the part has {_details_words(details)}, "
            f"which may need small cells there - {_ORIGINAL}")


def _judgement_count(report: Mapping) -> int:
    """How many located defects triage leaves to a person (cad/repair/triage.split)."""
    try:
        from meshpipeline.cad.repair.triage import split
        return len(split(dict(report)).judgement)
    except Exception:  # noqa: BLE001 - words never fail a run
        return 0


def _did_words(operations) -> tuple[int, list[str]]:
    """(defects repaired, what was done) from the kept operations, in the order first done."""
    counts: dict[tuple[str, str], int] = {}
    for op in operations or ():
        if not isinstance(op, Mapping) or op.get("status") != "kept":
            continue
        name, code = str(op.get("name") or op.get("op") or ""), str(op.get("defect_code") or "")
        key = next((k for k in ((name, code), (name, ""), ("", code)) if k in _DID), ("", ""))
        n = len(op.get("targets") or ()) or 1
        counts[key] = counts.get(key, 0) + n
    total = sum(counts.values())
    parts = [_plural(n, *_DID.get(k, _FIXED)) for k, n in counts.items()]
    return total, parts


def _review_count(out: Mapping) -> int:
    items = len(out.get("repair_review_candidates") or ())
    try:
        dropped = max(int(out.get("repair_review_truncated") or 0), 0)
    except (TypeError, ValueError):
        dropped = 0
    return items + dropped


def _needs_look(n: int) -> str:
    return f"{_defects(n)} {'needs' if n == 1 else 'need'} an engineer's look"


def repair_check_words(state: Mapping, out: Mapping) -> str | None:
    """The Repair check lane's one line, from the inspection in `state` and the attempt's `out`.

    None when there was nothing to inspect: the lane was never opened, and a line about a check
    that did not happen would be invented.
    """
    state = state or {}
    out = out or {}
    report = dict(state.get("repair_report") or {})
    record = dict(out.get("repair_attempt") or {})
    if not report or record.get("outcome") in ("no_report", "no_geometry"):
        return None

    status = str(state.get("repair_status") or "")
    found = found_count(report)
    review = _review_count(out)
    suffix = ""
    try:
        from meshpipeline.pipeline.geometry_state import geometry_path
        suffix = Path(geometry_path(state) or "").suffix.lower()
    except Exception:  # noqa: BLE001
        suffix = ""

    def _with_review(line: str) -> str:
        return f"{line}. {_needs_look(review)}" if review else line

    if record.get("attempted"):
        total, parts = _did_words(record.get("operations"))
        if not total:
            return _with_review("Repaired your part")
        line = f"Repaired {_defects(total)}: {', '.join(parts)}"
        if review:
            line += f"; {review} more {'needs' if review == 1 else 'need'} an engineer's look"
        return line

    outcome = str(record.get("outcome") or "")
    route = str(record.get("route") or "")
    failure = str(report.get("service_failure") or "")

    if failure == "not_inspected_too_large":
        return f"Too large to check for repair; {_ORIGINAL}"
    if failure or status == "inconclusive":
        return f"The repair check could not finish; {_ORIGINAL}"
    if ((status == "clean" and not found) or route == "mesh_as_is") and not review:
        return "The repair check found nothing to fix"      # what the check found; the clean verdict is the stage's
    details_only = clean_with_details(state, report)
    if details_only and not review:
        return details_only
    if suffix in (".igs", ".iges") and outcome in ("format", "off", "not_autonomous"):
        return _with_review(f"IGES files are not repaired yet; {_ORIGINAL}")
    if outcome == "format":
        kind = suffix.lstrip(".").upper() or "These"
        return _with_review(f"{kind} files are not repaired yet; {_ORIGINAL}")
    if outcome in ("off", "not_autonomous"):
        return _with_review(f"Found {_defects(found)}; repair is off here, so {_ORIGINAL}"
                            if found else f"Repair is off here; {_ORIGINAL}")
    if outcome == "triage":
        if route == "manual_cleanup":
            n = review or _judgement_count(report) or found
            return f"{_needs_look(n)}; {_ORIGINAL}" if n else f"Your part needs an engineer's look; {_ORIGINAL}"
        if route == "ask_customer":
            return _with_review(f"Your file could not be read for repair; {_ORIGINAL}")
        return _with_review(f"We found something we do not repair yet; {_ORIGINAL}")
    if outcome == "refused":
        measured = (record.get("detail") or {}).get("measurements") or {}
        if measured.get("result_discarded"):
            return _with_review(f"Repair undone: it changed the shape too much; {_ORIGINAL}")
        # FIVE DIFFERENT ANSWERS USED TO SHARE ONE SENTENCE. `cad/repair/ending.py` says which
        # this was; the line follows it. Every branch still ends in "meshing your original file",
        # so none of them can read as a delivery, and the fallback is the sentence this surface
        # said before - a run whose ending we do not know is told the harshest true thing.
        said = _ENDING_WORDS.get(str(measured.get("ending") or ""))
        if said:
            return _with_review(f"{said}; {_ORIGINAL}")
        return _with_review(f"Nothing could be repaired safely; {_ORIGINAL}")
    if outcome == "needs_review":
        # CRS v5 beta "Bypass permission": a fix past the allowance, no bypass, no review here
        return _with_review("Some fixes would change your part more than is applied without "
                            f"your approval; {_ORIGINAL}")
    if outcome == "not_promoted":
        return _with_review(f"The repaired file failed our checks; {_ORIGINAL}")
    if outcome == "not_proved":
        # NOT "failed our checks": that is a verdict, and a proof stopped by our own clock reached
        # none. The repair may have been perfectly sound - it was never finished being checked.
        return _with_review(f"The repaired file was not checked in time; {_ORIGINAL}")
    if outcome in ("timeout", "unavailable", "failed"):
        return _with_review(f"The repair could not finish; {_ORIGINAL}")
    if review:
        return f"{_needs_look(review)}; {_ORIGINAL}"
    return f"Your part was checked; {_ORIGINAL}"


def _tone(line: str) -> str:
    return "warn" if "engineer's look" in line else "info"


async def aopen(job_id: str) -> None:
    """The Repair check lane opens: the inspection is starting."""
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        _pub = execution_publisher(job_id, agent=AGENT)
        await _pub.astage(op_id="repair:lane")
    except StaleExecutionPublish:
        # a lost claim is not a stream hiccup: a superseded generation must stop here
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


async def asay(job_id: str, line: str | None) -> None:
    """The lane's one line. Nothing when there is nothing true to say."""
    if not line:
        return
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        _pub = execution_publisher(job_id, agent=AGENT)
        await _pub.anote(line, _tone(line), op_id="repair:said")
    except StaleExecutionPublish:
        raise
    except Exception:  # noqa: BLE001 - the user stream is never load-bearing
        pass


__all__ = ["AGENT", "aopen", "asay", "found_count", "repair_check_words"]
