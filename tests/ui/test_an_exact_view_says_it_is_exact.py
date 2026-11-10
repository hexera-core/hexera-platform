# Responsibility: Verify in a real browser that a mesh drawn through the lossless merge says SO,
# and never borrows the merged view's wording about being a stand-in.
# Boundaries: the payload is the one the backend builds; only the network is stood in for.
from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "exact-job"

# WHY THIS TEST EXISTS. `test_a_merged_view_says_it_is_merged` holds the sentence a BUDGET-reduced
# mesh has to say: it is a stand-in, at this size, and the patch counts are the mesher's not the
# picture's. The exact route needs the opposite guard, and for a less obvious reason.
#
# Both routes merge faces, so both have a drawn count far below the delivered one, and the lazy
# reading is that they are the same thing at different ratios. They are not. The budget route
# CLUSTERS faces, which moves the surface - measured on the delivered plate it draws 98.65% of the
# coolant's area. The exact route merges only faces that already lay flat in the same plane, so the
# surface is the mesh's own to 1e-12. Telling a reader the second is a stand-in sends them to
# Inspect to check a shape that is already exact, and telling them the first is the mesh is the bug
# the sibling test exists for. The wording is the only thing that separates them on screen, because
# the pictures look nearly identical - under 1% of canvas pixels differ.


def _sheet(name: str, nx: int, ny: int, h: float = 0.001) -> dict:
    """A flat nx-by-ny sheet of quads, already merged to ONE rectangle - which is what the exact
    route does to a flat sheet, and is also the smallest honest fixture for this."""
    pts = np.array([[0, 0, 0], [nx * h, 0, 0], [nx * h, ny * h, 0], [0, ny * h, 0]],
                   dtype=np.float32)
    polys = np.array([4, 0, 1, 2, 3], dtype=np.uint32)
    return {"name": name, "type": "wall", "face_count": 1, "points": pts.tobytes(),
            "polys": polys.tobytes()}


def _payload(*, delivered: int = 14_400, polygons: int = 1) -> dict:
    surf = polymesh_response([_sheet("wall", 120, 120)], {"p10": 0.001, "typ": 0.001},
                             {"wall": "wall"}, "m")
    surf["patches"][0]["poly_count"] = polygons
    surf["patches"][0]["delivered_faces"] = delivered
    surf["exact"] = {
        "delivered_faces": delivered,
        "drawn_polygons": polygons,
        "drawn_triangles": polygons * 2,
        "collapse": delivered / polygons,
        "lossless": True,
        "area_error": 1.0e-12,
        "patches": {"wall": {"delivered_faces": delivered, "drawn_polygons": polygons,
                             "area_error": 1.0e-12}},
    }
    surf.update(is_mesh=True, parts=[],
                quality={"engine": "cfmesh_conjugate", "units": "m",
                         "boundary_faces": delivered})
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


def _text(live, el: str) -> str:
    return live.evaluate(
        f"(document.getElementById('{el}-{JOB}')||{{}}).textContent || ''") or ""


def test_an_exact_view_does_not_call_itself_a_stand_in(live):
    """The whole point. None of the merged view's wording may appear."""
    _open(live, _payload())
    status = _text(live, "v-status")
    odo = _text(live, "v-scale-o")
    assert "drawn merged" not in status, f"an exact surface claimed to be merged: {status}"
    assert "merged surface" not in odo, f"the odometer called an exact surface merged: {odo}"
    assert "stand-in" not in status
    assert_clean(live, "the viewer with an exact mesh")


def test_an_exact_view_says_it_is_exact_and_how_many_cells_a_polygon_spans(live):
    """It has to say both: that nothing was approximated, and what a polygon still hides."""
    _open(live, _payload())
    status = _text(live, "v-status")
    odo = _text(live, "v-scale-o")
    assert "drawn exactly" in status, f"no statement that this is exact: {status}"
    assert "1,297,428" not in status, "a figure from another mesh leaked into the fixture"
    assert "14,400" in status, f"it has to say how many faces it stands for: {status}"
    assert "the mesh exactly" in odo, f"the odometer has to say what the surface is: {odo}"
    # 14,400 delivered over 1 polygon, so the odometer owes the reader that number
    assert "14400" in odo.replace(",", "") or "cells" in odo, odo


def test_the_badge_reports_polygons_and_faces_as_different_things(live):
    """`face_count` is the drawn count in a served payload, so the badge must not print it as
    the mesh's. The merged route had exactly this bug and the sibling test guards its version."""
    _open(live, _payload())
    badge = _text(live, "v-count")
    assert "14,599,615" not in badge
    assert "14,400" in badge, f"the delivered count has to be on the badge: {badge}"
    assert "polygons" in badge, f"and it has to be distinguished from the drawn one: {badge}"


def test_the_patch_line_reports_delivered_faces_not_polygons(live):
    """Same rule as the merged route: the number beside a patch name is the mesh's own."""
    _open(live, _payload(delivered=14_400, polygons=1))
    status = _text(live, "v-status")
    assert "wall: 14,400 faces" in status, (
        f"the patch must report its DELIVERED faces, not the 1 drawn; got: {status}")


def test_the_help_does_not_tell_a_reader_the_shape_is_approximate(live):
    """The first help row is the one claim that depends on the payload, and it is written into the
    shell BEFORE the surface is fetched - so it is swapped afterwards or it stays wrong."""
    _open(live, _payload())
    row = live.evaluate(
        f"(document.querySelector('#v-insp-helpbox-{JOB} .v-insp-hrow')||{{}}).textContent || ''")
    assert "A stand-in" not in row, f"the help still calls an exact surface a stand-in: {row}"
    assert "The mesh itself" in row, f"the help does not say what this surface is: {row}"


def test_a_merged_payload_is_untouched_by_any_of_this(live):
    """The exact wording must not leak onto a budget-reduced mesh."""
    surf = polymesh_response([_sheet("wall", 120, 120)], {"p10": 0.001, "typ": 0.001},
                             {"wall": "wall"}, "m",
                             reduced={"delivered_faces": 14_400, "drawn_triangles": 2,
                                      "resolution_m": 0.012,
                                      "patches": {"wall": {"delivered_faces": 14_400,
                                                           "drawn_triangles": 2,
                                                           "resolution_m": 0.012}}})
    surf.update(is_mesh=True, parts=[], quality={"engine": "cfmesh_conjugate", "units": "m"})
    _open(live, surf)
    status = _text(live, "v-status")
    odo = _text(live, "v-scale-o")
    assert "drawn merged at" in status, f"a merged mesh stopped admitting it: {status}"
    assert "drawn exactly" not in status
    assert "the mesh exactly" not in odo, f"a merged mesh claimed to be exact: {odo}"
