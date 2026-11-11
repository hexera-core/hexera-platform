"""The conjugate engine's viewer draws exactly when it can, and says which route it took.

`render.exact_surface` being correct is held next door. This is the other half: that the engine
every customer mesh goes through actually USES it, that it falls back rather than failing when it
cannot, and that the payload never claims a route it did not take.

THE CLAIM IS ALL-OR-NOTHING ACROSS REGIONS, which is the case worth a test of its own. A conjugate
payload that is exact on the coolant and clustered on the metal is not an exact payload, and
labelling it one would be the merged view's own bug in better wording - a reader told the surface
is the mesh would measure a wall thickness on the half that is a stand-in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate.viewer import (_exact_total, _triangles,
                                                          conjugate_viewer)
from meshpipeline.render.boundary_index import write_index
from tests.foam_fixtures import write_row_of_hexes


def _ws(tmp_path: Path, regions=("fluid", "solid"), **kw) -> Path:
    ws = tmp_path / "ws"
    for r in regions:
        pm = ws / "constant" / r / "polyMesh"
        write_row_of_hexes(pm, **kw)
        write_index(pm)
    return ws


def test_a_conjugate_mesh_is_drawn_exactly_and_says_so(tmp_path):
    resp = conjugate_viewer(_ws(tmp_path), roles={}, units="m")
    assert resp is not None
    assert "exact" in resp, "the product path did not take the lossless route"
    assert "reduced" not in resp, "it merged to a budget as well as merging losslessly"
    ex = resp["exact"]
    assert ex["lossless"] is True
    # three unit cubes have fourteen boundary faces and six flat sides, per region
    assert ex["delivered_faces"] == 28
    assert ex["drawn_polygons"] == 12
    assert ex["max_vertex_shift_m"] == 0.0, "a merged rectangle moved a corner"
    assert ex["area_error"] < 1e-12


def test_the_per_patch_block_is_keyed_by_the_name_the_payload_draws(tmp_path):
    """viewer.js looks this up by the DRAWN patch name, which carries the region prefix.

    `exact_surface` is called without `region=` so its own keys are bare; if that leaked, every
    lookup would miss and every patch would print its drawn count as the mesh's own.
    """
    resp = conjugate_viewer(_ws(tmp_path), roles={}, units="m")
    drawn = {str(p["name"]) for p in resp["patches"]}
    keys = set(resp["exact"]["patches"])
    assert drawn, "nothing was drawn"
    assert drawn <= keys, "patches with no entry: %s" % sorted(drawn - keys)
    for name in drawn:
        assert "/" in name, name
        assert resp["exact"]["patches"][name]["delivered_faces"] > 0


def test_face_count_is_the_drawn_count_and_the_mesh_s_own_is_kept(tmp_path):
    """The convention `fit_to_budget` sets. Skipping the budget means nothing else sets it.

    The mesh's own count is NOT on the patch - `polymesh_response` builds each patch dict
    explicitly and drops anything it does not name - so it has to be reachable in the provenance
    block, which is where `reduced` keeps its copy and where viewer.js looks.
    """
    resp = conjugate_viewer(_ws(tmp_path), roles={}, units="m")
    assert resp["patches"], "nothing was drawn"
    for p in resp["patches"]:
        got = resp["exact"]["patches"][str(p["name"])]
        assert p["face_count"] == got["drawn_polygons"], str(p["name"])
        assert got["delivered_faces"] >= p["face_count"], str(p["name"])
    assert sum(p["face_count"] for p in resp["patches"]) == resp["exact"]["drawn_polygons"]


def test_a_tight_budget_still_reduces_and_admits_both_steps(tmp_path, monkeypatch):
    """The lossless merge is not a licence to ignore the budget on a mesh that is still too big.

    With the budget below what even the exact surface needs, `fit_to_budget` has to run - on the
    exact polygons, which is strictly better input than the raw faces - and the payload has to
    carry `reduced` and NOT claim to be exact.
    """
    import meshpipeline.render.polymesh_budget as budget
    monkeypatch.setattr(budget, "DRAWN_MAX_TRIANGLES", 4)
    # `fit_to_budget` takes its limit as a DEFAULT ARGUMENT, bound when the function was defined,
    # so patching the module constant alone does not reach it - and on a fourteen-face fixture it
    # would have nothing to reduce anyway. What this test is actually for is that the budget gets
    # its turn and the exact claim is withheld, so it watches the call rather than its output.
    called = []
    real = budget.fit_to_budget
    monkeypatch.setattr(budget, "fit_to_budget",
                        lambda raw, **kw: (called.append(len(raw)), real(raw, **kw))[1])
    resp = conjugate_viewer(_ws(tmp_path), roles={}, units="m")
    assert resp is not None
    assert "exact" not in resp, "it claimed to be exact while over the budget"
    assert called, "the budget never ran on a surface that was over it"
    assert resp["patches"], "the fallback lost the mesh"


def test_the_triangle_count_is_read_off_the_stream(tmp_path):
    """An n-gon fans to n - 2, so 2 x polygons is right only for an all-quad patch."""
    import numpy as np
    quad = np.array([4, 0, 1, 2, 3], dtype=np.uint32).tobytes()
    hexa = np.array([6, 0, 1, 2, 3, 4, 5], dtype=np.uint32).tobytes()
    assert _triangles({"polys": quad}) == 2
    assert _triangles({"polys": hexa}) == 4
    assert _triangles({"polys": quad + hexa}) == 6
    assert _triangles({}) == 0


def test_one_region_failing_disqualifies_the_whole_claim(tmp_path):
    """A payload exact on one region and clustered on the other must not say it is exact."""
    ws = _ws(tmp_path)
    # take the index away from the metal: that region falls back to a route with no exact note
    import shutil
    shutil.rmtree(ws / "constant" / "solid" / "polyMesh" / "boundaryIndex")
    resp = conjugate_viewer(ws, roles={}, units="m")
    assert resp is not None, "losing one index must not lose the payload"
    assert "exact" not in resp, (
        "the coolant was drawn exactly and the metal was not, and it claimed to be exact")


def test_exact_total_maximises_the_shift_rather_than_summing_it(tmp_path):
    """A displacement is a maximum over regions. Summing it would report a distance nothing moved."""
    notes = {"fluid": {"max_vertex_shift_m": 0.0, "area_error": 1e-16},
             "solid": {"max_vertex_shift_m": 3e-9, "area_error": 2e-16}}
    raw = [{"name": "fluid/a", "face_count": 10, "poly_count": 2, "exact": {}},
           {"name": "solid/b", "face_count": 20, "poly_count": 4, "exact": {}}]
    got = _exact_total(notes, raw, triangles=12)
    assert got["max_vertex_shift_m"] == pytest.approx(3e-9)
    assert got["area_error"] == pytest.approx(2e-16)
    assert got["delivered_faces"] == 30
    assert got["drawn_polygons"] == 6
    assert set(got["patches"]) == {"fluid/a", "solid/b"}
