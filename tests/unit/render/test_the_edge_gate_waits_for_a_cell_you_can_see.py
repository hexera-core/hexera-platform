# Responsibility: Hold viewer.js's _EDGE_MIN_CELL_PX to the measurement that chose it, hold both
# served copies to the same number, and hold it to the one quantity this file already gates two
# other things on.
# Boundaries: arithmetic over recorded measurements, plus reading constants out of the two served
# copies of viewer.js. It opens no mesh and starts no browser.
"""THE CAP WAS NOT THE THING PAYING, AND NOTHING COULD SEE THAT EITHER.

`test_the_edge_cap_admits_the_plate_it_draws` is the sibling of this file and it establishes that
the polygon ceiling never fires on the delivered plate: the worst that payload can put on screen
with every overlay on is 5,989,053 line segments against the 7,620,174 that took the WebGL
context away. The ceiling is correct and it stays. What it was NOT doing was stopping the page
spending 6.9x the frame on a lattice no reader can resolve, because a count of polygons cannot
know how big they are on screen.

MEASURED 2026-11-19 ON THAT JOB'S OWN SERVED PAYLOAD, headless Chrome 157 on the real GPU
(renderer "ANGLE (Intel, Intel(R) UHD Graphics (0x0000A788) Direct3D11 vs_5_0 ps_5_0, D3D11)",
WebGL2), canvas 502x754, camera aimed at the surface so the mesh fills the view. Frames timed by
WALL CLOCK between requestAnimationFrame callbacks - gl.finish() does not block on this host and
reports a 100 ms frame as 25 ms, which is how an earlier reading of this came out wrong.

    a cell is   1 px is   edges off   edges on   detail gain
     0.73 px     274 um     16.7 ms    115.2 ms     0.59x
     1.01 px     199 um     16.7 ms    161.8 ms     0.75x
     1.27 px     159 um     16.7 ms    101.5 ms     1.17x
     1.52 px     133 um     16.7 ms     94.6 ms     1.73x
     1.90 px     106 um     16.7 ms     82.0 ms       -
     2.03 px      99 um     16.7 ms     87.9 ms     2.95x
     4.08 px      49 um     16.7 ms     39.8 ms     3.59x
    10.58 px      19 um     16.7 ms     20.6 ms    12.14x

GAIN is the measurement that chose the threshold. Two screenshots at one camera, edges off and
edges on, each blurred by one pixel - below that is where sub-pixel edge aliasing lives and also
below what a reader resolves on a screen - and then the mean gradient over drawn-surface pixels,
on over off. Below 1.0 the edge pass is covering the mesh up rather than drawing it. It crosses
1.0 at 1.18 px per cell, interpolated between the two rungs that bracket it, and at the framing
the page opens at it is 0.59: the lines remove 41% of what a reader could otherwise resolve, and
56% of the surface's brightness, for 6.9x the frame.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

SERVED = ("ui/js/viewer/viewer.js",
          "apps/console/public/static/js/viewer/viewer.js")

#: WHERE THE EDGE PASS STOPS PAYING FOR ITSELF, measured as described above. Below this a cell on
#: screen is too small for the lines around it to leave any of it showing.
BREAK_EVEN_PX = 1.18

#: THE TWO ROWS THE BRACKET IS DRAWN BETWEEN, both on the real plate: nothing cell-like resolves
#: at the framing the page opens at, and the channel walls, fins and cell rows read at 1.90.
NOTHING_RESOLVES_PX = 0.73
THE_LATTICE_READS_PX = 1.90

#: ...and the two gates this file already has on the SAME quantity, both of them 1.5 and both with
#: the note that below about 1.5 px a real cell is smaller than the pixel that would draw it.
#: A third gate on one physical question should be the same number as the other two.
SIBLING_GATES = ("FOLLOW_MIN_PX", "STREAM_MIN_PX")


def _const(rel: str, name: str) -> float:
    text = (REPO / rel).read_text(encoding="utf-8")
    found = re.search(rf"^const {re.escape(name)}=([\d.]+);", text, re.MULTILINE)
    assert found, f"{rel} no longer declares {name} at the top level"
    return float(found.group(1))


def _gate(rel: str) -> float:
    return _const(rel, "_EDGE_MIN_CELL_PX")


def _sibling(rel: str, name: str) -> float:
    text = (REPO / rel).read_text(encoding="utf-8")
    found = re.search(rf"\b{re.escape(name)}=([\d.]+);", text)
    assert found, f"{rel} no longer declares {name}"
    return float(found.group(1))


def test_both_served_copies_declare_the_same_gate():
    gates = {rel: _gate(rel) for rel in SERVED}
    assert len(set(gates.values())) == 1, (
        f"the two served viewers disagree about when a cell is big enough to edge: {gates}")


def test_the_gate_is_clear_of_the_measured_break_even():
    """A threshold sitting ON break-even buys nothing: at it the edge pass is adding exactly as
    much as it is covering up, and it still costs the frame."""
    gate = _gate(SERVED[0])
    assert gate > BREAK_EVEN_PX, (
        f"the gate is {gate} px against a measured break-even of {BREAK_EVEN_PX} px, so it admits "
        "framings where the lines take more detail away than they draw")


def test_the_gate_sits_inside_the_bracket_the_measurements_left_open():
    """Nothing cell-like resolves at 0.73 px and the lattice reads at 1.90, so the answer is
    between them. A gate above 1.90 would be taking edges away from a framing where a reader was
    already reading the mesh by them."""
    gate = _gate(SERVED[0])
    assert NOTHING_RESOLVES_PX < gate < THE_LATTICE_READS_PX, (
        f"the gate is {gate} px, outside the bracket "
        f"({NOTHING_RESOLVES_PX}, {THE_LATTICE_READS_PX}) the measurements actually support")


def test_the_gate_is_the_same_number_as_the_other_two_gates_on_the_same_question():
    """`FOLLOW_MIN_PX` and `STREAM_MIN_PX` both decide, from the same arithmetic, when a cell is
    big enough on screen to be worth fetching at 1:1. This decides when it is big enough to be
    worth drawing a line round. One question, one number: a reader who zooms until the cells
    appear is handed the real ones at the same moment the drawn ones become worth drawing."""
    gate = _gate(SERVED[0])
    for name in SIBLING_GATES:
        other = _sibling(SERVED[0], name)
        assert other == gate, (
            f"{name} is {other} px and the edge gate is {gate} px. Both answer 'is a cell big "
            "enough on screen to be worth the work', and two answers to one question means a "
            "reader crosses one of them without crossing the other")


def test_the_ceiling_is_still_there_above_the_gate():
    """THE GATE SITS INSIDE THE CEILING, it does not replace it. The ceiling bounds a context
    loss, which is not recoverable and cannot be detected; the gate bounds a frame spent on
    nothing, which is. Deleting the first because the second exists would be reading the
    2026-11-08 record as though it had been withdrawn."""
    cap = _const(SERVED[0], "_EDGE_MAX_POLYS")
    assert cap == 900_000, cap
    text = (REPO / SERVED[0]).read_text(encoding="utf-8")
    # the ceiling is tested BEFORE the cell size, so no zoom can talk a surface past it
    rule = re.search(r"function edgeVerdict\(nPolys,cellPx\)\{(.+?)\n", text, re.S)
    assert rule, "viewer.js no longer declares edgeVerdict(nPolys,cellPx)"
    body = text[rule.start():text.index("/* Count the polygons actually in", rule.start())]
    assert body.index("_EDGE_MAX_POLYS") < body.index("_EDGE_MIN_CELL_PX"), (
        "the cell-size gate is tested before the polygon ceiling, so a reader who zooms far "
        "enough in would be handed edges on a surface the ceiling exists to refuse")
