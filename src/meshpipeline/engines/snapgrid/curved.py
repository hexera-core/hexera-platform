# Responsibility: Put the curved surfaces of a snap-grid mesh back where the file says they are: the staircase around every cylinder and every round vent moved onto the true ellipse, so their surface areas, volumes and contacts are the file's, while every box stays exact.
# Owns: which points may move (and only within which plane), where each one goes, the guard that undoes a move that would fold a cell, and the curved-surface report (volume, side area, end areas, contacts - file against mesh).
# Boundaries: moves points of an already built topology; never changes which cell is which part.
# Collaborates with: engines/snapgrid/mesher.py (calls it between topology and writing), engines/snapgrid/boxmesh.py (the topology, the points).
"""Curved parts on a Cartesian grid.

A cylinder painted cell by cell is a staircase: its volume is close (cells are taken by their
centre), but its side is the outline of the cells - up to 4/pi = +27% too much area - and a
cylinder lying on a board touches it over a whole row of cells where the file has a line.

Here the staircase is pulled onto the true surface, the way a body-fitted mesher snaps:

* every point of a face between the cylinder and anything else, on the cylinder's side (not its
  flat ends), moves in the cylinder's cross-section to the ellipse - straight out from its axis
  (exactly the nearest point for a circle);
* a point that is also on the face of a BOX part (the board under a cylinder, a block its end
  touches) or on the domain side may move only within that face's plane, so the box stays exactly
  the file's; a point on two such planes, or on a wall between two blocks, does not move;
* moves never fold a cell: a move that would give a cell less than a third of its volume, or a
  face less than a twentieth of its area, is undone, and the rest are tried again.

The mesh keeps its topology: every cell is still the same part. What changes is reported: the
side area, the volume and the contact areas, against the file's numbers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad.ingest.ecxml import axis_of_plane


@dataclass
class Curve:
    """One curved surface: an elliptic cylinder along `axis` over [a0, a1], centre and radii in
    the two other axes (u, v in increasing order), bounding part `part` from outside (a solid
    cylinder) or from inside (a round hole in a wall)."""
    part: int
    name: str
    axis: int
    a0: float
    a1: float
    centre: tuple[float, float]
    radii: tuple[float, float]
    hole: bool = False


@dataclass
class Snapped:
    keys: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    xyz: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    rows: list[dict] = field(default_factory=list)
    undone: int = 0


def curves_of(placement, live: list[int], names: dict) -> list[Curve]:
    out = []
    for k in live:
        p = placement.parts[k]
        if p.kind == "solidCylinder":
            axis = axis_of_plane(p.objects[0].plane)
            out.append(_curve(k, names[k], p.box, axis, hole=False))
        for kind, cut, axis in p.cutters:
            if kind != "round":
                continue
            slabs = [s for ax_, _lo, _hi, s in p.walls if ax_ == axis and all(
                min(s.hi[i], cut.hi[i]) > max(s.lo[i], cut.lo[i]) for i in range(3) if i != axis)]
            if not slabs:
                continue
            c = _curve(k, names[k], cut, axis, hole=True)
            c.a0 = max(min(s.lo[axis] for s in slabs), cut.lo[axis])
            c.a1 = min(max(s.hi[axis] for s in slabs), cut.hi[axis])
            out.append(c)
    return out


def _curve(k, name, box, axis, *, hole) -> Curve:
    u, v = [i for i in range(3) if i != axis]
    return Curve(part=k, name=name, axis=axis, a0=box.lo[axis], a1=box.hi[axis],
                 centre=((box.lo[u] + box.hi[u]) / 2, (box.lo[v] + box.hi[v]) / 2),
                 radii=((box.hi[u] - box.lo[u]) / 2, (box.hi[v] - box.lo[v]) / 2), hole=hole)


# ------------------------------------------------------------------------------ geometry -----
def face_geometry(points: np.ndarray, off: np.ndarray, ids: np.ndarray):
    """Area vectors and centres of polygon faces, triangulated about their point average (what
    OpenFOAM does for a face that is not flat)."""
    n = len(off) - 1
    sizes = np.diff(off)
    fid = np.repeat(np.arange(n), sizes)
    p = points[ids]
    cen = np.zeros((n, 3))
    np.add.at(cen, fid, p)
    cen /= sizes[:, None]
    nxt = np.arange(len(ids)) + 1
    last = off[1:] - 1
    nxt[last] = off[:-1]
    q = points[ids[nxt]]
    tri = 0.5 * np.cross(p - cen[fid], q - cen[fid])
    area = np.zeros((n, 3))
    np.add.at(area, fid, tri)
    tcen = (p + q + cen[fid]) / 3.0
    mag = np.linalg.norm(tri, axis=1)
    fc = np.zeros((n, 3))
    np.add.at(fc, fid, tcen * mag[:, None])
    tot = np.zeros(n)
    np.add.at(tot, fid, mag)
    fc = np.where(tot[:, None] > 0, fc / np.maximum(tot, 1e-300)[:, None], cen)
    return area, fc


def cell_volumes(n_cells: int, owner, neighbour, area, fc, b_owner, b_area, b_fc) -> np.ndarray:
    """Cell volumes by the divergence theorem over the faces (owner side outward)."""
    vol = np.zeros(n_cells)
    np.add.at(vol, owner, np.einsum("ij,ij->i", area, fc))
    np.add.at(vol, neighbour, -np.einsum("ij,ij->i", area, fc))
    np.add.at(vol, b_owner, np.einsum("ij,ij->i", b_area, b_fc))
    return vol / 3.0


# ------------------------------------------------------------------------------ the snap -----
def snap(curves: list[Curve], points: np.ndarray, keys: np.ndarray, topo, zone: np.ndarray,
         solid_box: np.ndarray, wall_keys: np.ndarray) -> Snapped:
    """Where each curve's staircase points go. `zone` is each cell's part (-1 air); `solid_box[k]`
    says part k's faces must stay where they are (every part but a cylinder); `wall_keys` are the
    points on a wall between two blocks (they never move: they are hanging nodes of a coarser
    face)."""
    out = Snapped()
    if not curves:
        return out
    ids_i = np.searchsorted(keys, topo.face_pts)
    ids_b = np.searchsorted(keys, topo.b_pts)
    fid_i = np.repeat(np.arange(len(topo.face_off) - 1), np.diff(topo.face_off))
    fid_b = np.repeat(np.arange(len(topo.b_off) - 1), np.diff(topo.b_off))
    zo, zn = zone[topo.owner], zone[topo.neighbour]
    normal_i = _normal_axis(points, topo.face_off, ids_i)
    normal_b = _normal_axis(points, topo.b_off, ids_b)
    fc = _face_centres(points, topo.face_off, ids_i)
    fixed = np.zeros(len(points), dtype=bool)
    fixed[np.searchsorted(keys, wall_keys)] = True
    on_box = ((zo >= 0) & solid_box[np.maximum(zo, 0)]) | \
        ((zn >= 0) & solid_box[np.maximum(zn, 0)])
    domain_pts = [np.unique(ids_b[np.isin(fid_b, np.flatnonzero(normal_b == a_))])
                  for a_ in range(3)]
    new = points.copy()
    moved = np.zeros(len(points), dtype=bool)
    for c in curves:
        u, v = [i for i in range(3) if i != c.axis]
        side = (zo == c.part) ^ (zn == c.part)
        ru, rv = c.radii
        cu, cv = c.centre
        inside = (fc[:, c.axis] > c.a0 - 1e-12) & (fc[:, c.axis] < c.a1 + 1e-12)
        near = _band(fc, u, v, c)
        # this curve's staircase sides: against air or another curved part - a face it shares
        # with a box part is that box's face, and holds its points
        other = np.where(zo == c.part, zn, zo)
        other_box = (other >= 0) & solid_box[np.maximum(other, 0)] & (other != c.part)
        own = side & inside & near & (normal_i != c.axis) & ~other_box
        faces = np.flatnonzero(own)
        if not len(faces):
            continue
        pts = np.unique(ids_i[np.isin(fid_i, faces)])
        pts = pts[~fixed[pts]]
        # the planes a point must stay on: faces of box parts that are not this staircase, and
        # the domain's sides
        held = np.zeros((len(points), 3), dtype=bool)
        for a_ in (u, v):
            box_faces = np.flatnonzero(on_box & ~own & (normal_i == a_))
            held[ids_i[np.isin(fid_i, box_faces)], a_] = True
            held[domain_pts[a_], a_] = True
        on_curve = np.zeros(len(points), dtype=bool)
        for a_, slide in ((u, v), (v, u)):
            strip = np.flatnonzero(side & inside & near & other_box & (normal_i == a_))
            if not len(strip):
                continue
            r_held = c.radii[0] if a_ == u else c.radii[1]
            c_held = c.centre[0] if a_ == u else c.centre[1]
            c_slide = c.centre[1] if a_ == u else c.centre[0]
            plane = fc[strip, a_]
            tangent = np.abs(np.abs(plane - c_held) - r_held) <= TANGENT_TOL * r_held
            sp = np.unique(ids_i[np.isin(fid_i, strip[tangent])])
            sp = sp[~fixed[sp]]
            if len(sp):
                new[sp, slide] = c_slide + TANGENT_SHRINK * (points[sp, slide] - c_slide)
                moved[sp] = True
                on_curve[sp] = True
        for pi in pts.tolist():
            if on_curve[pi]:
                continue
            target = _target(points[pi], c, u, v, [a_ for a_ in (u, v) if held[pi, a_]])
            if target is None:
                continue
            new[pi] = target
            moved[pi] = True
            on_curve[pi] = True
        # the cells around the surface relaxed in the cross-section, so the snapped ring of
        # cells is not left squeezed against unmoved neighbours
        relaxed = _relax(new, points, c, u, v, topo, ids_i, ids_b, fid_i, on_curve,
                         fixed | held[:, u] | held[:, v] | moved)
        moved |= relaxed
    target = new.copy()
    undone = _guard(points, new, moved, topo, ids_i, ids_b)
    # a move undone whole is tried again part of the way (half, then a quarter): a corner of the
    # staircase that cannot reach the surface without folding its cell still goes as far as it can
    for frac in (0.5, 0.25):
        retry = moved & undone
        if not retry.any():
            break
        base = np.where((moved & ~undone)[:, None], target, points)
        trial = base.copy()
        trial[retry] = points[retry] + frac * (target[retry] - points[retry])
        again = _guard(base, trial, retry, topo, ids_i, ids_b)
        ok = retry & ~again
        new[ok] = trial[ok]
        target[ok] = trial[ok]
        undone &= ~ok
    keep = np.flatnonzero(moved & ~undone)
    out.keys = keys[keep]
    out.xyz = target[keep]
    out.undone = int((moved & undone).sum())
    return out


#: The band around an ellipse whose faces are its staircase: face centres between these multiples
#: of the radius (a staircase step is at most a cell, and a curve gets cylinder_cells across).
_BAND = (0.6, 1.4)


def _band(fc, u: int, v: int, c: Curve) -> np.ndarray:
    rho = np.hypot((fc[:, u] - c.centre[0]) / c.radii[0], (fc[:, v] - c.centre[1]) / c.radii[1])
    return (rho >= _BAND[0]) & (rho <= _BAND[1])


def _face_centres(points, off, ids):
    sizes = np.diff(off)
    fid = np.repeat(np.arange(len(off) - 1), sizes)
    cen = np.zeros((len(off) - 1, 3))
    np.add.at(cen, fid, points[ids])
    return cen / sizes[:, None]


def _normal_axis(points, off, ids) -> np.ndarray:
    """The axis each (axis-aligned) face is normal to, before anything moves."""
    p0 = points[ids[off[:-1]]]
    p1 = points[ids[off[:-1] + 1]]
    p2 = points[ids[off[:-1] + 2]]
    n = np.cross(p1 - p0, p2 - p0)
    return np.argmax(np.abs(n), axis=1)


def _target(x, c: Curve, u: int, v: int, held: list[int]):
    """Where point x goes on the ellipse: straight out from the axis, or - held on a plane of
    normal u or v - along that plane to the ellipse; None when it must stay."""
    cu, cv = c.centre
    ru, rv = c.radii
    du, dv = (x[u] - cu) / ru, (x[v] - cv) / rv
    out = x.copy()
    if not held:
        rho = math.hypot(du, dv)
        if rho < 1e-9:
            return None
        out[u] = cu + (x[u] - cu) / rho
        out[v] = cv + (x[v] - cv) / rho
    elif len(held) == 1:
        if held[0] == u:                    # u is held: slide along v
            if abs(du) >= 1.0 - 1e-12:
                return None
            s_ = math.sqrt(1.0 - du * du) * rv
            out[v] = cv + (s_ if x[v] >= cv else -s_)
        else:
            if abs(dv) >= 1.0 - 1e-12:
                return None
            s_ = math.sqrt(1.0 - dv * dv) * ru
            out[u] = cu + (s_ if x[u] >= cu else -s_)
    else:
        return None
    return out


#: A plane within this share of the radius of touching the ellipse is a tangent (a line contact).
TANGENT_TOL = 0.02
#: How far the cells resting on a tangent plane are drawn in towards the contact line (the
#: strip keeps this share of its width; the guard undoes it where a cell would fold).
TANGENT_SHRINK = 0.1


#: Relaxation sweeps of the points next to a snapped surface, and their weight.
RELAX_SWEEPS = 3
RELAX_WEIGHT = 0.3


def _relax(new, old, c: Curve, u: int, v: int, topo, ids_i, ids_b, fid_i, on_curve, frozen
           ) -> np.ndarray:
    """Laplacian relaxation, in the cross-section only, of the free points of the cells that
    touch the snapped surface (one ring each side): each moves halfway to the average of its
    cross-section neighbours, a few sweeps. Returns which points moved."""
    if not on_curve.any():
        return np.zeros(len(old), dtype=bool)
    curve_faces = np.unique(fid_i[on_curve[ids_i]])
    cells = np.unique(np.concatenate([topo.owner[curve_faces], topo.neighbour[curve_faces]]))
    mark = np.zeros(topo.n_cells, dtype=bool)
    mark[cells] = True
    faces = np.flatnonzero(mark[topo.owner] | mark[topo.neighbour])
    sizes = np.diff(topo.face_off)[faces]
    starts = topo.face_off[:-1][faces]
    idx = np.repeat(starts, sizes) + (np.arange(int(sizes.sum()))
                                      - np.repeat(np.cumsum(sizes) - sizes, sizes))
    a_ = ids_i[idx]
    nxt = idx + 1
    ends = np.repeat(starts + sizes, sizes)
    nxt = np.where(nxt >= ends, np.repeat(starts, sizes), nxt)
    b_ = ids_i[nxt]
    same = np.abs(old[a_, c.axis] - old[b_, c.axis]) <= 1e-12 * (abs(old[a_, c.axis]) + 1.0)
    a_, b_ = a_[same], b_[same]
    free = np.unique(np.concatenate([a_, b_]))
    free = free[~frozen[free] & ~on_curve[free]]
    if not len(free):
        return np.zeros(len(old), dtype=bool)
    # keep within the curve's reach along its axis, on the part's own side of the surface
    ax = old[free, c.axis]
    ru, rv = c.radii
    rho = np.hypot((old[free, u] - c.centre[0]) / ru, (old[free, v] - c.centre[1]) / rv)
    own_side = rho > 1.0 if c.hole else rho < 1.0
    free = free[(ax >= c.a0 - 1e-12) & (ax <= c.a1 + 1e-12) & own_side]
    is_free = np.zeros(len(old), dtype=bool)
    is_free[free] = True
    edges = np.concatenate([np.stack([a_, b_], 1), np.stack([b_, a_], 1)])
    edges = np.unique(edges, axis=0)
    edges = edges[is_free[edges[:, 0]]]
    if not len(edges):
        return np.zeros(len(old), dtype=bool)
    count = np.bincount(edges[:, 0], minlength=len(old)).astype(float)
    for _ in range(RELAX_SWEEPS):
        acc = np.zeros((len(old), 2))
        np.add.at(acc, edges[:, 0], new[edges[:, 1]][:, [u, v]])
        mean = acc[free] / np.maximum(count[free], 1.0)[:, None]
        cur = new[free][:, [u, v]]
        upd = (1 - RELAX_WEIGHT) * cur + RELAX_WEIGHT * mean
        new[free, u] = upd[:, 0]
        new[free, v] = upd[:, 1]
    return is_free


def _sub(topo, cells: np.ndarray):
    """The faces of a set of cells (internal and boundary), for local geometry."""
    mark = np.zeros(topo.n_cells, dtype=bool)
    mark[cells] = True
    fi = np.flatnonzero(mark[topo.owner] | mark[topo.neighbour])
    fb = np.flatnonzero(mark[topo.b_owner])
    return fi, fb


def _gather(off, flat, faces):
    sizes = np.diff(off)[faces]
    new_off = np.concatenate(([0], np.cumsum(sizes))).astype(np.int64)
    idx = np.repeat(off[:-1][faces] - new_off[:-1], sizes) + np.arange(int(new_off[-1]))
    return new_off, flat[idx]


#: A face whose two cells' centres stand further off its normal than this after a move undoes the
#: move (the product's bar on non-orthogonality is 65 degrees).
MAX_SNAP_ANGLE_DEG = 65.0
#: ...and a face this skewed (OpenFOAM's measure; checkMesh flags faces above 4).
MAX_SNAP_SKEW = 3.5


def _guard(old, new, moved, topo, ids_i, ids_b, *, rounds: int = 16) -> np.ndarray:
    """Points whose move is undone: those of any face whose area would shrink below a twentieth
    or flip, of any cell whose volume would fall below a third, and of any face whose cells'
    centres would stand more than MAX_SNAP_ANGLE_DEG off its normal (and more than before) -
    repeated until none is. Only the cells around moved points (and their neighbours, whose
    centres the angles need) are measured."""
    undone = np.zeros(len(old), dtype=bool)
    if not moved.any():
        return undone
    fid_i = np.repeat(np.arange(len(topo.face_off) - 1), np.diff(topo.face_off))
    fid_b = np.repeat(np.arange(len(topo.b_off) - 1), np.diff(topo.b_off))
    touched_i = np.unique(fid_i[moved[ids_i]])
    touched_b = np.unique(fid_b[moved[ids_b]])
    ring1 = np.unique(np.concatenate([topo.owner[touched_i], topo.neighbour[touched_i],
                                      topo.b_owner[touched_b]]))
    f1, _b1 = _sub(topo, ring1)
    cells = np.unique(np.concatenate([ring1, topo.owner[f1], topo.neighbour[f1]]))
    fi, fb = _sub(topo, cells)
    off_i, pts_i = _gather(topo.face_off, ids_i, fi)
    off_b, pts_b = _gather(topo.b_off, ids_b, fb)
    local = np.full(topo.n_cells, -1, dtype=np.int64)
    local[cells] = np.arange(len(cells))
    own_i, nei_i, own_b = local[topo.owner[fi]], local[topo.neighbour[fi]], local[topo.b_owner[fb]]
    both = (own_i >= 0) & (nei_i >= 0)
    n = len(cells)

    def measure(pts):
        a_i, c_i = face_geometry(pts, off_i, pts_i)
        a_b, c_b = face_geometry(pts, off_b, pts_b)
        # cell centroids the way OpenFOAM takes them: pyramids from the faces' mean centre
        cnt = np.zeros(n)
        c0 = np.zeros((n, 3))
        for idx, cen in ((own_i, c_i), (nei_i, c_i), (own_b, c_b)):
            ok = idx >= 0
            np.add.at(c0, idx[ok], cen[ok])
            np.add.at(cnt, idx[ok], 1.0)
        c0 /= np.maximum(cnt, 1.0)[:, None]
        vol = np.zeros(n)
        mom = np.zeros((n, 3))
        for idx, area, cen, sign in ((own_i, a_i, c_i, 1.0), (nei_i, a_i, c_i, -1.0),
                                     (own_b, a_b, c_b, 1.0)):
            ok = idx >= 0
            j = idx[ok]
            dv = sign * np.einsum("ij,ij->i", area[ok], cen[ok] - c0[j]) / 3.0
            pc = c0[j] + 0.75 * (cen[ok] - c0[j])
            np.add.at(vol, j, dv)
            np.add.at(mom, j, pc * dv[:, None])
        tiny = np.abs(vol) <= 1e-30
        cc = np.where(tiny[:, None], c0, mom / np.where(tiny, 1.0, vol)[:, None])
        own_c, nei_c = cc[np.maximum(own_i, 0)], cc[np.maximum(nei_i, 0)]
        d = nei_c - own_c
        cos = np.einsum("ij,ij->i", a_i, d) / np.maximum(
            np.linalg.norm(a_i, axis=1) * np.linalg.norm(d, axis=1), 1e-300)
        # OpenFOAM's face skewness: how far the line between the cells' centres passes from the
        # face centre, against the face's own extent in that direction
        cpf = c_i - own_c
        sv = cpf - (np.einsum("ij,ij->i", a_i, cpf)
                    / (np.einsum("ij,ij->i", a_i, d) + 1e-300))[:, None] * d
        svn = np.linalg.norm(sv, axis=1)
        sv_hat = sv / (svn + 1e-300)[:, None]
        reach = np.abs(np.einsum("ij,ij->i", np.repeat(sv_hat, np.diff(off_i), axis=0),
                                 pts[pts_i] - np.repeat(c_i, np.diff(off_i), axis=0)))
        fd = np.maximum.reduceat(reach, off_i[:-1]) if len(reach) else np.zeros(0)
        fd = np.maximum(fd, 0.2 * np.linalg.norm(d, axis=1)) + 1e-300
        skew = svn / fd
        return a_i, a_b, vol, np.where(both, cos, 1.0), np.where(both, skew, 0.0)

    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        a0_i, a0_b, v0, cos0, skew0 = measure(old)
    m0_i = np.einsum("ij,ij->i", a0_i, a0_i)
    m0_b = np.einsum("ij,ij->i", a0_b, a0_b)
    cos_bar = min(math.cos(math.radians(MAX_SNAP_ANGLE_DEG)), 1.0)
    fid_si = np.repeat(np.arange(len(fi)), np.diff(off_i))
    fid_sb = np.repeat(np.arange(len(fb)), np.diff(off_b))
    for _ in range(rounds):
        cur = np.where((moved & ~undone)[:, None], new, old)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            a_i, a_b, vol, cos, skew = measure(cur)
        bad_i = np.einsum("ij,ij->i", a_i, a0_i) < m0_i / 20.0
        bad_i |= both & (cos < cos_bar) & (cos < cos0 - 1e-9)
        bad_b = np.einsum("ij,ij->i", a_b, a0_b) < m0_b / 20.0
        bad_c = vol < v0 / 3.0
        if bad_c.any():
            bad_i |= (own_i >= 0) & bad_c[np.maximum(own_i, 0)] | \
                (nei_i >= 0) & bad_c[np.maximum(nei_i, 0)]
            bad_b |= bad_c[own_b]
        # a face whose angle went bad: undo the moves of its own points and of its two cells'
        bad_cells = np.zeros(n, dtype=bool)
        tilt = both & (((cos < cos_bar) & (cos < cos0 - 1e-9))
                       | ((skew > MAX_SNAP_SKEW) & (skew > skew0 + 1e-9)))
        bad_cells[own_i[tilt]] = True
        bad_cells[nei_i[tilt]] = True
        bad_i |= (own_i >= 0) & bad_cells[np.maximum(own_i, 0)] | \
            (nei_i >= 0) & bad_cells[np.maximum(nei_i, 0)]
        pts = np.concatenate([pts_i[bad_i[fid_si]], pts_b[bad_b[fid_sb]]])
        culprits = np.unique(pts)
        culprits = culprits[moved[culprits] & ~undone[culprits]]
        if not len(culprits):
            break
        undone[culprits] = True
    return undone


def report(curves: list[Curve], points: np.ndarray, keys: np.ndarray, topo, zone: np.ndarray,
           names_of_part: dict) -> list[dict]:
    """Per curved part: its side area, end areas and volume in the mesh against the file's."""
    ids_i = np.searchsorted(keys, topo.face_pts)
    ids_b = np.searchsorted(keys, topo.b_pts)
    area, fc = face_geometry(points, topo.face_off, ids_i)
    barea, bfc = face_geometry(points, topo.b_off, ids_b)
    vol = cell_volumes(topo.n_cells, topo.owner, topo.neighbour, area, fc, topo.b_owner, barea,
                       bfc)
    zo, zn = zone[topo.owner], zone[topo.neighbour]
    rows = []
    for c in curves:
        ru, rv = c.radii
        L = c.a1 - c.a0
        perim = math.pi * (3 * (ru + rv) - math.sqrt((3 * ru + rv) * (ru + 3 * rv)))
        side = (zo == c.part) ^ (zn == c.part)
        normal = np.argmax(np.abs(area), axis=1)
        u, v = [i for i in range(3) if i != c.axis]
        inside = (fc[:, c.axis] > c.a0 + 1e-12 * L) & (fc[:, c.axis] < c.a1 - 1e-12 * L)
        near = _band(fc, u, v, c)
        lat = side & inside & near & (normal != c.axis)
        mesh_side = float(np.linalg.norm(area[lat], axis=1).sum())
        row: dict = {"part": c.name, "kind": "round hole" if c.hole else "cylinder",
               "file_side_area_m2": perim * L, "mesh_side_area_m2": mesh_side,
               "side_area_error_pct": 100.0 * (mesh_side - perim * L) / (perim * L)}
        if not c.hole:
            want = math.pi * ru * rv * L
            got = float(vol[zone == c.part].sum())
            row.update(file_volume_m3=want, mesh_volume_m3=got,
                       volume_error_pct=100.0 * (got - want) / want)
            # contacts: faces shared with another solid, by the other part
            other = np.where(zo == c.part, zn, np.where(zn == c.part, zo, -1))
            touch = side & (other >= 0)
            contacts: dict[str, float] = {}
            for o, a_ in zip(other[touch].tolist(), np.linalg.norm(area[touch], axis=1).tolist()):
                nm = names_of_part.get(o, str(o))
                contacts[nm] = contacts.get(nm, 0.0) + a_
            row["contacts_m2"] = contacts
        rows.append(row)
    return rows


__all__ = ["Curve", "Snapped", "cell_volumes", "curves_of", "face_geometry", "report", "snap"]
