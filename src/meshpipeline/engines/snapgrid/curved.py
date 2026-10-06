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
from typing import Any

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
    topo: Any = None                 # the topology after risers collapsed (None: unchanged)
    collapsed: int = 0               # riser edges collapsed onto the surface


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
    disp = np.zeros_like(points)                # where each surface point is asked to go
    on_surface = np.zeros(len(points), dtype=bool)
    regions: list[tuple[np.ndarray, np.ndarray, int, int]] = []
    risers: list[np.ndarray] = []
    no_merge = fixed.copy()
    for a_ in range(3):
        no_merge[domain_pts[a_]] = True
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
                disp[sp, slide] = TANGENT_SHRINK * (points[sp, slide] - c_slide) \
                    - (points[sp, slide] - c_slide)
                on_curve[sp] = True
        for pi in pts.tolist():
            if on_curve[pi]:
                continue
            target = _target(points[pi], c, u, v, [a_ for a_ in (u, v) if held[pi, a_]])
            if target is None:
                continue
            disp[pi] = target - points[pi]
            on_curve[pi] = True
        on_surface |= on_curve
        risers.append(_risers(points, disp, c, topo, ids_i, faces, on_curve,
                              no_merge | held[:, u] | held[:, v]))
        # the cells around the surface follow it, the move fading over a few rings of cells,
        # so the snapped cells are not left squeezed against unmoved neighbours
        regions.append(_relax_region(points, c, u, v, topo, ids_i, fid_i, faces, on_curve,
                                     fixed | held[:, u] | held[:, v]))
    pairs = np.concatenate(risers) if risers else np.zeros((0, 2), dtype=np.int64)
    if len(pairs):
        topo, keys, points, disp, on_surface, regions = _collapse(
            pairs, topo, keys, points, disp, on_surface, regions)
        out.topo = topo
        out.collapsed = int(len(pairs))
        ids_i = np.searchsorted(keys, topo.face_pts)
        ids_b = np.searchsorted(keys, topo.b_pts)
    moves, given_up = _settle(points, disp, on_surface, regions, topo, ids_i, ids_b)
    out.keys = keys
    out.xyz = points + moves
    out.undone = given_up
    return out


#: A staircase riser - an edge between two surface points - whose two ends would land closer than
#: this share of its length is collapsed onto the surface: its two points become one and the face
#: along it, left with no area, goes. That is what lets the surface reach the true one where it
#: runs almost along the grid (a riser cannot shrink to nothing and stay an edge).
COLLAPSE_RATIO = 0.35


def _risers(points, disp, c: Curve, topo, ids_i, faces, on_curve, frozen) -> np.ndarray:
    """The riser edges of one curve's staircase to collapse: cross-section edges of its side faces
    whose two ends are both surface points free to merge and would land within COLLAPSE_RATIO of
    the edge's length of each other."""
    sizes = np.diff(topo.face_off)[faces]
    starts = topo.face_off[:-1][faces]
    idx = np.repeat(starts, sizes) + (np.arange(int(sizes.sum()))
                                      - np.repeat(np.cumsum(sizes) - sizes, sizes))
    nxt = idx + 1
    nxt = np.where(nxt >= np.repeat(starts + sizes, sizes), np.repeat(starts, sizes), nxt)
    a_, b_ = ids_i[idx], ids_i[nxt]
    ok = on_curve[a_] & on_curve[b_] & ~frozen[a_] & ~frozen[b_]
    ok &= np.abs(points[a_, c.axis] - points[b_, c.axis]) <= 1e-12 * (abs(points[a_, c.axis]) + 1)
    a_, b_ = a_[ok], b_[ok]
    length = np.linalg.norm(points[a_] - points[b_], axis=1)
    land = np.linalg.norm((points[a_] + disp[a_]) - (points[b_] + disp[b_]), axis=1)
    # ...or would turn by more than 60 degrees (the two ends crossing over on a flat stretch)
    before = points[b_] - points[a_]
    after = (points[b_] + disp[b_]) - (points[a_] + disp[a_])
    turn = np.einsum("ij,ij->i", before, after) < 0.5 * length * land
    sel = (land < COLLAPSE_RATIO * length) | turn
    pairs = np.stack([np.minimum(a_[sel], b_[sel]), np.maximum(a_[sel], b_[sel])], axis=1)
    return np.unique(pairs, axis=0) if len(pairs) else pairs.reshape(0, 2).astype(np.int64)


def _collapse(pairs, topo, keys, points, disp, on_surface, regions):
    """Merge each collapsing riser's points into one (at the mean of where they were going), drop
    the faces left with fewer than three points, and drop repeated points from the others. The
    domain sides are never touched (their points never merge), so the boundary faces stay as they
    are. Returns the new topology and everything re-indexed onto its points."""
    from dataclasses import replace as dc_replace

    n = len(points)
    parent = np.arange(n)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a_, b_ in pairs.tolist():
        ra, rb = find(a_), find(b_)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    root = np.array([find(x) for x in range(n)]) if len(pairs) else np.arange(n)
    target = points + disp
    merged = root != np.arange(n)
    if merged.any():
        group = np.unique(root[merged])
        mem = np.isin(root, group)
        acc = np.zeros((n, 3))
        cnt = np.zeros(n)
        np.add.at(acc, root[mem], target[mem])
        np.add.at(cnt, root[mem], 1.0)
        disp = disp.copy()
        # the merged point stands at the middle of its riser and goes where its ends were going
        mid = np.zeros((n, 3))
        np.add.at(mid, root[mem], points[mem])
        points = points.copy()
        points[group] = mid[group] / cnt[group, None]
        disp[group] = acc[group] / cnt[group, None] - points[group]
    ids_i = np.searchsorted(keys, topo.face_pts)
    loops = root[ids_i]
    off = topo.face_off
    keep_pt = np.ones(len(loops), dtype=bool)
    sizes = np.diff(off)
    fid = np.repeat(np.arange(len(off) - 1), sizes)
    prev = np.arange(len(loops)) - 1
    first = off[:-1]
    prev[first] = off[1:] - 1
    keep_pt = loops != loops[prev]
    # a loop whose every point is the same keeps none: never a face
    new_sizes = np.bincount(fid, weights=keep_pt, minlength=len(sizes)).astype(np.int64)
    keep_face = new_sizes >= 3
    keep_pt &= keep_face[fid]
    new_off = np.concatenate(([0], np.cumsum(new_sizes[keep_face]))).astype(np.int64)
    new_pts = keys[loops[keep_pt]]
    topo2 = dc_replace(topo, owner=topo.owner[keep_face], neighbour=topo.neighbour[keep_face],
                       face_off=new_off, face_pts=new_pts)
    keys2 = np.unique(np.concatenate([topo2.face_pts, topo2.b_pts]))
    at = np.searchsorted(keys, keys2)
    regions2 = []
    for free, edges, u, v in regions:
        e = root[edges]
        e = e[e[:, 0] != e[:, 1]]
        f2 = np.unique(root[free])
        f2 = f2[~on_surface[f2]]
        remap = np.full(n, -1, dtype=np.int64)
        remap[at] = np.arange(len(at))
        e2 = remap[e]
        e2 = e2[(e2 >= 0).all(axis=1)]
        f3 = remap[f2]
        regions2.append((f3[f3 >= 0], np.unique(e2, axis=0), u, v))
    return (topo2, keys2, points[at], disp[at], on_surface[at], regions2)


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


def _nearest_on_ellipse(px: float, py: float, a: float, b: float) -> tuple[float, float]:
    """The point of the ellipse x^2/a^2 + y^2/b^2 = 1 nearest (px, py) - straight out for a
    circle; for an ellipse a few Newton steps on its parameter (a point moved straight out would
    also slide along the surface, and shear the cells it moves)."""
    if abs(a - b) <= 1e-12 * max(a, b):
        r = math.hypot(px, py)
        return a * px / r, a * py / r
    t = math.atan2(a * py, b * px)
    for _ in range(30):
        st, ct = math.sin(t), math.cos(t)
        f = (a * a - b * b) * st * ct - px * a * st + py * b * ct
        df = (a * a - b * b) * (ct * ct - st * st) - px * a * ct - py * b * st
        if df == 0.0:
            break
        step = f / df
        t -= step
        if abs(step) < 1e-14:
            break
    return a * math.cos(t), b * math.sin(t)


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
        nu, nv = _nearest_on_ellipse(x[u] - cu, x[v] - cv, ru, rv)
        out[u] = cu + nu
        out[v] = cv + nv
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


#: Rings of cells around a snapped surface that follow it (the outermost one stays put), and the
#: sweeps that spread the move over them.
RELAX_RINGS = 5
RELAX_SWEEPS = 80
#: Halvings of a surface point's move before it is given up.
SETTLE_ROUNDS = 10


def _relax_region(points, c: Curve, u: int, v: int, topo, ids_i, fid_i, faces, on_curve,
                  frozen) -> tuple[np.ndarray, np.ndarray, int, int]:
    """The points that follow a snapped surface - those of the first RELAX_RINGS - 1 rings of
    cells around its staircase that are free to move (not on a box face, a domain side or a block
    wall, and within the curve's length) - and the cross-section edges between all points of the
    rings (the last ring's points hold still: the move fades to nothing there)."""
    mark = np.zeros(topo.n_cells, dtype=bool)
    mark[topo.owner[faces]] = True
    mark[topo.neighbour[faces]] = True
    inner = mark.copy()
    for ring in range(RELAX_RINGS):
        touch = np.flatnonzero(mark[topo.owner] | mark[topo.neighbour])
        mark[topo.owner[touch]] = True
        mark[topo.neighbour[touch]] = True
        if ring == RELAX_RINGS - 2:
            inner = mark.copy()
    fsel = np.flatnonzero(mark[topo.owner] | mark[topo.neighbour])
    fin = np.flatnonzero(inner[topo.owner] | inner[topo.neighbour])
    sizes = np.diff(topo.face_off)[fsel]
    starts = topo.face_off[:-1][fsel]
    idx = np.repeat(starts, sizes) + (np.arange(int(sizes.sum()))
                                      - np.repeat(np.cumsum(sizes) - sizes, sizes))
    nxt = idx + 1
    nxt = np.where(nxt >= np.repeat(starts + sizes, sizes), np.repeat(starts, sizes), nxt)
    a_, b_ = ids_i[idx], ids_i[nxt]
    same = np.abs(points[a_, c.axis] - points[b_, c.axis]) <= 1e-12 * (abs(points[a_, c.axis]) + 1)
    edges = np.unique(np.concatenate([np.stack([a_[same], b_[same]], 1),
                                      np.stack([b_[same], a_[same]], 1)]), axis=0)
    in_pts = np.unique(ids_i[np.isin(fid_i, fin)])
    ax = points[in_pts, c.axis]
    free = in_pts[~frozen[in_pts] & ~on_curve[in_pts]
                  & (ax >= c.a0 - 1e-12) & (ax <= c.a1 + 1e-12)]
    return free, edges, u, v


def _diffuse(disp: np.ndarray, regions) -> np.ndarray:
    """The surface points' moves spread over each region's free points (Jacobi sweeps of the mean
    of their cross-section neighbours; points outside the free set keep their move, or none)."""
    out = disp.copy()
    for free, edges, u, v in regions:
        if not len(free) or not len(edges):
            continue
        is_free = np.zeros(len(out), dtype=bool)
        is_free[free] = True
        e = edges[is_free[edges[:, 0]]]
        if not len(e):
            continue
        count = np.bincount(e[:, 0], minlength=len(out)).astype(float)
        for _ in range(RELAX_SWEEPS):
            acc = np.zeros((len(out), 2))
            np.add.at(acc, e[:, 0], out[e[:, 1]][:, [u, v]])
            mean = acc[free] / np.maximum(count[free], 1.0)[:, None]
            out[free, u] = mean[:, 0]
            out[free, v] = mean[:, 1]
    return out


def _settle(points, disp, on_surface, regions, topo, ids_i, ids_b) -> tuple[np.ndarray, int]:
    """The moves that keep every cell sound: every surface point goes all the way and the rings
    around it follow; where a face or a cell comes out bad (folded, crushed, past the angle or
    skewness bar), the surface points nearby go half as far, and again, until none is bad - a
    point that cannot move at all is given up (counted). Returns the moves and that count."""
    frac = np.where(on_surface, 1.0, 0.0)
    free_all = np.zeros(len(points), dtype=bool)
    edges_all = [np.zeros((0, 2), dtype=np.int64)]
    for free, edges, _u, _v in regions:
        free_all[free] = True
        edges_all.append(edges)
    edges_cat = np.concatenate(edges_all)
    guard = _Guard(points, on_surface | free_all, topo, ids_i, ids_b)
    moves = np.zeros_like(points)
    for rnd in range(SETTLE_ROUNDS + 1):
        moves = _diffuse(disp * frac[:, None], regions)
        moves[~(on_surface | free_all)] = 0.0
        bad = guard.bad_points(points + moves)
        if not bad.any():
            break
        # the surface points in the trouble; where a bad face holds only points that follow,
        # the surface points one edge out from them
        near = bad & on_surface
        followers = bad & ~on_surface
        if followers.any():
            hit = followers[edges_cat[:, 0]]
            reach = np.zeros_like(near)
            reach[edges_cat[hit, 1]] = True
            near |= reach & on_surface
        if not near.any():
            near = on_surface & (frac > 0)
        frac[near] = 0.0 if rnd >= SETTLE_ROUNDS - 1 else frac[near] * 0.5
    given_up = int((on_surface & (frac == 0.0)).sum())
    return moves, given_up


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
MAX_SNAP_ANGLE_DEG = 63.0
#: ...and a face this skewed (OpenFOAM's measure; checkMesh flags faces above 4).
MAX_SNAP_SKEW = 3.5


class _Guard:
    """The cells around the moving points (and their neighbours, whose centres the angles need),
    measured as they are and as they would be: a face that would flip or shrink below a
    twentieth, a cell below a third of its volume, a face past MAX_SNAP_ANGLE_DEG or skewness
    MAX_SNAP_SKEW (and worse than before) is bad."""

    def __init__(self, old, moved, topo, ids_i, ids_b) -> None:
        self.old = old
        fid_i = np.repeat(np.arange(len(topo.face_off) - 1), np.diff(topo.face_off))
        fid_b = np.repeat(np.arange(len(topo.b_off) - 1), np.diff(topo.b_off))
        touched_i = np.unique(fid_i[moved[ids_i]])
        touched_b = np.unique(fid_b[moved[ids_b]])
        ring1 = np.unique(np.concatenate([topo.owner[touched_i], topo.neighbour[touched_i],
                                          topo.b_owner[touched_b]]))
        f1, _b1 = _sub(topo, ring1)
        cells = np.unique(np.concatenate([ring1, topo.owner[f1], topo.neighbour[f1]]))
        self.fi, self.fb = _sub(topo, cells)
        self.off_i, self.pts_i = _gather(topo.face_off, ids_i, self.fi)
        self.off_b, self.pts_b = _gather(topo.b_off, ids_b, self.fb)
        local = np.full(topo.n_cells, -1, dtype=np.int64)
        local[cells] = np.arange(len(cells))
        self.own_i = local[topo.owner[self.fi]]
        self.nei_i = local[topo.neighbour[self.fi]]
        self.own_b = local[topo.b_owner[self.fb]]
        self.both = (self.own_i >= 0) & (self.nei_i >= 0)
        self.n = len(cells)
        self.fid_si = np.repeat(np.arange(len(self.fi)), np.diff(self.off_i))
        self.fid_sb = np.repeat(np.arange(len(self.fb)), np.diff(self.off_b))
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            self.a0_i, self.a0_b, self.v0, self.cos0, self.skew0 = self._measure(old)
        self.m0_i = np.einsum("ij,ij->i", self.a0_i, self.a0_i)
        self.m0_b = np.einsum("ij,ij->i", self.a0_b, self.a0_b)
        self.cos_bar = math.cos(math.radians(MAX_SNAP_ANGLE_DEG))
        # a face with a moving point is held to the bars outright (its unmoved shape - a riser
        # collapsed where it stood - is no baseline); only an untouched face may keep a
        # non-orthogonality or skewness it already had (a hanging-node face of the grid)
        touched = np.zeros(len(self.fi), dtype=bool)
        touched[np.unique(self.fid_si[moved[self.pts_i]])] = True
        self.cos0 = np.where(touched, 1.0, self.cos0)
        self.skew0 = np.where(touched, 0.0, self.skew0)

    def _measure(self, pts):
        own_i, nei_i, own_b, both, n = self.own_i, self.nei_i, self.own_b, self.both, self.n
        a_i, c_i = face_geometry(pts, self.off_i, self.pts_i)
        a_b, c_b = face_geometry(pts, self.off_b, self.pts_b)
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
        cpf = c_i - own_c
        sv = cpf - (np.einsum("ij,ij->i", a_i, cpf)
                    / (np.einsum("ij,ij->i", a_i, d) + 1e-300))[:, None] * d
        svn = np.linalg.norm(sv, axis=1)
        sv_hat = sv / (svn + 1e-300)[:, None]
        sizes = np.diff(self.off_i)
        reach = np.abs(np.einsum("ij,ij->i", np.repeat(sv_hat, sizes, axis=0),
                                 pts[self.pts_i] - np.repeat(c_i, sizes, axis=0)))
        fd = np.maximum.reduceat(reach, self.off_i[:-1]) if len(reach) else np.zeros(0)
        fd = np.maximum(fd, 0.2 * np.linalg.norm(d, axis=1)) + 1e-300
        skew = svn / fd
        return a_i, a_b, vol, np.where(both, cos, 1.0), np.where(both, skew, 0.0)

    def bad_points(self, cur) -> np.ndarray:
        """The points of every bad face and of every face of a bad cell."""
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            a_i, a_b, vol, cos, skew = self._measure(cur)
        own_i, nei_i, own_b, both = self.own_i, self.nei_i, self.own_b, self.both
        bad_i = np.einsum("ij,ij->i", a_i, self.a0_i) < self.m0_i / 20.0
        bad_b = np.einsum("ij,ij->i", a_b, self.a0_b) < self.m0_b / 20.0
        tilt = both & (((cos < self.cos_bar) & (cos < self.cos0 - 1e-9))
                       | ((skew > MAX_SNAP_SKEW) & (skew > self.skew0 + 1e-9)))
        bad_c = vol < self.v0 / 3.0
        bad_c[own_i[tilt]] = True
        bad_c[nei_i[tilt]] = True
        bad_i |= tilt | ((own_i >= 0) & bad_c[np.maximum(own_i, 0)]) | \
            ((nei_i >= 0) & bad_c[np.maximum(nei_i, 0)])
        bad_b |= bad_c[own_b]
        out = np.zeros(len(cur), dtype=bool)
        out[self.pts_i[bad_i[self.fid_si]]] = True
        out[self.pts_b[bad_b[self.fid_sb]]] = True
        return out


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
