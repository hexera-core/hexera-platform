# Responsibility: Verify a volume mesh - cells plus the boundary the viewer draws - becomes the
# face mesh the heatmap measures exactly as an OpenFOAM polyMesh of the same cells does: every
# field aligned with the polygons drawn, every face wound out of its cell, every cell closed.
from __future__ import annotations

import base64
from pathlib import Path

import numpy as np
import pytest
from tests.foam_fixtures import ROW_SHEAR as SHEAR
from tests.foam_fixtures import ROW_SHEAR_NON_ORTHO_DEG as EXPECTED_DEG
from tests.foam_fixtures import _foam, write_row_of_hexes

from meshpipeline.render import face_quality as FQ
from meshpipeline.render import volume_quality as VQ

ROOT = Path(__file__).parents[3]
FIELDS = ("non_ortho_b64", "skewness_b64", "aspect_ratio_b64")


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


# ------------------------------------------------ the fixture row of hexes, as a volume ----
def _p(ix: int, iy: int, iz: int) -> int:
    return ix * 4 + iy * 2 + iz


def _row_volume(shear: float = 0.0):
    """The three hexes of tests.foam_fixtures.write_row_of_hexes as VTK-ordered cells, with the
    boundary quads in the polyMesh's own order (inlet, outlet, then the twelve wall faces)."""
    pts = []
    for ix in range(4):
        for iy in range(2):
            for iz in range(2):
                pts.append((float(ix), iy + (shear if ix == 3 else 0.0), float(iz)))
    cells = np.asarray([[_p(c, 0, 0), _p(c + 1, 0, 0), _p(c + 1, 1, 0), _p(c, 1, 0),
                         _p(c, 0, 1), _p(c + 1, 0, 1), _p(c + 1, 1, 1), _p(c, 1, 1)]
                        for c in range(3)])
    inlet = [[_p(0, 0, 0), _p(0, 0, 1), _p(0, 1, 1), _p(0, 1, 0)]]
    outlet = [[_p(3, 0, 0), _p(3, 1, 0), _p(3, 1, 1), _p(3, 0, 1)]]
    wall = []
    for c in range(3):
        wall.append([_p(c, 0, 0), _p(c + 1, 0, 0), _p(c + 1, 0, 1), _p(c, 0, 1)])
        wall.append([_p(c, 1, 0), _p(c, 1, 1), _p(c + 1, 1, 1), _p(c + 1, 1, 0)])
        wall.append([_p(c, 0, 0), _p(c, 1, 0), _p(c + 1, 1, 0), _p(c + 1, 0, 0)])
        wall.append([_p(c, 0, 1), _p(c + 1, 0, 1), _p(c + 1, 1, 1), _p(c, 1, 1)])
    return np.asarray(pts), [cells], [("inlet", inlet), ("outlet", outlet), ("wall", wall)]


# ---------------------------------------------------- a unit cube as six tetrahedra ----
CUBE = np.asarray([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                   (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], dtype=float)
KUHN = np.asarray([(0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6)])
CUBE_SIDES = [("x0", [[0, 3, 7], [0, 7, 4]]), ("x1", [[1, 2, 6], [5, 1, 6]]),
              ("y0", [[0, 5, 1], [0, 4, 5]]), ("y1", [[2, 3, 6], [3, 7, 6]]),
              ("z0", [[0, 1, 2], [0, 2, 3]]), ("z1", [[4, 5, 6], [7, 4, 6]])]


def _closed_and_positive(mesh) -> None:
    """Every cell's area vectors sum to zero and its volume is positive - the two facts every
    metric below rests on. A face wound into its cell, or a face missing, breaks one of them."""
    fCtrs, fAreas = FQ._face_geometry(mesh.points, mesh.face_flat, mesh.face_off)
    nc = mesh.n_cells
    ni = len(mesh.neighbour)
    closure = np.zeros((nc, 3))
    np.add.at(closure, mesh.owner, fAreas)
    np.add.at(closure, mesh.neighbour, -fAreas[:ni])
    assert np.allclose(closure, 0.0, atol=1e-12)
    _, _, vols = FQ._cell_geometry(fCtrs, fAreas, mesh.owner, mesh.neighbour, nc)
    assert (vols > 0).all()


# ------------------------------------------------------------------------- the tests ----
def test_a_volume_of_hexes_measures_exactly_as_the_polymesh_of_the_same_cells(tmp_path):
    write_row_of_hexes(tmp_path / "polyMesh", shear_last_y=SHEAR)
    ref = FQ.quality_fields(tmp_path / "polyMesh", non_ortho_limit=65.0)
    points, cells, boundary = _row_volume(SHEAR)
    mesh = VQ.mesh_from_volume(points, cells, boundary)
    q = FQ.quality_fields_of(mesh, non_ortho_limit=65.0, patch_names=["inlet", "outlet", "wall"])

    assert q is not None and mesh.n_faces == 16 and len(mesh.neighbour) == 2
    for m in ("non_ortho", "skewness", "aspect_ratio"):
        assert q["metrics"][m]["max"] == pytest.approx(ref["metrics"][m]["max"], rel=1e-9)
        assert q["metrics"][m]["n_over"] == ref["metrics"][m]["n_over"]
    for name in ("inlet", "outlet", "wall"):
        for field in FIELDS:
            mine, theirs = _f32(q["patches"][name][field]), _f32(ref["patches"][name][field])
            # a quad is drawn as two fan triangles and reads twice; both halves say the same
            assert len(mine) == 2 * len(theirs)
            assert np.allclose(mine[::2], theirs, rtol=1e-6, atol=1e-6)
            assert np.allclose(mine[1::2], theirs, rtol=1e-6, atol=1e-6)
    assert _f32(q["patches"]["outlet"]["non_ortho_b64"])[0] == pytest.approx(EXPECTED_DEG, abs=0.05)
    assert [(h["metric"], h["patch"]) for h in q["hotspots"]] == \
        [(h["metric"], h["patch"]) for h in ref["hotspots"]]


def test_the_tet_cube_is_closed_and_round_trips_through_a_polymesh(tmp_path):
    mesh = VQ.mesh_from_volume(CUBE, [KUHN], CUBE_SIDES)
    assert mesh.n_cells == 6 and mesh.n_faces == 6 * 4 - 6 and len(mesh.neighbour) == 6
    _closed_and_positive(mesh)

    # write the very faces out as a polyMesh, patches in draw order, and let the file reader
    # measure them: the two paths must agree to the bit on what the cells are worth
    pm = tmp_path / "polyMesh"
    pm.mkdir()
    ni = len(mesh.neighbour)
    order = list(range(ni)) + [int(i) for _, p in zip(CUBE_SIDES, mesh.patches)
                               for i in FQ._patch_face_ids(mesh, p)]
    assert sorted(order) == list(range(mesh.n_faces))            # every face exactly once
    sizes = np.diff(mesh.face_off)
    faces = [f"{sizes[f]}(" + " ".join(str(int(v)) for v in
             mesh.face_flat[mesh.face_off[f]:mesh.face_off[f + 1]]) + ")" for f in order]
    _foam(pm / "points", "vectorField", "points", len(CUBE),
          [f"({x} {y} {z})" for x, y, z in CUBE])
    _foam(pm / "faces", "faceList", "faces", len(faces), faces)
    _foam(pm / "owner", "labelList", "owner", len(order), [str(int(mesh.owner[f])) for f in order])
    _foam(pm / "neighbour", "labelList", "neighbour", ni, [str(int(n)) for n in mesh.neighbour])
    body, start = [], ni
    for name, polys in CUBE_SIDES:
        body.append(f"    {name}\n    {{\n        type patch;\n"
                    f"        nFaces {len(polys)};\n        startFace {start};\n    }}")
        start += len(polys)
    _foam(pm / "boundary", "polyBoundaryMesh", "boundary", len(body), body)

    ref = FQ.quality_fields(pm, non_ortho_limit=65.0)
    q = FQ.quality_fields_of(mesh, non_ortho_limit=65.0, patch_names=[n for n, _ in CUBE_SIDES])
    assert ref is not None and q is not None
    assert q["metrics"] == ref["metrics"]
    for name, _ in CUBE_SIDES:
        for field in FIELDS:
            assert np.array_equal(_f32(q["patches"][name][field]), _f32(ref["patches"][name][field]))
    assert 0.0 < q["metrics"]["non_ortho"]["max"] < 90.0       # tets are never orthogonal


def test_the_node_order_a_mesher_used_does_not_matter():
    # gmsh and VTK agree on corner order, but a mesher can still hand over inverted cells (vmtk's
    # layer tets do). Every face is wound out of its cell numerically, so reversing the nodes of
    # every cell changes nothing.
    a = FQ.quality_fields_of(VQ.mesh_from_volume(CUBE, [KUHN], CUBE_SIDES), non_ortho_limit=65.0)
    b = FQ.quality_fields_of(VQ.mesh_from_volume(CUBE, [KUHN[:, ::-1]], CUBE_SIDES),
                             non_ortho_limit=65.0)
    assert a["metrics"] == b["metrics"]
    for name, _ in CUBE_SIDES:
        for field in FIELDS:
            assert np.array_equal(_f32(a["patches"][name][field]), _f32(b["patches"][name][field]))


def test_wedges_and_pyramids_close_their_cells_too():
    # one wedge (a triangular prism) and one pyramid, each with its whole boundary drawn
    wedge_pts = np.asarray([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1)], float)
    wedge = VQ.mesh_from_volume(wedge_pts, [np.asarray([[0, 1, 2, 3, 4, 5]])],
                                [("ends", [[0, 2, 1], [3, 4, 5]]),
                                 ("sides", [[0, 1, 4, 3], [1, 2, 5, 4], [2, 0, 3, 5]])])
    assert wedge.n_faces == 5 and len(wedge.neighbour) == 0
    _closed_and_positive(wedge)
    pyr_pts = np.asarray([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0.5, 0.5, 1)], float)
    pyramid = VQ.mesh_from_volume(pyr_pts, [np.asarray([[0, 1, 2, 3, 4]])],
                                  [("base", [[0, 1, 2, 3]]),
                                   ("sides", [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]])])
    assert pyramid.n_faces == 5
    _closed_and_positive(pyramid)
    q = FQ.quality_fields_of(pyramid, non_ortho_limit=65.0)
    assert q["patches"]["base"]["count"] == 2 and q["patches"]["sides"]["count"] == 4


def test_a_face_drawn_twice_reads_twice_but_is_counted_once():
    # vmtk's surface extraction can hand the same triangle over under two entity ids; the
    # field follows what is drawn, while the cell stays closed by exactly its own faces
    twice = CUBE_SIDES + [("again", CUBE_SIDES[0][1] + CUBE_SIDES[0][1])]
    mesh = VQ.mesh_from_volume(CUBE, [KUHN], twice)
    assert mesh.n_faces == 18
    q = FQ.quality_fields_of(mesh, non_ortho_limit=65.0)
    x0, again = _f32(q["patches"]["x0"]["non_ortho_b64"]), _f32(q["patches"]["again"]["non_ortho_b64"])
    assert len(x0) == 2 and len(again) == 4
    assert np.array_equal(again, np.concatenate([x0, x0]))


def test_a_drawn_polygon_that_is_not_a_cell_face_is_refused_not_guessed():
    bogus = CUBE_SIDES + [("floating", [[0, 2, 5]])]      # a diagonal slice, no cell's face
    with pytest.raises(FQ.UnreadableMesh, match="floating"):
        VQ.mesh_from_volume(CUBE, [KUHN], bogus)
    resp = {"kind": "stl", "patches": [{"name": "x0"}]}
    VQ.attach_volume_quality(resp, CUBE, [KUHN], bogus)
    assert "quality_fields" not in resp                    # skipped, and the response intact


def test_attach_never_costs_the_delivery(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("something unexpected")
    monkeypatch.setattr(VQ, "mesh_from_volume", boom)
    resp = {"kind": "stl", "patches": [{"name": "x0"}]}
    VQ.attach_volume_quality(resp, CUBE, [KUHN], CUBE_SIDES)
    assert resp == {"kind": "stl", "patches": [{"name": "x0"}]}


def test_attach_colours_only_the_patches_the_response_draws():
    resp = {"kind": "stl", "patches": [{"name": "x0"}, {"name": "z1"}]}
    VQ.attach_volume_quality(resp, CUBE, [KUHN], CUBE_SIDES)
    qf = resp["quality_fields"]
    assert set(qf["patches"]) == {"x0", "z1"}
    assert qf["metrics"]["non_ortho"]["limit"] == VQ.NON_ORTHO_LIMIT == 65.0


def test_volume_quality_stays_free_of_engine_knowledge():
    src = (ROOT / "src/meshpipeline/render/volume_quality.py").read_text(encoding="utf-8")
    assert "meshpipeline.engines" not in src
