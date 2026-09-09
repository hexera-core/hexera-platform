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
    """

    thickness_m: np.ndarray
    sharp: np.ndarray
    area_m2: np.ndarray
    measured: bool

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
                           sharp_span: float = 0.75, chunk: int = 65536) -> ThinFeatureField:
    """The probe itself, on raw triangles (N,3,3). Unit-agnostic: thickness comes back in the
    triangles' own unit, so callers that need metres must hand in metre triangles."""
    T = np.asarray(tris, dtype=float)
    n = int(len(T))
    if n == 0 or T.ndim != 3:
        return _unmeasured(n)
    c = T.mean(axis=1)
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    a2 = np.linalg.norm(fn, axis=1)
    area = 0.5 * a2
    ok = a2 > 0
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
        thickness[rows] = np.where(opposing, proj, np.inf).min(axis=1)
    return ThinFeatureField(thickness_m=thickness, sharp=sharp, area_m2=area, measured=True)


def measure_thin_features(surface, **kw) -> ThinFeatureField:
    """Measure a metre-normalised PreparedSurface (the same contract analyze_surface holds)."""
    from meshpipeline.cad.analysis import AmbiguousSurfaceUnits
    from meshpipeline.cad.prepared_surface import PreparedSurface
    from meshpipeline.cad.stl_io import read_stl_triangles

    if not isinstance(surface, PreparedSurface):
        raise AmbiguousSurfaceUnits(
            "measure_thin_features reports physical thickness, so it needs a PreparedSurface "
            f"that states its coordinates are metres - got {type(surface).__name__}.")
    return measure_from_triangles(read_stl_triangles(surface.path), **kw)


def classify_faces(field: ThinFeatureField, *, thin_below_m: float, razor_below_m: float,
                   sharp_razor_factor: float = 2.0) -> np.ndarray:
    """Per-triangle class codes against caller-supplied thresholds.

    razor: thinner than razor_below_m outright, or SHARP and within sharp_razor_factor of it
           (the wedge band walking into a knife edge fails like the edge, not like a plate).
    thin:  thinner than thin_below_m.
    normal: everything else - including everything, when the field is unmeasured.
    """
    n = field.n_triangles
    labels = np.zeros(n, dtype=np.uint8)
    if not field.measured or n == 0:
        return labels
    t = field.thickness_m
    labels[t < float(thin_below_m)] = CLASS_THIN
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


def thin_refinement_boxes(tris, *, cell_m: float, cells_across: int = 2,
                          pad_frac: float = 0.6, max_level_bump: int = 4,
                          budget_cells: int | None = None) -> list[dict]:
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
    """
    import math as _math

    field = measure_from_triangles(tris)
    if not field.measured:
        return []
    thickness = np.asarray(field.thickness_m, dtype=float)
    # "too thin" = the planned cell cannot fit cells_across of itself across the feature
    too_thin = np.isfinite(thickness) & (thickness > 0.0) & (
        thickness < cell_m * float(cells_across))
    if not bool(too_thin.any()):
        return []
    thinnest = float(thickness[too_thin].min())
    target = thinnest / float(cells_across)
    if target <= 0.0:
        return []
    bump = int(_math.ceil(_math.log2(max(cell_m / target, 1.0) + 1e-12)))
    bump = max(1, min(int(max_level_bump), bump))
    tri_v = np.asarray(tris, dtype=float)[too_thin]
    pad = cell_m * float(pad_frac)
    # a few wall cells: fine enough that features a blade pitch apart stay separate groups
    bin_size = max(3.0 * float(cell_m), 4.0 * thinnest)
    groups = _cluster_boxes(tri_v, bin_size)
    boxes = []
    for idx in groups:
        v = tri_v[idx].reshape(-1, 3)
        lo, hi = v.min(axis=0) - pad, v.max(axis=0) + pad
        boxes.append({"min": lo.tolist(), "max": hi.tolist(), "thinnest_m": thinnest,
                      "n_triangles": int(len(idx)),
                      "volume_m3": float(np.prod(np.maximum(hi - lo, 0.0)))})
    total_vol = sum(b["volume_m3"] for b in boxes)

    def _est(b_):
        return total_vol / (cell_m / (2 ** b_)) ** 3

    clamped = False
    if budget_cells is not None and budget_cells > 0:
        while bump > 1 and _est(bump) > float(budget_cells):
            bump -= 1
            clamped = True
    for b in boxes:
        b["level_bump"] = bump
        b["est_cells"] = int(b["volume_m3"] / (cell_m / (2 ** bump)) ** 3)
    logger.info("thin_refinement_boxes: thinnest feature %.4g m vs cell %.4g m -> +%d level(s) "
                "over %d triangle(s) in %d box(es), ~%.0f cells%s", thinnest, cell_m, bump,
                int(too_thin.sum()), len(boxes), _est(bump),
                (f" (clamped to the {budget_cells}-cell thin-refinement budget - the feature "
                 "may stay under-resolved)" if clamped else ""))
    return boxes
