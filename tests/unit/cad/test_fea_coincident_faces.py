# Responsibility: Pin PLAN-fea-3's reading of a wall where two faces of one body lie on each other (a self-overlapping solid: a zero-thickness membrane on a wall): the coincidence is read through out to the two faces' tessellation error, never taken as a micron-thin wall; and a part with no such pair reads exactly as before.
# Boundaries: hand-built triangle surfaces (cad/fea_features.Surface) and cad/fea_features.wall_thickness only - no CAD kernel, no plan.
from __future__ import annotations

import numpy as np

from meshpipeline.cad import fea_features as ff

# THE DEFECT (job bf863215, 26 Oct). An 11-part bolted assembly's 1.2 mm tube carried four patches of its own
# bore written again with the opposite side out - membranes lying ON the inner wall, sharing no edge
# with it. Rays from the wall met them 0.00002 to 0.006 mm away: most under the 0.0054 mm floor and
# passed through, the rest just over it read as a 0.0055 mm wall. The plan held 2.8 um elements over
# 15 mm spots - 138M elements and a 961 GiB machine for an 11-part, half-metre assembly.

L, T = 20.0, 1.2          # the slab: 20 x 20 mm, 1.2 mm thick


def _grid(x0, x1, y0, y1, z, n, up: bool, tilt=(0.0, 0.0)):
    xs, ys = np.linspace(x0, x1, n + 1), np.linspace(y0, y1, n + 1)
    pts = np.array([(x, y, z + tilt[0] * (x - x0) + tilt[1] * (y - y0)) for y in ys for x in xs])
    tris = []
    for j in range(n):
        for i in range(n):
            a, b = j * (n + 1) + i, j * (n + 1) + i + 1
            c, d = a + n + 1, b + n + 1
            tris += [(a, b, d), (a, d, c)] if up else [(a, d, b), (a, c, d)]
    return pts, np.array(tris)


def _side(p0, p1, p2, p3):
    """A quad, outward by the order given (counter-clockwise seen from outside)."""
    return np.array([p0, p1, p2, p3], dtype=float), np.array([(0, 1, 2), (0, 2, 3)])


def _slab(with_membrane: bool, defl: float | None) -> ff.Surface:
    parts = [_grid(0, L, 0, L, 0.0, 20, up=False),                     # Face_1: the bottom, outward -z
             _grid(0, L, 0, L, T, 20, up=True)]                        # Face_2: the top, outward +z
    c = [(0, 0), (L, 0), (L, L), (0, L)]
    for k in range(4):
        (xa, ya), (xb, yb) = c[k], c[(k + 1) % 4]
        parts.append(_side((xa, ya, 0), (xb, yb, 0), (xb, yb, T), (xa, ya, T)))
    if with_membrane:
        # Face_7: a patch of the bottom written again, the other side out, lying ON it: a few microns
        # off at one edge (the two tessellations' wobble), touching it at the other
        parts.append(_grid(6, 14, 6, 14, 0.00002, 8, up=True, tilt=(0.0008, 0.0)))
    xyz, tris, label, base = [], [], [], 0
    for k, (p, t) in enumerate(parts):
        xyz.append(p)
        tris.append(t + base)
        label.append(np.full(len(t), k))
        base += len(p)
    n = len(parts)
    return ff.Surface(xyz=np.vstack(xyz), tris=np.vstack(tris), label=np.concatenate(label),
                      names=[f"Face_{k}" for k in range(1, n + 1)], body=np.zeros(n, dtype=np.int64),
                      tol_mm=np.full(n, 1e-7), defl_mm=None if defl is None else np.full(n, defl))


def _wall(s: ff.Surface, label: int = 0) -> float | None:
    got = ff.wall_thickness(s)["per_label"].get(label)
    return None if got is None else got[0]


def _thin(s: ff.Surface, label: int = 0) -> list[float]:
    return [sp["thickness"] for sp in (ff.wall_thickness(s).get("thin") or {}).get(label, {}).get("spots", [])]


def test_a_membrane_lying_on_a_wall_used_to_read_as_a_micron_wall():
    """Without the faces' tessellation error (a triangle file) the floor alone decides, as before."""
    s = _slab(with_membrane=True, defl=None)
    assert min([_wall(s) or T, *_thin(s)]) < 0.01


def test_a_membrane_lying_on_a_wall_is_read_through_to_the_wall():
    s = _slab(with_membrane=True, defl=0.01)
    assert abs((_wall(s) or 0.0) - T) < 1e-6
    assert _thin(s) == []
    assert ff.wall_thickness(s)["min"][0] > 1.0


def test_a_part_with_no_faces_on_each_other_reads_exactly_as_before():
    plain = ff.wall_thickness(_slab(with_membrane=False, defl=None))
    told = ff.wall_thickness(_slab(with_membrane=False, defl=0.05))
    assert plain["per_label"].keys() == told["per_label"].keys()
    for k, v in plain["per_label"].items():
        assert v[0] == told["per_label"][k][0] and v[1] == told["per_label"][k][1]


def test_only_the_pair_lying_on_each_other_is_read_past_the_floor():
    """A real thin wall elsewhere on the same part is still a wall: the band is that pair's alone."""
    s = _slab(with_membrane=True, defl=0.01)
    # the top over the bottom: 1.2 mm, untouched by the membrane's band
    assert abs((_wall(s, 1) or 0.0) - T) < 1e-6
