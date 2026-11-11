# Responsibility: Build the small thin-walled parts the shell (mid-surface) tests read, each of KNOWN
# geometry - plate thicknesses, junction types, holes, bends - as OpenCASCADE shapes or STEP files under
# the test's own tmp_path.
# Boundaries: generated per test, never committed. Millimetres throughout.
from __future__ import annotations

import math
from pathlib import Path

from tests.fea_fixtures import _box, _cut, _cyl, _edges_where, _fillet, _fuse, _unify


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


def bent_bracket(t: float = 2.0, r: float = 3.0, a: float = 60.0, b: float = 40.0, width: float = 50.0,
                 outer_r: float | None = None):
    """A sheet-metal angle: a `t` thick sheet bent 90 degrees at inside radius `r`, flanges `a` (along x)
    and `b` (along z) long outside the bend, `width` deep along y. Mid-surface: two planes and the
    quarter cylinder of radius r + t/2 between them, meeting tangentially. `outer_r`: the outside radius when
    the bend is NOT concentric (a CAD model rounding both corners alike: outer_r = r puts the outside arc's
    centre t off the inside one's along each flange, the wall t at the flanges and t * sqrt(2) mid-bend)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec

    ro = r + t if outer_r is None else outer_r
    c = (r + t, r + t)          # the inside arc's centre in (x, z)
    co = (ro, ro)               # the outside arc's (the same point when concentric)
    s2 = math.sqrt(0.5)

    def P(x, z):
        return gp_Pnt(x, 0, z)

    # profile in the xz plane: the horizontal flange runs along +x at z 0..t, the upright along +z at x 0..t
    pts = {
        "h_out_end": P(c[0] + a, 0), "h_in_end": P(c[0] + a, t),
        "bend_out_h": P(co[0], 0), "bend_in_h": P(c[0], t),
        "bend_out_v": P(0, co[1]), "bend_in_v": P(t, c[1]),
        "v_out_end": P(0, c[1] + b), "v_in_end": P(t, c[1] + b),
    }
    arc_out = GC_MakeArcOfCircle(pts["bend_out_h"], P(co[0] - ro * s2, co[1] - ro * s2), pts["bend_out_v"]).Value()
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


def plate_with_filleted_pad(t: float = 2.0, h: float = 10.0, r: float = 2.0):
    """A 200 x 100 x `t` plate with a 30 x 30 x `h` pad on top, its foot rounded into the plate by `r` mm fillets:
    the plate's edges at the pad lie past the fillets, further from the pad's faces than a wall."""
    s = _fuse(_box(0, 0, 0, 200, 100, t), _box(85, 35, t, 30, 30, h))

    def foot(b):
        return (abs(b[2] - t) < 1e-6 and abs(b[5] - t) < 1e-6 and b[0] >= 85 - 1e-6 and b[3] <= 115 + 1e-6
                and b[1] >= 35 - 1e-6 and b[4] <= 65 + 1e-6)

    return _unify(_fillet(s, _edges_where(s, foot), r))


def stepped_wall_through_plate(tp: float = 2.0, t_low: float = 4.0, t_high: float = 2.0, h: float = 60.0):
    """A 200 x 100 x `tp` plate (z 0..tp) crossed by an upright wall that is `t_low` thick below it and `t_high`
    above it, flush on one side (x = 102): the wall steps right where the plate crosses it, so the two wall
    pieces never touch each other - the plate parts them (a stamped heatsink's fin root)."""
    return _fuse(_fuse(_box(0, 0, 0, 200, 100, tp), _box(102 - t_low, 0, -h, t_low, 100, h)),
                 _box(102 - t_high, 0, tp, t_high, 100, h))


def plate_with_rod(t: float = 5.0, r: float = 8.0, length: float = 250.0):
    """A 200 x 100 x `t` plate with a solid round rod (radius `r`, `length` long) hung under its middle: the rod
    is round with a radius under two walls, yet no fillet - a solid region the plate is tied to."""
    return _fuse(_box(0, 0, 0, 200, 100, t), _cyl(100, 50, -length, r, length))


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


def short_step(t1: float = 3.0, t2: float = 2.6, a: float = 25.0, w: float = 20.0):
    """A short plate whose thickness steps halfway, flush underneath: `a` x `w` x `t1` then `a` x `w` x `t2` along x.
    With the defaults each half alone spans under 10 of its walls (SHELL-2: 7.5 and 8.6), the whole plate 10.5 of
    the thicker."""
    return _fuse(_box(0, 0, 0, a, w, t1), _box(a, 0, 0, a, w, t2))


def thin_web_on_thick_plate(tp: float = 8.0, ts: float = 3.0):
    """A 3 mm web standing on an 8 mm plate, its ends 20 mm short of the plate ends: the web band runs into the
    plate wall and its two sides end there, inside the plate."""
    return _fuse(_box(0, 0, 0, 200, 100, tp), _box(98, 20, tp, ts, 60, 50))


def oblique_half_pipe(r_out: float = 160.0, t: float = 3.0, length: float = 600.0, tilt_deg: float = 34.0):
    """Half of a round tube (y >= 0), `length` long along z, its top end cut by a plane tilted `tilt_deg` about
    the x axis (a hopper chute): the cut face meets the wall slantwise, so the two wall faces end at different
    heights and the mid-face's own end stops short of the cut face by a thin curved sliver of wall."""
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    from OCP.TopLoc import TopLoc_Location

    half = _cut(tube(r_out, t, length), _box(-2 * r_out, -2 * r_out, -1, 4 * r_out, 2 * r_out, length + 2))
    tr = gp_Trsf()
    tr.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0.5 * length), gp_Dir(1, 0, 0)), math.radians(tilt_deg))
    cutter = _box(-4 * r_out, -4 * r_out, 0.5 * length, 8 * r_out, 8 * r_out, length).Moved(TopLoc_Location(tr))
    return _cut(half, cutter)


def spool(tp: float = 4.0, r_out: float = 20.0, t: float = 4.0, h: float = 100.0, w: float = 120.0):
    """A tube welded between two `w` x `w` x `tp` plates (a flanged spool): a tube-on-plate junction at each end,
    with the disc of plate inside the tube a mid-face of its own (bounded by one circle). At w = 130 OCC's 2D
    offset of the top plate's mid-face left its hole's circle a 138 mm tolerance and broke the general fuse."""
    return _fuse(_fuse(_box(-w / 2, -w / 2, 0, w, w, tp), _box(-w / 2, -w / 2, h - tp, w, w, tp)),
                 tube(r_out, t, h - 2 * tp).Moved(_shift(0, 0, tp)))


def bolted_lap(t: float = 3.0, lap: float = 30.0, r: float = 3.0):
    """Two 200 x 100 x `t` plates laid one on the other over `lap` mm and fused (a lap joint, one body): the lower
    at z 0..t, x 0..200; the upper at z t..2t, x 200-lap..400-lap; two bolt holes of radius `r` through the lap.
    The lap is 2t thick and only `lap` wide, so it fails the width rule on its own (SHELL-8)."""
    s = _fuse(_box(0, 0, 0, 200, 100, t), _box(200 - lap, 0, t, 200, 100, t))
    for y in (30.0, 70.0):
        s = _cut(s, _cyl(200 - lap / 2, y, -1, r, 2 * t + 2))
    return _unify(s)


def nurbs(shape):
    """The same part with every face re-made as a B-spline (as some exporters write plain walls): its mid-faces
    are offset surfaces of B-splines."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert

    return BRepBuilderAPI_NurbsConvert(shape, True).Shape()


def gusseted_angle(tb: float = 4.0, tw: float = 3.0, tg: float = 3.0, leg: float = 40.0, relief: float = 10.0):
    """A base 120 x 60 x `tb` (z 0..tb) and an upright `tw` thick at x 0..tw, 60 high, with a triangular gusset
    `tg` thick at y 20..20+tg in their corner: legs `leg`, its inner corner cut off by a 45-degree weld relief
    `relief` long on each leg. The gusset's slanted edges run on into the base and the upright."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    poly = BRepBuilderAPI_MakePolygon()
    for x, z in ((tw + relief, tb), (tw + leg, tb), (tw, tb + leg), (tw, tb + relief)):
        poly.Add(gp_Pnt(x, 20.0, z))
    poly.Close()
    gusset = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(0, tg, 0)).Shape()
    return _fuse(_fuse(_box(0, 0, 0, 120, 60, tb), _box(0, 0, 0, tw, 60, 60)), gusset)


def short_angle(t: float = 3.0, leg: float = 16.0, length: float = 60.0):
    """An angle with short legs (`leg` x `length` x `t` each, at z 0..t and x 0..t): its corner is a fifth of
    its metal."""
    return _fuse(_box(0, 0, 0, leg, length, t), _box(0, 0, 0, t, length, leg))


def standoff_plate(t: float = 3.0, r: float = 3.0, h: float = 6.0, r_hole: float = 0.9):
    """A 100 x 80 x `t` plate with a round stand-off boss (radius `r`, `h` high) on top, a pilot hole of radius
    `r_hole` down its axis: the boss is a solid region, its cylinder a full turn - no fillet."""
    s = _fuse(_box(0, 0, 0, 100, 80, t), _cyl(50, 40, t, r, h))
    return _cut(s, _cyl(50, 40, t + 1.0, r_hole, h))


def slit_plate(t: float = 3.0, w: float = 0.5, length: float = 60.0):
    """A 200 x 100 x `t` plate with a straight slit `w` wide cut in from one edge, `length` long: narrower than
    the step the open-edge test takes past an edge."""
    return _cut(_box(0, 0, 0, 200, 100, t), _box(100 - w / 2, -1, -1, w, length + 1, t + 2))


def plugged_tube(r_out: float = 7.0, t: float = 1.2, length: float = 260.0, plug: float = 11.0, ring: float = 2.0):
    """A thin tube with a solid plug `plug` long inside each end, each plug bored through leaving a ring `ring`
    wide: the plug's end face facing into the tube is narrow (under 2 walls) but it is a side of the plug, which
    is a thick region - no chamfer."""
    r_in = r_out - t
    s = tube(r_out, t, length)
    for z0 in (0.0, length - plug):
        s = _fuse(s, _cut(_cyl(0, 0, z0, r_in, plug), _cyl(0, 0, z0 - 1, r_in - ring, plug + 2)))
    return s


# ------------------------------------------------------------------------------ variable walls (SHELL-9) ----
def _profile_xz(pts):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.gp import gp_Pnt

    poly = BRepBuilderAPI_MakePolygon()
    for x, z in pts:
        poly.Add(gp_Pnt(x, 0, z))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire()).Face()


def _revolved(pts):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt

    return _unify(BRepPrimAPI_MakeRevol(_profile_xz(pts), gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)),
                                        2 * math.pi).Shape())


def wedge_plate(t0: float = 4.0, t1: float = 8.0, length: float = 200.0, width: float = 100.0):
    """A TAPERED plate `length` x `width`, flat below (z = 0) and `t0` thick at x = 0 rising to `t1` at x = `length`:
    one wall whose thickness varies linearly, its top turned atan((t1 - t0) / length) from its bottom."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    return BRepPrimAPI_MakePrism(_profile_xz([(0, 0), (length, 0), (length, t1), (0, t0)]),
                                 gp_Vec(0, width, 0)).Shape()


def drafted_tube(r_out: float = 50.0, t0: float = 4.0, t1: float = 2.5, h: float = 120.0):
    """A tube with a straight outside (radius `r_out`) and a drafted inside: the wall `t0` thick at the bottom, `t1`
    at the top (a moulded or printed tube): a cylinder facing a cone, closed round its axis."""
    return _revolved([(r_out, 0), (r_out, h), (r_out - t1, h), (r_out - t0, 0)])


def drafted_cyclone(R: float = 100.0, r: float = 40.0, H: float = 200.0, L: float = 150.0):
    """A printed cyclone body: a cylinder (outside radius `R`, `H` high) with a drafted inside - its wall 5 mm at
    the bottom, 3 mm at the top - running into a cone down to radius `r` over `L` whose wall is slightly uneven
    (about 3.8 to 4.6 mm): two tapered walls meeting round a circle."""
    return _revolved([(R, H), (R, 0), (r, -L), (r - 4, -L), (R - 5, 0), (R - 3, H)])


def twisted_strap(w: float = 20.0, t: float = 1.27, length: float = 40.0, turn: float = 90.0, flats: float = 30.0,
                  sections: int = 9):
    """A flat bar `w` x `t` twisted `turn` degrees over `length` (lofted through `sections` turned rectangles: its two
    wide faces free-form, its wall about 1.18 to 1.27 thick) between two straight flats `flats` long, fused."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Transform
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

    def turned(y, ang):
        tr = gp_Trsf()
        tr.SetRotation(gp_Ax1(gp_Pnt(0, y, 0), gp_Dir(0, 1, 0)), ang)
        p = BRepBuilderAPI_MakePolygon()
        for x, z in ((-w / 2, -t / 2), (w / 2, -t / 2), (w / 2, t / 2), (-w / 2, t / 2)):
            p.Add(gp_Pnt(x, y, z).Transformed(tr))
        p.Close()
        return p.Wire()

    loft = BRepOffsetAPI_ThruSections(True, False)
    for i in range(sections):
        loft.AddWire(turned(length * i / (sections - 1), math.radians(turn) * i / (sections - 1)))
    loft.Build()
    s = loft.Shape()
    if flats:
        tr = gp_Trsf()
        tr.SetRotation(gp_Ax1(gp_Pnt(0, length, 0), gp_Dir(0, 1, 0)), math.radians(turn))
        far = BRepBuilderAPI_Transform(_box(-w / 2, length, -t / 2, w, flats, t), tr, True).Shape()
        s = _unify(_fuse(_fuse(s, _box(-w / 2, -flats, -t / 2, w, flats, t)), far))
    return s


def rounded_tube(w: float = 180.0, t: float = 1.0, r: float = 5.0, outer_r: float | None = None,
                 length: float = 90.0):
    """A square tube `w` wide outside, `t` thick, `length` long (along z), its corners rounded inside at `r` and
    outside at `outer_r` (default `r`: both alike, as a CAD model that rounds a box's edges and shells it gives,
    so the corners are NOT concentric - the wall t at the flats and about t * sqrt(2) mid-corner)."""
    ro = r if outer_r is None else outer_r

    def upright(x0, y0, dx, dy, z0, dz, rad):
        b = _box(x0, y0, z0, dx, dy, dz)
        return _fillet(b, _edges_where(b, lambda bb: bb[5] - bb[2] > 0.5 * dz), rad)

    return _unify(_cut(upright(0, 0, w, w, 0, length, ro), upright(t, t, w - 2 * t, w - 2 * t, -1, length + 2, r)))


def pipe_branch(R: float = 40.0, ratio: float = 1.0, angle_deg: float = 90.0, t: float = 2.0, run: float = 240.0):
    """A pipe branch: a run of outside radius R along z (centred on the origin) and a branch of radius
    ratio x R leaving it at angle_deg to the run in the xz plane, both wall t, both bores cut through - an
    equal tee at ratio 1, a lateral at 45 degrees. Every wall t, mid-cylinders R - t/2 and ratio x R - t/2."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    a = math.radians(angle_deg)
    d = gp_Dir(math.sin(a), 0.0, math.cos(a))
    r = ratio * R

    def cyl(rad, length, base, axis):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), axis), rad, length).Shape()

    z = gp_Dir(0, 0, 1)
    outer = _fuse(cyl(R, run, (0, 0, -run / 2), z), cyl(r, 120.0, (0, 0, 0), d))
    return _cut(_cut(outer, cyl(R - t, run + 2, (0, 0, -run / 2 - 1), z)), cyl(r - t, 130.0, (0, 0, 0), d))


def stepped_tube(r_out: float = 20.0, t1: float = 2.0, t2: float = 1.7, length: float = 200.0):
    """A round tube whose wall steps from `t1` to `t2` halfway along, flush outside: two mid-cylinders in line,
    (t1 - t2) / 2 apart."""
    r1, r2, half = r_out - t1, r_out - t2, length / 2
    return _revolved([(r1, 0), (r_out, 0), (r_out, length), (r2, length), (r2, half), (r1, half)])


def _prism_xz(pts, width: float = 100.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    return BRepPrimAPI_MakePrism(_profile_xz(pts), gp_Vec(0, width, 0)).Shape()


def knife_plate(t: float = 6.0, c: float = 3.0):
    """A 200 x 100 plate `t` thick whose one end is bevelled from both faces to a knife edge, `c` long: its two
    faces stop `c` short of its end."""
    return _prism_xz([(0, 0), (200 - c, 0), (200, t / 2), (200 - c, t), (0, t)])


def round_ended_plate(t: float = 4.0):
    """A 200 x 100 plate `t` thick whose one end is rounded over its whole thickness (radius t / 2)."""
    return _fuse(_box(0, 0, 0, 200, 100, t), _cyl(200, 0, t / 2, t / 2, 100, axis=(0, 1, 0)))


def plate_into_block(t: float = 2.0, hb: float = 20.0):
    """A 170 x 100 plate `t` thick running into a block 30 long and `hb` high, flush underneath."""
    return _prism_xz([(0, 0), (200, 0), (200, hb), (170, hb), (170, t), (0, t)])


def write_iges(shape, path: Path | str) -> Path:
    """The shape as an IGES file of trimmed surfaces (OpenCASCADE's faces mode: no B-rep topology), as most CAD
    systems write IGES."""
    from OCP.IGESControl import IGESControl_Writer
    from OCP.Interface import Interface_Static

    Interface_Static.SetIVal_s("write.iges.brep.mode", 0)
    w = IGESControl_Writer("MM", 0)
    w.AddShape(shape)
    w.ComputeModel()
    w.Write(str(path))
    return Path(path)


def sleep_for(seconds: float) -> None:
    """SHELL-18 tests: a bounded child's work that takes `seconds` (negative: raises ValueError)."""
    import time

    if seconds < 0:
        raise ValueError("no negative time")
    time.sleep(seconds)


def fork_and_sleep(pid_file: str) -> None:
    """SHELL-18b tests: a bounded child that forks a process of its own (as a general join does), writes the fork's
    pid, and sleeps."""
    import multiprocessing as mp
    import os
    import time

    def join() -> None:
        Path(pid_file).write_text(str(os.getpid()))
        time.sleep(120.0)

    mp.get_context("fork").Process(target=join, daemon=True).start()
    time.sleep(120.0)
