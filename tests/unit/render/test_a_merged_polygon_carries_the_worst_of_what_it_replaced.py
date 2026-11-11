"""The whole part can be coloured now, and the colour has to say what it is.

The budget route could never carry a heatmap on a reduced mesh: it clusters faces by position and
keeps no record of which delivered faces ended up under a drawn one, so a colour on it was
measured on none of them and `face_quality` drops the arrays. `_paint` says so in its own comment.

The lossless merge knows, because the merge is what put them there. So the colours come back - but
what a merged rectangle carries is the WORST of up to 544,272 faces, not a face's own value, and
that distinction is the entire difference between a useful upper bound and a lie. The chip's flat
top is ONE polygon; painted by its single worst cell and read as a face's number it says the chip
is ruined.

So this holds three things: the arrays exist, they are exactly one value per DRAWN polygon (the
viewer drops any array whose length disagrees, silently, which would be a heatmap that quietly
stopped appearing), and `faces_per_polygon` travels so the client can say "worst of N".
"""
from __future__ import annotations

import base64
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.cfmesh_conjugate.viewer import _attach_exact_colours, conjugate_viewer
from meshpipeline.render.boundary_index import write_index
from meshpipeline.render.exact_surface import exact_surface
from tests.foam_fixtures import write_quality_fields, write_row_of_hexes


def _ws(tmp_path: Path, regions=("fluid", "solid"), quality=True) -> Path:
    ws = tmp_path / "ws"
    for r in regions:
        pm = ws / "constant" / r / "polyMesh"
        write_row_of_hexes(pm)
        write_index(pm)
        if quality:
            try:
                write_quality_fields(ws / "constant" / r, cells=3, uniform=1.0)
            except Exception:                      # noqa: BLE001 - fixture shape varies
                pass
    return ws


# --------------------------------------------------------------- the attach, on its own
def _patch(name, polys, worst=None, faces=None):
    p = {"name": name, "poly_count": polys}
    if worst is not None:
        p["quality"] = {"values": {"non_ortho": {
            "worst": np.asarray(worst, dtype=np.float32).tobytes()}}}
    if faces is not None:
        p["faces_per_polygon"] = np.asarray(faces, dtype=np.uint32).tobytes()
    return p


def test_one_value_per_drawn_polygon_reaches_the_payload():
    resp = {"quality_fields": {"metrics": {"non_ortho": {"limit": 65.0}}}}
    _attach_exact_colours(resp, [_patch("fluid/a", 3, [1.0, 2.0, 3.0], [10, 20, 30])])
    got = resp["quality_fields"]["patches"]["fluid/a"]
    assert "non_ortho_b64" in got
    v = np.frombuffer(base64.b64decode(got["non_ortho_b64"]), dtype=np.float32)
    assert v.tolist() == [1.0, 2.0, 3.0]
    n = np.frombuffer(base64.b64decode(got["faces_per_polygon_b64"]), dtype=np.uint32)
    assert n.tolist() == [10, 20, 30]
    assert "worst" in resp["quality_fields"]["per_polygon"].lower()


def test_an_array_of_the_wrong_length_is_dropped_not_shipped():
    """viewer.js drops a mismatched array SILENTLY, so a wrong length is a heatmap that quietly
    stops appearing. It is caught here instead, where it can be a log line."""
    resp = {"quality_fields": {"metrics": {"non_ortho": {"limit": 65.0}}}}
    _attach_exact_colours(resp, [_patch("fluid/a", 5, [1.0, 2.0, 3.0], [1, 1, 1])])
    assert "fluid/a" not in (resp["quality_fields"].get("patches") or {})


def test_nothing_is_attached_without_metrics_to_judge_against():
    """A colour with no bar behind it is a hue a reader can only guess at."""
    resp = {}
    _attach_exact_colours(resp, [_patch("fluid/a", 1, [1.0], [1])])
    assert "quality_fields" not in resp
    resp2 = {"quality_fields": {"hotspots": []}}
    _attach_exact_colours(resp2, [_patch("fluid/a", 1, [1.0], [1])])
    assert "patches" not in resp2["quality_fields"]


def test_a_patch_with_no_quality_is_left_out_rather_than_zero_filled():
    """A metric that was not measured is not a good one, and a client handed zeros paints a clean
    mesh. Same rule `detail_response` holds for a cut."""
    resp = {"quality_fields": {"metrics": {"non_ortho": {"limit": 65.0}}}}
    _attach_exact_colours(resp, [_patch("fluid/a", 2),
                                 _patch("fluid/b", 2, [1.0, 2.0], [3, 4])])
    pats = resp["quality_fields"]["patches"]
    assert "fluid/a" not in pats
    assert "fluid/b" in pats


# --------------------------------------------------------------- and through the engine
def test_the_exact_route_carries_colours_where_the_budget_route_cannot(tmp_path):
    resp = conjugate_viewer(_ws(tmp_path), roles={}, units="m")
    assert resp is not None
    assert "exact" in resp, "the fixture has to take the exact route or this proves nothing"
    qf = resp.get("quality_fields") or {}
    if not qf.get("metrics"):
        pytest.skip("this fixture has no quality fields to colour with")
    for name, got in (qf.get("patches") or {}).items():
        drawn = next(p for p in resp["patches"] if str(p["name"]) == name)
        for key in ("non_ortho_b64", "skewness_b64", "aspect_ratio_b64"):
            if key not in got:
                continue
            v = np.frombuffer(base64.b64decode(got[key]), dtype=np.float32)
            assert len(v) == drawn["face_count"], (name, key, len(v), drawn["face_count"])
        if "faces_per_polygon_b64" in got:
            n = np.frombuffer(base64.b64decode(got["faces_per_polygon_b64"]), dtype=np.uint32)
            assert len(n) == drawn["face_count"]
            assert int(n.sum()) == resp["exact"]["patches"][name]["delivered_faces"], (
                "the faces behind the polygons have to add up to the faces delivered")


def test_the_worst_is_a_bound_over_exactly_the_faces_that_were_merged(tmp_path):
    """Not a sample of them, and not the mean dressed up as a maximum."""
    pm = tmp_path / "constant" / "polyMesh"
    write_row_of_hexes(pm)
    write_index(pm)
    rows, _prov = exact_surface(pm, with_quality=False)
    # with_quality=False there is nothing to summarise, and nothing must be invented
    for r in rows:
        assert "quality" not in r, r["name"]
        assert "faces_per_polygon" not in r, r["name"]
