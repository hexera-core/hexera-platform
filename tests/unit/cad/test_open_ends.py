# Responsibility: Verify the one hole definition the upload's stickers and the stage's click share
# (cad/open_ends) on the shapes real files throw at it: thin and thick walls, cut ends that are not
# flat, a ring split by a seam, a slanted cut, a hole in the side of a curved wall, a tiny hole in a
# big part, a part drawn far from the origin, an assembly of touching solids - and what is not a
# hole: a fluid body's capped mouth, the inside junction of a tee.
# Boundaries: numpy meshes built here, and OpenCASCADE for the shapes that need a boolean (skipped
# where it is not installed). No files beyond the test's own, no model, no storage.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.open_ends import find_holes

SIDES = 48


def _equivalent(r: float, sides: int = SIDES) -> float:
    """The diameter of the circle with the area of the regular polygon a round loop is drawn as."""
    return 2.0 * math.sqrt(0.5 * sides * r * r * math.sin(2 * math.pi / sides) / math.pi)


def _tube(r_out, r_in=None, length=0.3, origin=(0.0, 0.0, 0.0), end0=None, end1=None, sides=SIDES, rows=1):
    """A straight tube along +x from `origin`, as triangles: a thin wall open at both ends, or with
    `r_in` a thick wall closed by an end face at each end, its walls in `rows` rows along the axis.
    `end0`/`end1` move each end along the axis by f(angle, radius) - a warp, a seam, a slanted
    cut - so the ends need not be flat."""
    ox, oy, oz = origin
    f0 = end0 or (lambda a, r: 0.0)
    f1 = end1 or (lambda a, r: 0.0)

    def at(t, r, k):                                               # t = 0 at one end, 1 at the other
        a = 2 * math.pi * k / sides
        x = (1 - t) * f0(a, r) + t * (length + f1(a, r))
        return (ox + x, oy + r * math.cos(a), oz + r * math.sin(a))

    tris = []
    for k in range(sides):
        for j in range(rows):
            t0, t1 = j / rows, (j + 1) / rows
            a0, a1, b0, b1 = at(t0, r_out, k), at(t0, r_out, k + 1), at(t1, r_out, k), at(t1, r_out, k + 1)
            tris += [(a0, b1, b0), (a0, a1, b1)]                   # the outer wall, facing out
            if r_in is not None:
                c0, c1, d0, d1 = at(t0, r_in, k), at(t0, r_in, k + 1), at(t1, r_in, k), at(t1, r_in, k + 1)
                tris += [(c0, d0, d1), (c0, d1, c1)]               # the bore, facing in
        if r_in is None:
            continue
        a0, a1, b0, b1 = at(0, r_out, k), at(0, r_out, k + 1), at(1, r_out, k), at(1, r_out, k + 1)
        c0, c1, d0, d1 = at(0, r_in, k), at(0, r_in, k + 1), at(1, r_in, k), at(1, r_in, k + 1)
        tris += [(a0, c0, c1), (a0, c1, a1)]                       # the end face at x = 0
        tris += [(b0, d1, d0), (b0, b1, d1)]                       # the end face at x = length
    return np.asarray(tris, dtype=float)


def _ends(holes, origin=(0.0, 0.0, 0.0), length=0.3):
    """The holes at each end of a tube along +x, in that order."""
    ox = origin[0]
    by_end = sorted(holes, key=lambda h: h.centroid[0])
    assert len(by_end) == 2, [(h.kind, h.centroid) for h in holes]
    assert abs(by_end[0].centroid[0] - ox) < 0.05 and abs(by_end[1].centroid[0] - (ox + length)) < 0.08
    return by_end


def test_a_thin_walled_tube_has_its_two_open_rims():
    holes = _ends(find_holes(_tube(0.05)))
    for h, out in zip(holes, (-1.0, 1.0)):
        assert h.kind == "rim" and h.shape == "circle"
        assert h.equivalent_diameter == pytest.approx(_equivalent(0.05), rel=1e-6)
        assert h.normal[0] == pytest.approx(out, abs=1e-6)               # out of the part
        assert np.allclose(h.centroid[1:], 0.0, atol=1e-9)


def test_a_thick_walled_tube_has_the_inner_loop_of_each_end_as_its_hole():
    holes = _ends(find_holes(_tube(0.05, 0.04)))
    for h, out in zip(holes, (-1.0, 1.0)):
        assert h.kind == "bore"
        assert h.equivalent_diameter == pytest.approx(_equivalent(0.04), rel=1e-6)   # the bore, not the wall
        assert h.normal[0] == pytest.approx(out, abs=1e-6)
        assert h.outer is not None                                       # the end face around it, to click


def test_a_warped_end_is_still_a_hole():
    # the cut end is a saddle, 8 mm high on a 100 mm tube: nothing about it is flat
    warp = lambda a, r: 0.008 * math.sin(2 * a)  # noqa: E731
    holes = _ends(find_holes(_tube(0.05, 0.04, end0=warp, end1=warp)))
    for h in holes:
        assert h.kind == "bore" and h.planarity > 0.05
        assert h.equivalent_diameter == pytest.approx(_equivalent(0.04), rel=0.02)


def test_a_ring_split_by_a_seam_is_still_one_hole():
    # the end face is two halves meeting at a 40 degree crease across its middle: two faces of
    # the part, not one, and the hole between them is still the one opening
    seam = lambda a, r: math.tan(math.radians(20)) * abs(r * math.sin(a))  # noqa: E731
    holes = _ends(find_holes(_tube(0.05, 0.04, end0=seam, end1=seam)))
    for h in holes:
        assert h.kind == "bore"
        assert h.equivalent_diameter == pytest.approx(_equivalent(0.04), rel=0.05)


def test_a_slanted_cut_faces_along_its_cut():
    tilt = math.radians(35)
    slant = lambda a, r: math.tan(tilt) * r * math.cos(a)  # noqa: E731
    _, far = _ends(find_holes(_tube(0.05, 0.04, end1=slant)))
    assert far.normal @ np.array([math.cos(tilt), -math.sin(tilt), 0.0]) == pytest.approx(1.0, abs=1e-3)
    # an oblique section of a round bore: the bore's area over the cosine of the slant
    assert far.equivalent_diameter == pytest.approx(_equivalent(0.04) / math.sqrt(math.cos(tilt)), rel=0.01)


def test_a_part_far_from_the_origin_with_a_rough_mesh_keeps_its_holes():
    # the aorta's case: drawn 600 mm from the origin, its end rings a little uneven
    tris = _tube(0.05, 0.04, origin=(0.6, 0.55, 0.62))
    end = np.abs(tris[..., 0] - 0.6) < 1e-9                       # the first end's corners, moved
    tris[..., 0] += end * 1e-5 * np.sin(4000 * tris[..., 1] + 3000 * tris[..., 2])   # by where they are
    holes = _ends(find_holes(tris), origin=(0.6, 0.55, 0.62))
    assert all(h.kind == "bore" for h in holes)


def test_a_thin_tube_with_a_hole_in_its_side_has_three_holes():
    # a hole cut through the curved wall: a saddle-shaped open rim, facing out of the wall
    tris = _tube(0.05, rows=30)
    centres = tris.mean(axis=1)
    side = (np.abs(centres[:, 0] - 0.15) < 0.02) & (centres[:, 2] > 0.04)
    holes = find_holes(tris[~side])
    assert len(holes) == 3, [(h.kind, h.centroid) for h in holes]
    hole = next(h for h in holes if abs(h.centroid[0] - 0.15) < 0.01)
    assert hole.kind == "rim" and hole.normal[2] > 0.9 and hole.planarity > 0.01


def test_a_closed_fluid_body_has_no_holes():
    # a capped cylinder (the fluid volume itself): its mouths are faces to click, not holes
    tris = list(_tube(0.05))
    ring0 = [(0.0, 0.05 * math.cos(2 * math.pi * k / SIDES), 0.05 * math.sin(2 * math.pi * k / SIDES)) for k in range(SIDES)]
    ring1 = [(0.3, y, z) for _, y, z in ring0]
    for k in range(SIDES):
        tris.append(((0.0, 0.0, 0.0), ring0[(k + 1) % SIDES], ring0[k]))
        tris.append(((0.3, 0.0, 0.0), ring1[k], ring1[(k + 1) % SIDES]))
    assert find_holes(np.asarray(tris)) == []


def test_two_touching_solids_are_read_as_one_part():
    # a tube and a flange ring around its end, drawn as two solids that share the face where they
    # touch: the shared face is inside the part, and the tube keeps its two holes
    tube = _tube(0.05, 0.04)
    flange = _tube(0.08, 0.05, length=0.01)
    holes = find_holes(np.concatenate([tube, flange]))
    assert len(holes) == 2 and all(h.equivalent_diameter == pytest.approx(_equivalent(0.04), rel=1e-6) for h in holes)


# ------------------------------------------------------------------- shapes from OpenCASCADE ----
def _occ_tris(shape, deflection=0.0005):
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, 0.2, True)
    tris = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            pts = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                if face.Orientation() == TopAbs_REVERSED:
                    b, c = c, b
                tris.append([[pts[k - 1].X(), pts[k - 1].Y(), pts[k - 1].Z()] for k in (a, b, c)])
        ex.Next()
    return np.asarray(tris, dtype=float)


def _cyl(x, y, z, axis, r, h):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*axis)), r, h).Shape()


def test_a_hole_drilled_through_a_curved_thick_wall_is_found_with_the_ends():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    pipe = BRepAlgoAPI_Cut(_cyl(0, 0, 0, (1, 0, 0), 0.05, 0.3), _cyl(-0.01, 0, 0, (1, 0, 0), 0.04, 0.32)).Shape()
    drilled = BRepAlgoAPI_Cut(pipe, _cyl(0.15, 0, 0, (0, 0, 1), 0.008, 0.1)).Shape()
    holes = find_holes(_occ_tris(drilled))
    assert len(holes) == 3, [(h.kind, np.round(h.centroid, 3)) for h in holes]
    side = next(h for h in holes if abs(h.centroid[0] - 0.15) < 0.01)
    assert side.kind == "bore" and side.normal[2] > 0.95                 # out through the top
    assert side.centroid[2] > 0.045                                      # the outside mouth, not the inner one
    assert side.equivalent_diameter == pytest.approx(0.016, rel=0.05)


def test_a_tiny_hole_in_a_big_part_is_found():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 1.0, 1.0, 1.0).Shape(),
                          BRepPrimAPI_MakeBox(gp_Pnt(0.02, 0.02, 0.02), 0.96, 0.96, 0.96).Shape()).Shape()
    box = BRepAlgoAPI_Cut(box, _cyl(-0.01, 0.5, 0.5, (1, 0, 0), 0.3, 0.05)).Shape()       # the big inlet
    box = BRepAlgoAPI_Cut(box, _cyl(0.97, 0.3, 0.7, (1, 0, 0), 0.005, 0.05)).Shape()      # a 10 mm port
    holes = find_holes(_occ_tris(box, deflection=0.002))
    sizes = sorted(round(h.equivalent_diameter, 3) for h in holes)
    assert sizes == pytest.approx([0.01, 0.6], rel=0.03), sizes


def test_the_inside_of_a_tees_junction_is_not_a_hole():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse

    outer = BRepAlgoAPI_Fuse(_cyl(0, 0, 0, (1, 0, 0), 0.05, 0.4), _cyl(0.2, 0, 0, (0, 0, 1), 0.04, 0.2)).Shape()
    bores = BRepAlgoAPI_Fuse(_cyl(-0.01, 0, 0, (1, 0, 0), 0.04, 0.42), _cyl(0.2, 0, 0, (0, 0, 1), 0.03, 0.21)).Shape()
    tee = BRepAlgoAPI_Cut(outer, bores).Shape()
    holes = find_holes(_occ_tris(tee))
    assert len(holes) == 3, [(h.kind, np.round(h.centroid, 3)) for h in holes]
    assert all(h.kind == "bore" for h in holes)
    branch = next(h for h in holes if h.centroid[2] > 0.1)
    assert branch.centroid[2] == pytest.approx(0.2, abs=1e-3) and branch.normal[2] > 0.99
