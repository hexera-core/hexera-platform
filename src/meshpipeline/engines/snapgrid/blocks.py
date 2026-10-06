# Responsibility: Refine the snap grid LOCALLY: split the domain into blocks (a kd-tree on the planes the model states), give each block its own tensor grid holding only the planes of the parts that reach into it, graded by a 3D size field, and paint each block's cells.
# Owns: the global line set every block draws from, the block decomposition, each block's lines, the cell budget over the blocks, the per-block paint, and the cell counts.
# Boundaries: pure numpy on a Placement; the faces between blocks (hanging nodes) are engines/snapgrid/boxmesh.py's; which part wins an overlap is the placement's paint order.
# Collaborates with: cad/ingest/ecxml_place.py, engines/snapgrid/grid.py (the 1D size field), engines/snapgrid/boxmesh.py, engines/snapgrid/mesher.py.
"""Local refinement for the snap grid.

A single tensor grid carries every part's planes through the whole domain: a 0402 capacitor's four
faces become four planes of cells across the whole board, and a board with a thousand parts needs
the product of all their lines. Flotherm's answer is the localized grid; this is the same idea,
automatic:

* the domain is split recursively (a kd-tree) at planes the model states, wherever a split lowers
  the total cell count by a useful margin;
* each block holds the planes of the parts that reach into it, and no others - so a part's lines
  stop at its block's walls;
* cells are graded by one 3D size field: each part asks for `min_cells_across` cells through its
  thickness (a cylinder `cylinder_cells` across its diameter) and the size grows by `growth` per
  cell with the distance from it, along the axis and across - so a block next to a thin layer is
  fine where it meets it and coarse further away;
* every block draws its lines from ONE global set (every stated plane plus the graded lines of
  the finest field), so two blocks that meet share every line both keep, and a face between them
  is never a sliver thinner than the global grid itself.

Faces between blocks do not match one to one: a coarse cell meets several fine ones. Those faces
are cut along both blocks' lines and the coarse cell carries the extra points on its edges - the
hanging nodes every octree mesh (snappyHexMesh's included) has. Every cell is still an axis-aligned
box: a box part is still exactly a set of whole cells.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad.ingest.ecxml_build import _Box, _material
from meshpipeline.cad.ingest.ecxml_place import Placement
from meshpipeline.engines.snapgrid import grid as G

#: A block at or under this many cells is not split further.
LEAF_CELLS = 1024
#: A split is taken when the two halves need at most this share of the block's cells.
SPLIT_GAIN = 0.85
#: Recursion depth of the kd-tree.
MAX_DEPTH = 40
#: Candidate split positions, as fractions of the block's span on an axis.
_FRACTIONS = (0.25, 0.375, 0.5, 0.625, 0.75)


@dataclass
class Block:
    lo: tuple[int, int, int]                       # global-line index of each lower bound
    hi: tuple[int, int, int]                       # ... and upper bound
    lines: list[np.ndarray] = field(default_factory=list)   # global-line indices kept, per axis
    zone: np.ndarray | None = None                 # (z, y, x) part index per cell (AIR: -1)

    @property
    def shape(self) -> tuple[int, int, int]:
        return (len(self.lines[0]) - 1, len(self.lines[1]) - 1, len(self.lines[2]) - 1)

    @property
    def n_cells(self) -> int:
        nx, ny, nz = self.shape
        return nx * ny * nz


@dataclass
class Geometry:
    """The placement, read once into arrays: key planes, the boxes whose planes a block must hold
    where they reach into it, and the size sources."""
    domain: _Box
    keys: list[np.ndarray]                 # the stated planes per axis (clustered), floats
    real: list[np.ndarray]                 # per key: a stated plane (all, today)
    # boxes carrying planes (part material, cylinder boxes, vent outlines, sources, patches, ...)
    plane_lo: np.ndarray                   # (M, 3) key index
    plane_hi: np.ndarray                   # (M, 3) key index
    plane_closed: np.ndarray               # (M,) bool: a flat box (a rectangle) reaches a block it touches
    plane_skip: np.ndarray                 # (M,) axis whose bounds are not planes (-1: none)
    # size sources: a box and the cell size it asks for on each axis (inf: none)
    src_lo: np.ndarray                     # (S, 3) metres
    src_hi: np.ndarray
    src_size: np.ndarray                   # (S, 3) metres


@dataclass
class Layout:
    """The blocks and the global lines they draw from, for one background size."""
    geometry: Geometry
    plan: G.GridPlan
    H: float
    global_lines: list[np.ndarray]         # floats per axis
    key_to_global: list[np.ndarray]        # key index -> global line index, per axis
    blocks: list[Block]
    relaxed: list[str] = field(default_factory=list)
    balanced: int = 0                      # lines added so block faces meet at a bounded angle
    leaf_cells: int = LEAF_CELLS

    @property
    def n_cells(self) -> int:
        return int(sum(b.n_cells for b in self.blocks))

    def coords(self, block: Block, axis: int) -> np.ndarray:
        return self.global_lines[axis][block.lines[axis]]


# ------------------------------------------------------------------------------ geometry -----
def geometry_of(placement: Placement, plan: G.GridPlan) -> Geometry:
    from meshpipeline.cad.ingest.ecxml import axis_of_plane
    from meshpipeline.cad.ingest.ecxml_place import _rect_box

    dom = placement.domain
    noise = placement.tol.noise
    keys, real = [], []
    for i in range(3):
        k = G.cluster(placement.planes[i], noise)
        k = k[(k >= dom.lo[i]) & (k <= dom.hi[i])]
        k[0], k[-1] = dom.lo[i], dom.hi[i]
        keys.append(k)
        real.append(np.ones(len(k), dtype=bool))

    def kidx(i: int, v: float) -> int:
        a = keys[i]
        j = int(np.searchsorted(a, v))
        return min((c for c in (j - 1, j) if 0 <= c < len(a)), key=lambda c: abs(a[c] - v))

    boxes: list[tuple[_Box, bool, int]] = []        # (box, closed, skip axis)
    src: list[tuple[_Box, tuple[float, float, float]]] = []
    for p in placement.parts:
        if p.dropped:
            continue
        cyl = axis_of_plane(p.objects[0].plane) if p.kind == "solidCylinder" else None
        for b in _material(p):
            c = b.common(dom)
            if c is None:
                continue
            boxes.append((c, False, -1))
            size = tuple((c.hi[i] - c.lo[i]) / (plan.cylinder_cells if (cyl is not None and i != cyl)
                                                 else plan.min_cells_across) for i in range(3))
            src.append((c, size))
        for kind, cut, axis in p.cutters:
            # along the wall's normal the cutter reaches past the wall on purpose; what it opens
            # is the wall it sits in, so it reaches into the blocks that wall reaches into
            slabs = [s for ax_, _lo, _hi, s in p.walls if ax_ == axis and s.common(
                _Box([cut.lo[i] if i != axis else s.lo[i] for i in range(3)],
                     [cut.hi[i] if i != axis else s.hi[i] for i in range(3)])) is not None]
            lo_a = min((s.lo[axis] for s in slabs), default=dom.lo[axis])
            hi_a = max((s.hi[axis] for s in slabs), default=dom.hi[axis])
            box = _Box([cut.lo[i] if i != axis else lo_a for i in range(3)],
                       [cut.hi[i] if i != axis else hi_a for i in range(3)]).common(dom)
            if box is None:
                continue
            boxes.append((box, False, axis))
            if kind == "round":
                size = tuple(math.inf if i == axis else (cut.hi[i] - cut.lo[i]) / plan.cylinder_cells
                             for i in range(3))
                src.append((box, size))
    records = placement.records
    flat_rows = list(records["patches"]) + list(records["grilles"]) + \
        list(records["baffles"]) + list(records["surface_heat_sources"]) + \
        [r for r in records["fans"] if r.get("kind") == "rectangular_2d"]
    for r in flat_rows:
        c = _clip(_rect_box(r), dom)
        if c is not None:
            boxes.append((c, True, -1))
    for key in ("volume_heat_sources", "flow_resistances"):
        for r in records[key]:
            c = _clip(_Box(r["box_m"]["min"], r["box_m"]["max"]), dom)
            if c is not None:
                boxes.append((c, True, -1))
    for r in records["fans"]:
        if r.get("kind") == "axial_3d":
            c = _clip(_Box(r["box_m"]["min"], r["box_m"]["max"]), dom)
            if c is not None:
                boxes.append((c, True, -1))
    plane_lo = np.array([[kidx(i, b.lo[i]) for i in range(3)] for b, _c, _s in boxes] or
                        np.zeros((0, 3)), dtype=np.int64).reshape(-1, 3)
    plane_hi = np.array([[kidx(i, b.hi[i]) for i in range(3)] for b, _c, _s in boxes] or
                        np.zeros((0, 3)), dtype=np.int64).reshape(-1, 3)
    closed = np.array([c for _b, c, _s in boxes], dtype=bool)
    skip = np.array([s for _b, _c, s in boxes], dtype=np.int64)
    src_lo = np.array([b.lo for b, _s in src], dtype=float).reshape(-1, 3)
    src_hi = np.array([b.hi for b, _s in src], dtype=float).reshape(-1, 3)
    src_size = np.array([s for _b, s in src], dtype=float).reshape(-1, 3)
    return Geometry(domain=dom, keys=keys, real=real, plane_lo=plane_lo, plane_hi=plane_hi,
                    plane_closed=closed, plane_skip=skip, src_lo=src_lo, src_hi=src_hi,
                    src_size=src_size)


def _clip(b: _Box, dom: _Box) -> _Box | None:
    lo = [max(b.lo[i], dom.lo[i]) for i in range(3)]
    hi = [min(b.hi[i], dom.hi[i]) for i in range(3)]
    if any(hi[i] < lo[i] for i in range(3)):
        return None
    return _Box(lo, hi)


# ------------------------------------------------------------------------------ the lines ----
class _Lines:
    """Each block's lines, for one background size H: the stated planes of what reaches into the
    block, plus global lines chosen by the block's own size field."""

    def __init__(self, geo: Geometry, plan: G.GridPlan, H: float,
                 global_lines: list[np.ndarray], key_to_global: list[np.ndarray]) -> None:
        self.geo, self.plan, self.H = geo, plan, H
        self.G, self.k2g = global_lines, key_to_global
        self.m = math.log(plan.growth)
        self.real_coords = [geo.keys[a][geo.real[a]] for a in range(3)]
        # the plane-carrying boxes, in global-line indices
        if len(geo.plane_lo):
            self.p_lo = np.stack([key_to_global[a][geo.plane_lo[:, a]] for a in range(3)], 1)
            self.p_hi = np.stack([key_to_global[a][geo.plane_hi[:, a]] for a in range(3)], 1)
        else:
            self.p_lo = self.p_hi = np.zeros((0, 3), dtype=np.int64)

    def required(self, lo, hi, axis: int) -> np.ndarray:
        """Global-line indices of the planes inside [lo, hi] on `axis` that the block must hold:
        its own bounds and the faces of everything that reaches into it."""
        g = self.geo
        p_lo, p_hi = self.p_lo, self.p_hi
        reach = np.ones(len(p_lo), dtype=bool)
        for i in range(3):
            open_ = (p_hi[:, i] > lo[i]) & (p_lo[:, i] < hi[i])
            closed = (p_hi[:, i] >= lo[i]) & (p_lo[:, i] <= hi[i])
            reach &= np.where(g.plane_closed, closed, open_ | ((p_lo[:, i] == p_hi[:, i]) & closed))
        reach &= g.plane_skip != axis
        vals = np.concatenate([p_lo[reach, axis], p_hi[reach, axis], [lo[axis], hi[axis]]])
        vals = vals[(vals >= lo[axis]) & (vals <= hi[axis])]
        return np.unique(vals).astype(np.int64)

    def axis_lines(self, lo, hi, axis: int) -> np.ndarray:
        """Global-line indices of one block's lines on `axis`."""
        geo, H, m = self.geo, self.H, self.m
        req = self.required(lo, hi, axis)
        g0, g1 = int(req[0]), int(req[-1])
        x = self.G[axis][g0:g1 + 1]
        n = len(x)
        if n <= 2:
            return req
        blo = [self.G[i][lo[i]] for i in range(3)]
        bhi = [self.G[i][hi[i]] for i in range(3)]
        # part sources, with the distance across from the block added to their size
        size = geo.src_size[:, axis]
        cross = np.zeros(len(size))
        for c in range(3):
            if c == axis:
                continue
            gap = np.maximum(0.0, np.maximum(geo.src_lo[:, c] - bhi[c], blo[c] - geo.src_hi[:, c]))
            cross += gap * gap
        eff = size + m * np.sqrt(cross)
        rel = np.isfinite(eff) & (eff < H)
        cover = np.full(n, H)
        end_val = np.full(n, np.inf)
        start_val = np.full(n, np.inf)
        if rel.any():
            s_lo = geo.src_lo[rel, axis]
            s_hi = geo.src_hi[rel, axis]
            e = eff[rel]
            ia = np.searchsorted(x, s_lo, side="left")          # first line at or after lo
            ib = np.searchsorted(x, s_hi, side="right") - 1     # last line at or before hi
            for a_, b_, v, lo_, hi_ in zip(ia.tolist(), ib.tolist(), e.tolist(), s_lo.tolist(),
                                           s_hi.tolist()):
                if hi_ < x[0]:                    # wholly before the block: grows in from its start
                    end_val[0] = min(end_val[0], v + m * (x[0] - hi_))
                    continue
                if lo_ > x[-1]:
                    start_val[-1] = min(start_val[-1], v + m * (lo_ - x[-1]))
                    continue
                a_ = max(a_, 0)
                b_ = min(b_, n - 1)
                if b_ >= a_:
                    cover[a_:b_ + 1] = np.minimum(cover[a_:b_ + 1], v)
                    end_val[b_] = min(end_val[b_], v)
                    start_val[a_] = min(start_val[a_], v)
        # each interval between two required planes is a source of its own length - measured
        # between stated planes, so a block wall at a split candidate does not shorten it
        rl = req - g0
        L = np.diff(x[rl])
        rp = self.real_coords[axis]
        a0 = rp[np.clip(np.searchsorted(rp, x[rl[:-1]], side="right") - 1, 0, len(rp) - 1)]
        a1 = rp[np.clip(np.searchsorted(rp, x[rl[1:]], side="left"), 0, len(rp) - 1)]
        L = np.maximum(L, a1 - a0)
        for k in range(len(rl) - 1):
            cover[rl[k]:rl[k + 1] + 1] = np.minimum(cover[rl[k]:rl[k + 1] + 1], max(L[k], 0.0))
            end_val[rl[k + 1]] = min(end_val[rl[k + 1]], L[k])
            start_val[rl[k]] = min(start_val[rl[k]], L[k])
        left = np.minimum.accumulate(end_val - m * x) + m * x
        right = np.minimum.accumulate((start_val + m * x)[::-1])[::-1] - m * x
        h = np.minimum(np.minimum(cover, left), right)
        h = np.maximum(h, 1e-300)
        inv = 1.0 / h
        phi = np.concatenate(([0.0], np.cumsum(np.diff(x) * 0.5 * (inv[1:] + inv[:-1]))))
        picks = [rl]
        for k in range(len(rl) - 1):
            a_, b_ = int(rl[k]), int(rl[k + 1])
            if b_ - a_ < 2:
                continue
            tot = phi[b_] - phi[a_]
            cnt = max(1, math.ceil(tot - 0.05))
            if cnt <= 1:
                continue
            t = phi[a_] + tot * np.arange(1, cnt) / cnt
            j = np.searchsorted(phi[a_:b_ + 1], t) + a_
            j = np.clip(j, a_ + 1, b_ - 1)
            # nearest of the two candidates
            jm = np.clip(j - 1, a_ + 1, b_ - 1)
            pick = np.where(np.abs(phi[jm] - t) < np.abs(phi[j] - t), jm, j)
            picks.append(pick)
        out = np.unique(np.concatenate(picks)) + g0
        return out.astype(np.int64)

    def block(self, lo, hi) -> list[np.ndarray]:
        return [self.axis_lines(lo, hi, a) for a in range(3)]


def _cost(lines: list[np.ndarray]) -> int:
    return (len(lines[0]) - 1) * (len(lines[1]) - 1) * (len(lines[2]) - 1)


def _global(geo: Geometry, plan: G.GridPlan, H: float) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """The global lines: every stated plane, plus the graded lines of the 1D size field of every
    source (the finest any block can ask for), per axis."""
    m = math.log(plan.growth)
    lines, k2g = [], []
    for a in range(3):
        planes = geo.keys[a]
        covers = []
        for lo, hi, s in zip(geo.src_lo[:, a], geo.src_hi[:, a], geo.src_size[:, a]):
            if not math.isfinite(s):
                continue
            ia = int(np.searchsorted(planes, lo - 1e-15))
            ib = int(np.searchsorted(planes, hi + 1e-15)) - 1
            ia = min(max(ia, 0), len(planes) - 1)
            ib = min(max(ib, 0), len(planes) - 1)
            if ib > ia:
                covers.append((ia, ib, float(s)))
        rp = planes[geo.real[a]]
        a0 = rp[np.clip(np.searchsorted(rp, planes[:-1], side="right") - 1, 0, len(rp) - 1)]
        a1 = rp[np.clip(np.searchsorted(rp, planes[1:], side="left"), 0, len(rp) - 1)]
        nodes = G._place_axis(G._Axis(planes=planes, covers=covers, interval_size=a1 - a0), H, m)
        lines.append(nodes)
        k2g.append(np.searchsorted(nodes, planes).astype(np.int64))
    return lines, k2g


# ------------------------------------------------------------------------------ the layout ---
def decompose(geo: Geometry, plan: G.GridPlan, H: float, *, leaf_cells: int = LEAF_CELLS
              ) -> Layout:
    """The kd-tree of blocks for background size H: a block is cut (at the global line nearest a
    quarter, three eighths, ... of its span) when the two halves together need at most
    SPLIT_GAIN of its cells - an octree of tensor blocks, small where the detail is."""
    glines, k2g = _global(geo, plan, H)
    L = _Lines(geo, plan, H, glines, k2g)
    root = ((0, 0, 0), tuple(len(glines[i]) - 1 for i in range(3)))
    leaves: list[Block] = []
    stack = [(root[0], root[1], L.block(root[0], root[1]), 0)]
    while stack:
        lo, hi, lines, depth = stack.pop()
        cost = _cost(lines)
        best = None
        if cost > leaf_cells and depth < MAX_DEPTH:
            for a in range(3):
                if hi[a] - lo[a] < 2:
                    continue
                g = glines[a]
                seen = set()
                for j in _split_candidates(g, lines[a], lo[a], hi[a]):
                    if j in seen:
                        continue
                    seen.add(j)
                    mid_hi = tuple(j if i == a else hi[i] for i in range(3))
                    mid_lo = tuple(j if i == a else lo[i] for i in range(3))
                    left_l = L.block(lo, mid_hi)
                    right_l = L.block(mid_lo, hi)
                    c = _cost(left_l) + _cost(right_l)
                    if best is None or c < best[0]:
                        best = (c, (lo, mid_hi, left_l), (mid_lo, hi, right_l))
        if best is not None and best[0] <= SPLIT_GAIN * cost:
            stack.append((*best[1], depth + 1))
            stack.append((*best[2], depth + 1))
        else:
            leaves.append(Block(lo=lo, hi=hi, lines=lines))
    leaves.sort(key=lambda b: (b.lo[2], b.lo[1], b.lo[0]))
    out = Layout(geometry=geo, plan=plan, H=H, global_lines=glines, key_to_global=k2g,
                 blocks=leaves, leaf_cells=leaf_cells)
    out.balanced = balance(out)
    return out


def _split_candidates(g: np.ndarray, own: np.ndarray, lo: int, hi: int) -> list[int]:
    """Where a block may be cut on one axis: inside its widest cells (a wall there meets thick
    cells on both sides, so the blocks need no matching lines along it), at the global line
    nearest each one's centre; then near the middle of its span."""
    x = g[own]
    w = np.diff(x)
    out = []
    for k in np.argsort(-w, kind="stable")[:SPLIT_CANDIDATES].tolist():
        a_, b_ = int(own[k]), int(own[k + 1])
        if b_ - a_ < 2:
            continue
        mid = (x[k] + x[k + 1]) / 2
        j = int(np.searchsorted(g, mid))
        j = min(max(j, a_ + 1), b_ - 1)
        if j - 1 > a_ and abs(g[j - 1] - mid) < abs(g[j] - mid):
            j -= 1
        out.append(j)
    for f in _FRACTIONS:
        target = g[lo] + f * (g[hi] - g[lo])
        j = min(max(int(np.searchsorted(g, target)), lo + 1), hi - 1)
        out.append(j)
    return out


#: Cut positions tried per axis (inside the widest cells).
SPLIT_CANDIDATES = 4


def relayout(layout: Layout, plan: G.GridPlan, H: float) -> Layout:
    """The blocks for another background size or plan (decomposed anew: block walls are lines of
    one background size's global grid)."""
    return decompose(layout.geometry, plan, H, leaf_cells=layout.leaf_cells)


# ------------------------------------------------------------------------------ balancing ----
#: The largest angle a face between two blocks may make with the line between its two cells'
#: centres (OpenFOAM's non-orthogonality). The product's bar is 65 degrees.
MAX_FACE_ANGLE_DEG = 62.0


def adjacency(layout: Layout) -> list[tuple[int, int, int]]:
    """Pairs of blocks that share a rectangle: (block below, block above, axis)."""
    out = []
    by_plane: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
    for bi, b in enumerate(layout.blocks):
        for a in range(3):
            by_plane.setdefault((a, b.hi[a]), ([], []))[0].append(bi)
            by_plane.setdefault((a, b.lo[a]), ([], []))[1].append(bi)
    for (a, _p), (below, above) in by_plane.items():
        for ia in below:
            A = layout.blocks[ia]
            for ic in above:
                C = layout.blocks[ic]
                if all(min(A.hi[c], C.hi[c]) > max(A.lo[c], C.lo[c]) for c in range(3) if c != a):
                    out.append((ia, ic, a))
    return out


def balance(layout: Layout, *, max_angle_deg: float = MAX_FACE_ANGLE_DEG,
            max_rounds: int = 60) -> int:
    """Refine the coarse side of every face between two blocks until each face piece's two cells
    sit close enough across it: the offset of their centres along the face at most tan(angle)
    times their distance across it (split evenly over the two axes along the face), then grade the
    added lines into the block. Lines are only ever added, so it ends. Returns the lines added."""
    return _balance_blocks(layout.blocks, adjacency(layout), layout.global_lines,
                           max_angle_deg=max_angle_deg, max_rounds=max_rounds)


def _balance_blocks(blocks: list[Block], pairs, G_, *, max_angle_deg: float = MAX_FACE_ANGLE_DEG,
                    max_rounds: int = 60) -> int:
    T = math.tan(math.radians(max_angle_deg)) / math.sqrt(2.0)
    added = 0
    for _round in range(max_rounds):
        changed = 0
        for ia, ic, a in pairs:
            changed += _balance_pair(blocks[ia], blocks[ic], a, G_, T)
        added += changed
        if not changed:
            break
    return added


def _balance_pair(A: Block, C: Block, a: int, G_, T: float) -> int:
    """One face between block A (below on axis a) and block C: Q's lines brought into P wherever
    a P cell is too wide for the Q cells it faces. Returns the lines added."""
    ga, gc = G_[a][A.lines[a]], G_[a][C.lines[a]]
    t = (ga[-1] - ga[-2]) + (gc[1] - gc[0])          # the two layers across the face
    changed = 0
    for P, Q in ((A, C), (C, A)):
        for w in (c for c in range(3) if c != a):
            o0 = max(P.lines[w][0], Q.lines[w][0])
            o1 = min(P.lines[w][-1], Q.lines[w][-1])
            pl, ql = P.lines[w], Q.lines[w]
            px, qx = G_[w][pl], G_[w][ql]
            pc = (px[:-1] + px[1:]) / 2
            qc = (qx[:-1] + qx[1:]) / 2
            # each Q cell along w inside the overlap, and the P cell it faces
            qi = np.flatnonzero((ql[1:] > o0) & (ql[:-1] < o1))      # Q cells meeting the overlap
            if not len(qi):
                continue
            pi = np.clip(np.searchsorted(pl, np.maximum(ql[qi], o0), side="right") - 1, 0,
                         len(pl) - 2)
            bad = np.abs(pc[pi] - qc[qi]) > T * t / 2 + 1e-15
            if not bad.any():
                continue
            # Q's lines inside the offending P cells go into P
            cells = np.unique(pi[bad])
            inner = ql[(ql > o0) & (ql < o1)]
            take = np.zeros(len(inner), dtype=bool)
            for lo_, hi_ in zip(pl[cells].tolist(), pl[cells + 1].tolist()):
                take |= (inner > lo_) & (inner < hi_)
            if take.any():
                merged = np.union1d(pl, inner[take]).astype(np.int64)
                # grade the imported lines into each P cell they split - inside that cell only,
                # so a finer neighbour never coarsens or refines the rest of the block
                for lo_, hi_ in zip(pl[cells].tolist(), pl[cells + 1].tolist()):
                    sub = merged[(merged >= lo_) & (merged <= hi_)]
                    graded = _smooth(sub, G_[w], SMOOTH_RATIO)
                    if len(graded) > len(sub):
                        merged = np.union1d(merged, graded).astype(np.int64)
                changed += len(merged) - len(pl)
                P.lines[w] = merged
    return changed


#: Inside a block, neighbouring cells differ by at most this much once balancing has brought in
#: a finer neighbour's lines (where the global lines allow it).
SMOOTH_RATIO = 2.0


def _smooth(lines: np.ndarray, g: np.ndarray, ratio: float) -> np.ndarray:
    """`lines` (global indices) with global lines added until no cell is more than `ratio` times
    a neighbour - each too-large cell split at the global line nearest `ratio` times its smaller
    neighbour, on that neighbour's side. Stops where no global line is left to add."""
    cur = np.asarray(lines, dtype=np.int64)
    for _ in range(200):
        x = g[cur]
        w = np.diff(x)
        if len(w) < 2:
            return cur
        add = []
        left_big = w[1:] > ratio * w[:-1] * (1 + 1e-9)     # cell k+1 too big after cell k
        right_big = w[:-1] > ratio * w[1:] * (1 + 1e-9)    # cell k too big before cell k+1
        for k in np.flatnonzero(left_big).tolist():
            target = x[k + 1] + ratio * w[k]
            add.append(_nearest_inside(g, cur[k + 1], cur[k + 2], target))
        for k in np.flatnonzero(right_big).tolist():
            target = x[k + 1] - ratio * w[k + 1]
            add.append(_nearest_inside(g, cur[k], cur[k + 1], target))
        new = [a for a in add if a is not None]
        if not new:
            return cur
        merged = np.union1d(cur, np.array(new, dtype=np.int64))
        if len(merged) == len(cur):
            return cur
        cur = merged
    return cur


def _nearest_inside(g: np.ndarray, lo: int, hi: int, target: float) -> int | None:
    """The global line strictly between indices lo and hi nearest `target`, or None."""
    if hi - lo < 2:
        return None
    j = int(np.searchsorted(g[lo + 1:hi], target)) + lo + 1
    cands = [c for c in (j - 1, j) if lo < c < hi]
    return min(cands, key=lambda c: abs(g[c] - target)) if cands else None


def build_layout(placement: Placement, plan: G.GridPlan, *, local: bool = True) -> Layout:
    """The blocks within the plan's budget: the background size from `background_cells`, grown
    (binary search) when that is over budget, then the growth limit relaxed to 2, then one cell
    through each part, then fewer cells across cylinders - each said. A model that needs more than
    the budget with one cell through every part raises grid.OverBudget with the count it needs."""
    problems = plan.problems()
    if problems:
        raise ValueError("; ".join(problems))
    geo = geometry_of(placement, plan)
    dom = geo.domain
    longest = max(dom.hi[i] - dom.lo[i] for i in range(3))
    H0 = longest / plan.background_cells
    leaf = LEAF_CELLS if local else 10 ** 18
    candidates = [plan]
    if plan.growth < 2.0:
        candidates.append(G._replace(plan, growth=2.0))
    if plan.min_cells_across > 1:
        candidates.append(G._replace(candidates[-1], min_cells_across=1))
    if plan.cylinder_cells > 6:
        candidates.append(G._replace(candidates[-1], cylinder_cells=6))
    needed = 0
    for k, cand in enumerate(candidates):
        geo_k = geometry_of(placement, cand) if cand is not plan else geo
        layout = decompose(geo_k, cand, H0, leaf_cells=leaf)
        if layout.n_cells <= plan.max_cells:
            layout.relaxed = G._relaxed_words(plan, cand) if k else []
            return layout
        coarsest = relayout(layout, cand, longest)
        needed = coarsest.n_cells
        if needed > plan.max_cells:
            continue
        lo_h, hi_h, best = H0, longest, coarsest
        for _ in range(30):
            mid = math.sqrt(lo_h * hi_h)
            trial = relayout(layout, cand, mid)
            if trial.n_cells <= plan.max_cells:
                hi_h, best = mid, trial
            else:
                lo_h = mid
            if hi_h / lo_h < 1.08:
                break
        best.relaxed = [f"the cell budget ({plan.max_cells:,}) set the background cell size to "
                        f"{hi_h * 1e3:.4g} mm instead of {H0 * 1e3:.4g} mm"]
        if k:
            best.relaxed += G._relaxed_words(plan, cand)
        return best
    raise G.OverBudget(
        f"this model needs at least {needed:,} cells to keep one cell through every part (its "
        f"thinnest parts and narrowest gaps set the grid planes, refined locally), more than the "
        f"budget of {plan.max_cells:,}. Raise the cell budget to at least {needed:,}; no layer is "
        "merged away to fit.", needed)


# ------------------------------------------------------------------------------ the paint ----
def paint_blocks(placement: Placement, layout: Layout) -> None:
    """Each block's cells painted with the part whose material holds them (paint order: the file's
    precedence), exactly for boxes, by cell centre for cylinders and round vents."""
    tol = G.plane_tolerance(placement)
    for b in layout.blocks:
        lines = [layout.coords(b, a) for a in range(3)]
        sub = G.Grid(lines=lines, plan=layout.plan)
        bb = _Box([lines[a][0] for a in range(3)], [lines[a][-1] for a in range(3)])
        zone = np.full((sub.shape[2], sub.shape[1], sub.shape[0]), G.AIR, dtype=np.int32)
        for k in placement.paint:
            p = placement.parts[k]
            if p.dropped or not p.box.overlaps(bb, 0.0):
                continue
            G.paint_part(zone, sub, p, k, tol)
        b.zone = zone


__all__ = ["Block", "Geometry", "LEAF_CELLS", "Layout", "build_layout", "decompose",
           "geometry_of", "paint_blocks", "relayout"]
