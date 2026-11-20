# Responsibility: Verify in a real browser that a poor-cell highlight big enough to BE the view
# opens off, that a small one still opens shown, that the page says which it did, and that
# nothing is removed from the roster either way.
# Boundaries: the payload is the shape the backend builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "overlay-job"

# WHY THIS TEST EXISTS, and it is not a style preference.
#
# checkMesh's own sets are drawn over the mesh in red, at full resolution, and render/mesh_facts
# opens every one of them because "a set of a few hundred cells inside a mesh of millions reads as
# a mark on it and not as another surface". That is true and it has a size in it. Measured on the
# delivered cold plate (job 6dd6703c), 2026-11-19, from the served payload: 13 sets totalling
# 859,079 triangles over a mesh drawn as 526,914, so 61.98% of every triangle on the page was
# overlay and the overlays outnumbered the mesh 1.63 to 1. Two sets did almost all of it -
# fluid/underdeterminedCells at 212,284 faces and fluid/lowWeightFaces at 149,984. A reader
# opening that link was shown, mostly, not their mesh.
#
# THE INFORMATION IS NOT THE PROBLEM AND DOES NOT GO. Every set still ships, still appears under
# Poor-quality cells with its own count, and is one click from being on. The only thing that
# changed is which ones the page opens with.
#
# WHAT THIS FIXTURE CAN AND CANNOT REACH. The rule is two tests and-ed: a share of the drawn mesh
# (the principle - a highlight has to be small against what it marks) and an absolute floor (so a
# small mesh is never touched, because on a four-cell case four bad faces are a large fraction of
# everything). This file exercises the floor in both directions and the two-tests-together case.
# It does NOT exercise "over the floor but under the share", because the smallest mesh that could
# is about 1,000,050 triangles and the cheapest honest encoding of that is ~21 MB of base64 to
# push through CDP for one assertion. That branch is covered by the real plate instead, where the
# three sets that open off are 19.7%, 14.3% and 5.2% of the drawn mesh.


def _grid(name: str, n: int, role: str = "wall") -> dict:
    """A flat strip of `n` quads, points shared along it - an ordinary drawn patch."""
    xs = np.arange(n + 1, dtype=np.float32) * 0.001
    pts = np.empty((2 * (n + 1), 3), dtype=np.float32)
    pts[0::2] = np.stack([xs, np.zeros_like(xs), np.zeros_like(xs)], axis=1)
    pts[1::2] = np.stack([xs, np.full_like(xs, 0.001), np.zeros_like(xs)], axis=1)
    polys = np.empty((n, 5), dtype=np.uint32)
    polys[:, 0] = 4
    base = np.arange(n, dtype=np.uint32) * 2
    polys[:, 1] = base
    polys[:, 2] = base + 2
    polys[:, 3] = base + 3
    polys[:, 4] = base + 1
    return {"name": name, "type": role, "face_count": n,
            "points": pts.tobytes(), "polys": polys.reshape(-1).tobytes()}


def _set(name: str, triangles: int, *, span: float = 1.0) -> dict:
    """A poor-cell set: `triangles` separate little triangles scattered along `span` metres.

    SEPARATE, AND IT HAS TO BE. The first version of this helper stacked every triangle on the
    same three points, which is 25x smaller on the wire and counts the same - and a payload whose
    ONLY patch was one of those stopped the page answering CDP at all, for longer than the gate's
    30 s read budget, so the test failed as a timeout with no console error to read. It is not
    this change's doing and not the overlay rule's: a probe with the same geometry typed `wall`
    instead of `poorQuality` failed identically, and the same set beside an ordinary patch was
    fine. It is a degenerate model - thousands of coincident triangles filling a millimetre - and
    a real set of failed cell faces is not that. So the fixture is not that either.
    """
    i = np.arange(triangles, dtype=np.float64)
    x = (i % 1000) * (span / 1000.0)
    y = (i // 1000) * (span / 1000.0)
    h = span / 4000.0
    corners = np.empty((triangles, 3, 3), dtype=np.float32)
    corners[:, 0] = np.stack([x, y, np.full_like(x, 0.002)], axis=1)
    corners[:, 1] = np.stack([x + h, y, np.full_like(x, 0.002)], axis=1)
    corners[:, 2] = np.stack([x, y + h, np.full_like(x, 0.002 + 1e-4)], axis=1)
    polys = np.empty((triangles, 4), dtype=np.uint32)
    polys[:, 0] = 3
    base = np.arange(triangles, dtype=np.uint32) * 3
    polys[:, 1], polys[:, 2], polys[:, 3] = base, base + 1, base + 2
    return {"name": name, "type": "poorQuality", "face_count": triangles,
            "points": corners.reshape(-1, 3).tobytes(), "polys": polys.reshape(-1).tobytes()}


#: comfortably over the absolute floor (20,000 triangles) AND over the share
BIG = 20_001
#: over the share of a 2,000-triangle mesh many times over, and UNDER the floor - so it stays
SMALL = 100
#: the drawn mesh these are measured against
MESH_QUADS = 1_000


def _payload() -> dict:
    raw = [_grid("wall", MESH_QUADS),
           _set("fluid/underdeterminedCells", BIG),
           _set("fluid/nonOrthoFaces", SMALL)]
    surf = polymesh_response(raw, {"p10": 0.001, "typ": 0.001},
                             {"wall": "wall"}, "m")
    surf.update(is_mesh=True, cell_count=1_234_567,
                quality={"engine": "cfmesh_conjugate", "units": "m"},
                parts=[{"id": "boundary:wall", "label": "wall", "kind": "boundary",
                        "selectable": True, "renderable": True,
                        "default_visibility": "visible", "metadata": {"name": "wall"}}]
                + [{"id": "poorCells:" + p["name"], "label": p["name"], "kind": "poorCells",
                    "selectable": True, "renderable": True,
                    "default_visibility": "visible",
                    "metadata": {"name": p["name"], "faces": p["face_count"]}}
                   for p in raw[1:]])
    return surf


def _open(live, payload: dict) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __PAYLOAD__;
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
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=120)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=120,
                  what="the viewer to initialise")


def _surfaces(live) -> dict:
    rows = live.evaluate(f"window._vdbg['{JOB}'].surfaces()")
    return {r["patch"]: r for r in rows}


def _status(live) -> str:
    return live.evaluate(
        f"(document.getElementById('v-status-{JOB}')||{{}}).textContent || ''") or ""


def test_a_highlight_bigger_than_the_mesh_opens_off(live):
    """The defect, in one assertion. 20,001 triangles of highlight over a 2,000-triangle mesh is
    not a mark on it."""
    _open(live, _payload())
    rows = _surfaces(live)
    big = rows["fluid/underdeterminedCells"]
    assert big["openedOff"] is True, big
    assert big["visible"] is False, (
        "a set ten times the size of the mesh it marks must not be on screen at open")
    assert_clean(live, "the viewer with an oversized quality overlay")


def test_a_small_highlight_still_opens_shown(live):
    """The floor, which is the whole reason the rule is two tests and not one. 100 triangles is
    5% of this mesh - well over the share - and it stays, because on a small mesh every real
    highlight is a large fraction of everything and hiding it would hide the point."""
    _open(live, _payload())
    rows = _surfaces(live)
    small = rows["fluid/nonOrthoFaces"]
    assert small["triangles"] == SMALL
    assert small["triangles"] > 0.02 * rows["wall"]["triangles"], (
        "this fixture only proves the floor if the small set IS over the share")
    assert small["openedOff"] is False, small
    assert small["visible"] is True, "a real highlight on a small mesh must still be shown"


def test_the_page_says_which_overlays_it_held_back(live):
    """Held back silently is the same defect wearing better manners: a reader cannot tell a
    choice the page made from a property of their mesh unless the page says which it was."""
    _open(live, _payload())
    status = _status(live)
    assert "poor-cell overlay" in status, f"the page did not say it held anything back: {status}"
    assert "opened off" in status, status
    assert f"{BIG:,}" in status, (
        f"it has to say how much was held back, in triangles: {status}")
    assert "Poor-quality cells" in status, (
        f"and where to switch them on, by the roster's own words: {status}")


def test_nothing_is_removed_from_the_roster(live):
    """The information is the honest part of this viewer. Opening off is a default, not a
    deletion - both sets are still parts, still named, still one click from being on."""
    _open(live, _payload())
    chips = live.evaluate(
        f"""[...document.querySelectorAll('#v-bnd-chips-{JOB} .v-bnd-chip')]
            .map(c => ({{id: c.dataset.p, hidden: c.classList.contains('hidden-b')}}))""")
    by_id = {c["id"]: c for c in chips}
    assert "poorCells:fluid/underdeterminedCells" in by_id, (
        "the set that opened off is still a part the reader can find")
    assert "poorCells:fluid/nonOrthoFaces" in by_id
    assert by_id["poorCells:fluid/underdeterminedCells"]["hidden"] is True
    assert by_id["poorCells:fluid/nonOrthoFaces"]["hidden"] is False


def test_a_page_whose_only_surfaces_are_overlays_still_draws_them(live):
    """The share test alone would empty this viewport, because anything is more than 2% of
    nothing. A payload that draws only poor-cell sets is not a page whose overlays are covering
    the mesh - they ARE the page - and hiding them leaves a reader a black rectangle with the
    information one click away and nothing on screen to say so."""
    raw = [_set("fluid/underdeterminedCells", BIG)]
    # NO cell_stats. The set is a millimetre across and a "typical cell" of 1 mm would make the
    # whole model one cell wide, which is not a mesh anyone delivers and is not what this test is
    # about - the viewer's zoom envelope is computed from those two numbers against the model box.
    surf = polymesh_response(raw, None, {}, "m")
    surf.update(is_mesh=True, cell_count=9_999,
                quality={"engine": "cfmesh_conjugate", "units": "m"},
                parts=[{"id": "poorCells:fluid/underdeterminedCells",
                        "label": "fluid/underdeterminedCells", "kind": "poorCells",
                        "selectable": True, "renderable": True,
                        "default_visibility": "visible",
                        "metadata": {"name": "fluid/underdeterminedCells", "faces": BIG}}])
    _open(live, surf)
    rows = _surfaces(live)
    only = rows["fluid/underdeterminedCells"]
    assert only["triangles"] == BIG, only
    assert only["openedOff"] is False, (
        "with no mesh to be a highlight on, a set is the page and must not be opened off")
    assert only["visible"] is True
    assert "opened off" not in _status(live)


def test_no_surface_here_is_past_the_edge_ceiling_and_the_page_never_says_one_is(live):
    """The other half of the edge statement: the CEILING note must not fire when it has nothing
    to report. Every patch here is far under _EDGE_MAX_POLYS, and the case where it DOES fire
    needs a surface past 900,000 polygons, which is measured on the real plate rather than
    shipped as a fixture.

    WHAT CHANGED UNDER THIS TEST ON 2026-11-19, and it is a property of this fixture rather than
    of the overlay rule it belongs to. The payload is a metre across with a 1 mm typical cell, so
    it is a thousand cells across, and the camera envelope frames the whole of it - which means
    it opens in the same regime the delivered plate opens in, with a cell well under one pixel.
    The viewer no longer draws cell edges there. Measured on the plate at 0.73 px per cell: the
    lines take away 41% of what a reader could otherwise resolve and cost 6.9x the frame, so
    there is nothing on screen to show for them.

    So this test now holds the thing it was always about - that the page gives the RIGHT reason.
    Too small for this framing, which a zoom undoes, and never "past 900,000 polygons", which
    nothing on this page is anywhere near. The zoom at the end is that proof."""
    _open(live, _payload())
    rows = _surfaces(live)
    assert all(r["edgeState"] == "too-small" for r in rows.values()), rows
    status = _status(live)
    assert "past 900,000 polygons" not in status, (
        "the page blamed the size of these surfaces for something the framing decided: " + status)
    assert "px at this framing" in status, status

    # and the proof that it IS the framing: ten times closer and every one of them has its edges
    live.evaluate(f"window._vdbg['{JOB}'].dolly(0.1)")
    live.evaluate(f"window._vdbg['{JOB}'].edgesAgain()")
    rows = _surfaces(live)
    assert all(r["edges"] for r in rows.values()), rows
    assert "cell edges off" not in _status(live)
