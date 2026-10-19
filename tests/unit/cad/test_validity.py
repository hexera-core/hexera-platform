"""The clean contract (cad/validity): every rule says yes on a clean part, no - with the place -
on a part that breaks it, and the same bytes always give the same verdict."""
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.cad.crossings import find_crossings
from meshpipeline.cad.validity import check_file, check_triangles


def _icosphere(level: int = 3, centre=(0.0, 0.0, 0.0), radius: float = 1.0):
    t = (1.0 + 5 ** 0.5) / 2.0
    v = [(-1, t, 0), (1, t, 0), (-1, -t, 0), (1, -t, 0), (0, -1, t), (0, 1, t), (0, -1, -t), (0, 1, -t),
         (t, 0, -1), (t, 0, 1), (-t, 0, -1), (-t, 0, 1)]
    f = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4), (11, 10, 2),
         (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5),
         (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    pts = [np.array(p, dtype=float) / np.linalg.norm(p) for p in v]
    faces = list(f)
    for _ in range(level):
        mid: dict = {}

        def m(a, b, pts=pts, mid=mid):
            k = (min(a, b), max(a, b))
            if k not in mid:
                p = pts[a] + pts[b]
                pts.append(p / np.linalg.norm(p))
                mid[k] = len(pts) - 1
            return mid[k]
        nxt = []
        for a, b, c in faces:
            ab, bc, ca = m(a, b), m(b, c), m(c, a)
            nxt += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        faces = nxt
    return np.asarray(pts) * radius + np.asarray(centre), np.asarray(faces, dtype=np.int64)


def _codes(report) -> set[str]:
    return {f.code for f in report.reasons}


def _two(a, b):
    (pa, fa), (pb, fb) = a, b
    return np.vstack([pa, pb]), np.vstack([fa, fb + len(pa)])


# ------------------------------------------------------------------------------ triangles ---
def test_a_closed_outward_sphere_is_clean():
    p, f = _icosphere()
    r = check_triangles(p, f)
    assert r.clean and r.complete and not r.reasons
    assert r.as_dict()["summary"] == "The geometry is clean."


def test_a_hole_is_named_with_its_place():
    p, f = _icosphere()
    r = check_triangles(p, f[4:])
    assert not r.clean and "open_boundary" in _codes(r)
    hole = next(x for x in r.reasons if x.code == "open_boundary")
    assert hole.places and len(hole.places[0]["at"]) == 3


def test_flipped_faces_and_an_inside_out_part():
    p, f = _icosphere()
    flipped = f.copy()
    flipped[:6] = flipped[:6, ::-1]
    assert "inconsistent_orientation" in _codes(check_triangles(p, flipped))
    assert "inside_out" in _codes(check_triangles(p, f[:, ::-1]))


def test_a_cavity_facing_in_is_clean():
    outer = _icosphere()
    ip, if_ = _icosphere(radius=0.5)
    p, f = _two(outer, (ip, if_[:, ::-1]))
    assert check_triangles(p, f).clean


def test_overlapping_bodies_are_not_clean_but_separate_ones_are():
    a = _icosphere()
    assert "bodies_overlap" in _codes(check_triangles(*_two(a, _icosphere(centre=(1.2, 0.0, 0.0)))))
    assert check_triangles(*_two(a, _icosphere(centre=(3.0, 0.0, 0.0)))).clean


def test_a_surface_through_itself():
    p, f = _icosphere()
    p = p.copy()
    p[0] = -1.5 * p[0]                 # one corner pushed out through the far side
    assert "self_intersection" in _codes(check_triangles(p, f))


def test_degenerate_and_duplicate_faces():
    p, f = _icosphere()
    assert "duplicate_faces" in _codes(check_triangles(p, np.vstack([f, f[:2]])))
    q = np.vstack([p, [[0.0, 0.0, 0.0]]])
    sliver = np.array([[0, 0, len(p)]])
    assert "degenerate_faces" in _codes(check_triangles(q, np.vstack([f, sliver])))


def test_coordinates_must_be_numbers_and_near_enough_to_the_origin():
    p, f = _icosphere()
    bad = p.copy()
    bad[3] = np.nan
    assert _codes(check_triangles(bad, f)) == {"non_finite"}
    assert "implausible_coordinates" in _codes(check_triangles(p + 1e6, f, float32=True))
    assert check_triangles(p + 1e6, f, float32=False).clean


def test_two_bodies_meeting_at_one_corner():
    pa, fa = _icosphere(level=1)
    k = int(np.argmax(pa[:, 0]))
    pb = pa.copy()
    pb[:, 0] = 2 * pa[k, 0] - pb[:, 0]          # mirrored: the two touch at the one corner
    p, f = _two((pa, fa), (pb, fa[:, ::-1]))       # the mirror turns it inside out: turn it back
    assert "non_manifold_vertices" in _codes(check_triangles(p, f))


def test_a_flat_sheet_is_not_a_solid():
    p = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
    r = check_triangles(p, np.array([[0, 1, 2]]))
    assert {"flat", "open_boundary"} <= _codes(r)


def test_crossings_are_deterministic_and_catch_edges_crossing_exactly():
    # two identical tessellations crossing symmetrically: their edges meet each other exactly
    p, f = _two(_icosphere(), _icosphere(centre=(1.2, 0.0, 0.0)))
    one, two = find_crossings(p, f), find_crossings(p, f)
    assert one.count > 0 and one.complete
    assert np.array_equal(one.pairs, two.pairs)


# ------------------------------------------------------------------------------- B-rep -------
def _box(x=0.0, y=0.0, z=0.0, dx=10.0, dy=20.0, dz=30.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def _compound(*shapes):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def test_brep_rules():
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    from meshpipeline.cad.validity_brep import check_shape

    assert check_shape(_box()).clean
    assert check_shape(_compound(_box(), _box(x=10.0, dx=5.0))).clean          # touching
    assert "bodies_overlap" in _codes(check_shape(_compound(_box(), _box(x=5.0, y=5.0, z=5.0))))
    assert "inside_out" in _codes(check_shape(_box().Reversed()))
    faces = []
    ex = TopExp_Explorer(_box(), TopAbs_FACE)
    while ex.More():
        faces.append(ex.Current())
        ex.Next()
    opened = check_shape(_compound(*faces[:5]))
    assert "open_shell" in _codes(opened)
    assert opened.reasons[0].places[0]["at"]


def test_check_file_reads_step_and_stl(tmp_path):
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.cad.ingest.surface import write_binary_stl

    w = STEPControl_Writer()
    w.Transfer(_box(), STEPControl_AsIs)
    step = tmp_path / "box.step"
    w.Write(str(step))
    r = check_file(step)
    assert r.kind == "brep" and r.clean, r.as_dict()
    p, f = _icosphere()
    stl = tmp_path / "ball.stl"
    write_binary_stl(stl, p[f])
    r = check_file(stl)
    assert r.kind == "triangles" and r.clean
    write_binary_stl(stl, p[f[3:]])
    assert "open_boundary" in _codes(check_file(stl))


def test_an_unreadable_file_is_a_reason_not_a_crash(tmp_path):
    bad = tmp_path / "broken.stl"
    bad.write_bytes(b"solid x\nendsolid x\n")
    r = check_file(bad)
    assert not r.clean and _codes(r) & {"unreadable", "empty"}


def test_the_scout_never_fails_on_the_check(monkeypatch):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad import validity

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(validity, "check_canonical", boom)
    out = gc.clean_verdict(None, None)
    assert out["clean"] is None and "could not run" in out["summary"]


@pytest.mark.parametrize("ratio", [1e3, 1e5])
def test_offset_limit_scales_with_the_part(ratio):
    p, f = _icosphere()
    r = check_triangles(p + ratio * 2.0, f, float32=True)
    assert r.clean == (ratio < 1e4)


def _tri_box(lo, hi):
    """An outward box as 12 triangles."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    p = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return p, f


def test_a_body_inside_another_or_overlapping_on_matching_sides_is_an_overlap():
    big = _tri_box((0, 0, 0), (10, 10, 10))
    assert check_triangles(*big).clean
    inside = _two(big, _tri_box((3, 3, 3), (6, 6, 6)))
    assert "bodies_overlap" in _codes(check_triangles(*inside))
    matching = _two(big, _tri_box((5, 0, 0), (15, 10, 10)))     # shares four side planes
    assert "bodies_overlap" in _codes(check_triangles(*matching))
    touching = _two(big, _tri_box((10, 0, 0), (20, 10, 10)))    # face to face
    assert "bodies_overlap" not in _codes(check_triangles(*touching))


def test_a_body_in_a_cavity_is_not_an_overlap():
    p, f = _two(_tri_box((0, 0, 0), (10, 10, 10)), (lambda b: (b[0], b[1][:, ::-1]))(_tri_box((2, 2, 2), (8, 8, 8))))
    p, f = _two((p, f), _tri_box((4, 4, 4), (6, 6, 6)))
    assert check_triangles(p, f).clean


def test_far_from_the_origin_a_64_bit_file_still_shows_its_overlap():
    a, b = _icosphere(), _icosphere(centre=(1.2, 0.0, 0.0))
    p, f = _two(a, b)
    assert "bodies_overlap" in _codes(check_triangles(p + 1e6, f))


def test_brep_nested_and_matching_side_overlaps():
    from meshpipeline.cad.validity_brep import check_shape

    assert "bodies_overlap" in _codes(check_shape(_compound(_box(), _box(x=2.0, y=2.0, z=2.0, dx=2.0, dy=2.0, dz=2.0))))
    assert "bodies_overlap" in _codes(check_shape(_compound(_box(), _box(x=5.0))))
