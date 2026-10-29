"""The heatmap has to cover the metal, because the metal is the worse half.

The conjugate viewer painted the coolant only, on the grounds that the metal is untouched
background. Measured on the customer coupon, the engine's own checkMesh says the opposite in the
same report:

    fluid   max non-ortho 15.8181   avg 1.51736   max skew 0.295526   max aspect 1.61317
    solid   max non-ortho 26.9835   avg 2.03453   max skew 0.513708   max aspect 2.4443

and on the 336,666,582-cell plate the metal holds 27,932 of the 28,132 poor cells. A reader who
opens the heatmap, sees the coolant clean and concludes the mesh is clean has been shown the half
of it that is.

One response carries one `quality_fields`, so these are the merge rules.
"""
from __future__ import annotations

import inspect

from meshpipeline.engines.cfmesh_conjugate import viewer as V

#: The coupon's two regions as the quality reader hands them over, with the figures its checkMesh
#: measured on them.
FLUID = {
    "basis": "owner_cell_max",
    "patches": {"fluid/inlet": {"count": 1600, "non_ortho_b64": "A"},
                "fluid/fluid_to_solid": {"count": 346456, "non_ortho_b64": "B"}},
    "hotspots": [{"metric": "non_ortho", "value": 15.8181, "patch": "fluid/fluid_to_solid",
                  "x": 0.0, "y": 0.0, "z": 0.0}],
    "metrics": {
        "non_ortho": {"label": "non-orthogonality", "limit": 65.0, "max": 15.8181,
                      "n_over": 0, "n_faces": 7818309},
        "aspect_ratio": {"label": "aspect ratio", "limit": 1000.0, "floor": 1.0,
                         "scale_to": 2.0, "max": 1.61317, "n_over": 0, "n_faces": 7818309},
    },
}
SOLID = {
    "basis": "owner_cell_max",
    "patches": {"solid/xmin": {"count": 1928, "non_ortho_b64": "C"}},
    "hotspots": [{"metric": "non_ortho", "value": 26.9835, "patch": "solid/xmin",
                  "x": 1.0, "y": 0.0, "z": 0.0}],
    "metrics": {
        "non_ortho": {"label": "non-orthogonality", "limit": 65.0, "max": 26.9835,
                      "n_over": 0, "n_faces": 12562721},
        "aspect_ratio": {"label": "aspect ratio", "limit": 1000.0, "floor": 1.0,
                         "scale_to": 2.4443, "max": 2.4443, "n_over": 0, "n_faces": 12562721},
    },
}


def _merged():
    out: dict = {}
    V._merge_quality(out, FLUID)
    V._merge_quality(out, SOLID)
    return out


def test_the_metal_is_measured_at_all():
    """The whole point: both regions' polyMesh directories are read, not just the coolant's."""
    src = inspect.getsource(V._paint)
    assert "for region in (FLUID_REGION, SOLID_REGION)" in src
    assert src.count("attach_quality_fields") >= 1


def test_both_regions_patches_survive_the_merge():
    """Three patches went in under region-prefixed names; three come out."""
    got = _merged()
    assert set(got["patches"]) == {"fluid/inlet", "fluid/fluid_to_solid", "solid/xmin"}


def test_the_worse_max_is_the_one_reported():
    """26.9835 is the mesh's non-orthogonality, not 15.8181. Taking the coolant's would describe
    a mesh 1.7x better than the one delivered."""
    got = _merged()
    assert got["metrics"]["non_ortho"]["max"] == 26.9835
    assert got["metrics"]["aspect_ratio"]["max"] == 2.4443


def test_the_colour_range_covers_both():
    """`scale_to` spans the viewer's colours. Left at the coolant's 2.0 every metal face past it
    would be drawn at the top of the ramp and read as the worst thing in the mesh."""
    got = _merged()
    assert got["metrics"]["aspect_ratio"]["scale_to"] == 2.4443
    assert got["metrics"]["aspect_ratio"]["floor"] == 1.0


def test_the_counts_add_up():
    """A two-region mesh's face count is the two regions'. 7,818,309 + 12,562,721."""
    got = _merged()
    assert got["metrics"]["non_ortho"]["n_faces"] == 7818309 + 12562721
    assert got["metrics"]["non_ortho"]["n_over"] == 0


def test_the_bar_is_not_doubled():
    """`limit` is the same criterion on both sides - summing it would move the red line."""
    got = _merged()
    assert got["metrics"]["non_ortho"]["limit"] == 65.0
    assert got["metrics"]["aspect_ratio"]["limit"] == 1000.0


def test_every_hotspot_is_kept_with_the_patch_it_was_measured_on():
    """The hotspots are the locations a reader is sent to, and the metal's are the far ones."""
    got = _merged()
    assert len(got["hotspots"]) == 2
    assert {h["patch"] for h in got["hotspots"]} == {"fluid/fluid_to_solid", "solid/xmin"}
    assert max(h["value"] for h in got["hotspots"]) == 26.9835


def test_one_region_alone_is_not_damaged():
    """The common case on a single-region split, and on a mesh where one side would not read."""
    out: dict = {}
    V._merge_quality(out, FLUID)
    assert out["metrics"]["non_ortho"]["max"] == 15.8181
    assert out["basis"] == "owner_cell_max"
    assert set(out["patches"]) == {"fluid/inlet", "fluid/fluid_to_solid"}


def test_nothing_measured_leaves_nothing_behind():
    """A region whose mesh could not be read contributes nothing rather than an empty shell, so
    the viewer never receives a quality_fields with no numbers in it."""
    out: dict = {}
    V._merge_quality(out, None)
    V._merge_quality(out, "not a dict")
    assert out == {}


def test_a_metric_only_one_region_has_is_carried_not_dropped():
    """Skewness on one side and not the other is a real state: MAX_FACES_FOR_FIELDS can stop the
    bigger region being measured at all."""
    out: dict = {}
    V._merge_quality(out, FLUID)
    V._merge_quality(out, {"patches": {}, "hotspots": [],
                           "metrics": {"skewness": {"limit": 4.0, "max": 0.513708,
                                                    "n_over": 0, "n_faces": 12562721}}})
    assert out["metrics"]["skewness"]["max"] == 0.513708
    assert out["metrics"]["non_ortho"]["max"] == 15.8181


# #
# a mesh whose cells were merged for drawing
# #
def test_the_colours_go_but_the_places_stay(tmp_path, monkeypatch):
    """A drawn face on a merged mesh stands for several delivered ones, so a colour on it was
    measured on none of them. A hotspot is a place and a number, taken on the faces the mesher
    wrote; merging the drawing moves neither. This used to drop BOTH, on the mesh a reader most
    needs told where to look - the customer plate's coolant side is 13,022,400 faces against
    render.polymesh_budget.DRAWN_MAX_TRIANGLES of 800,000."""
    resp = {"patches": [{"name": "fluid/inlet", "type": "patch"}]}
    (tmp_path / "constant" / "fluid" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "fluid" / "polyMesh" / "boundary").write_text("0()")

    def fake(one, _polymesh, **_kw):
        one["quality_fields"] = {k: v for k, v in FLUID.items()}

    monkeypatch.setattr("meshpipeline.render.face_quality.attach_quality_fields", fake)
    V._paint(resp, tmp_path, colours=False)
    got = resp["quality_fields"]
    assert "patches" not in got, "a colour measured on no delivered face does not ship"
    assert got["metrics"]["non_ortho"]["max"] == 15.8181
    assert len(got["hotspots"]) == 1


def test_with_the_faces_as_delivered_the_colours_ship(tmp_path, monkeypatch):
    """The unmerged case is unchanged."""
    resp = {"patches": [{"name": "fluid/inlet", "type": "patch"}]}
    (tmp_path / "constant" / "fluid" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "fluid" / "polyMesh" / "boundary").write_text("0()")

    def fake(one, _polymesh, **_kw):
        one["quality_fields"] = {k: v for k, v in FLUID.items()}

    monkeypatch.setattr("meshpipeline.render.face_quality.attach_quality_fields", fake)
    V._paint(resp, tmp_path, colours=True)
    assert set(resp["quality_fields"]["patches"]) == {"fluid/inlet", "fluid/fluid_to_solid"}


def test_the_viewer_asks_for_the_places_whether_or_not_it_got_colours():
    """The browser half. The heatmap block used to require `patches` and bail without them, so a
    payload of figures and hotspots was ignored; buildAreas already makes an area per loose
    hotspot, and paintFaces already turns scalars off where there is nothing to paint."""
    from pathlib import Path as _P

    js = _P("apps/console/public/static/js/viewer/viewer.js")
    if not js.is_file():
        import pytest
        pytest.skip("the console is not in this checkout")
    src = js.read_text(encoding="utf-8")
    assert "if(!qf||!qf.metrics||!window.vtk)return;" in src, "patches must not be required"
    assert "if(!qf||!qf.patches||!qf.metrics" not in src
    assert "const p=(qf.patches||{})[en.patch]" in src, "absent patches is not an error"
    assert ("if(!entries.some(en=>Object.keys(en.q).length)"
            "&&!(qf.hotspots||[]).length)return;") in src
