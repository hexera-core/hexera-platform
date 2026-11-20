# Responsibility: Hold the edge gate to its rule in a real browser - edges on when a delivered
# cell is big enough on screen to be seen, off when it is not, the polygon ceiling above both, and
# the status line saying which of the two reasons is in force.
# Boundaries: one 1,600-quad sheet and one deliberately over-sized actor; only the network is
# stood in for. It asserts no pixels and no frame times.
"""THE CAP WAS THE WRONG QUESTION AT THE OPENING FRAMING.

`_EDGE_MAX_POLYS` bounds a failure counted in polygons: on 2026-11-08 the edge pass on a
1,904,636-polygon actor did not slow the frame, it took the WebGL context away. That is real and
the ceiling stays. But on the delivered cold plate (job 6dd6703c) the worst the payload can reach
is 78.6% of the line segments that broke it, so the ceiling never fires there - and what the page
was paying instead was 6.9x the frame to draw a lattice nobody can resolve.

MEASURED ON THAT PLATE, 2026-11-19, headless Chrome on the real GPU (Intel UHD via ANGLE/D3D11),
canvas 502x754, frames timed by wall clock between requestAnimationFrame callbacks:

    1 px = 274 um   a cell is 0.73 px   115.2 ms   9.0 fps   edges REMOVE 41% of the detail
    1 px = 106 um   a cell is 1.90 px    82.0 ms  12.5 fps   the lattice reads
    1 px =  49 um   a cell is 4.08 px    39.8 ms  25.2 fps   individual cells read
    1 px =  19 um   a cell is 10.6 px    20.6 ms  46.4 fps   cells as large diamonds

"Edges remove 41% of the detail" is not a figure of speech. Two screenshots at one camera, edges
off and on, blurred by one pixel and compared by their mean gradient over drawn-surface pixels:
the ratio is 0.59 at the opening framing and crosses 1.0 at 1.18 px per cell. Below that the
lines cover the mesh instead of drawing it, at 6.9x the frame.

WHAT THIS FILE CAN AND CANNOT SEE. Frame time is a property of a GPU and is measured on one, not
here - the repository's own UI gate runs with --disable-gpu and lands on SwiftShader, whose
timings are worthless for this. What is a property of the CODE, and is what this file holds, is
WHICH SURFACES ARE TOLD TO DRAW EDGES AT WHICH FRAMING, and what the page then says about it.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "edge-gate-job"

#: THE FIXTURE HAS TO BE ABLE TO REACH BOTH SIDES OF THE GATE, which is not automatic. The camera
#: envelope (`capRange`) stops a zoom-out at the FARTHER of "a typical cell is 2.5 px" and "85% of
#: the distance that frames the whole model", so on a mesh that is only a few hundred cells across
#: the first wins and a cell can never get small - which is why none of the existing fixtures in
#: this directory, all 20 to 120 cells across, change behaviour under this gate at all.
#: This sheet is 0.4 m across with a 0.5 mm typical cell, so it is 800 cells across and the
#: whole-model framing wins, exactly as it does on the 178 mm plate with its 201 um cells.
SHEET_SPAN_M = 0.01
SHEET_QUADS = 40
CELL_TYP_M = 0.0005


def _sheet(name: str, nx: int, ny: int, h: float) -> dict:
    """A flat nx-by-ny sheet of quads in the (points, polys) form a boundary reader hands over."""
    xs = np.arange(nx + 1) * h
    ys = np.arange(ny + 1) * h
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1).astype(np.float32)

    def pid(i, j):
        return i * (ny + 1) + j

    quads = [[4, pid(i, j), pid(i + 1, j), pid(i + 1, j + 1), pid(i, j + 1)]
             for i in range(nx) for j in range(ny)]
    return {"name": name, "type": "wall", "face_count": nx * ny, "points": pts.tobytes(),
            "polys": np.asarray(quads, dtype=np.uint32).reshape(-1).tobytes()}


#: "no cell_stats at all" is a case this file has to be able to build, so the default cannot be
#: None - that is the value under test.
_OWN = object()


def _payload(*, cell_stats=_OWN) -> dict:
    stats = {"p10": CELL_TYP_M, "typ": CELL_TYP_M} if cell_stats is _OWN else cell_stats
    surf = polymesh_response([_sheet("fluid/wall", SHEET_QUADS, SHEET_QUADS, SHEET_SPAN_M)],
                             stats, {"fluid/wall": "wall"}, "m")
    assert surf is not None and surf["kind"] == "polymesh"
    surf.update(is_mesh=True, parts=[], quality={"engine": "cfmesh_conjugate", "units": "m"})
    return surf


#: THE CEILING NEEDS AN ACTOR PAST IT, and shipping 900,001 polygons through a page expression
#: would be a 19 MB string. The connectivity is generated IN the page instead: 900,001 triangles
#: over the sheet's own four corner points. Every one of them is a real entry in the polygon
#: stream, which is what `_countPolys` counts and what the ceiling is counted in; they are
#: degenerate on screen, which is why this costs a software rasteriser nothing.
_OVERSIZE_JS = """
      (() => {
        const N = __N__;
        const a = new Uint32Array(N * 4);
        for (let i = 0, k = 0; i < N; i++) { a[k++] = 3; a[k++] = 0; a[k++] = 1; a[k++] = 2; }
        const u = new Uint8Array(a.buffer); let s = '';
        for (let i = 0; i < u.length; i += 8192)
          s += String.fromCharCode.apply(null, u.subarray(i, i + 8192));
        PAYLOAD.patches[0].polys_b64 = btoa(s);
      })();
"""


def _open(live, payload: dict, oversize: int = 0) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
      __OVERSIZE__
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__JOB__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__JOB__', document.getElementById('stage'), {metrics: {pass: true}});
    })()""".replace("__PAYLOAD__", json.dumps(payload))
             .replace("__OVERSIZE__", _OVERSIZE_JS.replace("__N__", str(oversize))
                      if oversize else "")
             .replace("__JOB__", JOB), timeout=180)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=120,
                  what="the viewer to initialise")


def _dbg(live, call: str):
    return live.evaluate(f"window._vdbg['{JOB}'].{call}", timeout=60)


def _edges_by_patch(live) -> dict:
    return {r["patch"]: r["edges"] for r in _dbg(live, "surfaces()")}


def _status(live) -> str:
    return live.evaluate(f"(document.getElementById('v-status-{JOB}')||{{}}).textContent || ''")


# --------------------------------------------------------------------------------------------
# 1. THE RULE ITSELF, with no mesh, no camera and no GPU. Three comparisons; a test should be
#    able to state them rather than infer them from a picture.
# --------------------------------------------------------------------------------------------

def _rule(live, polys, cell_px):
    return live.evaluate(
        "(async () => { const m = await import('/static/js/viewer/viewer.js');"
        f" return m.edgeVerdict({polys}, {cell_px}); }})()", timeout=60)


def test_the_rule_turns_on_the_size_of_a_cell_on_screen(live):
    limits = live.evaluate(
        "(async () => { const m = await import('/static/js/viewer/viewer.js');"
        " return [m._EDGE_MIN_CELL_PX, m._EDGE_MAX_POLYS]; })()", timeout=60)
    min_px, cap = limits
    assert min_px == 1.5, f"the gate moved without the measurement moving: {min_px}"
    assert cap == 900_000, cap
    # under the gate, off; at it and above it, on
    assert _rule(live, 1000, min_px - 0.01) == "too-small"
    assert _rule(live, 1000, min_px) == "on"
    assert _rule(live, 1000, 40) == "on"


def test_the_hard_ceiling_sits_above_the_gate_and_no_zoom_gets_past_it(live):
    """THE CEILING IS A SAFETY STOP, NOT A LEGIBILITY ONE. A surface past it never draws edges
    however big its cells get on screen, because what it bounds is the context going - and WebGL
    exposes no way to ask how much video memory is left, so it cannot be detected, only bounded."""
    assert _rule(live, 900_000, 40) == "on"
    assert _rule(live, 900_001, 40) == "too-big"
    assert _rule(live, 900_001, 4_000) == "too-big", (
        "zooming in must not be able to talk a surface past the ceiling into drawing edges")
    # and an empty surface is neither of those things
    assert _rule(live, 0, 40) == "none"


def test_a_payload_with_no_cell_size_is_left_exactly_where_it_was(live):
    """The STL fallback carries no cell_stats, so there is nothing to ask and the gate must not
    invent an answer: the ceiling alone decides, as it did before this existed."""
    assert _rule(live, 1000, 0) == "on"


# --------------------------------------------------------------------------------------------
# 2. THE RULE IN FORCE, on a mesh, through the camera a reader actually drives.
# --------------------------------------------------------------------------------------------

def test_a_mesh_whose_cells_are_under_a_pixel_opens_with_no_cell_edges(live):
    """THE DEFECT. Before this gate every surface under the polygon ceiling drew its edges at
    every framing, so the plate opened at 0.73 px per cell spending 6.9x the frame on a lattice
    that removed 41% of what a reader could resolve."""
    _open(live, _payload())
    assert _edges_by_patch(live) == {"fluid/wall": False}, (
        "a mesh whose cells are under a pixel still drew cell edges, which is 6.9x the frame "
        "spent removing 41% of what a reader could resolve")
    gate = _dbg(live, "edges()")
    assert gate["cellPx"] < gate["minCellPx"], (
        f"this fixture cannot reach the framing under test: {gate}")
    assert gate["state"]["fluid/wall"] == "too-small", gate
    assert_clean(live, "the viewer with a sub-pixel lattice")


def test_zooming_in_brings_the_edges_on_and_zooming_back_out_takes_them_off(live):
    """Both directions, because a gate that only ever latched one way would look right from the
    opening framing and never hand the cells back."""
    _open(live, _payload())
    assert _edges_by_patch(live)["fluid/wall"] is False

    after_in = _dbg(live, "dolly(0.1)")        # ten times closer: a cell is ten times bigger
    assert after_in > 1.5, after_in
    assert _dbg(live, "edgesAgain()") is True, "zooming in did not bring the edges back"
    assert _edges_by_patch(live)["fluid/wall"] is True
    assert _dbg(live, "edges()")["state"]["fluid/wall"] == "on"

    _dbg(live, "dolly(10.0)")                  # and back out again
    assert _dbg(live, "edgesAgain()") is True, "zooming out did not take the edges off"
    assert _edges_by_patch(live)["fluid/wall"] is False
    assert_clean(live, "the viewer zoomed in and out across the gate")


def test_an_orbit_at_a_fixed_zoom_toggles_nothing(live):
    """A trackball rotation keeps the camera's distance to its focal point, so a cell is the same
    size on screen before and after it. Nothing may flip - the flip is a buffer rebuild, measured
    at 126 MB for the plate's largest actor, and paying that for an orbit would be a stutter for
    no change on screen."""
    _open(live, _payload())
    _dbg(live, "dolly(0.1)")
    _dbg(live, "edgesAgain()")
    before_px = _dbg(live, "edges()")["cellPx"]
    before = _edges_by_patch(live)

    for deg in (17, 53, -91):
        _dbg(live, f"orbit({deg})")
        assert _dbg(live, "edgesAgain()") is False, (
            f"an orbit of {deg} degrees flipped the edge pass")

    after = _dbg(live, "edges()")
    assert after["cellPx"] == pytest.approx(before_px, rel=1e-9), (
        "an orbit changed how big a cell is on screen, which it cannot do")
    assert _edges_by_patch(live) == before


def test_the_gate_waits_for_the_reader_to_stop_rather_than_firing_on_every_tick(live):
    """A camera fires modified on every wheel tick of a thirty-tick zoom and the work this defers
    is a buffer rebuild, not a frame - so the gate is on a trailing debounce and must not have
    acted by the time the next tick would have arrived."""
    _open(live, _payload())
    settle = _dbg(live, "edges()")["settleMs"]
    assert settle >= 150, f"a debounce of {settle} ms is not a debounce"

    _dbg(live, "dolly(0.1)")                    # one 'tick', then look immediately
    time.sleep(settle / 1000.0 * 0.3)
    assert _edges_by_patch(live)["fluid/wall"] is False, (
        "the gate acted inside its own settle window, which is the stutter it exists to avoid")

    time.sleep(settle / 1000.0 * 2.0 + 0.4)     # and once the reader has stopped
    assert _edges_by_patch(live)["fluid/wall"] is True, (
        "the gate never fired after the camera settled")


def test_a_payload_with_no_cell_stats_keeps_its_edges_at_every_framing(live):
    """The STL fallback. It carries no cell size, so the gate has nothing to ask and must leave
    the page exactly as it was - this is the arm that stops the change reaching meshes it was
    never measured on."""
    _open(live, _payload(cell_stats=None))
    assert _edges_by_patch(live)["fluid/wall"] is True, (
        "a payload with no cell size had its edges taken away by a gate that had nothing to "
        "measure")
    assert _dbg(live, "edges()")["cellPx"] == 0
    _dbg(live, "dolly(8.0)")
    assert _dbg(live, "edgesAgain()") is False
    assert _edges_by_patch(live)["fluid/wall"] is True


# --------------------------------------------------------------------------------------------
# 3. WHAT THE PAGE SAYS. A reader must never be unable to tell a mesh drawn without edges from
#    one whose cells are too small to have any.
# --------------------------------------------------------------------------------------------

def test_the_status_line_says_the_cell_is_too_small_and_that_zooming_fixes_it(live):
    _open(live, _payload())
    s = _status(live)
    assert "cell edges off" in s, f"the page drew no edges and did not say so: {s}"
    assert "px at this framing" in s, f"it does not say what is too small: {s}"
    assert "Zoom in" in s, f"it does not say what a reader can do about it: {s}"
    assert "past 900,000 polygons" not in s, (
        "it blamed the surface for something that is a property of the framing: " + s)
    assert "the cells are there either way" in s, (
        "a reader is left unable to tell a drawing decision from a property of their mesh: " + s)


def test_the_status_line_blames_the_surface_when_that_is_the_reason(live):
    """The other arm, and the one the wording has to tell apart from it: a surface past the
    polygon ceiling is not waiting for a zoom, and saying 'zoom in' about it would be a lie."""
    _open(live, _payload(), oversize=900_001)
    s = _status(live)
    assert "past 900,000 polygons" in s, f"the ceiling fired and the line did not say so: {s}"
    assert "px at this framing" not in s, (
        "it told a reader to zoom in on a surface that will never draw edges: " + s)
    assert _dbg(live, "edges()")["state"]["fluid/wall"] == "too-big"


def test_the_line_keeps_up_with_the_reader_rather_than_freezing_at_the_opening_framing(live):
    """It quotes the cell size, and a reader moves that under its feet. The first version of this
    gate printed the size it saw when the page opened and never revised it: measured while
    building it, the line went on saying 0.49 px after a canvas resize had made a cell 0.73."""
    _open(live, _payload())
    first = _status(live)
    assert "px at this framing" in first, (
        "the line does not quote the cell size at all: " + first)
    _dbg(live, "dolly(0.6)")                   # still under the gate, but a different number
    _dbg(live, "edgesAgain()")
    second = _status(live)
    assert "px at this framing" in second
    assert second != first, (
        "the status line is quoting a cell size the camera has already left behind")
