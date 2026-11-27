# Responsibility: The rebuild after a second look at a delivered mesh (VER-11): each marked spot made finer, told with what the second look found there.
# Owns: the "finer at the spot you marked" asks the rebuild's plan takes, their words, and the per-mark lines of the result.
# Boundaries: pure - it reads the dispute and the second look's findings from the state; the FEA plan sizes the spots (engines/fea_part_sizes, PLAN-fea-36).
# Collaborates with: application/change_request (the marks' faces, carried in the dispute), cad/part_words.with_picks (a picked area), engines/fea_driver (where the asks join the plan), application/pipeline_run (the result's lines).
"""MARKS WITH OR WITHOUT WORDS LEAD TO A TOLD CHANGE AT THE MARKS (VER-18).

The person marks spots on the finished mesh and asks for a second look. The reviewer looks at the marks first, then
the mesh is rebuilt. The rebuild keeps everything the finished run was asked (the route carries its payload) and makes
the mesh finer at each marked spot that is on the part: half the size the last mesh had there, what is already finer
keeping its size, nothing else changed - the same step as "finer here" on a change (VER-9, PLAN-fea-36). What the
second look found there rides with it, so the result says, spot by spot, what was measured and what was changed.

Before (dev, 26 Nov: 8b79fdb2 -> 31e51296): one mark, no words - the second look passed, the rebuild re-planned the
part from the saved settings alone and nothing was said about the spot.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

RULE = "VER-18"
#: what the asks are called in the plan's records (never the user's words: the person typed none)
MARK_QUOTE = "finer at the spot you marked"
#: the lead of every line the result says about the marks
LEAD = "From your marks:"


def _dispute(state: Mapping | None) -> Mapping:
    d = (state or {}).get("user_dispute")
    return d if isinstance(d, Mapping) else {}


def tied_marks(state: Mapping | None) -> list[dict]:
    """The marks of a rebuild after a second look that are on the part: [{ordinal, faces: [{id, label, sig}]}]. []
    for any other run (no dispute, an accept, no mark on the part)."""
    d = _dispute(state)
    if str(d.get("mode") or "rebuild") != "rebuild":
        return []
    out: list[dict] = []
    for m in d.get("marks") or ():
        faces = [dict(f) for f in (m.get("faces") or ()) if isinstance(f, Mapping) and f.get("id")] \
            if isinstance(m, Mapping) else []
        if faces:
            out.append({"ordinal": int(m.get("ordinal") or len(out) + 1), "faces": faces})
    return out


def _finding(state: Mapping | None, ordinal: int) -> Mapping | None:
    for f in (state or {}).get("dispute_flag_findings") or ():
        if isinstance(f, Mapping) and int(f.get("ordinal") or 0) == int(ordinal):
            return f
    return None


def _short(text: str, n: int = 220) -> str:
    from meshpipeline.contracts import engineering_text

    w = " ".join(engineering_text.plain(str(text or "")).split()).rstrip(".")
    return w if len(w) <= n else w[: n - 3].rstrip() + "..."


def finding_words(state: Mapping | None, ordinal: int) -> str:
    """What the second look found at one mark, in a few plain words."""
    f = _finding(state, ordinal)
    if f is None:
        return "the second look gave no finding of its own there"
    status = str(f.get("status") or "")
    measured = _short(str(f.get("measurements") or ""), 120)
    seen = _short(str(f.get("observation") or ""))
    if status == "confirmed":
        return "the second look found a problem there" + (f": {seen}" if seen else "") + (
            f" ({measured})" if measured else "")
    if status == "not_confirmed":
        return "the second look found nothing past a limit there" + (f" ({measured})" if measured else "")
    why = _short(str(f.get("explanation") or ""), 160)
    return "the second look could not judge it there" + (f" ({why})" if why else "")


def with_marks(req: Mapping | None, state: Mapping | None) -> dict | None:
    """The requirements of a rebuild after a second look with one "finer" ask per mark on the part, tied to the mark's
    faces as a picked area is (cad/part_words.with_picks re-checks each face on this run's surface) and against the
    delivered mesh (`change_of`). `req` unchanged (None stays None) for any other run."""
    marks = tied_marks(state)
    if not marks:
        return dict(req) if isinstance(req, Mapping) else req
    out = copy.deepcopy(dict(req)) if isinstance(req, Mapping) else {}
    out.setdefault("local", [])
    of_job = str(_dispute(state).get("of_job_id") or "")
    for m in marks:
        out["local"].append({
            "id": f"mark_{m['ordinal']}", "what": "finer", "quote": MARK_QUOTE, "mm": None,
            "faces": [str(f["id"]) for f in m["faces"]], "labels": [str(f.get("label") or "") for f in m["faces"]],
            "sigs": [f.get("sig") for f in m["faces"]], "rule": RULE, "change_of": of_job,
            "marked": int(m["ordinal"]), "finding": finding_words(state, m["ordinal"])})
    return out


def spot_words(ordinal: int, where: str) -> str:
    """How a result line names a spot: "spot 1, on Face 66 · round Ø12 mm"."""
    return f"spot {int(ordinal)}" + (f", on {where}" if where else "")


def changed_words(row: Mapping, last: str, now: str) -> str:
    """One marked spot made finer (or kept), with what the second look found (engines/fea_part_sizes.finer_words)."""
    from meshpipeline.engines.fea_part_sizes import floor_words

    finding = str(row.get("finding") or "the second look gave no finding of its own there")
    where = str(row.get("where") or "")
    return (f"{LEAD} {spot_words(int(row.get('marked') or 1), where)} - {finding}; the last mesh had about {last} mm "
            f"there, this one has about {now} mm{floor_words(row)}.")


def result_lines(state: Mapping | None, said: list[str]) -> list[str]:
    """The result's line for every mark that no step already spoke of (`said`: the remarks so far): what the second
    look found there and that nothing was made finer there (a mark off the part, a face no longer on the model, a
    mesh that sizes no single face). [] for any other run."""
    d = _dispute(state)
    if str(d.get("mode") or "rebuild") != "rebuild" or not d.get("flags"):
        return []
    tied = {m["ordinal"]: m for m in tied_marks(state)}
    out: list[str] = []
    for n, _flag in enumerate(d.get("flags") or (), 1):
        lead = f"{LEAD} spot {n}"
        if any(str(s).startswith(lead + ",") or str(s).startswith(lead + " ") for s in said):
            continue
        m = tied.get(n)
        where = ", ".join(str(f.get("label") or f.get("id")) for f in (m or {}).get("faces", [])[:2])
        why = "" if m else " (the mark is not on a face of the part)"
        out.append(f"{LEAD} {spot_words(n, where)} - {finding_words(state, n)}; nothing was made finer there{why}.")
    return out


__all__ = ["LEAD", "MARK_QUOTE", "RULE", "changed_words", "finding_words", "result_lines", "spot_words",
           "tied_marks", "with_marks"]
