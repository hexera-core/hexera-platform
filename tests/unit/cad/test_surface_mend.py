# Responsibility: Verify SURF-12 (cad/surface_mend, through cad/surface_model.read): on a surface file, places where
# the surfaces do not meet are made to meet by changing the surfaces - an edge stopping short is extended along its
# own surface, an overlap is trimmed back to its neighbour's edge, a step is moved onto its neighbour, a web's foot
# short of its plate is extended onto it - within the reach, each change told; beyond the reach the place stays open
# and is told why; "do not change the surfaces" changes nothing; a slot drawn in a face is never closed; the open-place
# count goes down by exactly the places closed; the same file twice reads the same.
# Boundaries: small shapes drawn here with OpenCASCADE (tests/surface_fixtures); no mesh, no file.
from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.cad import surface_model as sm  # noqa: E402


def _read(shape, **kw):
    return sm.read(shape, mm_per_unit=1.0, said_shell=True, **kw)


def _area(reading) -> float:
    return sum(s["area_mm2"] for s in reading["sheets"])


def _web(drop: float):
    """A plate and an upright web whose foot stops `drop` above it."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.pi / 2)
    web = BRepBuilderAPI_Transform(sx.rect(20, 0, 180, 40), t, True).Shape()
    return sx.compound([sx.rect(0, -50, 200, 50), sx.moved(web, (0, 0, drop))])


def test_an_edge_stopping_short_is_extended_along_its_own_surface():
    reading, sewn = _read(sx.compound([sx.rect(0, 0, 100, 80), sx.rect(101.0, 0, 200, 80)]))
    c, m = reading["cleaned"], reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["extended"] == 1 and m["changes"][0]["how"].startswith("extended")
    assert len(reading["sheets"]) == 1 and c["open_places"] == 0
    assert c["places_before"] == 1 and c["closed_places"] == 1
    assert _area(reading) == pytest.approx(200.0 * 80.0, rel=1e-6)           # the gap is filled, nothing more
    assert sewn.names == ["Face_1", "Face_2"]


def test_an_overlap_is_trimmed_back_to_its_neighbours_edge():
    reading, _sewn = _read(sx.compound([sx.rect(0, 0, 101.0, 80), sx.rect(100, 0, 200, 80)]))
    c, m = reading["cleaned"], reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["trimmed"] == 1
    assert len(reading["sheets"]) == 1 and c["open_places"] == 0
    assert _area(reading) == pytest.approx(200.0 * 80.0, rel=1e-6)           # the double layer is gone


def test_a_step_is_moved_onto_its_neighbour():
    reading, _sewn = _read(sx.compound([sx.rect(0, 0, 100, 80), sx.moved(sx.rect(100, 0, 200, 80), (0, 0, 1.0))]))
    c, m = reading["cleaned"], reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["moved"] == 1
    assert len(reading["sheets"]) == 1 and c["open_places"] == 0


def test_a_web_stopping_short_of_its_plate_is_extended_onto_it():
    reading, _sewn = _read(_web(1.0))
    c, m = reading["cleaned"], reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["extended"] == 1
    assert len(reading["sheets"]) == 1 and c["open_places"] == 0
    assert sum(s["junction_edges"] + s["shared_edges"] for s in reading["sheets"]) >= 1


def test_beyond_the_reach_nothing_is_changed_and_the_notes_reach_closes_it():
    """5 mm on a 220 mm strip is past 1/100 of the part: not an open place (SURF-8), nothing changed. "close gaps up to
    6 mm" lets the change reach it."""
    shape = sx.compound([sx.rect(0, 0, 100, 80), sx.rect(105.0, 0, 205, 80)])
    reading, _sewn = _read(shape)
    assert reading["cleaned"]["mend"]["closed"] == 0 and len(reading["sheets"]) == 2
    said, _sewn = _read(shape, sew_mm=6.0)
    m = said["cleaned"]["mend"]
    assert m["closed"] == 1 and m["reach_from"] == "notes" and len(said["sheets"]) == 1
    assert said["cleaned"]["open_places"] == 0


def test_do_not_change_the_surfaces_changes_nothing():
    shape = sx.compound([sx.rect(0, 0, 100, 80), sx.rect(101.0, 0, 200, 80)])
    reading, _sewn = _read(shape, mend=False)
    assert reading["cleaned"]["mend"] == {"off": True} and reading["cleaned"]["open_places"] == 1
    assert len(reading["sheets"]) == 2


def test_a_slot_drawn_in_a_face_is_never_closed():
    shape = sx.plate_with_slot(slot=1.0, run=40.0)
    reading, _sewn = _read(shape)
    assert reading["cleaned"]["mend"].get("closed", 0) == 0
    assert reading["sheets"][0]["loop_count"] == 2


def test_the_open_count_goes_down_by_exactly_the_places_closed_and_twice_is_the_same():
    shape = sx.compound([sx.rect(0, 0, 100, 80), sx.rect(101.0, 0, 200, 80), sx.moved(sx.rect(200, 0, 300, 80), (0, 0, 1.0)),
                         sx.rect(306.0, 0, 400, 80)])
    a, _ = _read(shape)
    c = a["cleaned"]
    assert c["places_before"] - c["closed_places"] == c["open_places"]
    b, _ = _read(shape)
    strip = lambda r: json.dumps(r, sort_keys=True, default=str)  # noqa: E731
    assert strip(a) == strip(b)


def _slanted(cut_back: float):
    """A 300 x 200 plate cut in two along a slanted line; the right piece's slanted edge cut back by `cut_back` -
    two TRIMMED faces (the plate's own plane runs on past every cut)."""
    import numpy as np
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    def wall(p, n):
        t = np.cross(n, [0, 0, 1.0])
        t /= np.linalg.norm(t)
        z = np.array([0, 0, 1.0])
        return _quad([p + a * 800 * t + b * 50 * z for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])

    def cut(face, tool):
        args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        args.Append(face)
        tools.Append(tool)
        sp = BRepAlgoAPI_Splitter()
        sp.SetArguments(args)
        sp.SetTools(tools)
        sp.Build()
        return sx.faces_of(sp.Shape())

    n = np.array([200.0, 60.0, 0.0])
    n /= np.linalg.norm(n)
    p = np.array([150.0, 100.0, 0.0])
    left, right = sorted(cut(sx.rect(0, 0, 300, 200), wall(p, n)), key=lambda f: _cx(f))
    if cut_back:
        right = max(cut(right, wall(p + cut_back * n, n)), key=lambda f: _cx(f))
    return sx.compound([left, right])


def _quad(pts):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    poly = BRepBuilderAPI_MakePolygon()
    for q in pts:
        poly.Add(gp_Pnt(*map(float, q)))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()


def _cx(face) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    return float(g.CentreOfMass().X())


def test_a_trimmed_face_stopping_short_is_extended_on_its_own_surface():
    """The coordinator's case: most faces of a real stamped file are trimmed (cut out of a larger patch). A trimmed
    face is continued by a strip of its own surface past the edge, joined into one face, and meets its neighbour."""
    reading, _sewn = _read(_slanted(1.0))
    c, m = reading["cleaned"], reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["extended"] == 1, m
    assert len(reading["sheets"]) == 1 and c["open_places"] == 0
    assert _area(reading) == pytest.approx(300.0 * 200.0, rel=1e-6)


def _bspline(fn, u, v, n: int = 9):
    """A natural B-spline patch through an n x n grid of fn(a, b) over u x v (one wire, four sides)."""
    import numpy as np
    from OCP.Approx import Approx_IsoParametric
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt

    arr = TColgp_Array2OfPnt(1, n, 1, n)
    for i, a in enumerate(np.linspace(*u, n)):
        for j, b in enumerate(np.linspace(*v, n)):
            arr.SetValue(i + 1, j + 1, gp_Pnt(*map(float, fn(a, b))))
    api = GeomAPI_PointsToBSplineSurface()
    api.Interpolate(arr, Approx_IsoParametric, False)
    return BRepBuilderAPI_MakeFace(api.Surface(), 1e-7).Face()


def _fillet_and_bead(gap: float, short: bool = False):
    """A small natural fillet patch (a quarter round of 5 mm radius, 12 mm long) rising from a 200 x 12 mm panel, and a
    small natural bead patch (10 x 12 mm, bulging 0.5 mm) standing on the fillet's top edge but starting `gap` above
    it: two 4-sided natural B-spline patches a few millimetres across with a gap between them. With `short`, the bead is
    the clean bead's own surface stopping `gap` short instead (its lower edge on the same curve, higher up)."""
    r = 5.0
    fillet = _bspline(lambda a, b: (r * math.sin(a), b, r - r * math.cos(a)), (0.0, math.pi / 2), (0.0, 12.0))
    if short:
        bead = _bspline(lambda a, b: (r + 0.5 * math.sin(math.pi * a / 10.0), b, r + a), (gap, 10.0), (0.0, 12.0))
    else:
        bead = _bspline(lambda a, b: (r + 0.5 * math.sin(math.pi * a / 10.0), b, r + gap + a), (0.0, 10.0), (0.0, 12.0))
    return sx.compound([sx.rect(-200.0, 0.0, 0.0, 12.0), fillet, bead])


@pytest.mark.parametrize("gap,short", [(1.0, False), (0.6, True), (2.0, True)])
def test_small_natural_bspline_patches_a_millimetre_apart_are_made_to_meet(gap, short):
    """0.6 to 2 mm between two small natural B-spline patches (a fillet and a bead) is inside the reach: the fillet's own
    curve bends away from the bead, so the BEAD is continued down onto the fillet's edge (both faces of a place are
    tried), and the continued bead, rebuilt through its own points, is found again after the cut (it lies within the
    fit, not on the old patch to the last digit). The open-place count goes to 0, one sheet."""
    reading, _sewn = _read(_fillet_and_bead(gap, short=short))
    m = reading["cleaned"]["mend"]
    assert m["closed"] == 1 and m["extended"] >= 1, m
    assert reading["cleaned"]["open_places"] == 0, m
    assert len(reading["sheets"]) == 1


def test_a_gap_wider_than_a_quarter_of_the_narrower_face_is_still_closed():
    """A 3.5 mm gap between a 300 x 200 panel and a 12 mm wide strip: more than a quarter of the strip (3 mm), but under
    1/100 of the part and under half the width of the face that is changed, so it is closed."""
    reading, _sewn = _read(sx.compound([sx.rect(0, 0, 300, 200), sx.rect(303.5, 0, 315.5, 200)]))
    m = reading["cleaned"]["mend"]
    assert m["closed"] == 1 and reading["cleaned"]["open_places"] == 0, m


def test_the_users_element_size_widens_the_reach_on_a_small_face():
    """A 2 mm gap between two 3 mm wide strips: half the width of either strip is 1.5 mm, so it stays open - unless the
    user asked for 4 mm elements everywhere, then a 2 mm gap is under one element and it is closed."""
    shape = sx.compound([sx.rect(0, 0, 3, 400), sx.rect(5.0, 0, 8, 400)])
    plain, _sewn = _read(shape)
    assert plain["cleaned"]["mend"]["closed"] == 0
    asked, _sewn = _read(shape, element_mm=4.0)
    assert asked["cleaned"]["mend"]["closed"] == 1 and asked["cleaned"]["open_places"] == 0


