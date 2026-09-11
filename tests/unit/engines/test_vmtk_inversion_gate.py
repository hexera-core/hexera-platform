# Responsibility: Verify the inversion gate uses an explicit signed-volume convention and counts only inverted cells.
from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv
import vtk

from meshpipeline.engines.vmtk.vmtk_runner import check_mesh

# a unit right-handed tet in canonical VTK node order → analytic signed volume +1/6
_BASE_PTS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)


def _signed_volume(pts: np.ndarray, conn) -> float:
    a, b, c, d = (pts[i] for i in conn)
    return float(np.dot(np.cross(b - a, c - a), d - a) / 6.0)


def _write_tet_vtu(path, tets, pts=_BASE_PTS, extra_pts=None):
    points = pts if extra_pts is None else np.vstack([pts, extra_pts])
    cells = np.hstack([[4, *t] for t in tets]).astype(np.int64)
    ctypes = np.full(len(tets), vtk.VTK_TETRA, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells, ctypes, points)
    grid.save(str(path))


def test_the_signed_volume_convention_is_explicit_and_matches_analytic():
    pos = pv.UnstructuredGrid(
        np.array([4, 0, 1, 2, 3], np.int64), np.array([vtk.VTK_TETRA], np.uint8), _BASE_PTS)
    swp = pv.UnstructuredGrid(
        np.array([4, 1, 0, 2, 3], np.int64), np.array([vtk.VTK_TETRA], np.uint8), _BASE_PTS)
    vpos = float(pos.compute_cell_sizes(length=False, area=False, volume=True).cell_data["Volume"][0])
    vswp = float(swp.compute_cell_sizes(length=False, area=False, volume=True).cell_data["Volume"][0])
    assert vpos == pytest.approx(_signed_volume(_BASE_PTS, [0, 1, 2, 3]))   # +1/6
    assert vswp == pytest.approx(_signed_volume(_BASE_PTS, [1, 0, 2, 3]))   # -1/6
    assert vpos > 0 > vswp


def test_a_single_positively_oriented_tet_passes(tmp_path):
    _write_tet_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)])
    q = check_mesh(tmp_path)
    assert q["cells"] == 1
    assert q["fatal"] == []
    assert q["mesh_ok"] is True
    assert q["min_quality"] is not None and q["min_quality"] > 0.01


def test_two_swapped_vertices_is_reoriented_not_rejected(tmp_path):
    # A negative signed volume is a NODE ORDER, not a geometry: the same four points bound the
    # same tetrahedron. vmtk writes its whole boundary-layer block that way (every layer tet
    # negative, the block tiling the enclosed volume exactly), so the sign alone must not be
    # fatal. The order is normalised on disk; a real fold shows up as OVERLAP instead.
    _write_tet_vtu(tmp_path / "mesh.vtu", [(1, 0, 2, 3)])
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is True
    assert q["fatal"] == []
    assert q["reoriented_tets"] == 1
    saved = pv.read(str(tmp_path / "mesh.vtu"))
    vol = saved.compute_cell_sizes(length=False, area=False, volume=True).cell_data["Volume"]
    assert float(vol[0]) > 0                              # the deliverable was fixed, once
    assert check_mesh(tmp_path)["reoriented_tets"] == 0   # idempotent


def test_several_valid_tets_all_pass(tmp_path):
    # four apexes, each forming a positively-oriented tet with the same base
    apexes = np.array([[0, 0, 1], [0.2, 0.2, 1], [0.1, -0.3, 2], [-0.2, 0.1, 1.5]], float)
    tets, pts = [], np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    for i, ap in enumerate(apexes):
        base = 3 + i
        pts = np.vstack([pts, ap])
        conn = (0, 1, 2, base)
        # orient positively w.r.t. analytic sign
        if _signed_volume(pts, conn) < 0:
            conn = (1, 0, 2, base)
        tets.append(conn)
    _write_tet_vtu(tmp_path / "mesh.vtu", tets, pts=pts[:3], extra_pts=pts[3:])
    q = check_mesh(tmp_path)
    assert q["cells"] == len(apexes)
    assert q["fatal"] == []
    assert q["mesh_ok"] is True


def test_a_mixed_set_reorients_only_the_negative_ones(tmp_path):
    extra = np.array([[0.3, 0.3, 1.0]], float)   # a 5th point for a 2nd/3rd tet
    tets = [(0, 1, 2, 3),            # positive
            (1, 0, 2, 4),            # negative order (swapped) using the extra apex
            (2, 1, 0, 3)]            # negative order (swapped)
    _write_tet_vtu(tmp_path / "mesh.vtu", tets, pts=_BASE_PTS, extra_pts=extra)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is True and q["fatal"] == []
    assert q["reoriented_tets"] == 2 and q["cells"] == 3
    # the floor judges the isotropic fill; the reoriented (layer) block is reported apart
    assert q["layer_tets"] == 2 and q["layer_min_quality"] is not None
    assert q["min_quality"] is not None
    # a SECOND check (finalize runs one after the run tool) judges the same tets the same way:
    # the sign is gone from disk, the BoundaryLayer array is not
    q2 = check_mesh(tmp_path)
    assert q2["reoriented_tets"] == 0 and q2["layer_tets"] == 2
    assert q2["min_quality"] == q["min_quality"]
    assert "BoundaryLayer" in pv.read(str(tmp_path / "mesh.vtu")).cell_data


def _write_mixed_vtu(path, tets, tris, pts):
    cells = np.hstack([[4, *t] for t in tets] + [[3, *t] for t in tris]).astype(np.int64)
    ctypes = np.array([vtk.VTK_TETRA] * len(tets) + [vtk.VTK_TRIANGLE] * len(tris), dtype=np.uint8)
    pv.UnstructuredGrid(cells, ctypes, pts).save(str(path))


_TET_FACES = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]   # the boundary of tet (0,1,2,3)


def test_tets_that_tile_their_boundary_pass_the_overlap_test(tmp_path):
    _write_mixed_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)], _TET_FACES, _BASE_PTS)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is True and q["fatal"] == []


def test_an_incomplete_fill_is_rejected(tmp_path):
    # the boundary of a tet twice the size, but only the small tet inside: TetGen wrote the
    # boundary layer and gave up on the interior (vmtkmeshgenerator still exits 0)
    big = np.vstack([_BASE_PTS, 2.0 * _BASE_PTS])
    faces = [tuple(4 + i for i in f) for f in _TET_FACES]
    _write_mixed_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)], faces, big)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("incomplete" in f.lower() for f in q["fatal"])


def test_a_tetgen_exception_logged_with_rc0_fails(tmp_path):
    _write_tet_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)])
    (tmp_path / "log.vmtk").write_text("vtkvmtkTetGenWrapper: TetGen quit with an exception.\n"
                                       "An error occurred during tetrahedralization. Will only "
                                       "output surface mesh and boundary layer.\n")
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False


def test_layer_coverage_is_measured_from_the_layer_block(tmp_path):
    # the tet is the layer block (negative order); its faces are the wall (id 1) and one cap (id 2)
    cells = np.hstack([[4, 1, 0, 2, 3]] + [[3, *f] for f in _TET_FACES]).astype(np.int64)
    ctypes = np.array([vtk.VTK_TETRA] + [vtk.VTK_TRIANGLE] * 4, dtype=np.uint8)
    g = pv.UnstructuredGrid(cells, ctypes, _BASE_PTS)
    g.cell_data["CellEntityIds"] = np.array([0, 1, 1, 1, 2], dtype=np.int32)
    g.save(str(tmp_path / "mesh.vtu"))
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is True and q["layer_tets"] == 1
    assert q["layer_coverage"] == 100.0                 # every wall triangle sits on a layer tet
    import json
    rep = json.loads((tmp_path / "layer_report.json").read_text())
    assert rep["coverage"] == 100.0 and rep["wall_triangles"] == 3 and rep["layer_tets"] == 1
    # a layer-free mesh reads 0 %, never None - the reviewer needs a number to weigh
    _write_mixed_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)], _TET_FACES, _BASE_PTS)
    q2 = check_mesh(tmp_path)
    assert q2["layer_coverage"] == 0.0 and q2["layer_tets"] == 0


def test_a_cap_wound_the_other_way_does_not_read_as_overlap(tmp_path):
    # vmtk winds cap triangles opposite to the wall; the enclosed volume must not depend on it
    faces = list(_TET_FACES[:3]) + [tuple(reversed(_TET_FACES[3]))]
    _write_mixed_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)], faces, _BASE_PTS)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is True and q["fatal"] == []


def test_overlapping_tets_are_rejected_even_when_positively_oriented(tmp_path):
    # the same tetrahedron twice: both positive, but together they fill the boundary's volume
    # twice over - what a boundary layer folded back through the wall looks like
    _write_mixed_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3), (0, 1, 2, 3)], _TET_FACES, _BASE_PTS)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("overlap" in f.lower() for f in q["fatal"])


def test_a_degenerate_zero_volume_tet_is_rejected_by_explicit_policy(tmp_path):
    coplanar = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0.4, 0.4, 0]], float)  # all z=0
    _write_tet_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)], pts=coplanar)
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("inverted" in f.lower() or "non-positive" in f.lower() for f in q["fatal"])


def test_a_tetgen_refusal_logged_with_rc0_still_fails(tmp_path):
    _write_tet_vtu(tmp_path / "mesh.vtu", [(0, 1, 2, 3)])      # a perfectly good tet on disk
    (tmp_path / "log.vmtk").write_text("... Invalid PLC: subfaces intersect ...\n")
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("plc" in f.lower() or "self-intersect" in f.lower() for f in q["fatal"])


def test_a_truncated_vtu_is_rejected_before_the_reader_hangs(tmp_path):
    (tmp_path / "mesh.vtu").write_text('<?xml version="1.0"?>\n<VTKFile type="Unstructured')
    q = check_mesh(tmp_path)
    assert q["mesh_ok"] is False
    assert any("truncat" in f.lower() or "corrupt" in f.lower() for f in q["fatal"])
