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


def base_size(*, wall: float, size: float, width: float, span: float | None = None,
              panel_walls: float = PANEL_WALLS) -> tuple[float, str]:
    """SHMESH-1, SHMESH-15 and SHMESH-2: a sheet face's base element size (mm) and the rule that set it. `span`
    (mm) is the size of the largest sheet of the face's body (its own sheet's size when not given); `panel_walls`
    the width, in walls, from which a face is a panel (a mixed part's record lowers it, engines/mixed_run)."""
    want = min(size / BASE_DIVISIONS, width / MIN_ACROSS)
    rule = "SHMESH-1"
    span = size if span is None else max(span, size)
    if width >= max(panel_walls * wall, span / PANEL_SPAN) and width / PANEL_ACROSS < want:
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


#: SHMESH-27 (under the user's own sizes only): an element may be at most FEATURE_RATIO times as long as the smallest
#: piece of the CAD it touches - an edge of the CAD shorter than the size, a face narrower than it. A quad standing
#: on an edge s with its other sides h reads Jacobian 0.60 at s / h = 0.4 and 0.40 at 0.15 (2 x 2 Gauss points: the
#: width runs from s to h across it); a triangle on it reads 23 degrees at 0.4 and 20 at 0.35. So the size beside a
#: feature is held at 2.5 times the feature (1 / 0.4) and grown from there: every element stays inside the usual
#: Jacobian > 0.6, angle > 20 and aspect < 4 lines. A feature at least 1 / FEATURE_RATIO of the size needs nothing.
#: Never under the floor (SHMESH-2): an edge under a fifth of that is a CAD sliver, merged as before (SHMESH-12).
FEATURE_RATIO = 2.5
#: ... and FEATURE_FACE_RATIO times a narrow face's own width on that face: the nodes on a free strip's two sides do
#: not line up, and its quads then lean - 39 / 141 degrees when 2.5 times as long as wide, 45 / 135 at 2 (measured
#: on fr014's step strips at 2.5: 1,680 strip quads at 136 to 139 degrees, 2.2% of the part)
FEATURE_FACE_RATIO = 2.0


def feature_size(own: float, *, size: float, wall: float, ratio: float = FEATURE_RATIO) -> float | None:
    """SHMESH-27: the element size (mm) beside a piece of the CAD `own` mm across (an edge's length, a face's
    narrow width) inside faces sized `size`: `ratio` times it, never under the floor; None when the size already
    fits it."""
    if own <= 0 or ratio * own >= size:
        return None
    h = max(ratio * own, FLOOR_WALLS * wall)
    return _r(h) if h < size else None


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
    h, rule = base_size(wall=wall, size=size, width=width, span=span,
                        panel_walls=float(record.get("panel_walls", PANEL_WALLS)))
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


#: SHMESH-23: THE USER'S ELEMENT COUNT (engines/fea_driver.element_target, the same count the solid mesh is sized to)
#: scales every size of the plan by one factor. The user's count beats our minimums - finer than half the wall, or
#: coarser than the rules, is said, never refused - but the factor stays inside SCALE_RANGE (a count read wrongly
#: by a thousand times must not ask for a billion elements).
SCALE_RANGE = (0.02, 50.0)
#: SHMESH-23: an "about" count is met when the built mesh is within this share of it; past it, the plan is aimed
#: again once with the built-over-planned it measured
COUNT_SHARE = 0.25


def count_aim(planned: float, target: Mapping | None) -> float | None:
    """SHMESH-23: the element count the plan is aimed at, or None when the rules' own plan already meets the user's
    count (or there is none). `target` is fea_driver.element_target's {"elements", "mode", "low"/"high"}."""
    t = target or {}
    n = int(t.get("elements") or 0)
    if n <= 0 or planned <= 0:
        return None
    mode = str(t.get("mode") or "about")
    if mode == "range" and int(t.get("low") or 0) <= planned <= int(t.get("high") or n):
        return None
    if mode == "at_most":
        return None if planned <= n else 0.9 * n      # a little inside the bound, for the built count to stay in
    if mode == "at_least":
        return None if planned >= n else 1.1 * n
    return float(n)


def count_scale(planned: float, aim: float | None) -> float:
    """SHMESH-23: the factor on every element size that brings `planned` elements to `aim` (the count goes as the
    inverse square of the size on a surface), held inside SCALE_RANGE; 1.0 with no aim."""
    if not aim or planned <= 0:
        return 1.0
    lo, hi = SCALE_RANGE
    return min(hi, max(lo, math.sqrt(float(planned) / float(aim))))


def count_met(built: int, target: Mapping | None) -> bool:
    """SHMESH-23: whether a built count meets the user's: inside the range, under the most, over the least, or
    within COUNT_SHARE of an "about" count."""
    t = target or {}
    n = int(t.get("elements") or 0)
    if n <= 0:
        return True
    mode = str(t.get("mode") or "about")
    if mode == "range":
        return int(t.get("low") or 0) <= built <= int(t.get("high") or n)
    if mode == "at_most":
        return built <= n
    if mode == "at_least":
        return built >= n
    return abs(built - n) <= COUNT_SHARE * n


def count_words(t: Mapping | None) -> str:
    """The shell's line for the user's count (SHMESH-23), in plain words; "" with none asked."""
    t = t or {}
    asked = int(t.get("asked") or 0)
    if asked <= 0:
        return ""
    built = int(t.get("built") or 0)
    if t.get("kept_rules"):
        return f"Your count ({asked:,} elements) was already met by our rules' own sizes."
    said = f"Sized to the {asked:,} elements you asked for ({built:,} made)."
    if t.get("held"):
        said += " That was as far as the sizes could go."
    rules = int(t.get("rules_elements") or 0)
    if t.get("scale", 1.0) > 1.0 and rules:
        said += f" This is coarser than our accuracy rules ask for (about {rules:,} elements): results may be less exact."
    if t.get("under_floor"):
        said += " Some elements are smaller than half the wall, finer than a shell usually needs."
    return said


__all__ = ["BASE_DIVISIONS", "COUNT_SHARE", "CURVE_PER_QUARTER", "FLOOR_WALLS", "GROWTH", "MIN_ACROSS", "PANEL_ACROSS",
           "PANEL_WALLS", "SCALE_RANGE", "base_size", "count_aim", "count_met", "count_scale", "count_words",
           "curve_size", "face_plan"]
