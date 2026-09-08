# Responsibility: Verify the per-face quality fields the viewer colours its heatmap with are
# measured right, aligned with the polygons the viewer draws, and never cost the user their mesh.
from __future__ import annotations

import base64
from pathlib import Path

import numpy as np
import pytest
from tests.foam_fixtures import ROW_SHEAR as SHEAR
from tests.foam_fixtures import ROW_SHEAR_NON_ORTHO_DEG as EXPECTED_DEG
from tests.foam_fixtures import write_row_of_hexes

from meshpipeline.render import face_quality as FQ

ROOT = Path(__file__).parents[3]


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


# ------------------------------------------------------------------- the metrics ----
def test_an_orthogonal_mesh_reads_zero_everywhere_and_has_no_hotspots(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh")
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    assert q is not None and q["basis"] == "owner_cell_max"
    for name in ("inlet", "outlet", "wall"):
        assert np.allclose(_f32(q["patches"][name]["non_ortho_b64"]), 0.0, atol=1e-6)
        assert np.allclose(_f32(q["patches"][name]["skewness_b64"]), 0.0, atol=1e-6)
    assert q["hotspots"] == []
    assert q["metrics"]["non_ortho"]["n_over"] == 0
    assert q["metrics"]["non_ortho"]["limit"] == 65.0


def test_a_sheared_cell_shows_on_the_faces_of_the_cells_that_touch_it(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh", shear_last_y=SHEAR)
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    # the internal face between cells 1 and 2 is the non-orthogonal one; cell 0 never touches it
    assert _f32(q["patches"]["inlet"]["non_ortho_b64"])[0] == pytest.approx(0.0, abs=1e-6)
    assert _f32(q["patches"]["outlet"]["non_ortho_b64"])[0] == pytest.approx(EXPECTED_DEG, abs=0.05)
    wall = _f32(q["patches"]["wall"]["non_ortho_b64"])
    assert np.allclose(wall[:4], 0.0, atol=1e-6)                       # cell 0's four sides
    assert np.allclose(wall[4:], EXPECTED_DEG, atol=0.05)              # cells 1 and 2
    assert q["metrics"]["non_ortho"]["max"] == pytest.approx(EXPECTED_DEG, abs=0.05)
    assert q["metrics"]["non_ortho"]["n_over"] == 1
    # The sheared outlet face's OWN skewness is 5 - a boundary measurement checkMesh allows up
    # to 20. It must not be projected onto the cell and painted red against the internal bar
    # of 4 while the summary honestly reports nothing over.
    assert q["metrics"]["skewness"]["n_over"] == 0
    assert _f32(q["patches"]["outlet"]["skewness_b64"])[0] < 4.0


def test_the_hotspot_names_the_offending_face_and_where_it_is(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh", shear_last_y=SHEAR)
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    hot = [h for h in q["hotspots"] if h["metric"] == "non_ortho"]
    assert len(hot) == 1
    h = hot[0]
    assert h["value"] == pytest.approx(EXPECTED_DEG, abs=0.05)
    assert (h["x"], h["y"], h["z"]) == pytest.approx((2.0, 0.5, 0.5))   # the x=2 face centre
    assert h["patch"] is None                                            # an internal face
    assert h["cell_faces"] == 6                                          # its owner is a hex


def test_the_bar_is_the_callers_and_a_face_under_it_is_no_hotspot(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh", shear_last_y=SHEAR)
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=70.0)
    assert q["metrics"]["non_ortho"]["limit"] == 70.0
    assert q["metrics"]["non_ortho"]["n_over"] == 0
    assert [h for h in q["hotspots"] if h["metric"] == "non_ortho"] == []


# ------------------------------------------------ alignment with what the viewer draws ----
def test_every_array_is_aligned_with_the_polygons_the_viewer_ships(tmp_path):
    from meshpipeline.engines.snappy.polymesh_surface import boundary_surface
    info = write_row_of_hexes(tmp_path / "polyMesh", degenerate_wall_face=True)
    raw, _ = boundary_surface(tmp_path / "polyMesh")
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    drawn = {p["name"]: p["face_count"] for p in raw}
    assert drawn["wall"] == info["n_wall"] - 1, "the surface reader drops the 2-vertex face"
    for name, n in drawn.items():
        for key in ("non_ortho_b64", "skewness_b64"):
            assert len(_f32(q["patches"][name][key])) == n, f"{name}.{key} misaligned"
        assert len(base64.b64decode(q["patches"][name]["cell_faces_b64"])) == n
        assert q["patches"][name]["count"] == n


# ------------------------------------------------------------- it never costs a mesh ----
def test_a_missing_or_binary_mesh_yields_no_fields_not_an_error(tmp_path):
    assert FQ.quality_fields(tmp_path / "nowhere", non_ortho_limit=65.0) is None
    write_row_of_hexes(tmp_path / "polyMesh")
    pts = tmp_path / "polyMesh" / "points"
    pts.write_text(pts.read_text().replace("format      ascii", "format      binary"))
    assert FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0) is None


def test_a_mesh_over_the_size_cap_is_skipped(tmp_path, monkeypatch):
    write_row_of_hexes(tmp_path / "polyMesh")
    monkeypatch.setattr(FQ, "MAX_FACES_FOR_FIELDS", 3)
    assert FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0) is None


def test_attach_swallows_any_failure_and_leaves_the_response_intact(tmp_path, monkeypatch):
    resp = {"kind": "polymesh", "patches": [{"name": "wall"}]}

    def boom(*a, **k):
        raise RuntimeError("numpy fell over")
    monkeypatch.setattr(FQ, "quality_fields", boom)
    FQ.attach_quality_fields(resp, tmp_path / "polyMesh", non_ortho_limit=65.0)
    assert "quality_fields" not in resp
    assert resp["patches"] == [{"name": "wall"}]


# ------------------------------------------------------ through the engine's own hook ----
def test_the_snappy_viewer_ships_fields_matching_its_own_patches(tmp_path):
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", shear_last_y=SHEAR)
    resp = polymesh_viewer(tmp_path, roles={"wall": "wall"}, units="m")
    assert resp and "quality_fields" in resp
    q = resp["quality_fields"]
    for p in resp["patches"]:
        assert len(_f32(q["patches"][p["name"]]["non_ortho_b64"])) == p["face_count"]
    # the red line the viewer draws is the engine's own criterion
    from meshpipeline.engines.openfoam_criteria import MAX_NON_ORTHO
    assert q["metrics"]["non_ortho"]["limit"] == float(MAX_NON_ORTHO.threshold)


def test_every_openfoam_family_viewer_attaches_the_fields():
    for engine in ("snappy", "cfmesh", "snappy_multiregion"):
        src = (ROOT / "src" / "meshpipeline" / "engines" / engine / "viewer.py").read_text()
        assert "attach_quality_fields(" in src, f"{engine} ships a mesh with no heatmap data"
        assert "MAX_NON_ORTHO.threshold" in src, f"{engine} paints a bar its gate never drew"


def test_render_stays_free_of_engine_knowledge():
    src = (ROOT / "src" / "meshpipeline" / "render" / "face_quality.py").read_text()
    assert "meshpipeline.engines" not in src, "render/ imports an engine - the layering is broken"


# ------------------------------------------------------------------- aspect ratio ----
def test_aspect_ratio_reads_one_on_unit_cubes_and_the_stretch_on_a_brick(tmp_path):
    # checkMesh's number: a cube is 1, a 10:1:1 brick is 10 (the component ratio wins over the
    # area/volume term, which reads 1.5 there)
    write_row_of_hexes(tmp_path / "polyMesh", last_cell_length=10.0)
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    assert _f32(q["patches"]["inlet"]["aspect_ratio_b64"])[0] == pytest.approx(1.0, abs=1e-5)
    assert _f32(q["patches"]["outlet"]["aspect_ratio_b64"])[0] == pytest.approx(10.0, abs=1e-4)
    wall = _f32(q["patches"]["wall"]["aspect_ratio_b64"])
    assert wall[:4] == pytest.approx(1.0, abs=1e-5) and wall[-4:] == pytest.approx(10.0, abs=1e-4)
    md = q["metrics"]["aspect_ratio"]
    assert md["limit"] == FQ.ASPECT_RATIO_LIMIT == 1000.0
    assert md["max"] == pytest.approx(10.0, abs=1e-4) and md["floor"] == 1.0
    assert md["scale_to"] == pytest.approx(10.0, abs=1e-4)      # the viewer's scale: the mesh's own range
    assert md["n_over"] == 0 and not [h for h in q["hotspots"] if h["metric"] == "aspect_ratio"]


def test_a_cell_past_the_aspect_bar_is_a_hotspot_on_every_face_it_owns(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh", last_cell_length=2000.0)
    q = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    md = q["metrics"]["aspect_ratio"]
    assert md["max"] == pytest.approx(2000.0, rel=1e-4)
    # cell 2 owns its outlet face and four wall faces; the internal face at x=2 belongs to cell 1
    assert md["n_over"] == 5
    hot = [h for h in q["hotspots"] if h["metric"] == "aspect_ratio"]
    assert hot and all(h["x"] >= 2.0 for h in hot)
