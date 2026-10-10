# Responsibility: Build the snap grid of a placed model: grid lines on every plane the model states, graded cells between them, every cell painted with the part that owns it.
# Owns: the per-axis size field (local refinement, growth limit, cell budget), the grid lines, the cell-to-part paint and the air's connected spaces.
# Boundaries: pure numpy on a Placement; it writes no file and runs no mesher. Which part wins an overlap is the placement's paint order, never decided here.
# Collaborates with: cad/ingest/ecxml_place.py (the placement), engines/snapgrid/polymesh.py (writes the grid), engines/snapgrid/mesher.py (drives both).
"""The Cartesian snap grid (Flotherm's "grid snapped to objects").

1. Every plane the model states becomes a grid line: each part's faces, each vent's outline,
   each fan's and source's box, each patch rectangle on the domain sides. Coordinates closer than
   the file's own precision are one line. Every axis-aligned box is then a union of whole cells:
   exact, with no snapping tolerance and no boolean.
2. Between those lines, cells are graded by a size field per axis. The background size comes from
   the cell budget; a part asks for `min_cells_across` cells through its thickness on each axis,
   a cylinder (or a round vent) for `cylinder_cells` across its diameter, and every interval
   between two lines is itself a size source. Sizes grow away from a source by at most `growth`
   per cell, so refinement stays local along each axis and no cell is more than `growth` times
   its neighbour (except where a line pins a thinner cell).
3. Each cell is painted with the part whose material holds its centre, parts in paint order
   (lowest precedence first, cad/ingest/ecxml_place._paint_order). Boxes are exact; a cylinder
   or a round vent is staircased (its volume error is reported, never hidden).
4. The cells no part owns are the air, split into its connected spaces (a sealed enclosure keeps
   its own air).

A tensor grid refines along one axis across the whole domain: a 50 um layer adds a few planes of
cells through the whole model, not a 50 um cube everywhere (an octree's cost). That is the trade
Flotherm makes; the cell count it implies is reported before anything is written.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad.ingest.ecxml_build import _Box, _material
from meshpipeline.cad.ingest.ecxml_place import Placement

AIR = -1
#: Never place more cells on one axis than this (a guard, far above any sane plan).
MAX_LINES_PER_AXIS = 20_000


@dataclass(frozen=True)
class GridPlan:
    """What the grid is asked for. Every field has a default a thermal engineer would accept."""
    max_cells: int = 2_000_000          # the cell budget
    background_cells: int = 50          # cells along the domain's longest side, where nothing is
    min_cells_across: int = 2           # cells through each part, on each axis
    cylinder_cells: int = 12            # cells across a cylinder's or a round vent's diameter
    growth: float = 1.3                 # largest size ratio between neighbouring cells

    def problems(self) -> list[str]:
        out = []
        if self.max_cells < 1000:
            out.append("max_cells must be at least 1000")
        if self.background_cells < 2:
            out.append("background_cells must be at least 2")
        if self.min_cells_across < 1:
            out.append("min_cells_across must be at least 1")
        if self.cylinder_cells < 2:
            out.append("cylinder_cells must be at least 2")
        if not 1.05 <= self.growth <= 4.0:
            out.append("growth must be between 1.05 and 4")
        return out


@dataclass
class Grid:
    lines: list[np.ndarray]             # x, y, z node coordinates, metres
    plan: GridPlan                      # the plan actually used (after any relaxing)
    relaxed: list[str] = field(default_factory=list)   # what the budget forced, in words
    background: float = 0.0             # the background cell size used, metres

    @property
    def shape(self) -> tuple[int, int, int]:
        return (len(self.lines[0]) - 1, len(self.lines[1]) - 1, len(self.lines[2]) - 1)

    @property
    def n_cells(self) -> int:
        nx, ny, nz = self.shape
        return nx * ny * nz

    def index(self, axis: int, value: float, tol: float) -> int:
        """The node index of a plane the grid holds (a box bound); raises when it does not."""
        a = self.lines[axis]
        k = int(np.searchsorted(a, value))
        best = min((c for c in (k - 1, k) if 0 <= c < len(a)), key=lambda c: abs(a[c] - value))
        if abs(a[best] - value) > tol:
            raise ValueError(f"internal: {value!r} on axis {'xyz'[axis]} is not a grid plane "
                             f"(nearest {a[best]!r})")
        return best

    def centres(self, axis: int) -> np.ndarray:
        a = self.lines[axis]
        return (a[:-1] + a[1:]) / 2

    def widths(self, axis: int) -> np.ndarray:
        return np.diff(self.lines[axis])


# ------------------------------------------------------------------------------ the planes ----
def cluster(values, tol: float) -> np.ndarray:
    """Sorted planes with every run of values within `tol` of the one before kept as its first."""
    v = np.sort(np.asarray(values, dtype=float))
    if not len(v):
        return v
    keep = np.concatenate(([True], np.diff(v) > tol))
    return v[keep]


# ------------------------------------------------------------------------------ size field ----
@dataclass
class _Axis:
    planes: np.ndarray                  # the stated planes on this axis (first/last = domain)
    covers: list[tuple[int, int, float]]   # (plane index from, to, size): h <= size between them
    #: the size each interval between two planes asks for as a source (default: its length; an
    #: interval cut by a plane that is no face - a split candidate - asks for its whole length)
    interval_size: np.ndarray | None = None


def _place_axis(ax: _Axis, H: float, m: float) -> np.ndarray:
    """Node coordinates on one axis: every stated plane, plus graded nodes between them.

    The size field is h(x) = min(H, over sources (size + m * distance to the source)), with each
    source an interval [p_a, p_b] (a part's thickness, a cylinder, every interval between two
    planes) and m = ln(growth): cells placed at equal steps of the integral of 1/h then grow by
    exactly `growth` per cell away from a source. Integrated in closed form, interval by interval
    (h is the minimum of a constant and two lines there)."""
    p = ax.planes
    n = len(p) - 1
    L = np.diff(p)
    S = L if ax.interval_size is None else np.maximum(L, ax.interval_size)
    cover = np.minimum(np.full(n, H), S)     # each interval is its own source (one cell at most)
    end_size = np.full(n + 1, np.inf)        # smallest source size ending at plane e
    start_size = np.full(n + 1, np.inf)      # ... starting at plane b
    for i in range(n):
        end_size[i + 1] = min(end_size[i + 1], S[i])
        start_size[i] = min(start_size[i], S[i])
    for a, b, s in ax.covers:
        if b <= a:
            continue
        cover[a:b] = np.minimum(cover[a:b], s)
        end_size[b] = min(end_size[b], s)
        start_size[a] = min(start_size[a], s)
    # size arriving at plane i from the left: min over sources ending at e <= i of s_e + m (p_i - p_e)
    left = np.minimum.accumulate(end_size - m * p) + m * p
    # ... from the right: min over sources starting at b >= i of s_b + m (p_b - p_i)
    right = np.minimum.accumulate((start_size + m * p)[::-1])[::-1] - m * p
    nodes = [p[:1]]
    for i in range(n):
        inner = _graded(L[i], float(cover[i]), float(left[i]), float(right[i + 1]), m)
        if len(inner):
            nodes.append(p[i] + inner)
        nodes.append(p[i + 1:i + 2])
    out = np.concatenate(nodes)
    return out


def _graded(L: float, C: float, A: float, B: float, m: float) -> np.ndarray:
    """Interior node offsets in (0, L) for h(t) = min(C, A + m t, B + m (L - t))."""
    A, B = min(A, 1e300), min(B, 1e300)
    cuts = {0.0, L}
    for t in ((C - A) / m, L - (C - B) / m, (B + m * L - A) / (2 * m)):
        if 0.0 < t < L:
            cuts.add(t)
    knots = sorted(cuts)
    pieces = []                          # (t0, t1, kind, phi0)
    phi = 0.0
    for t0, t1 in zip(knots[:-1], knots[1:]):
        mid = (t0 + t1) / 2
        vals = (C, A + m * mid, B + m * (L - mid))
        kind = int(np.argmin(vals))
        if kind == 0:
            d = (t1 - t0) / C
        elif kind == 1:
            d = math.log((A + m * t1) / (A + m * t0)) / m
        else:
            d = math.log((B + m * (L - t0)) / (B + m * (L - t1))) / m
        pieces.append((t0, t1, kind, phi))
        phi += d
    count = max(1, math.ceil(phi - 1e-9))
    if count == 1:
        return np.zeros(0)
    if count > MAX_LINES_PER_AXIS:
        raise ValueError("internal: an interval asks for more cells than any plan allows")
    targets = phi * np.arange(1, count) / count
    out = np.empty(len(targets))
    k = 0
    for t0, _t1, kind, phi0 in pieces:
        phi1 = pieces[k + 1][3] if k + 1 < len(pieces) else phi
        sel = (targets >= phi0) & (targets < phi1) if k + 1 < len(pieces) else targets >= phi0
        g = targets[sel] - phi0
        if kind == 0:
            out[sel] = t0 + g * C
        elif kind == 1:
            out[sel] = ((A + m * t0) * np.exp(m * g) - A) / m
        else:
            out[sel] = L - ((B + m * (L - t0)) * np.exp(-m * g) - B) / m
        k += 1
    out = np.clip(out, 0.0, L)
    out = out[(out > L * 1e-12) & (out < L * (1 - 1e-12))]
    return np.unique(out)


# ------------------------------------------------------------------------------ the grid ------
def _axes(placement: Placement, plan: GridPlan) -> list[_Axis]:
    """Each axis's planes and size sources: part thicknesses, cylinders, round vents."""
    noise = placement.tol.noise
    dom = placement.domain
    axes = []
    for i in range(3):
        planes = cluster(placement.planes[i], noise)
        planes[0], planes[-1] = dom.lo[i], dom.hi[i]
        planes = planes[(planes >= dom.lo[i]) & (planes <= dom.hi[i])]
        axes.append(_Axis(planes=planes, covers=[]))

    def at(i: int, v: float) -> int:
        a = axes[i].planes
        k = int(np.searchsorted(a, v))
        best = min((c for c in (k - 1, k) if 0 <= c < len(a)), key=lambda c: abs(a[c] - v))
        return best

    def need(i: int, lo: float, hi: float, cells: int) -> None:
        lo, hi = max(lo, dom.lo[i]), min(hi, dom.hi[i])
        if hi - lo <= noise:
            return
        a, b = at(i, lo), at(i, hi)
        if b > a:
            axes[i].covers.append((a, b, (hi - lo) / cells))

    for p in placement.parts:
        if p.dropped:
            continue
        cyl_axis = None
        if p.kind == "solidCylinder":
            from meshpipeline.cad.ingest.ecxml import axis_of_plane
            cyl_axis = axis_of_plane(p.objects[0].plane)
        for b in _material(p):
            for i in range(3):
                cells = plan.cylinder_cells if (cyl_axis is not None and i != cyl_axis) else \
                    plan.min_cells_across
                need(i, b.lo[i], b.hi[i], cells)
        for kind, cut, axis in p.cutters:
            if kind == "round":
                for i in range(3):
                    if i != axis:
                        need(i, cut.lo[i], cut.hi[i], plan.cylinder_cells)
    return axes


def lines_for(placement: Placement, plan: GridPlan, H: float) -> list[np.ndarray]:
    axes = _axes(placement, plan)
    m = math.log(plan.growth)
    out = []
    for ax in axes:
        nodes = _place_axis(ax, H, m)
        if len(nodes) - 1 > MAX_LINES_PER_AXIS:
            raise ValueError(f"the grid would need {len(nodes) - 1:,} cells along one axis, more "
                             f"than the {MAX_LINES_PER_AXIS:,} any plan allows")
        out.append(nodes)
    return out


def _count(lines) -> int:
    return (len(lines[0]) - 1) * (len(lines[1]) - 1) * (len(lines[2]) - 1)


class OverBudget(ValueError):
    """The model cannot be meshed within the cell budget without losing a layer."""

    def __init__(self, message: str, needed: int) -> None:
        super().__init__(message)
        self.needed = needed


def build_grid(placement: Placement, plan: GridPlan) -> Grid:
    """The grid for this placement within the plan's budget. The background size is set from
    `background_cells`; when that is over budget it grows (binary search), then the growth limit
    relaxes to 2, then parts get one cell through their thickness instead of `min_cells_across`,
    then cylinders fewer cells. Every relaxing is reported. A model that needs more than the budget
    with one cell through every part raises OverBudget with the count it needs - a layer is never
    merged away to fit."""
    problems = plan.problems()
    if problems:
        raise ValueError("; ".join(problems))
    dom = placement.domain
    longest = max(dom.hi[i] - dom.lo[i] for i in range(3))
    relaxed: list[str] = []
    H0 = longest / plan.background_cells
    candidates = [plan]
    if plan.growth < 2.0:
        candidates.append(_replace(plan, growth=2.0))
    if plan.min_cells_across > 1:
        candidates.append(_replace(candidates[-1], min_cells_across=1))
    if plan.cylinder_cells > 6:
        candidates.append(_replace(candidates[-1], cylinder_cells=6))
    needed = 0
    for k, cand in enumerate(candidates):
        lines = lines_for(placement, cand, H0)
        if _count(lines) <= plan.max_cells:
            if k:
                relaxed += _relaxed_words(plan, cand)
            return Grid(lines=lines, plan=cand, relaxed=relaxed, background=H0)
        coarsest = lines_for(placement, cand, longest)
        needed = _count(coarsest)
        if needed > plan.max_cells:
            continue
        lo_h, hi_h = H0, longest
        best = coarsest
        for _ in range(40):
            mid = math.sqrt(lo_h * hi_h)
            trial = lines_for(placement, cand, mid)
            if _count(trial) <= plan.max_cells:
                hi_h, best = mid, trial
            else:
                lo_h = mid
            if hi_h / lo_h < 1.01:
                break
        relaxed.append(f"the cell budget ({plan.max_cells:,}) set the background cell size to "
                       f"{hi_h * 1e3:.4g} mm instead of {H0 * 1e3:.4g} mm")
        if k:
            relaxed += _relaxed_words(plan, cand)
        return Grid(lines=best, plan=cand, relaxed=relaxed, background=hi_h)
    raise OverBudget(
        f"this model needs at least {needed:,} cells to keep one cell through every part (its "
        f"thinnest part and narrowest gap set the grid planes), more than the budget of "
        f"{plan.max_cells:,}. Raise the cell budget to at least {needed:,}; no layer is merged "
        "away to fit.", needed)


def _replace(plan: GridPlan, **kw) -> GridPlan:
    from dataclasses import replace
    return replace(plan, **kw)


def _relaxed_words(want: GridPlan, got: GridPlan) -> list[str]:
    out = []
    if got.growth != want.growth:
        out.append(f"neighbouring cells may differ by up to {got.growth:g}x (asked "
                   f"{want.growth:g}x), to fit the cell budget")
    if got.min_cells_across != want.min_cells_across:
        out.append(f"parts get {got.min_cells_across} cell(s) through their thickness (asked "
                   f"{want.min_cells_across}), to fit the cell budget")
    if got.cylinder_cells != want.cylinder_cells:
        out.append(f"cylinders get {got.cylinder_cells} cells across (asked "
                   f"{want.cylinder_cells}), to fit the cell budget")
    return out


# ------------------------------------------------------------------------------ the paint -----
def _slice(grid: Grid, box: _Box, tol: float) -> tuple[slice, slice, slice] | None:
    """The (z, y, x) cell slice a box covers exactly, or None when it covers no cell. A box that
    does not reach into the grid on EVERY axis covers nothing - and its faces need not be planes
    of this grid - so that is settled before any face is looked up."""
    spans = []
    for i in range(3):
        lo = max(box.lo[i], float(grid.lines[i][0]))
        hi = min(box.hi[i], float(grid.lines[i][-1]))
        if hi - lo <= tol:
            return None
        spans.append((lo, hi))
    idx = []
    for i, (lo, hi) in enumerate(spans):
        a, b = grid.index(i, lo, tol), grid.index(i, hi, tol)
        if b <= a:
            return None
        idx.append(slice(a, b))
    return (idx[2], idx[1], idx[0])


def _centre_slice(grid: Grid, axis: int, lo: float, hi: float) -> slice:
    """Cells whose centre lies in [lo, hi] on one axis (for a bound that is no grid plane)."""
    c = grid.centres(axis)
    a = int(np.searchsorted(c, lo, side="left"))
    b = int(np.searchsorted(c, hi, side="right"))
    return slice(a, max(a, b))


def _ellipse_mask(grid: Grid, box: _Box, axis: int, sl: tuple[slice, slice, slice]) -> np.ndarray:
    """Within the slice: cells whose centre is inside the ellipse inscribed in the box's cross
    section normal to `axis`."""
    cross = [i for i in range(3) if i != axis]
    shape = tuple(s.stop - s.start for s in sl)            # (z, y, x)
    inside = np.ones(shape, dtype=bool)
    terms = []
    for i in cross:
        c = grid.centres(i)[sl[2 - i]]
        mid, r = (box.lo[i] + box.hi[i]) / 2, (box.hi[i] - box.lo[i]) / 2
        u = ((c - mid) / r) ** 2
        view = [1, 1, 1]
        view[2 - i] = len(u)
        terms.append(u.reshape(view))
    inside &= (terms[0] + terms[1]) <= 1.0
    return inside


def plane_tolerance(placement: Placement) -> float:
    """How far a stated bound may sit from the grid plane it was merged into: planes within the
    file's precision were merged one after the other, so a run of them spans a few times it."""
    return max(4.0 * placement.tol.noise, 1e-12 * placement.size)


def paint(placement: Placement, grid: Grid) -> np.ndarray:
    """The part index owning each cell (z, y, x order; AIR where no part does)."""
    tol = plane_tolerance(placement)
    zone = np.full((grid.shape[2], grid.shape[1], grid.shape[0]), AIR, dtype=np.int32)
    for k in placement.paint:
        p = placement.parts[k]
        if not p.dropped:
            paint_part(zone, grid, p, k, tol)
    return zone

def _span(subs, d: int) -> slice:
    return slice(min(s_[d].start for s_ in subs), max(s_[d].stop for s_ in subs))



def paint_part(zone: np.ndarray, grid: Grid, p, k: int, tol: float) -> None:
    """Paint part `p` (index `k`) into `zone` over `grid`: its material boxes exactly (their bounds
    are grid planes, or the grid's own bounds where the part reaches past them), a cylinder and a
    round vent by cell centre."""
    # the part's reach here: the cells its material boxes cover (a heat sink's box is not its
    # material - its blocks are, and only they need be planes of this grid)
    subs = [s_ for s_ in (_slice(grid, b, tol) for b in _material(p)) if s_ is not None]
    if not subs:
        return
    sl = (_span(subs, 0), _span(subs, 1), _span(subs, 2))
    shape = tuple(s.stop - s.start for s in sl)
    mask = np.zeros(shape, dtype=bool)
    for sub in subs:
        lo = [max(sub[d].start, sl[d].start) for d in range(3)]
        hi = [min(sub[d].stop, sl[d].stop) for d in range(3)]
        if any(hi[d] <= lo[d] for d in range(3)):
            continue
        mask[tuple(slice(lo[d] - sl[d].start, hi[d] - sl[d].start) for d in range(3))] = True
    if p.kind == "solidCylinder":
        from meshpipeline.cad.ingest.ecxml import axis_of_plane
        mask &= _ellipse_mask(grid, p.box, axis_of_plane(p.objects[0].plane), sl)
    for kind, cut, axis in p.cutters:
        cut = _within_walls(p, cut, axis)
        if cut is None:
            continue
        hole = _cut_slice(grid, cut, axis, tol, sl)
        if hole is None:
            continue
        local = tuple(slice(hole[d].start - sl[d].start, hole[d].stop - sl[d].start)
                      for d in range(3))
        if kind == "round":
            mask[local] &= ~_ellipse_mask(grid, cut, axis, hole)
        else:
            mask[local] = False
    zone[sl][mask] = k


def _within_walls(p, cut: _Box, axis: int) -> _Box | None:
    """A vent's cutter cut back, along the wall's normal, to the walls it opens: it reaches past
    them on purpose (so no sliver of wall is left), but there is no material beyond them to open,
    and the grid of a block beside the wall need not hold the vent's outline."""
    slabs = [s for ax_, _lo, _hi, s in p.walls if ax_ == axis and all(
        min(s.hi[i], cut.hi[i]) > max(s.lo[i], cut.lo[i]) for i in range(3) if i != axis)]
    if not slabs:
        return cut
    lo = max(cut.lo[axis], min(s.lo[axis] for s in slabs))
    hi = min(cut.hi[axis], max(s.hi[axis] for s in slabs))
    if hi <= lo:
        return None
    return _Box([cut.lo[i] if i != axis else lo for i in range(3)],
                [cut.hi[i] if i != axis else hi for i in range(3)])


def _cut_slice(grid: Grid, cut: _Box, axis: int, tol: float, within) -> tuple | None:
    """The cells a vent's cutter covers inside a part's slice: exact across the wall (its outline
    lies on grid planes), by cell centre along the wall's normal (the cutter reaches past the wall
    on purpose, to planes the grid does not hold)."""
    idx: list[slice] = [slice(0), slice(0), slice(0)]
    # a cutter that reaches no cell of this grid cuts nothing here (and its outline need not be
    # planes of this grid): looked at on every axis before any outline is looked up
    for i in range(3):
        if i != axis and min(cut.hi[i], float(grid.lines[i][-1])) - \
                max(cut.lo[i], float(grid.lines[i][0])) <= tol:
            return None
    for i in (axis, *(c for c in range(3) if c != axis)):
        if i == axis:
            s = _centre_slice(grid, i, cut.lo[i], cut.hi[i])
        else:
            lo = max(cut.lo[i], float(grid.lines[i][0]))
            hi = min(cut.hi[i], float(grid.lines[i][-1]))
            if hi - lo <= tol:
                return None
            s = slice(grid.index(i, lo, tol), grid.index(i, hi, tol))
        w = within[2 - i]
        s = slice(max(s.start, w.start), min(s.stop, w.stop))
        if s.stop <= s.start:
            return None
        idx[2 - i] = s
    return tuple(idx)


def air_spaces(zone: np.ndarray, volumes: np.ndarray | None = None) -> tuple[np.ndarray, int]:
    """The air split into its connected spaces (face neighbours), largest first: labels 0..n-1 on
    air cells, -1 elsewhere."""
    from scipy import ndimage

    labels, n = ndimage.label(zone == AIR)
    if n == 0:
        return np.full(zone.shape, -1, dtype=np.int32), 0
    flat = labels.ravel()
    weights = volumes.ravel() if volumes is not None else None
    size = np.bincount(flat, weights=weights, minlength=n + 1)[1:]
    order = np.argsort(-size, kind="stable")
    remap = np.full(n + 1, -1, dtype=np.int32)
    remap[order + 1] = np.arange(n, dtype=np.int32)
    return remap[labels], n


def cell_volumes(grid: Grid) -> np.ndarray:
    dx, dy, dz = (grid.widths(i) for i in range(3))
    return dz[:, None, None] * dy[None, :, None] * dx[None, None, :]


def stats(grid: Grid) -> dict:
    """Plain numbers about the grid: cells, sizes, the largest neighbour ratio and aspect."""
    out: dict = {"cells": grid.n_cells, "shape_xyz": list(grid.shape),
                 "background_cell_m": grid.background}
    worst_ratio = 1.0
    for i, a in enumerate("xyz"):
        w = grid.widths(i)
        ratio = float(np.max(np.maximum(w[1:] / w[:-1], w[:-1] / w[1:]))) if len(w) > 1 else 1.0
        worst_ratio = max(worst_ratio, ratio)
        out[f"{a}_cells"] = len(w)
        out[f"{a}_min_m"] = float(w.min())
        out[f"{a}_max_m"] = float(w.max())
        out[f"{a}_max_neighbour_ratio"] = ratio
    out["max_neighbour_ratio"] = worst_ratio
    wmin = [float(grid.widths(i).min()) for i in range(3)]
    wmax = [float(grid.widths(i).max()) for i in range(3)]
    out["max_aspect_ratio_bound"] = max(wmax) / min(wmin)
    return out


__all__ = ["AIR", "Grid", "GridPlan", "OverBudget", "air_spaces", "build_grid", "cell_volumes",
           "cluster", "lines_for", "paint", "stats"]
