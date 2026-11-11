# Responsibility: THE ONE LINE OF ADVICE for walls that are a shell only because the user asked for one (SHELL-23):
# under ten of their thicknesses across, or under four wide. Plain words with the wall's own numbers and the measured
# cost, from the rows the extraction keeps (cad/midsurface "short_walls").
# Boundaries: pure - no geometry, no OpenCASCADE, no numpy - so the stage (application/shell_check) can say the line
# again after the user corrects the unit, without loading the extraction.
# Collaborates with: cad/midsurface.py (writes the rows and the record's "advice"), cad/shell_pairs.py (the two
# ratios; a test holds these equal to them), application/shell_check.py (the stage's line), engines/shell_run.py
# (the result's words), docs/design/shell/README.md (1.5: where the numbers were measured).
"""SHELL-23's advice.

One wall:      "Part_6: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is less exact under 10
                times (about 2% off on a free plate this short, more where it bends from a joint)."
Several walls: "9 walls are short for their thickness (the shortest, Part_13: 31.8 mm thick over 126 mm, 4 times its
                thickness). Shell theory is less exact under 10 times (about 5% off ...)."
"""
from __future__ import annotations

#: SHELL-2's thin rule, in walls: a sheet at least SPAN_WALLS of its thickness across (the root of its mid-surface
#: area) and WIDTH_WALLS wide is a thin shell and gets no advice (1 / shell_pairs.SHELL_RATIO, 1 / SHELL_WIDTH_RATIO)
SPAN_WALLS = 10.0
WIDTH_WALLS = 4.0
#: WHAT A SHORT PLATE COSTS, MEASURED (10 Nov, CalculiX on one thread, free-free modes 7 to 12, the product's shell
#: mesh against a C3D10 solid mesh of the same plate, three elements through its thickness). A free square plate:
#: 12 walls across 2.4%, 10 2.3%, 9 2.3%, 7.6 2.1%, 6 2.3%, 5 3.2%, 4 4.7%, 3 7.4% (the worst of the six modes) -
#: a plate alone is no worse at 6 walls than at 12, and only under about 5 does it lose. (span under, about % off)
SPAN_OFF = ((3.5, 7), (4.5, 5), (5.5, 3), (float("inf"), 2))
#: ... and a free flat bar 200 mm long: 2 walls wide 3.2%, 3 2.2%, 4 2.1%, 6 2.0%. (width under, about % off)
WIDTH_OFF = ((2.5, 3), (float("inf"), 2))
#: ... the plate ALONE. A short wall bending from a joint is another matter: a shell wall bends from the middle of
#: its joint, and a box's 4 mm walls reaching 5 to 6.7 walls from theirs read +23% (design note 2.6, MIXED-8).
JOINT_WORDS = "more where it bends from a joint"


def num(x: float) -> str:
    """A length in plain digits, three significant (7000, 40100, 2.78 - never 7e+03)."""
    return f"{float(f'{float(x):.3g}'):g}"


def _off(table: tuple, walls: float) -> int:
    return next(pct for under, pct in table if walls < under)


def short_wall_words(rows: list[dict]) -> str:
    """The advice for the walls under SHELL-2's thin rule (`rows`: body, thickness_mm, span_mm, width_mm, span_walls,
    width_walls each), "" when there is none: the wall named - the shortest when there are several - with its
    thickness, its span or width, how many times its thickness that is, and what a free plate that short costs."""
    if not rows:
        return ""

    def key(r: dict):
        return (min(float(r["span_walls"]) / SPAN_WALLS, float(r["width_walls"]) / WIDTH_WALLS), str(r["body"]),
                str(r.get("sheet")))

    w = min(rows, key=key)
    if float(w["span_walls"]) < SPAN_WALLS:
        one = (f"{num(w['thickness_mm'])} mm thick over {num(w['span_mm'])} mm, {float(w['span_walls']):g} times its "
               "thickness")
        rule = (f"Shell theory is less exact under {SPAN_WALLS:g} times (about {_off(SPAN_OFF, float(w['span_walls']))}% "
                f"off on a free plate this short, {JOINT_WORDS}).")
    else:
        one = (f"{num(w['thickness_mm'])} mm thick and {num(w['width_mm'])} mm wide, {float(w['width_walls']):g} times "
               "its thickness")
        rule = (f"Shell theory is less exact under {WIDTH_WALLS:g} times (about "
                f"{_off(WIDTH_OFF, float(w['width_walls']))}% off on a free strip this narrow, {JOINT_WORDS}).")
    if len(rows) == 1:
        return f"{w['body']}: {one}. {rule}"
    return f"{len(rows)} walls are short for their thickness (the shortest, {w['body']}: {one}). {rule}"


__all__ = ["JOINT_WORDS", "SPAN_OFF", "SPAN_WALLS", "WIDTH_OFF", "WIDTH_WALLS", "num", "short_wall_words"]
