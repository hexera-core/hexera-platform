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
