# Responsibility: Measure, per triangle, where a tessellated surface is thin or sharp.
# Boundaries: measurement only - it names no layer counts, sets no thresholds of its own, and
# knows no engine. Classification against thresholds a caller supplies is the one judgement here.
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

#: Class codes, array-friendly. Consumers serialise the NAMES, never the codes.
CLASS_NORMAL, CLASS_THIN, CLASS_RAZOR = 0, 1, 2
CLASS_NAMES: dict[int, str] = {CLASS_NORMAL: "normal", CLASS_THIN: "thin", CLASS_RAZOR: "razor"}


@dataclass(frozen=True)
class ThinFeatureField:
    """Per-triangle thin/sharp measurements of one tessellated surface.

    `thickness_m` is an opposing-normal probe: for each triangle, the distance (projected on the
    triangle's own normal) to the nearest neighbouring triangle whose normal roughly OPPOSES it -
    the local wall thickness of a plate, the closing gap of a wedge, the width of a concave
    junction's slot. +inf means no opposing surface was found among the nearest neighbours, i.e.
    the surface is locally chunky at the tessellation's own scale. Detection is therefore
    CONSERVATIVE: a gap wider than the local neighbour ring reads as +inf (normal), never as thin -
    the razor band at a trailing edge, where the gap closes below the cell size, is exactly where
    the probe is guaranteed to see it.

    `sharp` marks triangles whose k-nearest normal fan contains a near-antiparallel member - a
    knife-edge fold (or the second sheet of a razor-thin plate, which for layer purposes is the
    same condition). A 90-degree box edge does NOT read as sharp. NOTE: coincident duplicated
    sheets in dirty CAD also read razor-thin; the pipeline's corpora are watertight by contract.

    `measured` False means the probe could not run (no scipy, empty/degenerate surface); every
    consumer must then degrade to "no thin features detected" rather than guess.

    `partner` is the index of the opposing triangle each finite thickness was measured to (-1
    where none), so a consumer can judge the PAIR - e.g. whether the reading is within the two
    faces' own tessellation error. None when the probe did not record it.

    `fluid_gap_m`, when the probe was told which side of each face the fluid is on, is the same
    reading taken only ACROSS THE FLUID: the distance to the nearest opposing face in front of
    this one. It is what decides whether two prism stacks collide - stacks grow into the fluid,
    away from the wall, so a plate's own thickness never brings them together; a slot, a gap
    between two parts, a passage does. None when the sides were not known.
    """

    thickness_m: np.ndarray
    sharp: np.ndarray
    area_m2: np.ndarray
    measured: bool
    partner: np.ndarray | None = None
    fluid_gap_m: np.ndarray | None = None

    @property
    def n_triangles(self) -> int:
        return int(len(self.area_m2))


def _unmeasured(n: int, area: np.ndarray | None = None) -> ThinFeatureField:
    return ThinFeatureField(
        thickness_m=np.full(n, np.inf),
        sharp=np.zeros(n, dtype=bool),
        area_m2=area if area is not None else np.zeros(n),
        measured=False)


def measure_from_triangles(tris, *, neighbours: int = 32, opposing_dot: float = -0.2,
                           sharp_span: float = 0.75, chunk: int = 65536,
                           wet_sides: np.ndarray | None = None) -> ThinFeatureField:
    """The probe itself, on raw triangles (N,3,3). Unit-agnostic: thickness comes back in the
    triangles' own unit, so callers that need metres must hand in metre triangles.

    `wet_sides`, when given, says per triangle which side the fluid is on - +1 the side its
    normal points to, -1 the other, 2 both, 0 neither (engines/sealed_cavities.CavityReading).
    Then:
    - a face the fluid never touches (0) - the inner skin of a hollow shell whose inside is kept
      out of the mesh - is neither measured (+inf, never sharp, zero area: it is no part of the
      meshed wall) nor measured AGAINST: a wall backed by a sealed space is as thick as that
      space, however close its other skin lies;
    - `fluid_gap_m` is read too: the gap to the nearest opposing face ON THE FLUID SIDE (both
      sides for a face wetted on both).
    A trailing edge, a fin or a slot keep their readings - the fluid touches both their faces."""
    T = np.asarray(tris, dtype=float)
    n = int(len(T))
    if n == 0 or T.ndim != 3:
        return _unmeasured(n)
    c = T.mean(axis=1)
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    a2 = np.linalg.norm(fn, axis=1)
    area = 0.5 * a2
    ok = a2 > 0
    side = None
    if wet_sides is not None:
        side = np.asarray(wet_sides).astype(np.int8).reshape(-1)
        if len(side) != n:
            raise ValueError(f"wet_sides has {len(side)} entries for {n} triangles")
        ok = ok & (side != 0)
        area = np.where(side != 0, area, 0.0)
    n_ok = int(ok.sum())
    if n_ok < 2:
        return _unmeasured(n, area)
    unit = np.zeros_like(fn)
    unit[ok] = fn[ok] / a2[ok, None]
    try:
        from scipy.spatial import cKDTree
    except Exception:  # noqa: BLE001 - perception is best-effort; consumers see measured=False
        logger.warning("thin-feature probe unavailable (scipy missing?)", exc_info=True)
        return _unmeasured(n, area)

    idx_ok = np.flatnonzero(ok)
    tree = cKDTree(c[idx_ok])
    k = min(int(neighbours) + 1, n_ok)          # +1: the query returns the face itself first
    thickness = np.full(n, np.inf)
    fluid_gap = np.full(n, np.inf) if side is not None else None
    partner = np.full(n, -1, dtype=np.int64)
    sharp = np.zeros(n, dtype=bool)
    for s in range(0, len(idx_ok), max(1, int(chunk))):
        rows = idx_ok[s:s + chunk]
        _, j = tree.query(c[rows], k=k)
        if k == 1:
            j = j[:, None]
        nb = idx_ok[np.asarray(j)]              # neighbour indices in the FULL triangle array
        ni = unit[rows][:, None, :]
        nj = unit[nb]
        dot = (ni * nj).sum(-1)                 # (m, k) alignment of each neighbour's normal
        sep = c[nb] - c[rows][:, None, :]
        d_c = np.linalg.norm(sep, axis=-1)      # centroid distance to each neighbour
        # opposing-normal gap, projected on the face's own normal so a laterally offset partner
        # still measures the true separation (parallel plates: exactly the plate thickness).
        proj = np.abs((sep * ni).sum(-1))
        # SHARP = a crease, not merely an opposite wall: an antiparallel neighbour whose
        # projected gap is far smaller than its distance lies BESIDE this face (the two sides
        # of a knife edge are nearly coplanar). A genuinely opposite wall (a box's far face)
        # has gap ~= distance and stays un-sharp; a 90-degree edge tops out at dispersion 0.5.
        anti = 0.5 * (1.0 - dot) >= sharp_span
        sharp[rows] = (anti & (proj < 0.25 * d_c)).any(axis=1)
        opposing = dot < opposing_dot
        gaps = np.where(opposing, proj, np.inf)
        at = gaps.argmin(axis=1)
        best = gaps[np.arange(len(rows)), at]
        thickness[rows] = best
        partner[rows] = np.where(np.isfinite(best), nb[np.arange(len(rows)), at], -1)
        if fluid_gap is not None and side is not None:
            # in front of the face on its fluid side: the far wall of a slot or a passage. A face
            # wetted on both sides (2) is a sheet: every opposing face is across fluid.
            s_i = side[rows].astype(float)[:, None]
            ahead = (s_i == 2.0) | (s_i * (sep * ni).sum(-1) > 0.0)
            fluid_gap[rows] = np.where(opposing & ahead, proj, np.inf).min(axis=1)
    return ThinFeatureField(thickness_m=thickness, sharp=sharp, area_m2=area, measured=True,
                            partner=partner, fluid_gap_m=fluid_gap)


def measure_thin_features(surface, **kw) -> ThinFeatureField:
    """Measure a metre-normalised PreparedSurface (the same contract analyze_surface holds)."""
    from meshpipeline.cad.analysis import AmbiguousSurfaceUnits
    from meshpipeline.cad.prepared_surface import PreparedSurface

    if not isinstance(surface, PreparedSurface):
        raise AmbiguousSurfaceUnits(
            "measure_thin_features reports physical thickness, so it needs a PreparedSurface "
            f"that states its coordinates are metres - got {type(surface).__name__}.")
    return measure_from_triangles(staged_triangles(surface.path), **kw)


def staged_triangles(path):
    """The wall's triangles exactly as the snappy surface prep stages them - degenerate facets
    dropped - so a per-triangle reading lines up with the staged surface one for one. Read
    without the drop, a car with 35 degenerate facets among 485 757 measured 485 757 labels for
    485 722 staged triangles, and its whole layer policy was silently switched off."""
    from pathlib import Path

    from meshpipeline.cad.stl_io import drop_degenerate, read_stl_triangles

    return drop_degenerate(read_stl_triangles(Path(path)))


def classify_faces(field: ThinFeatureField, *, thin_below_m: float, razor_below_m: float,
                   sharp_razor_factor: float = 2.0) -> np.ndarray:
    """Per-triangle class codes against caller-supplied thresholds.

    razor: thinner than razor_below_m outright, or SHARP and within sharp_razor_factor of it
           (the wedge band walking into a knife edge fails like the edge, not like a plate).
    thin:  thinner than thin_below_m - measured ACROSS THE FLUID when the field knows the fluid
           side (fluid_gap_m): prism stacks collide only where they grow towards each other, in
           a slot or a passage. A solid plate a few cells thick keeps its full stack on both faces;
           only one thinner than razor_below_m (it cannot be castellated) is still razor.
    normal: everything else - including everything, when the field is unmeasured.
    """
    n = field.n_triangles
    labels = np.zeros(n, dtype=np.uint8)
    if not field.measured or n == 0:
        return labels
    t = field.thickness_m
    gap = field.fluid_gap_m if field.fluid_gap_m is not None else t
    labels[gap < float(thin_below_m)] = CLASS_THIN
    razor = (t < float(razor_below_m)) | (
        field.sharp & (t < float(sharp_razor_factor) * float(razor_below_m)))
    labels[razor] = CLASS_RAZOR
    return labels


def class_area_fractions(labels: np.ndarray, area_m2: np.ndarray) -> dict[str, float]:
    total = float(np.asarray(area_m2).sum())
    out: dict[str, float] = {}
    for code, name in CLASS_NAMES.items():
        a = float(np.asarray(area_m2)[np.asarray(labels) == code].sum())
        out[name] = (a / total) if total > 0 else 0.0
    return out


# ------------------------------------------------------------------ refinement ----

def _cluster_boxes(tri_v: np.ndarray, bin_size: float, max_span_bins: int = 8) -> list[np.ndarray]:
    """Compact groups of thin triangles: occupied bins of a coarse grid, joined across the
    26-neighbourhood, and any group longer than max_span_bins bins sliced along its longest axis.
    Returns index arrays into tri_v. One hull over every thin triangle is what this replaces:
    on a blade row the trailing edges are thin along their whole span at five places around the
    annulus, so their common hull was the entire passage - refined to the finest level, 7 M
    cells against a 2 M budget (job 3cd77f85)."""
    cents = tri_v.mean(axis=1)
    keys = np.floor(cents / bin_size).astype(np.int64)
    by_bin: dict = {}
    for i, k in enumerate(map(tuple, keys)):
        by_bin.setdefault(k, []).append(i)
    seen: set = set()
    groups: list[list[int]] = []
    for start in by_bin:
        if start in seen:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:
            k = stack.pop()
            members.extend(by_bin[k])
            kx, ky, kz = k
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        nb = (kx + dx, ky + dy, kz + dz)
                        if nb in by_bin and nb not in seen:
                            seen.add(nb); stack.append(nb)
        groups.append(members)
    out: list[np.ndarray] = []
    for g in groups:
        idx = np.asarray(g, dtype=int)
        c = cents[idx]
        span = c.max(axis=0) - c.min(axis=0)
        axis = int(np.argmax(span))
        if span[axis] <= max_span_bins * bin_size:
            out.append(idx)
            continue
        # a long thin run (a trailing edge, a seam): slabs along its longest axis, each a
        # handful of bins long, so the boxes hug the feature instead of spanning its hull
        slab = 0.5 * max_span_bins * bin_size
        order = np.floor((c[:, axis] - c[:, axis].min()) / slab).astype(int)
        for s in np.unique(order):
            out.append(idx[order == s])
    return out


#: A thickness reading at or below this fraction of the surface's own size is not a thickness: it
#: is two faces lying on each other - coincident sheets, a zero-thickness baffle, a membrane the
#: staging laid over an undeclared opening beside the face around it - and the number the probe
#: returns there is float noise (exactly 0 on flat sheets, microns elsewhere). The staged wall of
#: a shell-and-tube exchanger's shell side (job 470c3eb9, 554 mm long: tube holes sealed at the end
#: face and at both baffles) carries over a thousand such triangles, read 0 to 0.04 mm; the
#: thinnest was announced as "0.0 mm across", bought the biggest level bump, and the run ran out
#: of time. No refinement can put cells across nothing. The same floor
#: cad.analysis.recommend_refinement puts under min_feature (diag * 1e-4); faces touching along a
#: CURVE are caught by _tessellation_sag below.
ZERO_THICKNESS_REL = 1e-4


class ThinRegions(list):
    """The refinement boxes (a plain list of dicts, so every caller that iterates them or tests
    them for truth reads them exactly as before), plus what the probe left out and why.

    note:  set when the refinement was cut to keep the cell count sane - regions left
           UNREFINED for the budget, or fewer levels than the thinnest needs - so the caller can
           tell the user; empty when every region found gets all it needs.
    thinnest_m: the thinnest real reading found (kept or not), None when nothing was thin.
    found: how many thin regions were found before the budget cap.
    coincident_triangles: faces lying on each other (read within their own tessellation error,
           or at the zero-thickness floor) - not thickness, ignored.
    """

    def __init__(self, boxes=(), *, note: str = "", thinnest_m: float | None = None,
                 found: int = 0, coincident_triangles: int = 0) -> None:
        super().__init__(boxes)
        self.note = note
        self.thinnest_m = thinnest_m
        self.found = int(found)
        self.coincident_triangles = int(coincident_triangles)


def _cells(n: float) -> str:
    n = float(n)
    return f"{n / 1e6:.1f} M cells" if n >= 1e5 else f"{int(round(n)):,} cells"


def _surface_diagonal(T: np.ndarray) -> float:
    if T.size == 0:
        return 0.0
    pts = T.reshape(-1, 3)
    pts = pts[np.isfinite(pts).all(axis=1)]
    if len(pts) == 0:
        return 0.0
    return float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))


#: Neighbouring facets whose normals differ by more than this are a real crease of the part, not
#: one smooth face approximated by flat triangles (the pipeline tessellates with a 0.2 rad angular
#: deflection, so adjacent facets of one curved face sit well inside it).
_SMOOTH_DIHEDRAL_RAD = 0.25


def _tessellation_sag(T: np.ndarray) -> np.ndarray:
    """Per triangle, how far the true curved surface can stand off its flat facet (m).

    A facet approximating a curved face of radius R across a chord c misses the face by
    R(1 - cos(phi/2)) ~ c*phi/8, phi being the angle between neighbouring facet normals and c
    the facet's width ACROSS the edge they share (its height over that edge). Two
    tessellations of the SAME curved face - a baffle rim lying on the shell it touches - can
    therefore disagree by up to the sum of their sags, and the opposing-normal probe reads that
    disagreement as a "gap" between 0 and ~2 sags (0.05 - 0.5 mm on the 136 mm shell of job
    470c3eb9). A reading inside it is not a thickness. Flat facets have zero sag, so a real plate
    on flat faces is never discounted; sharp creases (beyond _SMOOTH_DIHEDRAL_RAD) are edges of
    the part, not curvature, and add nothing."""
    n = int(len(T))
    sag = np.zeros(n)
    if n < 2:
        return sag
    finite = np.isfinite(T).all(axis=(1, 2))
    if not bool(finite.all()):
        # a broken facet measures nothing (the probe reads it +inf too); judge the rest alone
        if int(finite.sum()) >= 2:
            sag[finite] = _tessellation_sag(T[finite])
        return sag
    V = T.reshape(-1, 3)
    scale = float(np.abs(V).max())
    if not scale > 0.0:
        return sag
    # weld coincident corners (STL repeats them per facet) so facets sharing an edge can be found
    key = np.round(V / (scale * 1e-9)).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    P = V[first]                                   # one coordinate per welded corner
    F = np.asarray(inv).reshape(n, 3)
    E = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    owner = np.tile(np.arange(n), 3)
    order = np.lexsort((E[:, 1], E[:, 0]))
    Es, ts = E[order], owner[order]
    same = np.all(Es[1:] == Es[:-1], axis=1)
    a, b, e = ts[:-1][same], ts[1:][same], Es[:-1][same]
    if len(a) == 0:
        return sag
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    ln = np.linalg.norm(fn, axis=1)                # twice each facet's area
    unit = np.zeros_like(fn)
    good = ln > 0
    unit[good] = fn[good] / ln[good, None]
    phi = np.arccos(np.clip((unit[a] * unit[b]).sum(axis=1), -1.0, 1.0))
    edge_len = np.linalg.norm(P[e[:, 1]] - P[e[:, 0]], axis=1)
    smooth = (a != b) & good[a] & good[b] & (edge_len > 0) & (phi <= _SMOOTH_DIHEDRAL_RAD)
    a, b, phi, edge_len = a[smooth], b[smooth], phi[smooth], edge_len[smooth]
    # each facet's width across the shared edge = its height over it = 2 * area / edge length
    np.maximum.at(sag, a, ln[a] / edge_len * phi / 8.0)
    np.maximum.at(sag, b, ln[b] / edge_len * phi / 8.0)
    return sag


def thin_refinement_boxes(tris, *, cell_m: float, cells_across: int = 2,
                          pad_frac: float = 0.6, max_level_bump: int = 4,
                          budget_cells: int | None = None,
                          zero_rel: float = ZERO_THICKNESS_REL) -> ThinRegions:
    """Boxes enclosing surface regions too THIN for the planned cell, and the extra
    refinement level each needs.

    The orifice plate is the case this exists for: a 3 mm disc inside a 106 mm pipe. An
    internal plan sizes its wall cell from the BORE (bore / cells_across), so the cell
    lands near 4.4 mm - wider than the plate itself - castellation never captures the
    plate, its faces never become patches, and the run dies at the manifest gate.
    Refining globally to 3 mm would detonate the cell budget across a 400 mm pipe, so
    the correction is LOCAL: find the thin triangles, box them, refine only inside.

    cell_m is the cell size the plan will actually use at the wall. A feature needs
    cells_across cells over its thickness, so its target cell is thickness/cells_across
    and the extra level is log2(cell_m / target), clamped by max_level_bump so a pinhole
    cannot buy unbounded refinement. Returns an empty list when nothing is too thin or
    when the probe could not run - a caller gets no guess, only a measurement.

    The boxes are LOCAL: thin triangles are clustered into compact groups (_cluster_boxes) and
    each group gets its own box, so a feature that is thin along a long run - a blade's
    trailing edge - is hugged by a chain of small boxes rather than covered by one hull. And
    the refinement is BOUNDED: when budget_cells is given, the extra level is lowered until
    the boxes' estimated cell cost (their volume at the refined cell size, a deliberate
    over-estimate that counts the void inside each box too) fits it, never below +1. A clamp
    is logged as unmet need - the feature may then stay under-resolved, but the mesh keeps
    its budget instead of detonating it (7 M cells for a 2 M request, job 3cd77f85).

    When even +1 does not fit the budget, the over-budget refinement is NOT passed through:
    the cheapest boxes that fit are kept and the rest are left unrefined, with a note that
    says so (ThinRegions.note). Passing it through is what let a +1 estimated at 8.6 M cells
    ride along on a 1 M allowance.

    A reading at or below zero_rel of the surface's size is not a thickness at all (see
    ZERO_THICKNESS_REL) and never drives refinement.
    """
    import math as _math

    try:
        cell_m = float(cell_m)
    except (TypeError, ValueError):
        return ThinRegions()
    if not (_math.isfinite(cell_m) and cell_m > 0.0):
        return ThinRegions()       # no planned cell, no "too thin than it" - never a guess
    T = np.asarray(tris, dtype=float)
    field = measure_from_triangles(T)
    if not field.measured:
        return ThinRegions()
    thickness = np.asarray(field.thickness_m, dtype=float)
    finite = np.isfinite(thickness)
    # A GAP READS THE SAME FROM BOTH FACES. The probe projects the step to an opposing neighbour
    # on THIS face's normal; a neighbour well round a curved wall, facing nearly back, projects
    # to almost nothing on it while standing far off its own: annular_001's bore (r 75.6 mm),
    # paired with the tube's outer skin 21 degrees round (r 81 mm), read 0.42 mm "thick" - on
    # the partner's normal the same step is 10.5 mm - and 13 boxes the length of the part were
    # refined for a plate that is not there. A plate's two faces read its thickness on either
    # normal, so the thickness is the larger of the two.
    if field.partner is not None and bool(finite.any()):
        pj = np.asarray(field.partner)
        pair = finite & (pj >= 0)
        if bool(pair.any()):
            c = T.mean(axis=1)
            fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
            nn = fn / np.maximum(np.linalg.norm(fn, axis=1), 1e-300)[:, None]
            back = np.abs(((c[pj[pair]] - c[pair]) * nn[pj[pair]]).sum(axis=1))
            thickness = thickness.copy()
            thickness[pair] = np.maximum(thickness[pair], back)
    # FACES LYING ON EACH OTHER ARE NOT A PLATE. A reading is a thickness only when it clears
    # both the size floor (exact or float-noise zero) and the two facets' own tessellation
    # error (two tessellations of one curved face, touching) - see ZERO_THICKNESS_REL and
    # _tessellation_sag. Below that the faces touch: snappy captures a contact or a
    # zero-thickness baffle at any cell size, and no refinement can put cells across nothing.
    zero_floor = float(zero_rel) * _surface_diagonal(T)
    noise = np.full(len(thickness), zero_floor)
    if field.partner is not None and bool(finite.any()):
        sag = _tessellation_sag(T)
        pj = np.asarray(field.partner)
        has = finite & (pj >= 0)
        noise[has] += sag[has] + sag[pj[has]]
    coincident = finite & (thickness <= noise)
    if bool(coincident.any()):
        logger.info("thin_refinement_boxes: %d triangle(s) read no wider than their faces' own "
                    "tessellation error (up to %.3g m) - faces lying on each other (coincident "
                    "sheets, a contact, a zero-thickness baffle), not a thickness; they drive no "
                    "refinement", int(coincident.sum()), float(noise[coincident].max()))
    # "too thin" = the planned cell cannot fit cells_across of itself across the feature
    too_thin = finite & ~coincident & (thickness < cell_m * float(cells_across))
    if not bool(too_thin.any()):
        return ThinRegions(coincident_triangles=int(coincident.sum()))
    thinnest = float(thickness[too_thin].min())
    target = thinnest / float(cells_across)
    if not (_math.isfinite(target) and target > 0.0):
        return ThinRegions()
    needed = max(1, int(_math.ceil(_math.log2(max(cell_m / target, 1.0) + 1e-12))))
    bump = max(1, min(int(max_level_bump), needed))
    tri_v = T[too_thin]
    thin_t = thickness[too_thin]
    pad = cell_m * float(pad_frac)
    # a few wall cells: fine enough that features a blade pitch apart stay separate groups
    bin_size = max(3.0 * float(cell_m), 4.0 * thinnest)
    groups = _cluster_boxes(tri_v, bin_size)
    boxes = []
    for idx in groups:
        v = tri_v[idx].reshape(-1, 3)
        lo, hi = v.min(axis=0) - pad, v.max(axis=0) + pad
        boxes.append({"min": lo.tolist(), "max": hi.tolist(),
                      "thinnest_m": float(thin_t[idx].min()),
                      "n_triangles": int(len(idx)),
                      "volume_m3": float(np.prod(np.maximum(hi - lo, 0.0)))})

    def _cost(vol: float, b_: int) -> float:
        return vol / (cell_m / (2 ** b_)) ** 3

    def _est(b_):
        return _cost(sum(b["volume_m3"] for b in boxes), b_)

    clamped = False
    note = ""
    n_found = len(boxes)
    if budget_cells is not None and budget_cells > 0:
        while bump > 1 and _est(bump) > float(budget_cells):
            bump -= 1
            clamped = True
        if _est(bump) > float(budget_cells):
            # Even ONE extra level over every box is more than the budget allows. Keep the
            # cheapest boxes that fit - the compact, local features this exists for - and leave
            # the rest (the big hull-like regions that would have detonated the budget) alone.
            kept_ids: set[int] = set()
            spent = 0.0
            for b in sorted(boxes, key=lambda b_: b_["volume_m3"]):
                c = _cost(b["volume_m3"], bump)
                if spent + c <= float(budget_cells):
                    kept_ids.add(id(b))
                    spent += c
            left = [b for b in boxes if id(b) not in kept_ids]
            boxes = [b for b in boxes if id(b) in kept_ids]
            need = sum(_cost(b["volume_m3"], bump) for b in left)
            note = (f"{len(left)} of {n_found} thin spots left unrefined: they would need about "
                    f"{_cells(need)}, over the {_cells(budget_cells)} allowed for local "
                    "refinement")
            logger.warning("thin_refinement_boxes: %s", note)
    if boxes and bump < needed:
        # the honest half of a clamp: the user hears that the feature is refined LESS than it
        # needs, rather than reading "refining locally so it is captured" over a promise the
        # level cap or the budget cannot keep
        _short = (f"{bump} extra level(s) instead of the {needed} the thinnest needs, to keep the "
                  "cell count sane, so it may stay under-resolved")
        note = f"{note}; {_short}" if note else _short
    for b in boxes:
        b["level_bump"] = bump
        b["est_cells"] = int(_cost(b["volume_m3"], bump))
    logger.info("thin_refinement_boxes: thinnest feature %.4g m vs cell %.4g m -> +%d level(s) "
                "over %d triangle(s) in %d of %d box(es), ~%.0f cells%s", thinnest, cell_m, bump,
                int(too_thin.sum()), len(boxes), n_found, _est(bump),
                (f" (clamped to the {budget_cells}-cell thin-refinement budget - the feature "
                 "may stay under-resolved)" if clamped else ""))
    return ThinRegions(boxes, note=note, thinnest_m=thinnest, found=n_found,
                       coincident_triangles=int(coincident.sum()))
