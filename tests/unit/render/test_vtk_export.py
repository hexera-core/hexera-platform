# Responsibility: Verify the ParaView export carries the viewer's own polygons and numbers, and
# refuses to ship a field against the wrong faces.
from __future__ import annotations

import base64
from pathlib import Path

import numpy as np
import pytest
from tests.foam_fixtures import ROW_SHEAR as SHEAR
from tests.foam_fixtures import ROW_SHEAR_NON_ORTHO_DEG as EXPECTED_DEG
from tests.foam_fixtures import write_row_of_hexes

from meshpipeline.render import vtk_export as VX

ROOT = Path(__file__).parents[3]
ROLES = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}


def _surface(tmp_path, **kw) -> dict:
    from meshpipeline.engines.snappy.viewer import polymesh_viewer
    write_row_of_hexes(tmp_path / "constant" / "polyMesh", **kw)
    surf = polymesh_viewer(tmp_path, roles=ROLES, units="m")
    assert surf and "quality_fields" in surf
    return surf


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


def test_the_file_is_legacy_vtk_polydata_with_one_polygon_per_drawn_face(tmp_path):
    surf = _surface(tmp_path, shear_last_y=SHEAR)
    n = sum(p["face_count"] for p in surf["patches"])      # the 14 boundary faces of the row
    data = VX.surface_to_vtk(surf)
    head = data[:160].split(b"\n")
    assert head[0] == b"# vtk DataFile Version 3.0"
    assert head[1].startswith(b"hexera mesh quality - patches: 0=") and b"non_ortho<=65" in head[1]
    assert head[2] == b"BINARY" and head[3] == b"DATASET POLYDATA"
    assert n == 14 and f"POLYGONS {n} ".encode() in data and f"CELL_DATA {n}\n".encode() in data
    for f in ("patch_id int", "non_ortho float", "skewness float", "aspect_ratio float"):
        assert f"SCALARS {f} 1\nLOOKUP_TABLE default\n".encode() in data


def test_paraview_reads_back_the_viewers_numbers(tmp_path):
    pv = pytest.importorskip("pyvista")
    surf = _surface(tmp_path, shear_last_y=SHEAR)
    path = tmp_path / "quality.vtk"
    path.write_bytes(VX.surface_to_vtk(surf))
    mesh = pv.read(str(path))
    names = [p["name"] for p in surf["patches"]]
    assert mesh.n_cells == sum(p["face_count"] for p in surf["patches"]) == 14
    assert mesh.n_points == sum(len(_f32(p["points_b64"])) // 3 for p in surf["patches"])
    pid = np.asarray(mesh.cell_data["patch_id"])
    no = np.asarray(mesh.cell_data["non_ortho"])
    # the outlet's one face carries the sheared angle; the inlet's carries zero
    assert no[pid == names.index("outlet")][0] == pytest.approx(EXPECTED_DEG, abs=0.1)
    assert no[pid == names.index("inlet")][0] == pytest.approx(0.0, abs=1e-5)
    # every array is the viewer's own, patch by patch, byte for byte
    for i, p in enumerate(surf["patches"]):
        q = surf["quality_fields"]["patches"][p["name"]]
        for f in VX.FIELDS:
            np.testing.assert_allclose(np.asarray(mesh.cell_data[f])[pid == i], _f32(q[f + "_b64"]),
                                       atol=1e-6)
    assert float(np.asarray(mesh.cell_data["aspect_ratio"]).min()) == pytest.approx(1.0, abs=1e-4)


def test_a_surface_without_fields_still_exports_with_patch_ids(tmp_path):
    surf = _surface(tmp_path)
    surf.pop("quality_fields")
    data = VX.surface_to_vtk(surf)
    assert b"SCALARS patch_id int 1" in data
    assert b"SCALARS non_ortho" not in data and b"bars:" not in data[:300]


def test_a_misaligned_field_is_blanked_for_that_patch_not_shipped_wrong(tmp_path):
    pv = pytest.importorskip("pyvista")
    surf = _surface(tmp_path)
    q = surf["quality_fields"]["patches"]
    q["wall"]["skewness_b64"] = q["inlet"]["skewness_b64"]        # one value for twelve faces
    path = tmp_path / "quality.vtk"
    path.write_bytes(VX.surface_to_vtk(surf))
    mesh = pv.read(str(path))
    pid = np.asarray(mesh.cell_data["patch_id"])
    sk = np.asarray(mesh.cell_data["skewness"])
    wall = [p["name"] for p in surf["patches"]].index("wall")
    assert np.isnan(sk[pid == wall]).all() and not np.isnan(sk[pid != wall]).any()
    assert not np.isnan(np.asarray(mesh.cell_data["non_ortho"])).any()


def test_a_surface_with_no_polymesh_patches_is_refused():
    with pytest.raises(ValueError):
        VX.surface_to_vtk({"kind": "stl", "patches": [{"name": "wall", "positions_b64": "AA=="}]})


def test_export_stays_free_of_engine_knowledge():
    src = (ROOT / "src" / "meshpipeline" / "render" / "vtk_export.py").read_text()
    assert "meshpipeline.engines" not in src, "render/ imports an engine - the layering is broken"
