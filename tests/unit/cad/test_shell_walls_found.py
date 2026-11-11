# Responsibility: Hold the wall-finding rules SHELL-40 to SHELL-43 (cad/shell_pairs.py, cad/midsurface.py) to
# walls of KNOWN geometry that the pairing used to miss: a face that fills little of its parameter box, a wall
# closed round its axis whose two faces are written in different turns, an outline whose pieces meet only within
# the file's tolerance, a free-form wall that curves between the fitted midpoints.
# Boundaries: parts and faces are built in the test; OpenCASCADE only. No network, no store, no model, no mesher.
from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402
from tests.fea_fixtures import _cut, _cyl  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import shell_pairs as sp  # noqa: E402


def _kind_faces(shape, kind):
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return [f for f in ms._faces(shape) if BRepAdaptor_Surface(f, True).GetType() == kind]


def _adaptor(face):
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return BRepAdaptor_Surface(face, True)


# ------------------------------------------------------------------ SHELL-40: the wall read on the face ----
def ring_plate(r_out: float = 200.0, width: float = 6.0, t: float = 1.0):
    """A flat ring `width` wide and `t` thick (a flange ring, a gasket): its two flat faces fill under 5% of
    their parameter box, and none of the 49 grid points lies in them."""
    return _cut(_cyl(0, 0, 0, r_out, t), _cyl(0, 0, -1, r_out - width, t + 2))


def test_a_face_that_fills_its_box_is_read_on_the_same_49_points():
    from OCP.GeomAbs import GeomAbs_Plane

    top = max(_kind_faces(sf.flat_plate(), GeomAbs_Plane), key=ms._area)
    grid = sp.face_grid(top, _adaptor(top), 1e-9)
    assert len(grid) == sp.PAIR_SAMPLES ** 2


def test_a_ring_that_fills_little_of_its_box_is_read_on_a_finer_grid():
    from OCP.GeomAbs import GeomAbs_Plane

    a, b = _kind_faces(ring_plate(), GeomAbs_Plane)
    # none of the first grid's 49 points lies in the ring (the old reading: "too few points")
    grid = sp.face_grid(a, _adaptor(a), 1e-9)
    assert len(grid) >= sp.GRID_MIN_POINTS
    assert all(194.0 < math.hypot(u, v) < 200.0 for u, v in grid)
    ok, t, spread, why = sp.even_wall(a, b, 1.0)
    assert ok and not why and t == pytest.approx(1.0) and spread == pytest.approx(0.0, abs=1e-9)


def test_a_ring_plate_is_one_shell_wall():
    rec, _ = ms.extract(ring_plate(), mm_per_unit=1.0)
    assert rec["checks"] == {"fidelity": True, "connected": True, "faces_accounted": True, "volume": True}
    assert rec["bodies"][0]["verdict"] == "shell"
    (mid,) = [m for m in rec["mids"] if m["shell"]]
    assert mid["thickness_mm"] == pytest.approx(1.0)
    assert mid["area_mm2"] == pytest.approx(math.pi * (200.0 ** 2 - 194.0 ** 2), rel=1e-5)


# ------------------------------------------------------------------ SHELL-41: feet on a closed face ----
def _drafted_tube_faces(r_out: float = 50.0, t0: float = 4.0, t1: float = 2.5, h: float = 120.0):
    """The two faces of a drafted tube's wall as a STEP file can write them: the outside cylinder running from pi
    to 3 pi round its axis, the inside cone from -pi to pi (the cone turned inward, as a solid's inner skin is)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.Geom import Geom_ConicalSurface, Geom_CylindricalSurface
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS

    ax = gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    cyl = BRepBuilderAPI_MakeFace(Geom_CylindricalSurface(ax, r_out), math.pi, 3 * math.pi, 0.0, h, 1e-7).Face()
    half = math.atan((t0 - t1) / h)                     # the inside opens upward: thinner wall at the top
    slant = h / math.cos(half)
    cone = BRepBuilderAPI_MakeFace(Geom_ConicalSurface(ax, half, r_out - t0), -math.pi, math.pi, 0.0, slant,
                                   1e-7).Face()
    return TopoDS.Face_s(cone.Reversed()), cyl


def test_a_foot_on_a_closed_face_is_read_in_the_faces_own_turn():
    _cone, cyl = _drafted_tube_faces()
    # the projection answers in the first turn (0 to 2 pi); the face runs from pi to 3 pi
    for u in (0.5, 2.0, 4.0, 6.0, 0.5 + 2 * math.pi, 4.0 - 2 * math.pi):
        assert sp.foot_on_face(cyl, u, 60.0, 1e-7)
    assert not sp.foot_on_face(cyl, 2.0, 130.0, 1e-7)
    # a face that is not closed is read as it comes
    from OCP.GeomAbs import GeomAbs_Plane

    top = max(_kind_faces(sf.flat_plate(), GeomAbs_Plane), key=ms._area)
    u0, u1, v0, v1 = _adaptor(top).FirstUParameter(), _adaptor(top).LastUParameter(), \
        _adaptor(top).FirstVParameter(), _adaptor(top).LastVParameter()
    assert sp.foot_on_face(top, 0.5 * (u0 + u1), 0.5 * (v0 + v1), 1e-7)
    assert not sp.foot_on_face(top, u1 + 1.0, 0.5 * (v0 + v1), 1e-7)


def test_a_drafted_tube_written_in_two_turns_is_one_tapered_wall_with_its_mid_face():
    cone, cyl = _drafted_tube_faces()
    tap, lo, hi, turn, why = sp.tapered_wall(cone, cyl, 1.0)
    assert tap and not why
    # read at the middles of 7 bands up the tube: half a band (1.5 mm / 14) inside each end
    assert lo == pytest.approx(2.5 + 1.5 / 14, abs=0.01) and hi == pytest.approx(4.0 - 1.5 / 14, abs=0.01)
    assert turn < 1.0
    # the cylinder lies across the whole cone: the mid-face is the cone's own outline on the fitted surface
    assert ms._covers(cyl, cone)
    got, flo, fhi, _mean, why = ms.fitted_mid(cone, cyl, hi, 1e-4 * hi, wall_wide=True)
    assert got and not why
    assert flo == pytest.approx(2.5, rel=0.02) and fhi == pytest.approx(4.0, rel=0.02)
    # a ring round the axis at the wall's middle: mean radius 50 - 3.25 / 2, 120 high
    assert sum(ms._area(f) for f in got) == pytest.approx(2 * math.pi * (50.0 - 1.625) * 120.0, rel=5e-3)


# ------------------------------------------------------------------ SHELL-42: an outline is closed ----
def _segment(p, q):
    from OCP.Geom2d import Geom2d_BSplineCurve
    from OCP.gp import gp_Pnt2d
    from OCP.TColgp import TColgp_Array1OfPnt2d
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    poles = TColgp_Array1OfPnt2d(1, 2)
    poles.SetValue(1, gp_Pnt2d(*p))
    poles.SetValue(2, gp_Pnt2d(*q))
    knots = TColStd_Array1OfReal(1, 2)
    knots.SetValue(1, 0.0)
    knots.SetValue(2, 1.0)
    mults = TColStd_Array1OfInteger(1, 2)
    mults.SetValue(1, 2)
    mults.SetValue(2, 2)
    return Geom2d_BSplineCurve(poles, knots, mults, 1)


def _square(gap: float) -> list:
    """The unit square's four sides in order, each starting `gap` off the end of the one before."""
    return [_segment((0.0, 0.0), (1.0, 0.0)), _segment((1.0 + gap, 0.0), (1.0, 1.0)),
            _segment((1.0, 1.0 + gap), (0.0, 1.0)), _segment((-gap, 1.0), (0.0, gap))]


def _flat():
    from OCP.Geom import Geom_Plane
    from OCP.gp import gp_Pln

    return Geom_Plane(gp_Pln())


def test_an_outline_whose_pieces_meet_within_the_files_tolerance_is_closed():
    # 5e-6 of the box: what real free-form walls carried (the wire builder joins ends within 1e-7 only)
    loose = _square(5e-6)
    assert ms._region_on(_flat(), [loose]) is None
    joined = ms._chained(_square(5e-6))
    for i, c in enumerate(joined):
        prev = joined[i - 1]
        assert prev.Value(prev.LastParameter()).Distance(c.Value(c.FirstParameter())) == 0.0
    face = ms._region_on(_flat(), [joined])
    assert face is not None and ms._area(face) == pytest.approx(1.0, abs=1e-4)
    from OCP.BRepCheck import BRepCheck_Analyzer

    assert BRepCheck_Analyzer(face).IsValid()


def test_ends_that_are_not_one_point_are_left_apart():
    far = ms._chained(_square(10.0 * ms.CHAIN_GAP))
    c = far[1]
    assert c.Value(c.FirstParameter()).X() == pytest.approx(1.0 + 10.0 * ms.CHAIN_GAP, abs=1e-12)


def test_an_exact_outline_is_left_as_it_is():
    exact = ms._chained(_square(0.0))
    assert [(c.Pole(1).X(), c.Pole(1).Y()) for c in exact] == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


# ------------------------------------------------------------------ SHELL-43: a finer fit ----
def _wavy_face(z0: float, taper: float, amp: float = 3.0, wave: float = 25.0, length: float = 100.0,
               width: float = 40.0):
    """A free-form face z = z0 + taper x + amp sin(2 pi x / wave) over `length` x `width`, a B-spline through
    121 x 9 of its points (0.2% of its 3 mm waves off them at most)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt

    nx, ny = 121, 9
    pts = TColgp_Array2OfPnt(1, nx, 1, ny)
    for i in range(nx):
        x = length * i / (nx - 1)
        for j in range(ny):
            pts.SetValue(i + 1, j + 1, gp_Pnt(x, width * j / (ny - 1),
                                              z0 + taper * x + amp * math.sin(2 * math.pi * x / wave)))
    fit = GeomAPI_PointsToBSplineSurface()
    fit.Interpolate(pts)
    return BRepBuilderAPI_MakeFace(fit.Surface(), 1e-7).Face()


def test_a_wall_that_curves_between_the_midpoints_is_fitted_again_through_twice_as_many():
    # a corrugated wall 2 to 2.4 mm thick, four 25 mm waves along it: 17 midpoints are 6 mm apart
    a, b = _wavy_face(0.0, 0.0), _wavy_face(2.0, 0.004)
    got, _lo, _hi, _mean, why = ms.fitted_mid(a, b, 2.4, 2.4e-4, wall_wide=True)
    assert not got and why.startswith(ms.FIT_OFF_MIDDLE)
    got, lo, hi, _mean, why = ms.fitted_mid_refined(a, b, 2.4, 2.4e-4, wall_wide=True)
    assert got and not why
    _worst, rel, n, flo, fhi = ms.fitted_fidelity(got[0], a, b)
    assert n >= 9 and rel <= ms.FIDELITY_SHARE and lo <= flo <= fhi <= hi


def test_a_wall_whose_first_fit_holds_is_fitted_once(monkeypatch):
    shape = sf.wedge_plate(4.0, 8.0)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    (pair,) = [p for p in rec["pairs"] if p.get("tapered")]
    faces = ms._faces(shape)
    a, b = (faces[int(n.split("_")[1]) - 1] for n in pair["faces"])
    calls = []
    real = ms.fitted_mid

    def counted(*args, **kwargs):
        calls.append(kwargs.get("samples"))
        return real(*args, **kwargs)

    monkeypatch.setattr(ms, "fitted_mid", counted)
    got, _lo, _hi, _mean, why = ms.fitted_mid_refined(a, b, 8.0, 8e-4)
    assert got and not why and calls == [None]


def test_an_overlap_under_a_tenth_of_the_wall_wide_is_no_mid_face(monkeypatch):
    # two plates' faces that lie across each other on a 0.05 mm strip only: a sliver, not a wall
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pln, gp_Pnt
    from OCP.TopoDS import TopoDS

    low = BRepBuilderAPI_MakeFace(gp_Pln(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))), 0.0, 50.0, 0.0, 40.0).Face()
    high = BRepBuilderAPI_MakeFace(gp_Pln(gp_Ax3(gp_Pnt(0, 0, 2), gp_Dir(0.01, 0, 1))), 49.95, 120.0, 0.0,
                                   40.0).Face()
    got, _lo, _hi, _mean, why = ms.fitted_mid(low, TopoDS.Face_s(high.Reversed()), 2.0, 2e-4, wall_wide=True)
    assert not got and why == "the two faces do not overlap"


# ------------------------------------------------------------------ SHELL-44: no ray lost on a flat face ----
def test_the_pairing_rays_read_every_flat_face_they_meet():
    from meshpipeline.cad import fea_features as ff
    from meshpipeline.cad import fea_measure as fm

    shape = sf.flat_plate(4.0)
    s, _faces, _kinds = fm._brep_surface(shape, 1.0)
    P, D, L = sp._sample_rays(s)
    eps = 1e-6 * s.diag
    hit = [None, None]
    for k, tol in enumerate((0.0, sp.RAY_TOL_MM)):
        h = np.full(len(P), -1, dtype=np.int64)
        ff.wall_reach(s, P + D * eps, D, L, eps=eps, exit_label=h, ray_tol=tol)
        hit[k] = h
    # every ray from the plate's two big faces leaves through the other one (a ray from a rim reads the plate's
    # length: not counted)
    big = sorted(range(len(s.names)), key=lambda k: -float(s.areas[s.label == k].sum()))[:2]
    from_big = np.isin(L, big)
    assert (hit[1][from_big] >= 0).all()
    assert (hit[1] >= 0).sum() >= (hit[0] >= 0).sum()
    # and where both read a wall they read the same face
    both = (hit[0] >= 0) & (hit[1] >= 0)
    assert (hit[0][both] == hit[1][both]).all()


# ------------------------------------------------------------------ SHELL-45: a bend not concentric ----
def _bend_faces(shape):
    from OCP.GeomAbs import GeomAbs_Cylinder

    return sorted(_kind_faces(shape, GeomAbs_Cylinder), key=ms._area)


def test_a_bend_whose_rounds_are_not_concentric_is_read_as_one_wall():
    # 2 mm sheet, both rounds 5 mm: the faces turn 13.7 degrees at the bend's ends, face each other squarely in its
    # middle, and the wall goes 2.4 to 2.8 mm (read at the 49 points' band middles)
    a, b = _bend_faces(sf.bent_bracket(t=2.0, r=5.0, outer_r=5.0))
    tap, lo, hi, turn, why = sp.tapered_wall(a, b, 1.0)
    assert tap and not why
    assert sp.TAPER_DEG < turn <= sp.BEND_DEG and hi <= sp.BEND_RATIO * lo
    assert lo == pytest.approx(2.0 * 1.2, rel=0.03) and hi == pytest.approx(2.0 * math.sqrt(2.0), rel=0.01)


def test_a_wall_turned_the_same_everywhere_past_the_taper_limit_is_no_bend():
    # a wedge turned 20 degrees from end to end: its faces never face each other squarely (SHELL-9's own case)
    from OCP.GeomAbs import GeomAbs_Plane

    shape = sf.wedge_plate(3.0, 40.0, 100.0)
    faces = sorted(_kind_faces(shape, GeomAbs_Plane), key=ms._area)[-2:]
    tap, _lo, _hi, turn, _why = sp.tapered_wall(faces[0], faces[1], 1.0)
    assert not tap and turn > sp.TAPER_DEG
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    assert rec["bodies"][0]["verdict"] == "solid"


def test_the_bend_ratio_is_a_tapers_gain_across_the_narrowest_shell_wall():
    assert sp.BEND_RATIO == pytest.approx(1.0 + math.tan(math.radians(sp.TAPER_DEG)) / sp.SHELL_WIDTH_RATIO)
