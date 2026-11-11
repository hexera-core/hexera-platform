# Responsibility: THE FEA STAGE FOR A FILE THAT IS A SURFACE, NOT A SOLID (SURF-1, SURF-4, SURF-6). Reads the
# surface reading the FEA measurement kept (cad/surface_model, measured once with the part), says whether the file is
# a surface part, reads the wall thickness from the user's own words, and gives the stage its one line: "This file
# is a surface, not a solid. It is meshed as a shell, 1.2 mm thick." - in the stage's existing shell fold, with no
# control of its own. Settles the thickness at Proceed so the run uses what the stage said.
# Boundaries: pure over the stored check's facts and the conversation, but for the one read of the stored
# confirmation. It measures nothing itself (measure() is the geometry check's call into cad/surface_model), never
# asks a question and never refuses: a surface with no thickness given proceeds, and its line says how to set one.
# A file with a solid, or whose faces close, has no reading here and is untouched.
# Collaborates with: cad/surface_model.py (the reading), contracts/shell_thickness.py (the words), application/
# fea_check.py (the stage's blocker and clean line), application/shell_check.py (the fold's shape, settle),
# api/v1/geometry.py (serves and settles it), application/pipeline_run.py (reads the settled thickness into the
# run), engines/surface_run.py (the run and the line's words).
from __future__ import annotations

import json
import logging

from meshpipeline.application import purpose_map as pm
from meshpipeline.contracts import shell_thickness as st

logger = logging.getLogger(__name__)

#: where the reading is kept: inside the FEA measurement's record (so a reused measurement carries it)
KEY = "surface_model"
SURFACE, GAP = "surface", "gap"
#: the stage's check line for a surface (the clean check judges solids: an open edge is a surface's own)
CLEAN_SHORT = "A surface, not a solid"
UNMET_SHORT = "A surface · {n} edge{s} do not meet"
UNMET_WORDS = ("{n} edge{s} lie{v} along a neighbouring face without meeting it (within {tol} mm): the shell mesh has "
               "a gap there.")
#: the clean check's reasons that only restate "this is a surface"
SURFACE_REASONS = frozenset({"open_shell", "open_boundary", "flat", "zero_volume"})
#: a file whose faces nearly close: said, with the two ways on
GAP_WORDS = ("This file has no solid, and its faces nearly close into one but leave small gaps, so it is read "
             "neither as a solid nor as a sheet.")
GAP_WAY_OUT = ("To go on, close the gaps in your CAD and upload it again. If it is a surface you want meshed as a "
               'shell, say so (for example "shell, 1.2 mm thick").')


def summarise(reading: dict | None) -> dict | None:
    """What the stage keeps of cad/surface_model's reading (small: no loops, no places); None for a file with a
    solid or no faces."""
    r = reading or {}
    if r.get("kind") not in (SURFACE, GAP, "closed"):
        return None
    sheets = list(r.get("sheets") or [])
    return {"kind": r["kind"], "why": str(r.get("why") or ""), "faces": r.get("faces"), "sheets": len(sheets),
            "open_share": r.get("open_share"), "sew_tolerance_mm": r.get("sew_tolerance_mm"),
            "free_edges": sum(int(s.get("free_edges") or 0) for s in sheets),
            "unmet_edges": sum(int(s.get("unmet_edges") or 0) for s in sheets),
            "size_mm": max([float(s.get("size_mm") or 0.0) for s in sheets] or [0.0])}


def measure(shape, *, mm_per_unit: float) -> dict | None:
    """The geometry check's call (with the FEA measurement, for a CAD file with no solid): the surface reading of
    the shape as read, summarised. Never raises: a reading that cannot be made is no reading."""
    try:
        from meshpipeline.cad import surface_model as sm

        return summarise(sm.read(shape, mm_per_unit=mm_per_unit)[0])
    except Exception as exc:  # noqa: BLE001 - no reading: the stage is the one from before surfaces
        logger.warning("surface check: the file could not be read as a surface (%s: %s)", type(exc).__name__,
                       str(exc)[:200])
        return None


def reading_of(facts: dict | None) -> dict | None:
    from meshpipeline.cad.fea_measure import FACT_KEY

    rec = (facts or {}).get(FACT_KEY)
    got = rec.get(KEY) if isinstance(rec, dict) else None
    return got if isinstance(got, dict) else None


def _user_texts(messages) -> list[str]:
    return [str(m.get("content") or "") for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user"]


def thickness_of(messages) -> st.ThicknessReading:
    """SURF-4: the wall thickness the user's own messages give (the latest word stands)."""
    return st.read(_user_texts(messages))


def said_shell(facts: dict | None, messages=None) -> bool:
    """Whether the user's words asked for a shell: shell words (SHSTAGE-1), or a wall thickness."""
    from meshpipeline.application import shell_check

    return shell_check.wants_shell(facts, messages) or thickness_of(messages).given


def is_surface(facts: dict | None, messages=None) -> bool:
    """SURF-1 on the stage: the file is a surface part - by its shape, or (a file whose faces nearly close) because
    the user's words asked for a shell."""
    r = reading_of(facts)
    if r is None:
        return False
    return r.get("kind") == SURFACE or (r.get("kind") == GAP and said_shell(facts, messages))


def is_gap(facts: dict | None, messages=None) -> bool:
    r = reading_of(facts)
    return r is not None and r.get("kind") == GAP and not said_shell(facts, messages)


def view(facts: dict | None, messages=None) -> dict | None:
    """SURF-6: the stage's shell fold for a surface file (the shape application/shell_check.shell_view gives, so the
    stage draws it with what it has): its ONE line, and the thickness read from the user's words. None for any other
    file."""
    if not is_surface(facts, messages):
        return None
    from meshpipeline.engines.surface_run import NO_THICKNESS_YET, line

    r = reading_of(facts) or {}
    got = thickness_of(messages)
    said = line(got.mm)
    return {"question": "Mesh as", "state": "ready", "said": pm.MESH_SHELL, "said_words": "",
            "mesh_as": pm.MESH_SHELL, "label": said, "reason": "" if got.given else NO_THICKNESS_YET,
            "measuring_words": "",
            "surface": True, "by": "surface file", "settle_why": said,
            "thickness_mm": got.mm, "thickness_quote": got.quote, "sheets": r.get("sheets"),
            "bodies": [], "not_shelled": [], "open_edges": 0, "open_at_mm": [], "overlay": False}


def clean_view(facts: dict | None) -> dict | None:
    """What the stage's check row says of a surface: a surface's open edges are its own, so the clean check's
    "not closed" is not said as a fault; anything else it found still is, and so are edges that do not meet."""
    r = reading_of(facts)
    if r is None:
        return None
    v = (facts or {}).get("validity") or {}
    other = [str(x.get("message") or x.get("code")) for x in v.get("reasons") or []
             if isinstance(x, dict) and str(x.get("severity") or "error") == "error"
             and str(x.get("code")) not in SURFACE_REASONS]
    n = int(r.get("unmet_edges") or 0)
    details = list(other)
    short = CLEAN_SHORT
    if n:
        one = n == 1
        short = UNMET_SHORT.format(n=n, s="" if one else "s").replace("edge do not", "edge does not")
        details.insert(0, UNMET_WORDS.format(n=n, s="" if one else "s", v="s" if one else "",
                                             tol=f"{float(r.get('sew_tolerance_mm') or 0.0):.2g}"))
    return {"clean": not details, "clean_short": short, "clean_details": details,
            "clean_summary": " ".join(details), "bodies_sewn": int(r.get("sheets") or 0)}


def confirmed_thickness(session_id: str) -> float | None:
    """The thickness settled on the stage at Proceed (the stored confirmation's fea_shell_thickness_mm), None when
    there is none - no confirmation, no thickness said, a store that cannot be read."""
    if not session_id:
        return None
    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(object_key=check_object_key(session_id, "confirmed.json")))
        t = float(record.get("fea_shell_thickness_mm") or 0.0) if isinstance(record, dict) else 0.0
    except Exception:  # noqa: BLE001 - unknown is "not given", never a refusal
        return None
    return t if st.MIN_MM <= t <= st.MAX_MM else None


__all__ = ["CLEAN_SHORT", "GAP", "GAP_WAY_OUT", "GAP_WORDS", "KEY", "SURFACE", "clean_view", "confirmed_thickness",
           "is_gap", "is_surface", "measure", "reading_of", "said_shell", "summarise", "thickness_of", "view"]
