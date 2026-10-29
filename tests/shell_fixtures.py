# Responsibility: Build the small thin-walled parts the shell (mid-surface) tests read, each of KNOWN
# geometry - plate thicknesses, junction types, holes, bends - as OpenCASCADE shapes or STEP files under
# the test's own tmp_path.
# Boundaries: generated per test, never committed. Millimetres throughout.
from __future__ import annotations

import math
from pathlib import Path

from tests.fea_fixtures import _box, _cut, _cyl, _fuse, _unify


def write_step(shape, path: Path | str) -> Path:
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    return Path(path)


def flat_plate(t: float = 5.0):
    """A 200 x 100 mm plate, `t` thick (z 0..t): one mid-plane at z = t/2, area 20,000 mm2."""
    return _box(0, 0, 0, 200, 100, t)


def plate_with_hole(t: float = 3.0, r: float = 10.0):
    """A 200 x 100 x `t` plate with an `r` mm hole through its middle."""
    return _cut(_box(0, 0, 0, 200, 100, t), _cyl(100, 50, -1, r, t + 2))


def t_joint(tp: float = 5.0, ts: float = 4.0, h: float = 60.0):
    """A plate 200 x 100 x `tp` (z 0..tp) with a stiffener `ts` thick standing on it at x = 98..98+ts,
    `h` high, the plate's whole width: a T junction. Mid-planes z = tp/2 and x = 98 + ts/2."""
    return _fuse(_box(0, 0, 0, 200, 100, tp), _box(98, 0, tp, ts, 100, h))


def l_angle(t1: float = 5.0, t2: float = 5.0):
    """An angle: a leg 100 x 80 x `t1` (z 0..t1) and an upright `t2` x 80 x 100 at x 0..t2: an L corner.
    Mid-planes z = t1/2 and x = t2/2, meeting at the line x = t2/2, z = t1/2."""
    return _fuse(_box(0, 0, 0, 100, 80, t1), _box(0, 0, 0, t2, 80, 100))


def box_section(w: float = 100.0, h: float = 60.0, t: float = 4.0, length: float = 300.0):
    """A closed rectangular hollow section `w` x `h` outside, wall `t`, `length` long along x: four
    walls, four L corners, a closed ring of mid-planes."""
    return _cut(_box(0, 0, 0, length, w, h), _box(-1, t, t, length + 2, w - 2 * t, h - 2 * t))


def stiffened_panel(tp: float = 6.0, ts: float = 8.0, h: float = 50.0, n: int = 3):
    """A 400 x 300 x `tp` deck plate with `n` flat-bar stiffeners `ts` thick and `h` high along y, evenly
    spaced: n T junctions (the hull panel's simplest form)."""
    s = _box(0, 0, 0, 400, 300, tp)
    for i in range(n):
        x = 400.0 * (i + 1) / (n + 1) - ts / 2
        s = _fuse(s, _box(x, 0, tp, ts, 300, h))
    return s


def tube(r_out: float = 25.0, t: float = 2.0, length: float = 200.0):
    """A round tube: outside radius `r_out`, wall `t`, `length` long along z. Mid-cylinder radius r_out - t/2."""
    return _cut(_cyl(0, 0, 0, r_out, length), _cyl(0, 0, -1, r_out - t, length + 2))


def bent_bracket(t: float = 2.0, r: float = 3.0, a: float = 60.0, b: float = 40.0, width: float = 50.0):
    """A sheet-metal angle: a `t` thick sheet bent 90 degrees at inside radius `r`, flanges `a` (along x)
    and `b` (along z) long outside the bend, `width` deep along y. Mid-surface: two planes and the
    quarter cylinder of radius r + t/2 between them, meeting tangentially."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec

    ro = r + t
    c = (r + t, r + t)          # the bend's centre in (x, z)
    s2 = math.sqrt(0.5)

    def P(x, z):
        return gp_Pnt(x, 0, z)

    # profile in the xz plane: the horizontal flange runs along +x at z 0..t, the upright along +z at x 0..t
    pts = {
        "h_out_end": P(c[0] + a, 0), "h_in_end": P(c[0] + a, t),
        "bend_out_h": P(c[0], 0), "bend_in_h": P(c[0], t),
        "bend_out_v": P(0, c[1]), "bend_in_v": P(t, c[1]),
        "v_out_end": P(0, c[1] + b), "v_in_end": P(t, c[1] + b),
    }
    arc_out = GC_MakeArcOfCircle(pts["bend_out_h"], P(c[0] - ro * s2, c[1] - ro * s2), pts["bend_out_v"]).Value()
    arc_in = GC_MakeArcOfCircle(pts["bend_in_v"], P(c[0] - r * s2, c[1] - r * s2), pts["bend_in_h"]).Value()
    E = BRepBuilderAPI_MakeEdge
    edges = [E(pts["h_in_end"], pts["h_out_end"]).Edge(), E(pts["h_out_end"], pts["bend_out_h"]).Edge(),
             E(arc_out).Edge(), E(pts["bend_out_v"], pts["v_out_end"]).Edge(),
             E(pts["v_out_end"], pts["v_in_end"]).Edge(), E(pts["v_in_end"], pts["bend_in_v"]).Edge(),
             E(arc_in).Edge(), E(pts["bend_in_h"], pts["h_in_end"]).Edge()]
    w = BRepBuilderAPI_MakeWire()
    for e in edges:
        w.Add(e)
    face = BRepBuilderAPI_MakeFace(w.Wire()).Face()
    return _unify(BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape())


def chunky_block():
    """A 50 x 40 x 30 mm block: no wall is thin against its size - a solid."""
    return _box(0, 0, 0, 50, 40, 30)


def plate_with_boss(t: float = 4.0):
    """A 200 x 100 x `t` plate with a 30 mm cube boss on top: thin where it is a plate, thick at the boss
    (a mixed part)."""
    return _fuse(_box(0, 0, 0, 200, 100, t), _box(85, 35, t, 30, 30, 30))


def two_plates_assembly(t: float = 3.0):
    """Two separate plates in one file (two bodies): 100 x 100 x `t` at z 0 and a 100 x 50 x `t` one at
    z 20 above it."""
    from tests.fea_fixtures import _compound

    return _compound(_box(0, 0, 0, 100, 100, t), _box(0, 0, 20, 100, 50, t))


# --------------------------------------------------------------------- junction classes (one per class) ----
def _shift(x, y, z):
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    tr = gp_Trsf()
    tr.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(tr)


def tube_on_plate(tp: float = 5.0, r_out: float = 20.0, t: float = 2.0, h: float = 100.0):
    """A round tube (outside radius `r_out`, wall `t`) welded standing on a 200 x 200 x `tp` plate: the
    tube-on-plate junction. Mid-surfaces: the plane z = tp/2 and the cylinder r_out - t/2, meeting on a circle."""
    return _fuse(_box(-100, -100, 0, 200, 200, tp), tube(r_out, t, h).Moved(_shift(0, 0, tp)))


def cone_cylinder(R: float = 100.0, r: float = 40.0, t: float = 3.0, H: float = 200.0, L: float = 150.0):
    """A cyclone body: a cylinder of outside radius `R` and height `H` running into a cone down to outside radius
    `r` over `L`, one wall `t` thick throughout (measured normal to each wall): the cylinder-cone junction."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt

    # the outer outline A (R, H) -> B (R, 0) -> C (r, -L); the inner one is offset t towards the axis
    ux, uz = (r - R), -L
    n = math.hypot(ux, uz)
    ux, uz = ux / n, uz / n
    nx, nz = uz, -ux                       # normal to the cone wall, turned to the axis side
    if nx > 0:
        nx, nz = -nx, -nz
    px, pz = R + nx * t, nz * t            # a point on the inner cone line
    s = ((R - t) - px) / ux
    bx, bz = R - t, pz + uz * s            # the inner corner, where the inner cylinder meets the inner cone
    s2 = (-L - pz) / uz
    cx = px + ux * s2
    poly = BRepBuilderAPI_MakePolygon()
    for x, z in ((R, H), (R, 0), (r, -L), (cx, -L), (bx, bz), (R - t, H)):
        poly.Add(gp_Pnt(x, 0, z))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    return _unify(BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 2 * math.pi).Shape())


def relief_flange(t: float = 2.0):
    """A 100 x 60 x `t` sheet with an upstand flange 40 high along its y = 0 edge that stops 5 mm short of each
    end (the relief cut): the plate's edge faces the flange over only part of its length."""
    return _fuse(_box(0, 0, 0, 100, 60, t), _box(5, 0, 0, 90, t, 40))


def mitred_tube_corner(w: float = 40.0, t: float = 3.0, length: float = 200.0):
    """Two square tubes `w` x `w`, wall `t`, welded at a mitred 90 degree corner (open far ends): four L-shaped
    walls turning the corner, no wall across the inside."""
    outer = _fuse(_box(0, 0, 0, length, w, w), _box(0, 0, 0, w, length, w))
    inner = _fuse(_box(t, t, t, length + 1 - t, w - 2 * t, w - 2 * t),
                  _box(t, t, t, w - 2 * t, length + 1 - t, w - 2 * t))
    return _cut(outer, inner)


def grillage(tp: float = 6.0, ts: float = 8.0, h: float = 60.0):
    """A 400 x 300 x `tp` deck plate with two girders along x and two along y, `ts` thick and `h` deep, crossing
    each other: T junctions on the plate and cross junctions where the girders cross."""
    s = _box(0, 0, 0, 400, 300, tp)
    for y in (100.0, 200.0):
        s = _fuse(s, _box(0, y - ts / 2, tp, 400, ts, h))
    for x in (130.0, 270.0):
        s = _fuse(s, _box(x - ts / 2, 0, tp, ts, 300, h))
    return s


def filleted_t(tp: float = 5.0, ts: float = 4.0, r: float = 4.0):
    """The T joint with `r` mm fillets at the stiffener root on both sides."""
    from tests.fea_fixtures import _edges_where, _fillet

    s = t_joint(tp, ts)

    def root(b):
        return (abs(b[2] - tp) < 1e-6 and abs(b[5] - tp) < 1e-6 and abs(b[3] - b[0]) < 1e-6
                and (abs(b[0] - 98) < 1e-6 or abs(b[0] - (98 + ts)) < 1e-6))

    return _unify(_fillet(s, _edges_where(s, root), r))


def hemmed_sheet(t: float = 2.0, r: float = 1.0, a: float = 80.0, b: float = 20.0, width: float = 50.0):
    """A `t` sheet `a` long with its end folded back 180 degrees at inside radius `r` into a hem `b` long: two flat
    walls joined tangentially by a half-cylinder bend (a canister wall's rolled edge)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec

    ro = r + t
    zc = ro                                   # bend centre at (0, zc); the long wall below it, the hem above

    def P(x, z):
        return gp_Pnt(x, 0, z)

    E = BRepBuilderAPI_MakeEdge
    outer = GC_MakeArcOfCircle(P(0, zc - ro), P(-ro, zc), P(0, zc + ro)).Value()
    inner = GC_MakeArcOfCircle(P(0, zc + r), P(-r, zc), P(0, zc - r)).Value()
    edges = [E(P(a, 0), P(0, 0)).Edge(), E(outer).Edge(), E(P(0, zc + ro), P(b, zc + ro)).Edge(),
             E(P(b, zc + ro), P(b, zc + r)).Edge(), E(P(b, zc + r), P(0, zc + r)).Edge(), E(inner).Edge(),
             E(P(0, zc - r), P(a, zc - r)).Edge(), E(P(a, zc - r), P(a, 0)).Edge()]
    w = BRepBuilderAPI_MakeWire()
    for e in edges:
        w.Add(e)
    face = BRepBuilderAPI_MakeFace(w.Wire()).Face()
    return _unify(BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape())


def duct_elbow(R: float = 300.0, r_out: float = 100.0, t: float = 2.0, angle_deg: float = 90.0):
    """A round duct elbow: a torus segment of bend radius `R`, outside radius `r_out`, wall `t`, turning
    `angle_deg`, with flat end rims: one torus wall whose mid-surface has a seam."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    ang = math.radians(angle_deg)
    outer = BRepPrimAPI_MakeTorus(R, r_out, ang).Shape()
    inner = BRepPrimAPI_MakeTorus(R, r_out - t, ang).Shape()
    return _cut(outer, inner)


def butt_step(t1: float = 6.0, t2: float = 3.0):
    """Two plates of different thickness butted in line, flush underneath: 100 x 50 x `t1` then 100 x 50 x `t2`
    along x. Their mid-planes are parallel and offset, so nothing the extension does joins them yet."""
    return _fuse(_box(0, 0, 0, 100, 50, t1), _box(100, 0, 0, 100, 50, t2))


def thin_web_on_thick_plate(tp: float = 8.0, ts: float = 3.0):
    """A 3 mm web standing on an 8 mm plate, its ends 20 mm short of the plate ends: the web band runs into the
    plate wall and its two sides end there, inside the plate."""
    return _fuse(_box(0, 0, 0, 200, 100, tp), _box(98, 20, tp, ts, 60, 50))


def spool(tp: float = 4.0, r_out: float = 20.0, t: float = 4.0, h: float = 100.0):
    """A tube welded between two 120 x 120 x `tp` plates (a flanged spool): a tube-on-plate junction at each end,
    with the disc of plate inside the tube a mid-face of its own (bounded by one circle)."""
    return _fuse(_fuse(_box(-60, -60, 0, 120, 120, tp), _box(-60, -60, h - tp, 120, 120, tp)),
                 tube(r_out, t, h - 2 * tp).Moved(_shift(0, 0, tp)))
