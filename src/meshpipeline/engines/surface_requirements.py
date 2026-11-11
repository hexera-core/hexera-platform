# Responsibility: THE NOTES' MESH REQUIREMENTS ON A SURFACE FILE (SURF-7): where the surface run (engines/surface_run,
# SURF-3..6) and the requirements read from the user's notes (engines/mesh_requirements, NOTE-7..9) meet. Two
# things, none of which either side decides alone: (1) the ONE thickness a surface file is given, from the two
# places a thickness can be read (the fixed word table, contracts/shell_thickness, and the "thickness" entry of the
# notes' typed set); (2) the asked-against-delivered rows a surface file's shell tells differently from a solid
# part's (its thickness is the user's, not measured from CAD).
# Boundaries: pure over plain data; no engine, no file, no model. With no requirement every function hands back what
# it was given: a surface file with nothing said in the notes is meshed exactly as before.
# Collaborates with: engines/surface_run.py (calls all three), contracts/shell_thickness.py (the word table's
# reading), engines/mesh_requirements.py (the typed set and the report's shape), engines/shell_requirements.py (the
# report the shell mesher measured).
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from meshpipeline.contracts import shell_thickness as st

RULE = "SURF-7"
#: where the run's thickness came from
BY_WORDS = "words"            # the word table (the notes, the stage, the request): what the stage showed the user
BY_SET = "notes reading"      # the notes' typed set (the model's reading, checked), when the word table read none
#: the two readings are the same thickness within this share
SAME = 1e-6


def _set_thickness(req: Mapping | None) -> tuple[float | None, str]:
    """(the thickness the notes' typed set carries, in mm, when it is one a shell can have; the user's words it was
    read from)."""
    try:
        mm = float(((req or {}).get("sizes") or {}).get("thickness_mm") or 0.0)
    except (TypeError, ValueError):
        return None, ""
    if not (st.MIN_MM <= mm <= st.MAX_MM):
        return None, ""
    quote = next((str(r.get("quote") or "") for r in (req or {}).get("rows") or []
                  if isinstance(r, Mapping) and r.get("field") == "thickness"), "")
    return float(f"{mm:.6g}"), " ".join(quote.split())


def thickness(words: st.ThicknessReading, req: Mapping | None) -> tuple[st.ThicknessReading, dict]:
    """SURF-7: THE ONE THICKNESS of a surface file's run, and where it came from.

    The word table wins whenever it reads one (`words`: engines/surface_run.thickness_for - the notes, else the
    stage, else the request): it is the thickness the stage showed the user before they pressed Proceed, and it is
    read by fixed rules, so the same words give the same thickness every run, model or no model. The notes' typed
    set is used only when the word table read nothing AND flagged nothing (a bare gauge number or a range it
    refused stays refused, SURF-4). When both read one and they differ, the word table's stands and the other is
    said beside it (as a second thickness the notes name).

    Returns (the reading the run uses, {"by": BY_WORDS | BY_SET | None, "set_mm": the set's own or None,
    "differs": bool})."""
    set_mm, quote = _set_thickness(req)
    how: dict = {"by": None, "set_mm": set_mm, "differs": False}
    if words.given:
        how["by"] = BY_WORDS
        if set_mm is not None and abs(set_mm - float(words.mm)) > SAME * max(set_mm, float(words.mm)):
            how["differs"] = True
            if not any(abs(set_mm - o) <= SAME * max(set_mm, o) for o in words.others_mm):
                words = replace(words, others_mm=(*words.others_mm, set_mm))
        return words, how
    if set_mm is not None and not words.unread:
        how["by"] = BY_SET
        return st.ThicknessReading(mm=set_mm, quote=quote or f"{st.words(set_mm)} thick"), how
    return words, how


def global_size(req: Mapping | None) -> float | None:
    """The global element size the notes ask for (mm), None when they give none."""
    try:
        g = float(((req or {}).get("sizes") or {}).get("global_mm") or 0.0)
    except (TypeError, ValueError):
        return None
    return g if g > 0 else None


#: the clause engines/shell_requirements adds to the global-size row when elements are under half the wall
_HALF_WALL = "; some elements are smaller than half the wall, finer than a shell usually needs"


def told(report: Mapping | None, *, elements: int, thickness_mm: float | None, how: Mapping | None = None,
         unread: str = "") -> dict | None:
    """SURF-7: the shell mesher's asked-against-delivered report (of a mesh of `elements` elements), said as a
    SURFACE file's. A new report; None for none.

    * The "thickness" row: on a solid part it reads "not applied - each wall's thickness is measured from your
      CAD"; a surface has no thickness of its own, so here it is the one thing that IS applied exactly - met when
      every property carries what was asked, and otherwise said with the thickness that is in the files and why.
    * With no thickness given there is no wall: the global-size row never says "smaller than half the wall"."""
    from meshpipeline.engines import mesh_requirements as MR

    if not isinstance(report, Mapping) or not report.get("rows"):
        return None
    used = float(thickness_mm) if thickness_mm else None
    rows = []
    for r in report["rows"]:
        row = {k: v for k, v in r.items() if k != "excess"}
        if row.get("field") == "thickness":
            asked = (how or {}).get("set_mm")
            if used is not None and asked is not None and abs(used - asked) <= SAME * max(used, asked):
                row.update(applied=True, met=True, measured=used,
                           delivered=f"every shell property carries {st.words(used)}")
            elif used is not None:
                row.update(applied=False, met=None, measured=used,
                           delivered=f"not applied - the files carry {st.words(used)}, the thickness read from your "
                                     "words before the run")
            else:
                why = f"{unread} was not read as a thickness" if unread else "it was not read as a thickness"
                row.update(applied=False, met=None, delivered=f"not applied - {why}: say it in mm")
        elif row.get("field") == "global" and used is None and isinstance(row.get("delivered"), str):
            row["delivered"] = row["delivered"].replace(_HALF_WALL, "")
        rows.append(row)
    out = MR.finish_report(rows, int(elements), str(report.get("definitions") or ""))
    return {**{k: v for k, v in report.items() if k not in out}, **out}


def words(how: Mapping | None, req: Mapping | None, *, given: bool) -> str:
    """One plain sentence for the why record: what of the notes reached this surface file."""
    said = []
    g = global_size(req)
    if g is not None:
        said.append(f"the sizes are the {g:.4g} mm your notes ask for")
    if req and req.get("limits"):
        n = len(req["limits"])
        said.append(f"the {n} quality limit{'' if n == 1 else 's'} in your notes are aimed at")
    by = (how or {}).get("by")
    if by == BY_SET:
        said.append("the thickness is the one read from your notes")
    elif by == BY_WORDS and (how or {}).get("differs"):
        said.append(f"your notes were also read as {st.words((how or {})['set_mm'])} thick; the thickness said before "
                    "the run is the one used")
    if not said:
        return ""
    text = "; ".join(said) + "."
    return text[:1].upper() + text[1:]


__all__ = ["BY_SET", "BY_WORDS", "RULE", "global_size", "thickness", "told", "words"]
