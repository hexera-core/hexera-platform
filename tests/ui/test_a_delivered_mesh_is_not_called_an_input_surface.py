# Responsibility: Verify in a real browser that a delivered polyMesh is described as the delivered
# mesh, and that an input skin is still described as an input skin.
# Boundaries: the payload is the one the backend's own packer builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response, stl_response

pytestmark = pytest.mark.ui

JOB = "kind-job"

# WHY THIS TEST EXISTS. The status line decided "is this the delivered mesh?" by reading
# `surf.is_mesh` alone. `render/viewer_pack` does not set that key - its own comment says so, for
# `kind=stl`, because stl serves BOTH gmsh's delivered surface groups and the API's fallback input
# skin and the packer cannot tell them apart. `application/viewer_payload` stamps it on afterwards.
# So a polyMesh payload that reaches the browser without that one optional key makes the viewer
# tell the reader "the mesh itself could not be rendered" directly above a correctly rendered mesh,
# listing its patches and its merge resolution. Measured on the first customer cold plate
# (283,410,955 cells) on 2026-11-17: the caption claimed the mesh could not be drawn while
# 524,190 of its 17,070,364 boundary faces were on screen.
#
# `kind` is the decisive fact and two other places in this codebase already say so in the same
# words - `api/v1/simulation.get_surface_vtk` and viewer.js's own vtkExport guard both read
# `kind == "polymesh" or (kind == "stl" and is_mesh is True)`. A polyMesh boundary is never an
# input skin: the input-skin path produces `kind` of `stl` or `skin`, never `polymesh`. This test
# holds the caption to the same rule, so the three cannot drift apart again.
#
# NOTE ON THE OTHER FIXTURES IN THIS DIRECTORY: five of them call `surf.update(is_mesh=True, ...)`
# on a polymesh payload by hand. That is why this was never caught - every existing test fabricates
# the field whose absence is the bug. Nothing here sets it.


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


def _triangles(n: int) -> list:
    """n degenerate-free triangles, the shape `stl_response` wants."""
    return [[[0.0, 0.0, float(i)], [1.0, 0.0, float(i)], [0.0, 1.0, float(i)]] for i in range(n)]


def _polymesh_without_is_mesh() -> dict:
    """Exactly what `viewer_pack.polymesh_response` returns, with nothing stamped on afterwards."""
    surf = polymesh_response([_grid("wall", 20, 20)], {"p10": 0.001, "typ": 0.001},
                             {"wall": "wall"}, "m")
    assert surf is not None
    assert surf["kind"] == "polymesh"
    assert "is_mesh" not in surf, "the packer is not supposed to set it; this fixture depends on that"
    surf.update(parts=[], quality={"engine": "cfmesh_conjugate", "units": "m"})
    return surf


def _input_skin() -> dict:
    """The API's fallback INPUT skin: kind=stl and explicitly not a mesh."""
    surf = stl_response({"wall": _triangles(8)}, {"wall": "wall"}, "m", ())
    assert surf is not None and surf["kind"] == "stl"
    surf.update(is_mesh=False, cell_count=0, parts=[])
    return surf


def _delivered_stl() -> dict:
    """A tetrahedral engine's DELIVERED surface (gmsh, vmtk): kind=stl marked is_mesh."""
    surf = stl_response({"wall": _triangles(8)}, {"wall": "wall"}, "m", ())
    assert surf is not None and surf["kind"] == "stl"
    surf.update(is_mesh=True, cell_count=1_234, parts=[],
                quality={"engine": "gmsh", "units": "m"})
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
    return live.evaluate(f"document.getElementById('v-status-{JOB}').textContent") or ""


def test_a_polymesh_is_the_delivered_mesh_whatever_else_the_payload_carries(live):
    """kind=polymesh is decisive. A missing optional key must not turn a mesh into a non-mesh."""
    payload = _polymesh_without_is_mesh()
    _open(live, payload)
    text = _status(live)
    assert "could not be rendered" not in text, (
        "the viewer told the reader its mesh could not be rendered, directly above the rendered "
        f"mesh and its patch list: {text}")
    assert "your delivered mesh" in text, (
        f"a polyMesh boundary is a delivered mesh and nothing else can produce one: {text}")
    assert "wall: 400 faces" in text, f"and it still has to report the patch: {text}"
    assert_clean(live, "the viewer with a polymesh payload carrying no is_mesh")


def test_an_input_skin_is_still_called_an_input_skin(live):
    """The other half, so the rule above cannot be satisfied by calling everything a mesh."""
    payload = _input_skin()
    _open(live, payload)
    text = _status(live)
    assert "could not be rendered" in text, (
        f"an INPUT skin shown in place of a mesh must say so: {text}")
    assert "your delivered mesh" not in text, f"an input skin claimed to be the mesh: {text}"
    assert_clean(live, "the viewer with an input skin")


def test_a_tetrahedral_engines_delivered_surface_is_the_delivered_mesh(live):
    """kind=stl marked is_mesh - gmsh and vmtk - is the one case where that key is the fact."""
    payload = _delivered_stl()
    _open(live, payload)
    text = _status(live)
    assert "your delivered mesh" in text, (
        f"a tetrahedral engine's delivered surface is the mesh it built: {text}")
    assert "could not be rendered" not in text, text
    assert_clean(live, "the viewer with a delivered stl surface")
