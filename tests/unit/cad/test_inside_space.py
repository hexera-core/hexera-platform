# Responsibility: Verify the inside-space map (cad/inside_space, docs/RULES.md INS-1..6) on shapes
# built here: a thick tube's two mouths and the deepest point on its axis, a step inside a bore and
# a hole's inner edge that are no mouths, a shallow dent in a plate that is no inside, a closed
# solid with no inside at all, a bolt hole beside a bore that is a side pocket, the stage's flow
# question, the ring lid round a centre body of any shape, and the flood leak test.
# Boundaries: numpy meshes and OpenCASCADE shapes made here (skipped where OCP is missing). No
# files beyond the test's own, no model, no storage, no engine.
from __future__ import annotations

import math

import numpy as np
import pytest
from tests.unit.cad.test_open_ends import _cyl, _occ_tris, _tube

from meshpipeline.cad import inside_space as ins
from meshpipeline.cad.open_ends import find_holes, surface

CELLS = 400_000


def _map(tris, cells=CELLS):
    return ins.read_inside(surface(np.asarray(tris, dtype=float)), cells=cells)


def test_a_thick_tube_has_one_passage_with_its_two_bore_mouths():
    m = _map(_tube(0.05, 0.04))
    assert m.has_inside()
    [p] = m.passages()
    assert len(p.mouths) == 2
    xs = sorted(round(float(m.mouth(i).centroid[0]), 3) for i in p.mouths)
    assert xs == [0.0, 0.3]
    for i in p.mouths:                                    # out of the bore, each its own way
        mo = m.mouth(i)
        assert mo.normal[0] == pytest.approx(-1.0 if mo.centroid[0] < 0.15 else 1.0, abs=1e-6)
        assert mo.diameter == pytest.approx(0.08, rel=0.02)
    # the deepest point is on the axis, about a bore radius from the wall
    assert abs(p.deepest[1]) < 0.01 and abs(p.deepest[2]) < 0.01
    assert p.clearance == pytest.approx(0.04, abs=3 * m.h)


def test_a_closed_solid_has_no_inside_and_a_deepest_point_in_it():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    m = _map(_occ_tris(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.2, 0.1, 0.1).Shape(), deflection=0.002))
    assert not m.has_inside() and not m.pockets and not m.mouths
    assert m.enclosed_volume == pytest.approx(0.002, rel=0.25)
    assert m.enclosed_deepest is not None and m.enclosed_deepest[0] == pytest.approx(0.1, abs=0.05)


def test_a_step_inside_a_bore_and_a_holes_inner_edge_are_no_mouths():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    # a 40 mm bar, a 20 mm bore through it, a 30 mm counterbore 15 mm deep at one end
    bar = _cyl(0.0, 0.0, 0.0, (1, 0, 0), 0.04, 0.12)
    bar = BRepAlgoAPI_Cut(bar, _cyl(-0.01, 0.0, 0.0, (1, 0, 0), 0.01, 0.14)).Shape()
    bar = BRepAlgoAPI_Cut(bar, _cyl(-0.01, 0.0, 0.0, (1, 0, 0), 0.015, 0.025)).Shape()
    m = _map(_occ_tris(bar, deflection=0.0005), cells=800_000)
    [p] = m.passages()
    ends = sorted((m.mouth(i) for i in p.mouths), key=lambda mo: mo.centroid[0])
    assert len(ends) == 2, [(mo.centroid, mo.diameter) for mo in m.mouths]
    assert ends[0].centroid[0] == pytest.approx(0.0, abs=1e-3)       # the counterbore's mouth, not its step
    assert ends[0].diameter == pytest.approx(0.03, rel=0.05)
    assert ends[1].centroid[0] == pytest.approx(0.12, abs=1e-3)
    assert ends[1].diameter == pytest.approx(0.02, rel=0.05)


def test_a_shallow_dish_in_a_plate_is_a_dent_not_an_inside():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    # a 200 x 200 x 20 mm plate with a 160 mm dish 4 mm deep in its top
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-0.1, -0.1, 0.0), 0.2, 0.2, 0.02).Shape()
    plate = BRepAlgoAPI_Cut(plate, _cyl(0.0, 0.0, 0.016, (0, 0, 1), 0.08, 0.01)).Shape()
    m = _map(_occ_tris(plate, deflection=0.001))
    assert not m.has_inside() and not m.mouths


def test_a_bolt_hole_beside_a_bore_is_a_side_pocket_shown_but_not_proposed():
    pytest.importorskip("OCP")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    # a pipe stub (bore 40 mm, 100 mm long) on a 200 mm flange 15 mm thick with a 10 mm bolt hole
    pipe = _cyl(0.0, 0.0, 0.0, (0, 0, 1), 0.03, 0.1)
    flange = _cyl(0.0, 0.0, 0.0, (0, 0, 1), 0.1, 0.015)
    body = BRepAlgoAPI_Fuse(pipe, flange).Shape()
    body = BRepAlgoAPI_Cut(body, _cyl(0.0, 0.0, -0.01, (0, 0, 1), 0.02, 0.12)).Shape()
    body = BRepAlgoAPI_Cut(body, _cyl(0.07, 0.0, -0.01, (0, 0, 1), 0.005, 0.04)).Shape()
    tris = _occ_tris(body, deflection=0.0005)
    m = _map(tris, cells=1_000_000)
    [p] = m.passages()
    assert len(p.mouths) == 2
    side = [q for q in m.pockets if not q.passage]
    assert len(side) == 1 and len(side[0].mouths) == 2
    holes = find_holes(tris)
    assert sorted(h.passage for h in holes) == [False, False, True, True]


def test_the_stage_asks_when_the_part_has_no_inside_and_not_when_it_has():
    from meshpipeline.application.geometry_confirmation import (
        NO_INSIDE_QUESTION,
        ONE_MOUTH_QUESTION,
        flow_question,
    )
    none = {"rule": "INS-1", "has_inside": False, "mouths_per_passage": []}
    one = {"rule": "INS-1", "has_inside": False, "mouths_per_passage": [1]}
    two = {"rule": "INS-1", "has_inside": True, "mouths_per_passage": [2]}
    ports = [{"role": "inlet"}, {"role": "outlet"}]
    assert flow_question(none, []) == NO_INSIDE_QUESTION
    assert flow_question(one, [{"role": "inlet"}]) == ONE_MOUTH_QUESTION
    assert flow_question(two, []) == ""                      # a way through: the user places the openings
    assert flow_question(none, ports) == ""                  # a body that is the fluid, with its two lids
    assert flow_question({"rule": "INS-1", "mapped": False}, []) == ""
    assert flow_question(None, []) == ""
    assert "flow around it" in NO_INSIDE_QUESTION


def test_a_ring_round_a_centre_body_of_any_shape_is_closed():
    from meshpipeline.cad.lids import LidError, ring, ring_bridged
    # the mouth: a 64-gon of radius 1; the centre body: a plus-shaped hub, which does not turn once
    # round the mouth's centre seen from it when shifted off it
    P = np.array([[math.cos(t), math.sin(t), 0.0] for t in np.linspace(0, 2 * math.pi, 64, endpoint=False)])
    arm, w = 0.6, 0.15
    plus = [(w, w), (arm, w), (arm, -w), (w, -w), (w, -arm), (-w, -arm), (-w, -w), (-arm, -w),
            (-arm, w), (-w, w), (-w, arm), (w, arm)]
    Q = np.array([[x + 0.25, y, 0.0] for x, y in plus])
    with pytest.raises(LidError):
        ring(P, Q)
    tris = ring_bridged(P, Q)
    pts = np.vstack([P, Q])
    area = 0.5 * np.linalg.norm(np.cross(pts[tris[:, 1]] - pts[tris[:, 0]], pts[tris[:, 2]] - pts[tris[:, 0]]), axis=1).sum()
    disc = 0.5 * 64 * math.sin(2 * math.pi / 64)
    hub = 4 * (arm - w) * 2 * w + (2 * w) ** 2
    assert area == pytest.approx(disc - hub, rel=1e-6)
    n = np.cross(pts[tris[:, 1]] - pts[tris[:, 0]], pts[tris[:, 2]] - pts[tris[:, 0]])
    assert np.all(n[:, 2] > 0)                                # wound with the mouth's loop


def test_the_flood_stays_in_a_closed_boundary_and_names_a_leak():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.open_ends import skin_faces
    tris = _occ_tris(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.1, 0.1, 0.1).Shape(), deflection=0.002)
    V, F = skin_faces(tris)
    leak, facts = ins.leak_test(V, F, (0.05, 0.05, 0.05), cells=200_000)
    assert leak is None and facts["sealed"]
    # take away the faces of the top: the flood gets out there
    top = np.all(V[F][:, :, 2] > 0.0999, axis=1)
    leak, facts = ins.leak_test(V, F[~top], (0.05, 0.05, 0.05), cells=200_000)
    assert leak is not None and not facts["sealed"]
    assert leak.point[2] > 0.09


def test_the_map_is_the_same_twice():
    a = _map(_tube(0.05, 0.04, rows=3)).as_dict()
    b = _map(_tube(0.05, 0.04, rows=3)).as_dict()
    assert a == b


def test_a_short_wide_passage_keeps_both_its_mouths():
    """A ring shorter than it is wide (30 mm long, an 80 mm bore): its two ends face apart, so
    they are two mouths, never one opening seen twice."""
    m = _map(_tube(0.05, 0.04, length=0.03), cells=600_000)
    [p] = m.passages()
    assert len(p.mouths) == 2 and m.has_inside()


def test_a_map_past_its_loop_budget_is_no_map(monkeypatch):
    """More loops than the map caps: a partial map would read the inside wrong, so there is no
    map and the hole finder's shape rules stand alone."""
    monkeypatch.setattr(ins, "MAX_LOOPS", 1)
    tris = _tube(0.05, 0.04)
    assert ins.read_inside_or_none(surface(tris)) is None
    assert len(find_holes(tris)) == 2                       # the shape rules' two bore mouths


def test_a_broken_surface_is_told_it_needs_cad_repair_not_asked_about_flow_around_it():
    """INS-7: detection only - open edges that edge no opening, or loose pieces, are said; nothing is healed."""
    from meshpipeline.application.geometry_confirmation import NO_INSIDE_QUESTION, flow_question, needs_cad_repair
    none = {"rule": "INS-1", "has_inside": False, "mouths_per_passage": []}
    clean = {"rule": "INS-7", "open_edges": 0, "open_edges_not_openings": 0, "pieces": 1}
    rims_only = {"rule": "INS-7", "open_edges": 64, "open_edges_not_openings": 0, "pieces": 1}
    torn = {"rule": "INS-7", "open_edges": 40, "open_edges_not_openings": 12, "pieces": 1}
    loose = {"rule": "INS-7", "open_edges": 0, "open_edges_not_openings": 0, "pieces": 3}
    assert flow_question(none, [], clean) == NO_INSIDE_QUESTION
    assert flow_question(none, [], rims_only) == NO_INSIDE_QUESTION      # open pipe ends are openings
    q = flow_question(none, [], torn)
    assert "needs CAD repair" in q and "12 open edges" in q and "flow around" not in q
    assert "3 loose pieces" in flow_question(none, [], loose)
    assert needs_cad_repair(None) == "" and needs_cad_repair(clean) == ""
    # a part with a way through is never told this here: the question is not asked at all
    assert flow_question({"rule": "INS-1", "has_inside": True, "mouths_per_passage": [2]}, [], torn) == ""


def test_the_surface_health_counts_only_open_edges_off_the_openings():
    import math as _m

    from meshpipeline.cad.open_ends import find_holes, surface
    from meshpipeline.cad.scout_mesh import surface_health

    def ring(r, z, n=24):
        return [(r * _m.cos(2 * _m.pi * i / n), r * _m.sin(2 * _m.pi * i / n), z) for i in range(n)]

    def band(a, b):
        out = []
        for i in range(len(a)):
            out += [(a[i], a[(i + 1) % len(a)], b[(i + 1) % len(a)]), (a[i], b[(i + 1) % len(a)], b[i])]
        return out

    tube = np.asarray(band(ring(0.05, 0.0), ring(0.05, 1.0)), dtype=float)
    m = surface(tube)
    h = surface_health(m, find_holes(mesh=m))
    assert h["open_edges"] == 48 and h["open_edges_not_openings"] == 0 and h["pieces"] == 1
    torn = np.delete(tube, [len(tube) // 2], axis=0)       # one triangle missing from the wall
    m = surface(torn)
    h = surface_health(m, find_holes(mesh=m))
    assert h["open_edges_not_openings"] >= 3
