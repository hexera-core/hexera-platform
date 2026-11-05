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
    """THE SAME WORD AS THE STAGE. The inspection called the part clean and found only small CAD
    details (warnings, never errors): the line says the part is clean - as the geometry check told
    the user - and names the details, never "Found 42 defects" (the stud punch of the 2 Nov dry run:
    "The geometry is clean" on the stage, "Found 42 defects; repair is off here" on the run, for 36
    tiny edges and 6 sliver faces). '' when the inspection found more than details."""
    if str(state.get("repair_status") or "") != "clean":
        return ""
    total = found_count(report)
    details = _small_details(report)
    if not total or sum(details.values()) != total:
        return ""
    return (f"Your part is clean and needed no repair; it has {_details_words(details)}, "
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
        return "Your part needed no repair"
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
        return _with_review(f"Nothing could be repaired safely; {_ORIGINAL}")
    if outcome == "not_promoted":
        return _with_review(f"The repaired file failed our checks; {_ORIGINAL}")
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
