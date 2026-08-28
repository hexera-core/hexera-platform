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
