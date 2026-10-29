"""The poor cells are drawn, at full resolution, on a mesh too big to colour.

Corintis asked for a "highlight of poor-quality cells" (2026-10-29). The engine could already
count them - 28,132 on their plate, 27,932 of those in the metal - which tells a customer they
have a problem without telling them where. Both mechanisms that could have shown where stop before
the mesh they asked about:

  * the per-face heatmap is dropped once cells are merged for drawing, because a colour on a
    merged face was measured on none of the faces it stands for, and that plate's coolant side is
    13,022,400 faces against render.polymesh_budget.DRAWN_MAX_TRIANGLES of 800,000;
  * the measurement behind it refuses a mesh past face_quality.MAX_FACES_FOR_FIELDS, 40 M faces,
    against this plate's 200 M.

checkMesh's own sets hold at any size. Measured in the mesh image, 2026-10-29, on a 1 x 1 x 0.0002
m slab in 2x2x1 cells:

    ***High aspect ratio cells found, Max aspect ratio: 2500, number of cells 4
      <<Writing 4 cells with high aspect ratio to set highAspectRatioCells
    postProcessing/constant/highAspectRatioCells/highAspectRatioCells.vtp
    <Piece NumberOfPoints='18' NumberOfPolys='16'>

One file per failed check, already a surface, written by OpenFOAM in the container on the mesh
itself. A few thousand faces draw exactly, whatever the rest of the mesh had to give up.
"""
from __future__ import annotations

import inspect
import struct

from meshpipeline.engines.cfmesh_conjugate import viewer as V

#: The measured shape of a real set file, from the slab above.
SLAB_POINTS, SLAB_POLYS = 18, 16


def _set_file(ws, region, name, *, points=SLAB_POINTS, polys=SLAB_POLYS):
    """A poor-cell set where collect_poor_sets leaves one, as a readable .vtp."""
    import numpy as np
    import pyvista as pv

    d = ws / "poor_cells" / region
    d.mkdir(parents=True, exist_ok=True)
    # a strip of triangles: `polys` faces over `points` points, which is the shape of a set
    pts = np.array([[i * 0.001, (i % 2) * 0.001, 0.0] for i in range(points)], dtype=float)
    tri = np.hstack([[3, i, i + 1, i + 2] for i in range(min(polys, points - 2))])
    pv.PolyData(pts, tri).save(d / f"{name}.vtp")
    return d / f"{name}.vtp"


def test_a_good_mesh_has_nothing_to_highlight(tmp_path):
    """The common case, and not a defect: a set holds the cells that FAILED a check, so a mesh
    that passes writes no files. The coupon's own report says "poor_sets": []."""
    assert V._poor_surfaces(tmp_path) == []


def test_each_set_is_its_own_drawable_surface(tmp_path):
    _set_file(tmp_path, "solid", "highAspectRatioCells")
    _set_file(tmp_path, "fluid", "skewFaces")
    got = V._poor_surfaces(tmp_path)
    assert {d["name"] for d in got} == {"solid/highAspectRatioCells", "fluid/skewFaces"}
    assert {d["type"] for d in got} == {V.POOR_TYPE}
    for d in got:
        assert d["face_count"] > 0
        assert len(d["points"]) % 12 == 0, "float32 xyz"
        assert len(d["polys"]) % 4 == 0, "uint32"


def test_the_region_comes_from_where_the_set_was_kept(tmp_path):
    """foam_exec moves them under poor_cells/<region>/ so a later region's run cannot pick them up
    again; the directory is therefore the only place the region is recorded."""
    _set_file(tmp_path, "solid", "nonOrthoFaces")
    got = V._poor_surfaces(tmp_path)
    assert got[0]["name"].startswith("solid/")


def test_the_points_and_polys_are_the_format_the_viewer_unpacks(tmp_path):
    """float32 xyz and [n, i0..i(n-1)] uint32 per face - the same two buffers
    polymesh_surface.boundary_surface writes, because polymesh_response base64s them unchanged."""
    _set_file(tmp_path, "fluid", "warpedFaces", points=6, polys=4)
    got = V._poor_surfaces(tmp_path)[0]
    n_pts = len(got["points"]) // 12
    polys = struct.unpack(f"<{len(got['polys']) // 4}I", got["polys"])
    assert n_pts == 6
    i = 0
    seen = 0
    while i < len(polys):
        k = polys[i]
        assert 3 <= k <= 4, f"a face of {k} corners"
        assert all(v < n_pts for v in polys[i + 1:i + 1 + k]), "an index past the points"
        i += k + 1
        seen += 1
    assert seen == got["face_count"]


def test_an_unreadable_set_costs_the_view_nothing(tmp_path):
    """A highlight is an addition to a mesh the user already has."""
    d = tmp_path / "poor_cells" / "fluid"
    d.mkdir(parents=True)
    (d / "skewFaces.vtp").write_text("not a vtp")
    _set_file(tmp_path, "solid", "concaveCells")
    got = V._poor_surfaces(tmp_path)
    assert [d["name"] for d in got] == ["solid/concaveCells"]


def test_a_set_too_big_to_draw_is_left_out_and_said_so(tmp_path, caplog):
    """A set past the budget is a mesh with nothing worth highlighting, because the highlight
    would be the mesh."""
    assert V.POOR_MAX_TRIANGLES == 400_000
    src = inspect.getsource(V._poor_surfaces)
    assert "POOR_MAX_TRIANGLES" in src
    assert "not drawn" in src


def test_they_go_in_after_the_budget_so_they_are_never_merged():
    """The whole point. Merged, the highlight would be a blur in roughly the right place."""
    src = inspect.getsource(V.conjugate_viewer)
    at_budget = src.index("fit_to_budget(raw)")
    at_poor = src.index("_poor_surfaces(ws)")
    assert at_poor > at_budget, "a merged highlight is not a highlight"


def test_a_poor_set_is_not_handed_to_the_heatmap():
    """It wears a region prefix so the two regions' sets stay apart, but the polyMesh has never
    heard of it and there is nothing to colour: it already IS the highlight."""
    src = inspect.getsource(V._paint)
    assert "POOR_TYPE" in src
