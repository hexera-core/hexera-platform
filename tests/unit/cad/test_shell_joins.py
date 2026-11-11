# Responsibility: Hold the joining stage of the shell mid-surface extraction (cad/midsurface.py: the closing of
# open edges and the reading of what lies past them) to small parts of KNOWN geometry: an edge carried on piece by
# piece with the wall it meets (SHELL-30), no join with a face whose boundary does not close (SHELL-31), a wall
# ahead only when the walk comes over it within the reach (SHELL-32).
# Boundaries: parts are built in the test (tests/shell_fixtures.py) and mid-faces drawn by hand where the rule is
# read alone; OpenCASCADE only. No network, no store, no model, no mesher.
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402


def _poly(*pts):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    poly = BRepBuilderAPI_MakePolygon()
    for p in pts:
        poly.Add(gp_Pnt(*map(float, p)))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire()).Face()


def _free_edges(shape) -> list:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    e2f = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, e2f)
    fmap = ms._map(shape, TopAbs_FACE)
    out = []
    for i in range(1, e2f.Extent() + 1):
        e = TopoDS.Edge_s(e2f.FindKey(i))
        if not BRep_Tool.Degenerated_s(e) and len({fmap.FindIndex(f) for f in e2f.FindFromIndex(i)}) == 1:
            out.append(e)
    return out


def _notched_corner(t: float = 5.0):
    """An L angle (leg z 0..t, upright x 0..t, 80 wide) with its two mid-faces drawn by hand, both stopped half a
    wall short of the corner line (x = t/2, z = t/2), the leg's notched twice along the corner (y 20..35 and
    45..60, three walls wide, 7 deep): (the body, the leg's mid-face, the upright's)."""
    h = t / 2
    leg = _poly((t, 0, h), (100, 0, h), (100, 80, h), (t, 80, h), (t, 60, h), (t + 7, 60, h), (t + 7, 45, h),
                (t, 45, h), (t, 35, h), (t + 7, 35, h), (t + 7, 20, h), (t, 20, h))
    upright = _poly((h, 0, t), (h, 80, t), (h, 80, 100), (h, 0, 100))
    return sf.l_angle(t, t), leg, upright


def test_both_walls_are_carried_on_to_the_corner_line_where_the_edge_has_one():
    # SHELL-30: the leg meets the upright over three stretches of the corner (20, 10 and 20 mm); the upright's one
    # 80 mm edge is carried down to the leg's plane over those stretches only, and the leg across to the same line
    t = 5.0
    body, leg, upright = _notched_corner(t)
    solid = ms._Solid(body, 1e-3 * t, wall=t)
    mine = [SimpleNamespace(t_max=t, k=0), SimpleNamespace(t_max=t, k=1)]
    fuzzy = 1e-3 * t
    open_at = [(t, 10.0, t / 2), (t, 40.0, t / 2), (t, 70.0, t / 2), (t / 2, 40.0, t)]
    # the older rule alone joins the upright to its nearest points on the leg: a strip laid across the corner
    old = ms.close_gaps(ms._compound([leg, upright]), [[leg], [upright]], mine, solid, fuzzy, [(t / 2, 40.0, t)])
    assert old is not None and any(hi[0] - lo[0] > 1.0 and hi[2] - lo[2] > 1.0
                                   for lo, hi in (ms._box(f) for _i, _j, f in old[2]))
    got = ms.carry_on(ms._compound([leg, upright]), [[leg], [upright]], mine, solid, fuzzy, open_at)
    assert got is not None
    fused, images, strips = got
    # three strips of the upright, in its plane, and three of the leg, in its plane, each half a wall wide; a piece
    # ends where the leg does (within the 30 degrees a border may run off square: 1.45 mm past a notch's corner)
    made = [(i, ms._area(f), ms._box(f)) for i, _j, f in strips]
    assert sorted(i for i, _a, _b in made) == [0, 0, 0, 1, 1, 1]
    reach = (t / 2) * np.tan(np.radians(ms.CARRY_TURN_DEG))
    for i in (0, 1):
        area = sum(a for k, a, _b in made if k == i)
        assert 50.0 * t / 2 <= area <= (50.0 + 4 * reach) * t / 2 + 1e-6
    for i, _a, (lo, hi) in made:
        if i == 0:
            assert lo[2] == pytest.approx(t / 2, abs=1e-6) and hi[2] == pytest.approx(t / 2, abs=1e-6)
        else:
            assert lo[0] == pytest.approx(t / 2, abs=1e-6) and hi[0] == pytest.approx(t / 2, abs=1e-6)
    # along the corner line the two walls now share their edges: no free edge is left on it
    on_line = [e for e in _free_edges(fused)
               if all(abs(p[0] - t / 2) < 1e-6 and abs(p[2] - t / 2) < 1e-6 for p in ms._edge_points(e, 2))]
    assert on_line == []
    # ... and each stretch of the leg's border is shared with its strip (no longer free)
    for y in (10.0, 40.0, 70.0):
        assert not any(ms._dist(ms._vertex((t, y, t / 2)), e) < 1e-6 for e in _free_edges(fused))
    # the upright's edge is still free across the notches (the leg is not there to meet)
    assert any(ms._dist(ms._vertex((t / 2, 27.5, t)), e) < 1e-6 for e in _free_edges(fused))
    assert ms.unsound_faces(images) == 0


def test_a_whole_edge_that_meets_the_other_mid_face_itself_is_left_to_the_older_rule():
    # SHELL-30 acts on pieces and on walls that both stop short; a web stopped at its plate's face, the plate's
    # mid-face running under it, is SHELL-22's (made as before)
    tp, ts = 5.0, 4.0
    body = sf.t_joint(tp, ts)
    plate = _poly((0, 0, tp / 2), (200, 0, tp / 2), (200, 100, tp / 2), (0, 100, tp / 2))
    web = _poly((100, 0, tp), (100, 100, tp), (100, 100, tp + 60), (100, 0, tp + 60))
    solid = ms._Solid(body, 1e-3 * ts, wall=ts)
    mine = [SimpleNamespace(t_max=tp, k=0), SimpleNamespace(t_max=ts, k=1)]
    assert ms.carry_on(ms._compound([plate, web]), [[plate], [web]], mine, solid, 1e-3 * ts, [(100, 50, tp)]) is None
    assert ms.close_gaps(ms._compound([plate, web]), [[plate], [web]], mine, solid, 1e-3 * ts, [(100, 50, tp)]) is not None


def test_the_runs_of_points_that_meet_are_read_in_order():
    assert ms._runs([False, True, True, False, True]) == [(1, 2), (4, 4)]
    assert ms._runs([True, True]) == [(0, 1)]
    assert ms._runs([False, False]) == []


def _open_face():
    """A square face whose boundary is three of its four sides: no closed loop."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Pln, gp_Pnt
    from OCP.TopoDS import TopoDS_Face, TopoDS_Wire

    pts = [gp_Pnt(0, 0, 0), gp_Pnt(10, 0, 0), gp_Pnt(10, 10, 0), gp_Pnt(0, 10, 0)]
    b = BRep_Builder()
    wire = TopoDS_Wire()
    b.MakeWire(wire)
    for p, q in zip(pts[:-1], pts[1:], strict=True):
        b.Add(wire, BRepBuilderAPI_MakeEdge(p, q).Edge())
    face = TopoDS_Face()
    from OCP.Geom import Geom_Plane

    b.MakeFace(face, Geom_Plane(gp_Pln()), 1e-7)
    b.Add(face, wire)
    return face


def test_a_face_whose_boundary_does_not_close_is_counted_once():
    # SHELL-31
    good = _poly((0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0))
    bad = _open_face()
    assert ms.unsound_faces([[good]]) == 0
    assert ms.unsound_faces([[good], [bad, bad]]) == 1


def test_a_general_join_with_an_unsound_face_is_not_used(monkeypatch):
    # SHELL-31: the general join is the one that closes a tube on a plate; told it holds a face whose boundary does
    # not close, the extraction keeps the joins already made and says so
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert {s["join"] for s in rec["sewn"]} == {"general"}
    assert not any("SHELL-31" in n for n in rec["notes"])
    monkeypatch.setattr(ms, "unsound_faces", lambda images: 2)
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert {s["join"] for s in rec["sewn"]} == {"straight"}
    assert any("SHELL-31" in n and "2 face(s)" in n for n in rec["notes"])


def _wall(k: int, face, w: float):
    return (k, face, w, ms._box(face))


def test_a_wall_beside_the_way_is_not_a_wall_ahead():
    # SHELL-32: a plate's edge with a block past it; another piece of wall lies BESIDE the way out of the edge, its
    # border 0.7 mm (a third of a wall) to the side - inside half a wall of every point of the walk, over none
    t = 2.0
    solid = ms._Solid(sf.plate_into_block(t), 1e-3 * t, wall=t)
    own = _poly((0, 0, t / 2), (170, 0, t / 2), (170, 40, t / 2), (0, 40, t / 2))
    beside = _poly((160, 40.5, t / 2), (176, 40.5, t / 2), (176, 100, t / 2), (160, 100, t / 2))
    p, d = np.array([170.0, 39.8, t / 2]), np.array([1.0, 0.0, 0.0])
    assert not ms._wall_ahead(p, d, 1, [_wall(1, own, t), _wall(2, beside, t)], t, solid)
    # the reading before this rule took it for a wall to meet
    assert ms._dist(ms._vertex(p + d * 0.625), beside) <= 0.5 * t * ms.IN_WALL_SLACK
    assert not ms._over_face(p + d * 0.625, beside, solid.tol, ms.AHEAD_OVER_CLEAR * t)


def test_a_wall_in_line_past_a_gap_is_still_a_wall_ahead():
    # SHELL-32 keeps SHELL-21's own case: the next piece of wall in line, 2 mm (one wall) on
    t = 2.0
    solid = ms._Solid(sf.plate_into_block(t), 1e-3 * t, wall=t)
    own = _poly((0, 0, t / 2), (170, 0, t / 2), (170, 40, t / 2), (0, 40, t / 2))
    on = _poly((172, 0, t / 2), (180, 0, t / 2), (180, 100, t / 2), (172, 100, t / 2))
    p, d = np.array([170.0, 20.0, t / 2]), np.array([1.0, 0.0, 0.0])
    assert ms._wall_ahead(p, d, 1, [_wall(1, own, t), _wall(2, on, t)], t, solid)
    assert ms._over_face(np.array([173.0, 20.0, t / 2 + 0.5]), on, solid.tol, ms.AHEAD_OVER_CLEAR * t)


def test_a_wall_whose_mid_face_lies_past_the_reach_is_not_a_wall_ahead():
    # SHELL-32: the reach is half the other wall plus two walls (5 mm here) to the other wall's MID-FACE; a wall
    # standing across the way 5.9 mm on has its slab inside the walk's last step and its mid-face past the reach
    t = 2.0
    solid = ms._Solid(sf.plate_into_block(t), 1e-3 * t, wall=t)
    own = _poly((0, 0, t / 2), (170, 0, t / 2), (170, 100, t / 2), (0, 100, t / 2))
    p, d = np.array([170.0, 50.0, t / 2]), np.array([1.0, 0.0, 0.0])
    far = _poly((175.9, 0, 0.2), (175.9, 100, 0.2), (175.9, 100, 19.0), (175.9, 0, 19.0))
    assert not ms._wall_ahead(p, d, 1, [_wall(1, own, t), _wall(2, far, t)], t, solid)
    near = _poly((174.0, 0, 0.2), (174.0, 100, 0.2), (174.0, 100, 19.0), (174.0, 0, 19.0))
    assert ms._wall_ahead(p, d, 1, [_wall(1, own, t), _wall(2, near, t)], t, solid)


def test_a_web_stopped_at_its_plates_face_is_still_open():
    # SHELL-32 changes nothing for a junction the joins did not close (SHELL-21's own test, held here too)
    tp, ts = 5.0, 4.0
    body = sf.t_joint(tp, ts)
    plate = _poly((0, 0, tp / 2), (200, 0, tp / 2), (200, 100, tp / 2), (0, 100, tp / 2))
    web = _poly((100, 0, tp), (100, 100, tp), (100, 100, tp + 60), (100, 0, tp + 60))
    solid = ms._Solid(body, 1e-3 * ts, wall=ts)
    for mixed in (True, False):
        row = ms.edge_census(ms._compound([plate, web]), {1: tp, 2: ts}, solid, scale=1.0, mixed=mixed)
        assert row["open_edges"] == 1 and row["solid_link_edges"] == 0 and row["rim_edges"] == 0


def test_the_notched_corner_is_the_same_twice():
    t = 5.0
    out = []
    for _ in range(2):
        body, leg, upright = _notched_corner(t)
        solid = ms._Solid(body, 1e-3 * t, wall=t)
        mine = [SimpleNamespace(t_max=t, k=0), SimpleNamespace(t_max=t, k=1)]
        open_at = [(t, 10.0, t / 2), (t, 40.0, t / 2), (t, 70.0, t / 2), (t / 2, 40.0, t)]
        fused, images, strips = ms.carry_on(ms._compound([leg, upright]), [[leg], [upright]], mine, solid, 1e-3 * t,
                                            open_at)
        out.append(([(i, j, round(ms._area(f), 9)) for i, j, f in strips],
                    [round(ms._area(f), 9) for f in ms._faces(fused)]))
    assert out[0] == out[1]


def test_every_open_edge_is_offered_to_the_closing_passes_not_only_those_the_record_lists(monkeypatch):
    # SHELL-33: the record lists at most 50 open edges per body; the passes read the census's full list. Here the
    # listed points are emptied (as for a body with more open edges than the record lists): the T is still closed
    monkeypatch.setattr(ms, "extend", lambda *a, **k: None)
    monkeypatch.setattr(ms, "_join_general", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    orig = ms.edge_census

    def listed_none(*a, **k):
        row = orig(*a, **k)
        row["open_at_mm"] = []
        return row

    monkeypatch.setattr(ms, "edge_census", listed_none)
    rec, _ = ms.extract(sf.t_joint(5.0, 4.0), mm_per_unit=1.0)
    row = next(s for s in rec["sewn"] if s["body"] == "Part_1")
    assert row["open_edges"] == 0
    assert ms.OPEN_ALL_KEY not in row
