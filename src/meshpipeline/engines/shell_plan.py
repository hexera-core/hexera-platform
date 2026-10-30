# Responsibility: The shell mesh's sizing rules (SHMESH-1..7, docs/design/shell/README.md section 3): for every
# sewn mid-face, the element size its sheet's size, its width, its wall and its curves ask for - each number
# naming the rule it comes from.
# Boundaries: pure over plain numbers (the shell record's sheets and walls, the face's measured width and
# curve radii); no engine, no file. The mesher (engines/gmsh/shell_adapter.py) measures the faces and applies
# the sizes.
# Collaborates with: cad/midsurface.py (the record), engines/gmsh/shell_adapter.py (applies the plan),
# engines/shell_quality.py (judges the result).
from __future__ import annotations

import math
from collections.abc import Mapping

#: SHMESH-1: the base size of a sheet is at most its size L over BASE_DIVISIONS, and fits at least MIN_ACROSS
#: elements across every mid-face's narrowest width.
BASE_DIVISIONS = 10
MIN_ACROSS = 4
#: SHMESH-15 (modal accuracy): a face at least PANEL_WALLS walls wide is a panel, which bends across its own width in
#: the part's first modes, so it gets at least PANEL_ACROSS elements across. Measured on 14 corpus parts (free-free
#: modes 7-12, CalculiX S4 shells against C3D10 solids of the CAD): 4 across left 5 parts 10-23% too stiff; 20
#: across on panels (still 4 on narrower faces - bends, flanges, strips, which do not bend across in those modes)
#: put every part within 4.1%, for 1.3 times the elements. 16 across everywhere did as well for 2.3 times them.
PANEL_WALLS = 20.0
PANEL_ACROSS = 20
#: ... and at least 1/PANEL_SPAN of the size of its body's largest sheet: a corrugated roof sheet's 23 mm flanks
#: are 48 walls wide, but the 2 m sheet bends as a whole in its first modes, long before a flank bends across
PANEL_SPAN = 10.0
#: SHMESH-2: no element smaller than FLOOR_WALLS times the wall - a shell finer than about half its wall is the
#: wrong element there. A rule asking for less is held at the floor, and said so.
FLOOR_WALLS = 0.5
#: SHMESH-3 / SHMESH-4: CURVE_PER_QUARTER elements per quarter-turn of every hole edge and every curved face (24
#: round a full circle, 15 degrees each), as PLAN-fea-4.
CURVE_PER_QUARTER = 6
#: SHMESH-6: element sizes grow by at most GROWTH from one element to the next.
GROWTH = 1.3


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def base_size(*, wall: float, size: float, width: float, span: float | None = None) -> tuple[float, str]:
    """SHMESH-1, SHMESH-15 and SHMESH-2: a sheet face's base element size (mm) and the rule that set it. `span`
    (mm) is the size of the largest sheet of the face's body (its own sheet's size when not given)."""
    want = min(size / BASE_DIVISIONS, width / MIN_ACROSS)
    rule = "SHMESH-1"
    span = size if span is None else max(span, size)
    if width >= max(PANEL_WALLS * wall, span / PANEL_SPAN) and width / PANEL_ACROSS < want:
        want, rule = width / PANEL_ACROSS, "SHMESH-15"
    if want < FLOOR_WALLS * wall:
        want, rule = FLOOR_WALLS * wall, "SHMESH-2"
    return want, rule


def curve_size(radius: float, *, wall: float) -> tuple[float, str]:
    """SHMESH-3/4: the element size round a curve of `radius` (mm), never under the floor."""
    want = radius * (math.pi / 2.0) / CURVE_PER_QUARTER
    if want < FLOOR_WALLS * wall:
        return FLOOR_WALLS * wall, "SHMESH-2"
    return want, "SHMESH-3"


def face_plan(record: Mapping, mid_id: str, *, width: float, radii: list[float], bend: float | None = None) -> dict:
    """The plan for one sewn face of mid-face `mid_id`: {"mid", "wall_mm", "size_mm", "rule", "curve_mm",
    "curve_rule", "floored"}. `width` (mm) is the face's own narrowest width, `radii` (mm) its edges' radii (hole
    edges: refined near them, then grown), `bend` (mm) the face's own radius of curvature when it is curved
    (SHMESH-4 holds over the whole face)."""
    mid = next(m for m in record["mids"] if m["id"] == mid_id)
    pair = next(p for p in record["pairs"] if p["id"] == mid["pair"])
    wall = float(pair["thickness_mm"])
    if mid.get("thickness_range_mm"):
        # SHELL-9b: a wall that varies is sized by its thinnest point (where it bends first; a wedge sized by its
        # thickest got 4 elements across where 20 were due, and its modes came out 14% stiff)
        wall = min(wall, float(mid["thickness_range_mm"][0]))
    size = float(pair.get("size_mm") or width)
    pairs = {p["id"]: p for p in record["pairs"]}
    span = max([float(pairs[m["pair"]].get("size_mm") or 0.0) for m in record["mids"]
                if m.get("body") == mid.get("body") and m["pair"] in pairs] + [size])
    h, rule = base_size(wall=wall, size=size, width=width, span=span)
    if bend is not None and bend > 0:
        hb, brule = curve_size(bend, wall=wall)
        if hb < h:
            h, rule = hb, ("SHMESH-4" if brule == "SHMESH-3" else brule)
    out = {"mid": mid_id, "wall_mm": _r(wall), "size_mm": _r(h), "rule": rule, "floored": rule == "SHMESH-2"}
    if radii:
        c, crule = curve_size(min(radii), wall=wall)
        if c < h:
            out.update(curve_mm=_r(c), curve_rule=crule, floored=out["floored"] or crule == "SHMESH-2")
    return out


__all__ = ["BASE_DIVISIONS", "CURVE_PER_QUARTER", "FLOOR_WALLS", "GROWTH", "MIN_ACROSS", "PANEL_ACROSS", "PANEL_WALLS",
           "base_size", "curve_size", "face_plan"]
