# Responsibility: Verify the triangle scout reads what the CAD scout reads - openings, kind, flow -
# from meshes built on paper: an open tube, a capped tube (fine and coarse), a fluid body with a side
# branch, a coarse closed blob, a pipe wall with ring ends, a box, and a block of boxes on the ground.
# Boundaries: numpy meshes written as STL, and OpenCASCADE for the shape that needs a boolean
# (skipped where it is not installed); no model, no storage.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.scout_mesh import read_triangles, scout_mesh, write_mesh_skin
from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary


def _ring(r, z, n=32):
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def _band(inner_ring, outer_ring):
    """Quads between two rings of equal count, as triangles."""
    n = len(inner_ring)
    tris = []
    for i in range(n):
        a, b = inner_ring[i], inner_ring[(i + 1) % n]
        c, d = outer_ring[i], outer_ring[(i + 1) % n]
        tris.append((list(a), list(b), list(d)))
        tris.append((list(a), list(d), list(c)))
    return tris


def _fan(centre, ring, flip=False):
    n = len(ring)
    tris = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        tris.append((list(centre), list(b), list(a)) if flip else (list(centre), list(a), list(b)))
    return tris


def _tube(r=0.05, length=1.0):
    return _band(_ring(r, 0.0), _ring(r, length))


@pytest.fixture
def stl(tmp_path):
    def _write(name, tris):
        p = tmp_path / f"{name}.stl"
        write_stl_binary(p, tris)
        return p
    return _write


def test_an_open_tube_reads_its_two_rims_as_the_openings(stl):
    res = scout_mesh(stl("tube", _tube()), scale_to_m=1.0)
    assert res.flow == "internal" and res.input_kind == "body-surface"
    assert len(res.openings) == 2
    assert {o.role for o in res.openings} == {"inlet", "outlet"}
    for o in res.openings:
        assert o.kind == "rim" and o.shape == "circle"
        assert abs(o.equivalent_diameter - 0.1) < 0.006      # a 32-gon is a hair under the circle
    zs = sorted(o.centroid[2] for o in res.openings)
    assert abs(zs[0]) < 1e-6 and abs(zs[1] - 1.0) < 1e-6
    assert res.seed_point is not None
    assert any("close, not exact" in n for n in res.notes)


def test_a_capped_tube_is_a_fluid_body_with_two_disc_mouths(stl):
    tris = _tube() + _fan((0, 0, 0), _ring(0.05, 0.0), flip=True) + _fan((0, 0, 1.0), _ring(0.05, 1.0))
    res = scout_mesh(stl("fluid", tris), scale_to_m=1.0)
    assert res.flow == "internal" and res.input_kind == "fluid-domain"
    assert [o.kind for o in res.openings] == ["disc", "disc"]
    assert all(o.on_extremity for o in res.openings)


def _rows(r, length, sides, rows):
    """A tube along z in `rows` rows of `sides` facets: a coarse export's wall."""
    tris = []
    for j in range(rows):
        tris += _band(_ring(r, length * j / rows, sides), _ring(r, length * (j + 1) / rows, sides))
    return tris


def test_a_coarse_fluid_body_proposes_its_two_lids_and_none_of_its_facets(stl):
    # a solver's coarse export of a pipe's fluid: ten facets round, flat runs of them all along it
    # - each one flat, cornered against the next - and only the two end lids are openings
    tris = _rows(0.05, 1.0, 10, 20) + _fan((0, 0, 0), _ring(0.05, 0.0, 10), flip=True) + _fan((0, 0, 1.0), _ring(0.05, 1.0, 10))
    res = scout_mesh(stl("coarse", tris), scale_to_m=1.0)
    assert res.input_kind == "fluid-domain" and len(res.openings) == 2
    assert sorted(round(o.centroid[2], 6) for o in res.openings) == [0.0, 1.0]
    assert all(o.confidence >= 0.9 for o in res.openings)


def _cylinders_fused(*cyls, deflection=0.004):
    """Cylinders (base point, axis, radius, height) fused into one solid and tessellated the way a
    CAD export draws it, as triangles."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    shape = None
    for p, axis, r, h in cyls:
        c = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*axis)), r, h).Shape()
        shape = c if shape is None else BRepAlgoAPI_Fuse(shape, c).Shape()
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.4, True)
    tris = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face_s(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        pts = [tri.Node(i).Transformed(loc.Transformation()) for i in range(1, tri.NbNodes() + 1)]
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            if face.Orientation() == TopAbs_REVERSED:
                b, c = c, b
            tris.append([[pts[k - 1].X(), pts[k - 1].Y(), pts[k - 1].Z()] for k in (a, b, c)])
        ex.Next()
    return tris


def test_a_side_branchs_lid_is_proposed_wherever_it_sits(stl):
    # the fluid of a pipe with a branch off its side, as a CAD export draws it: the branch's lid is
    # nowhere near the part's box, and it is an opening as much as the two ends are
    pytest.importorskip("OCP")
    # a short branch and, further along, a long one the same way: the short one's lid is inside
    # the part's box, short of the long one's
    tris = _cylinders_fused(((0, 0, 0), (0, 0, 1), 0.05, 1.0), ((0, 0, 0.3), (1, 0, 0), 0.015, 0.09),
                            ((0, 0, 0.7), (1, 0, 0), 0.02, 0.3))
    res = scout_mesh(stl("branch", tris), scale_to_m=1.0)
    assert res.input_kind == "fluid-domain" and len(res.openings) == 4, [o.centroid for o in res.openings]
    branch = next(o for o in res.openings if abs(o.centroid[2] - 0.3) < 0.01)
    assert not branch.on_extremity and branch.equivalent_diameter == pytest.approx(0.03, rel=0.05)
    assert branch.normal[0] == pytest.approx(1.0, abs=1e-6)


def test_a_body_with_more_lids_than_a_passage_has_mouths_is_machined(stl):
    # a connector: a bar with two flat ends and thirteen pins standing out of one side, each pin's
    # flat tip a lid by every local measure - fifteen lids is a machined part, not a passage
    pytest.importorskip("OCP")
    pins = [((0, 0, 0.06 + 0.067 * k), (1, 0, 0), 0.006, 0.08) for k in range(13)]
    tris = _cylinders_fused(((0, 0, 0), (0, 0, 1), 0.05, 1.0), *pins)
    res = scout_mesh(stl("connector", tris), scale_to_m=1.0)
    assert res.flow == "external" and res.openings == []
    assert any("more than a passage has mouths" in n for n in res.notes)


def test_a_coarse_closed_body_is_a_body_in_a_flow(stl):
    # a coarse ellipsoid: every facet flat, none of them a lid
    u, v = np.meshgrid(np.linspace(0, np.pi, 9), np.linspace(0, 2 * np.pi, 13))
    P = np.stack([0.6 * np.sin(u) * np.cos(v), 0.2 * np.sin(u) * np.sin(v), 0.3 * np.cos(u)], axis=-1)
    tris = []
    for i in range(12):
        for j in range(8):
            a, b, c, d = P[i, j], P[i + 1, j], P[i, j + 1], P[i + 1, j + 1]
            tris += [(a, b, d), (a, d, c)]
    res = scout_mesh(stl("blob", np.asarray(tris).tolist()), scale_to_m=1.0)
    assert res.flow == "external" and res.openings == []


def test_a_pipe_wall_with_ring_ends_is_read_as_a_wall(stl):
    outer, inner, L = 0.06, 0.05, 1.0
    tris = _band(_ring(outer, 0.0), _ring(outer, L))                 # outside skin
    tris += _band(_ring(inner, L), _ring(inner, 0.0))                 # inside skin, wound the other way
    tris += _band(_ring(inner, 0.0), _ring(outer, 0.0))               # the two annular ends
    tris += _band(_ring(outer, L), _ring(inner, L))
    res = scout_mesh(stl("wall", tris), scale_to_m=1.0)
    assert res.body_kind == "pipe_wall" and res.input_kind == "body-surface" and res.flow == "internal"
    assert len(res.openings) == 2 and all(o.kind == "ring" for o in res.openings)
    for o in res.openings:
        assert abs(o.equivalent_diameter - 2 * inner) < 0.01      # the HOLE is the opening


def _wall(outer, inner, L, at=(0.0, 0.0, 0.0), n=32):
    """A pipe wall along z with flat ring ends, moved to `at`."""
    tris = _band(_ring(outer, 0.0, n), _ring(outer, L, n))
    tris += _band(_ring(inner, L, n), _ring(inner, 0.0, n))
    tris += _band(_ring(inner, 0.0, n), _ring(outer, 0.0, n))
    tris += _band(_ring(outer, L, n), _ring(inner, L, n))
    return [[[p[0] + at[0], p[1] + at[1], p[2] + at[2]] for p in t] for t in tris]


def test_a_pipe_wall_drawn_far_from_the_origin_on_a_rough_mesh_keeps_its_ring_ends(stl):
    """The aorta (Aorta1_offset.stl): drawn ~600 mm from the origin, its flat end rings a little
    uneven. Two triangles were 'one plane' when their distances from the ORIGIN agreed, and normals
    a fraction of a degree apart moved those by millimetres there: the rings fell apart and the
    scout found none of its five ends. The ends are holes now, found the way the stage finds them."""
    tris = np.asarray(_wall(0.06, 0.05, 0.3, at=(0.4, 0.3, 0.3), n=96))
    end = np.abs(tris[..., 2] - 0.3) < 1e-9
    tris[..., 2] += end * 2e-5 * np.sin(5000 * tris[..., 0] + 3000 * tris[..., 1])     # by position: still welded
    res = scout_mesh(stl("far", tris.tolist()), scale_to_m=1.0)
    assert res.body_kind == "pipe_wall" and res.input_kind == "body-surface" and res.flow == "internal"
    assert len(res.openings) == 2 and all(o.kind == "ring" for o in res.openings)
    holes = res.extra["holes"]                                   # what the stage's click snaps to
    assert len(holes) == 2 and all(abs(h["diameter_mm"] - 100.0) < 1.0 for h in holes)


def test_a_small_hole_beside_a_big_one_is_still_proposed(stl):
    # the aorta's 3.7 mm branch beside its 27.7 mm root: under 2 % of the largest's area, which
    # a flat face would not be, and every hole is a real opening
    tris = _wall(0.15, 0.14, 0.5) + _wall(0.022, 0.018, 0.1, at=(0.5, 0.0, 0.0))
    res = scout_mesh(stl("small", tris), scale_to_m=1.0)
    assert len(res.openings) == 4
    assert sorted(o.equivalent_diameter for o in res.openings) == pytest.approx([0.036, 0.036, 0.28, 0.28], abs=0.002)


def test_a_box_is_a_solid_body_in_a_flow(stl):
    res = scout_mesh(stl("box", _box_triangles((0, 0, 0), (2.0, 1.0, 0.8))), scale_to_m=1.0)
    assert res.flow == "external" and res.input_kind == "solid-body"
    assert res.openings == []
    assert res.extra["flow_axis_guess"] == "+x"


def test_a_block_of_boxes_on_the_ground_is_a_scene(stl):
    tris = []
    for i in range(3):
        for j in range(3):
            x, y = 3.0 * i, 3.0 * j
            tris += _box_triangles((x, y, 0.0), (x + 1.0, y + 1.0, 2.0 + i))
    res = scout_mesh(stl("city", tris), scale_to_m=1.0)
    assert res.flow == "external" and res.solids == 9 and res.body_kind == "scene"
    assert res.extra["grounded"] is True
    assert any("scene" in n for n in res.notes)


def test_the_unit_scales_every_measurement(stl):
    p = stl("mm_tube", _tube(r=50.0, length=1000.0))
    res = scout_mesh(p, scale_to_m=0.001)
    assert abs(res.size[2] - 1.0) < 1e-6
    assert abs(res.openings[0].equivalent_diameter - 0.1) < 0.006
    skin = write_mesh_skin(p, p.with_name("skin.stl"), scale_to_m=0.001)
    assert np.abs(read_triangles(skin)).max() <= 1.0 + 1e-6


def test_obj_files_read_the_same_triangles(tmp_path):
    p = tmp_path / "box.obj"
    lo, hi = (0, 0, 0), (1, 1, 1)
    P = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
         (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]), (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]
    F = [(1, 4, 3, 2), (5, 6, 7, 8), (1, 2, 6, 5), (2, 3, 7, 6), (3, 4, 8, 7), (4, 1, 5, 8)]
    p.write_text("".join(f"v {x} {y} {z}\n" for x, y, z in P) + "".join("f " + " ".join(map(str, f)) + "\n" for f in F))
    tris = read_triangles(p)
    assert tris.shape == (12, 3, 3)
    res = scout_mesh(p, scale_to_m=1.0)
    assert res.flow == "external" and res.solids == 1


def test_the_mesh_scouts_result_is_plain_json(tmp_path):
    """The mesh scout measures with numpy; its result is stored as JSON. One numpy bool in a face
    record (clear_ahead, on_extremity) refused the whole store, and the check waited forever."""
    import json

    import numpy as np

    from meshpipeline.cad.scout_mesh import scout_mesh

    tris = np.array([  # an open box: five faces of a 10 mm cube, the top missing - a rim and four flats
        [[0, 0, 0], [10, 0, 0], [10, 10, 0]], [[0, 0, 0], [10, 10, 0], [0, 10, 0]],
        [[0, 0, 0], [0, 0, 10], [10, 0, 10]], [[0, 0, 0], [10, 0, 10], [10, 0, 0]],
        [[10, 0, 0], [10, 0, 10], [10, 10, 10]], [[10, 0, 0], [10, 10, 10], [10, 10, 0]],
        [[10, 10, 0], [10, 10, 10], [0, 10, 10]], [[10, 10, 0], [0, 10, 10], [0, 10, 0]],
        [[0, 10, 0], [0, 10, 10], [0, 0, 10]], [[0, 10, 0], [0, 0, 10], [0, 0, 0]],
    ], dtype=float)
    path = tmp_path / "box.stl"
    with path.open("w") as fh:
        print("solid box", file=fh)
        for tri in tris:
            print("facet normal 0 0 0", file=fh)
            print("outer loop", file=fh)
            for v in tri:
                print(f"vertex {v[0]} {v[1]} {v[2]}", file=fh)
            print("endloop", file=fh)
            print("endfacet", file=fh)
        print("endsolid box", file=fh)
    r = scout_mesh(path, scale_to_m=0.001)
    d = r.as_dict()
    d.update(getattr(r, "extra", {}) or {})
    text = json.dumps(d)                         # raises TypeError on a numpy scalar
    assert '"on_extremity"' in text and '"clear_ahead"' in text


def test_the_mesh_scouts_openings_carry_plain_bool_flags(tmp_path):
    """A tube open at both ends is internal flow, so its two rims survive as proposed openings.
    Each opening's flags are measured with numpy and must come out as plain bools, or the check
    store refuses the whole record."""
    import json
    import math

    from meshpipeline.cad.scout_mesh import scout_mesh

    n, r, length = 24, 5.0, 40.0
    ring0 = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), 0.0) for i in range(n)]
    ring1 = [(x, y, length) for x, y, _ in ring0]
    path = tmp_path / "tube.stl"
    with path.open("w") as fh:
        print("solid tube", file=fh)
        for i in range(n):
            j = (i + 1) % n
            for tri in ((ring0[i], ring0[j], ring1[j]), (ring0[i], ring1[j], ring1[i])):
                print("facet normal 0 0 0", file=fh)
                print("outer loop", file=fh)
                for v in tri:
                    print(f"vertex {v[0]} {v[1]} {v[2]}", file=fh)
                print("endloop", file=fh)
                print("endfacet", file=fh)
        print("endsolid tube", file=fh)
    r = scout_mesh(path, scale_to_m=0.001)
    assert r.flow == "internal" and len(r.openings) == 2, (r.flow, len(r.openings))
    for o in r.openings:
        d = o.as_dict()
        assert type(d["on_extremity"]) is bool and type(d["clear_ahead"]) is bool, d
    json.dumps(r.as_dict())                     # raises TypeError on a numpy scalar

