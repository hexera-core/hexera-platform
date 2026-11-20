# Responsibility: Hold the two places that describe one drawn polygon - the status line and the
# size legend's odometer - to the same statement, and hold that statement to the payload's own
# per-polygon distribution rather than to a mean of it.
# Boundaries: one 400-quad sheet with a declared distribution; only the network is stood in for.
"""TWO LINES ON ONE PAGE SAID DIFFERENT THINGS ABOUT THE SAME PAYLOAD.

Measured on the delivered cold plate (job 6dd6703c) on 2026-11-19, both read off the live page at
the same moment:

    status line   "a line is one drawn polygon, which here is one delivered face except where a
                   flat run of them tiles a single rectangle"
    odometer      "1 px = 274 um - the mesh exactly - one polygon spans about 16 cells"

A reader cannot hold both. The status line was right. 16.1 is `delivered_faces / drawn_polygons`,
an arithmetic mean over a distribution that has no middle - counted off `faces_per_polygon` for
all 1,059,668 polygons that payload serves:

    exactly one delivered face   90.3449% of polygons      median 1     p90 1
    p99 140 faces                p99.9 3,360               largest 32,867
    the top 1,000 polygons (0.094% of them) hold 43.66% of all 17,070,364 delivered faces

So nine polygons in ten ARE one cell and the mean belongs to a thousand enormous flat runs. "About
16 cells" is true of almost no polygon a reader can put their cursor on.

THE FIX IS NOT A WORDING CHANGE, it is one source. `mergeSpread` counts the distribution once and
both lines are built from it (`polygonSpanWords`), so the two cannot drift apart again - which is
what this file holds. A payload that does not carry the distribution still gets the mean, but with
the word "average" in it, because that is what it is.
"""
from __future__ import annotations

import base64
import json

import numpy as np
import pytest
from conftest import assert_clean

from meshpipeline.render.viewer_pack import polymesh_response

pytestmark = pytest.mark.ui

JOB = "one-polygon-job"

#: THE SHAPE OF THE REAL PLATE'S DISTRIBUTION, at 400 polygons instead of 1,059,668: nine in ten
#: are a single delivered face, a tail of merged runs, and one enormous rectangle. The largest is
#: the plate's own figure because it is the number that makes the point - a single drawn polygon
#: standing for 32,867 cells.
ONES = 361
TAIL, TAIL_FACES = 38, 100
LARGEST = 32_867
POLYGONS = ONES + TAIL + 1
DELIVERED = ONES + TAIL * TAIL_FACES + LARGEST
#: ...and what a mean of that would have said. 92.6 faces per polygon, true of ONE of the 400.
MEAN = DELIVERED / POLYGONS


def _grid(name: str, nx: int, ny: int, h: float = 0.001) -> dict:
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


def _faces_per_polygon() -> str:
    a = np.concatenate([np.ones(ONES, dtype=np.uint32),
                        np.full(TAIL, TAIL_FACES, dtype=np.uint32),
                        np.asarray([LARGEST], dtype=np.uint32)])
    assert a.size == POLYGONS and int(a.sum()) == DELIVERED
    return base64.b64encode(a.tobytes()).decode("ascii")


def _payload(*, distribution: bool) -> dict:
    surf = polymesh_response([_grid("wall", 20, 20)], {"p10": 0.001, "typ": 0.001},
                             {"wall": "wall"}, "m")
    assert surf is not None and surf["kind"] == "polymesh"
    surf["exact"] = {"delivered_faces": DELIVERED, "drawn_polygons": POLYGONS,
                     "drawn_triangles": POLYGONS * 2, "collapse": MEAN, "lossless": True,
                     "area_error": 1.0e-15, "max_vertex_shift_m": 0.0,
                     "patches": {"wall": {"delivered_faces": DELIVERED,
                                          "drawn_polygons": POLYGONS,
                                          "area_error": 1.0e-15}}}
    if distribution:
        # `metrics` has to exist or the heatmap closure never reaches its own guard; it is empty
        # because no metric is under test here, only the per-polygon counts beside them.
        surf["quality_fields"] = {"metrics": {}, "hotspots": [],
                                  "patches": {"wall": {"faces_per_polygon_b64":
                                                       _faces_per_polygon()}}}
    surf.update(is_mesh=True, parts=[], quality={"engine": "cfmesh_conjugate", "units": "m"})
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


def _text(live, el: str) -> str:
    return live.evaluate(
        f"(document.getElementById('{el}-{JOB}')||{{}}).textContent || ''") or ""


#: the one sentence both lines have to carry, built here from the fixture's own numbers so the
#: test states the claim rather than quoting whatever the page happens to print
AGREED = "9 polygons in 10 are one delivered cell, the largest 32,867"


def test_the_odometer_describes_the_polygon_a_reader_can_point_at(live):
    """THE DEFECT. The odometer stated a mean as if it were typical, and on this distribution the
    mean is true of one polygon in 400."""
    _open(live, _payload(distribution=True))
    odo = _text(live, "v-scale-o")
    assert AGREED in odo, f"the odometer does not describe the typical polygon: {odo}"
    assert "spans about" not in odo, (
        f"the odometer is still claiming a mean is what one polygon is: {odo}")
    assert f"about {round(MEAN)} cells" not in odo, odo
    assert "the mesh exactly" in odo, f"an exact surface stopped saying so: {odo}"
    assert_clean(live, "the viewer with a per-polygon distribution")


def test_the_status_line_makes_the_same_statement_in_the_same_words(live):
    """One source, so there is nothing left to disagree. Both are built from `mergeSpread`."""
    _open(live, _payload(distribution=True))
    status = _text(live, "v-status")
    odo = _text(live, "v-scale-o")
    assert "cell edges on every surface" in status, (
        f"the edge note is not on screen, so this test is asserting nothing: {status}")
    assert AGREED in status, f"the status line does not carry the agreed statement: {status}"
    assert AGREED in odo
    assert "except where a flat run of them tiles a single rectangle" not in status, (
        "the status line is still describing the merge in words the odometer cannot match: "
        + status)


def test_neither_line_reports_a_mean_as_a_typical_polygon(live):
    """The arithmetic the defect came from, named so it cannot come back under another wording."""
    _open(live, _payload(distribution=True))
    both = _text(live, "v-status") + " || " + _text(live, "v-scale-o")
    assert str(round(MEAN)) not in both, (
        f"the mean ({MEAN:.1f} faces per polygon, true of 1 polygon in {POLYGONS}) is on screen "
        f"as though it described one: {both}")


def test_a_payload_without_the_distribution_gets_the_mean_and_is_told_it_is_one(live):
    """The honest fallback. A mean is still the only thing such a payload can say - what it may
    not do is say it the way the plate's odometer used to, as a fact about one polygon."""
    _open(live, _payload(distribution=False))
    odo = _text(live, "v-scale-o")
    assert f"one polygon spans {round(MEAN)} cells on average" in odo, odo
    assert "spans about" not in odo, odo
    assert "polygons in 10" not in odo, (
        f"a payload that cannot count its polygons claimed to have counted them: {odo}")
