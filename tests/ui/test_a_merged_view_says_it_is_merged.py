# Responsibility: Verify in a real browser that a mesh drawn under the viewer's budget says so,
# and still reports the faces the mesher delivered rather than the ones on screen.
# Boundaries: the payload is the one the backend builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.polymesh_budget import fit_to_budget
from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "merged-job"

# WHY THIS TEST EXISTS. The delivered-mesh path had no size budget, so a 13,022,400-face customer
# mesh packed to 555.8 MB of base64 and no browser ever saw it. Now the server merges cells for
# drawing - and the moment it does, `face_count` on a patch stops being the mesh's own measurement
# of itself. The first version of the status line printed it anyway, which told a reader their mesh
# was twenty times coarser than the one they paid for. This is the guard on that sentence.


def _grid(name: str, nx: int, ny: int, h: float = 0.001) -> dict:
    """A flat nx-by-ny sheet of quads in the (points, polys) form a boundary reader hands over."""
    xs = np.arange(nx + 1) * h
    ys = np.arange(ny + 1) * h
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1).astype(np.float32)
    pid = lambda i, j: i * (ny + 1) + j                                   # noqa: E731
    quads = [[4, pid(i, j), pid(i + 1, j), pid(i + 1, j + 1), pid(i, j + 1)]
             for i in range(nx) for j in range(ny)]
    return {"name": name, "type": "wall", "face_count": nx * ny,
            "points": pts.tobytes(),
            "polys": np.asarray(quads, dtype=np.uint32).reshape(-1).tobytes()}


def _payload(*, budget: int) -> dict:
    raw, note = fit_to_budget([_grid("wall", 120, 120)], max_triangles=budget)
    surf = polymesh_response(raw, {"p10": 0.001, "typ": 0.001}, {"wall": "wall"}, "m",
                             reduced=note)
    surf.update(is_mesh=True, cell_count=14_400, parts=[],
                quality={"cells": 14_400, "faces": 14_400, "engine": "cfmesh_conjugate",
                         "units": "m", "max_non_ortho": 0.0, "max_skewness": 0.0})
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
    })()""".replace("__PAYLOAD__", json.dumps(payload)).replace("__JOB__", JOB), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{JOB}']", timeout=90,
                  what="the viewer to initialise")


def _status(live) -> str:
    return live.evaluate(
        f"document.getElementById('v-status-{JOB}').textContent") or ""


def test_a_merged_view_reports_the_faces_the_mesher_delivered(live):
    """The number beside the patch name is the mesh's, not the picture's."""
    payload = _payload(budget=2_000)
    assert payload.get("reduced"), "the fixture has to be past the budget or this proves nothing"
    drawn = payload["patches"][0]["face_count"]
    assert drawn < 14_400, "and it has to have actually been reduced"

    _open(live, payload)
    text = _status(live)
    assert "wall: 14,400 faces" in text, (
        f"the patch must report its DELIVERED faces, not the {drawn:,} drawn; got: {text}")
    assert f"{drawn:,} faces" not in text.split("drawn merged")[0], (
        "the drawn count must not appear where a reader reads the delivered one")
    assert_clean(live, "the viewer with a merged mesh")


def test_a_merged_view_says_it_is_merged_and_at_what_size(live):
    """A preview presented as the delivered mesh is the failure this guards against."""
    payload = _payload(budget=2_000)
    red = payload["reduced"]
    _open(live, payload)
    text = _status(live)
    assert "drawn merged at" in text, f"no admission that this is a merged view: {text}"
    assert f"{red['drawn_triangles']:,}" in text, "it has to say how much is on screen"
    assert f"{red['delivered_faces']:,}" in text, "and against what"
    assert "mm" in text.split("drawn merged at")[1][:12], (
        f"the size the cells were merged at has to be on screen: {text}")


def test_a_mesh_that_fits_claims_nothing(live):
    """The other half. A mesh drawn exactly as delivered must not carry a merge notice."""
    payload = _payload(budget=1_000_000)
    assert "reduced" not in payload, "this fixture is supposed to fit the budget whole"
    _open(live, payload)
    text = _status(live)
    assert "wall: 14,400 faces" in text
    assert "drawn merged" not in text, f"an untouched mesh claimed to be merged: {text}"
    assert_clean(live, "the viewer with an unreduced mesh")
