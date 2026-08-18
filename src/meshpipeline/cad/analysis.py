# Responsibility: Measure an uploaded surface and say what it implies for meshing.
# Boundaries: measurement and recommendation; it modifies no geometry and never resolves an ambiguous unit by guessing.
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np

from meshpipeline.cad.stl_io import read_stl_triangles

logger = logging.getLogger(__name__)


def _metre_surface_path(surface) -> Path:
    from meshpipeline.cad.prepared_surface import PreparedSurface

    if isinstance(surface, PreparedSurface):
        return Path(surface.path)
    raise AmbiguousSurfaceUnits(
        "analyze_surface measures physical size, so it needs a PreparedSurface that states its "
        f"coordinates are metres - got {type(surface).__name__}. Prepare the surface through "
        "cad.staging.prepare_surface first; a path alone cannot say what its numbers mean.")


class AmbiguousSurfaceUnits(TypeError):
    pass


def _triangle_array(stl_path: Path) -> np.ndarray:
    tris = read_stl_triangles(Path(stl_path))
    if not tris:
        raise ValueError(f"no triangles read from {stl_path}")
    return np.asarray(tris, dtype=float)


def analyze_surface(surface, *, max_samples: int = 60000) -> dict:
    stl_path = _metre_surface_path(surface)
    T = _triangle_array(stl_path)                      # (N,3,3)
    pts = T.reshape(-1, 3)
    mn, mx = pts.min(0), pts.max(0)
    ext = mx - mn
    diag = float(np.linalg.norm(ext))
    L = float(ext.max())

    # edge lengths = the size of features the CAD already resolved (TE panels, fillets)
    e = np.concatenate([
        np.linalg.norm(T[:, 0] - T[:, 1], axis=1),
        np.linalg.norm(T[:, 1] - T[:, 2], axis=1),
        np.linalg.norm(T[:, 2] - T[:, 0], axis=1)])
    e = e[e > diag * 1e-9]
    edge_p = {p: float(np.percentile(e, p)) for p in (1, 5, 50, 95)}

    # face centroids + unit normals
    c = T.mean(axis=1)
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    nrm = np.linalg.norm(fn, axis=1)
    keep = nrm > 0
    c, fn = c[keep], fn[keep] / nrm[keep, None]

    # LOCAL FEATURE SIZE via proximity: for each sampled face, the distance to the nearest
    # FACING surface (normal roughly opposed) beyond its own neighbourhood. That distance is
    # the local wall thickness / gap width - a medial-axis approximation. The low percentile
    # is the thinnest feature in the part (sets the finest refinement you must reach).
    thin_gap = edge_p[1]
    try:
        from scipy.spatial import cKDTree
        n = len(c)
        si = (np.random.default_rng(0).choice(n, size=min(max_samples, n), replace=False)
              if n > max_samples else np.arange(n))
        tree = cKDTree(c)
        d, j = tree.query(c[si], k=min(24, n))
        adj = edge_p[5] * 0.75                          # ignore self + immediate neighbours
        lfs = []
        for r in range(len(si)):
            ns = fn[si[r]]
            for m in range(1, d.shape[1]):
                if d[r, m] > adj and float(ns @ fn[j[r, m]]) < -0.2:
                    lfs.append(d[r, m])
                    break
        if lfs:
            thin_gap = float(np.percentile(lfs, 1))
    except Exception:                                   # noqa: BLE001 - perception is best-effort
        logger.warning("LFS proximity pass failed; falling back to edge percentile", exc_info=True)

    # curvature proxy: dispersion of normals among nearest neighbours (high = sharp/curved)
    curvature_hi = 0.0
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(c)
        d2, j2 = tree.query(c[si], k=min(8, len(c)))
        spread = 1.0 - np.abs((fn[si][:, None, :] * fn[j2]).sum(-1)).mean(1)
        curvature_hi = float(np.percentile(spread, 95))
    except Exception:                                   # noqa: BLE001 - perception is best-effort
        # A silent 0.0 reads downstream as a perfectly FLAT surface, and the builder
        # then under-refines. The 0.0 fallback is fine; swallowing the reason is not -
        # a systematic scipy failure must be visible, like the LFS pass above.
        logger.warning("curvature proxy failed; curvature_hi defaults to 0.0 (flat)",
                       exc_info=True)

    # ROBUST fine-feature scale (this replaces an earlier edge-min + arbitrary floor that, on
    # dirty CAD, was driven by degenerate slivers / a hard-coded constant - NOT real geometry).
    # AREA-WEIGHT the per-triangle characteristic length: slivers and degenerate triangles carry
    # ~zero area, so they cannot pull the feature size down. min_feature is the scale below which
    # only ~1% of the surface AREA lies - a genuine, resolvable feature, not tessellation noise.
    v0, v1, v2 = T[:, 0], T[:, 1], T[:, 2]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    med = float(np.median(area)) if area.size else 0.0
    good = area > med * 1e-6                                 # drop degenerate slivers
    area_g = area[good]
    if area_g.size:
        char = np.sqrt(2.0 * area_g)                        # characteristic length per triangle
        order = np.argsort(char)
        cum = np.cumsum(area_g[order])
        idx = int(np.searchsorted(cum, 0.01 * cum[-1]))     # 1%-of-area quantile
        feature_size = float(char[order][min(idx, len(char) - 1)])
    else:
        feature_size = edge_p[50]
    # the proximity gap is kept as INFO only - it is noise-prone on coincident-surface CAD, so it
    # no longer silently drives refinement. Trust it only when it is a real narrow channel.
    gap_is_real = feature_size * 0.25 < thin_gap < feature_size * 4
    min_feature = min(feature_size, thin_gap) if gap_is_real else feature_size
    return {
        "bbox_min": mn.tolist(), "bbox_max": mx.tolist(),
        "extent": ext.tolist(), "diag": diag, "L": L, "n_triangles": int(len(T)),
        "edge_pct": edge_p, "thin_gap": float(thin_gap), "feature_size": float(feature_size),
        "min_feature": float(min_feature), "curvature_hi": curvature_hi,
        "surface_area": float(area_g.sum()),     # wetted area - drives the budget back-solve
    }


def recommend_refinement(analysis: dict, *, cells_per_feature: int = 8,
                         base_divisions: int = 24, body_cells: int = 150,
                         max_level: int = 8, max_cells: int = 8_000_000,
                         depth: int = 12) -> dict:
    diag, L = analysis["diag"], analysis["L"]
    minf = max(analysis["min_feature"], diag * 1e-4)
    area = float(analysis.get("surface_area") or diag * diag)
    base_cell = L / base_divisions

    def _level(cell_target: float) -> int:
        return int(math.ceil(math.log2(max(base_cell / max(cell_target, 1e-30), 1.0))))

    # BUDGET CEILING: finest surface level whose refined volume stays within max_cells,
    # derived from the MEASURED area. This is what stops the band volume from exploding.
    afford = int(math.floor(0.5 * math.log2(
        max(max_cells * base_cell ** 2 / (depth * max(area, 1e-9)), 1.0))))
    afford = max(1, min(afford, max_level))

    # GLOBAL surface level = min(capture-the-body, budget ceiling).
    surf_level = max(1, min(_level(diag / body_cells), afford, max_level - 1))
    # LOCAL feature (eMesh edge) level - cap the JUMP above the surface level to +2. A larger
    # jump (e.g. 3→6) forces huge nCellsBetweenLevels transition shells around every feature
    # edge (the second overshoot), and on a sub-mm TE over a 60 m wing you can't resolve it at
    # this budget anyway. TRUE need is reported so the number never implies full resolution.
    feat_level_true = _level(minf / cells_per_feature)
    feat_level = max(surf_level, min(feat_level_true, surf_level + 2, max_level))

    # distance bands in SURFACE-CELL units: the near band hugs the wall a few cells out (BL +
    # immediate wake), then a coarser transition band - so band cost scales with the surface,
    # not a large absolute volume.
    surf_cell = base_cell / 2 ** surf_level
    near = round(6.0 * surf_cell, 6)
    far = round(max(4.0 * near, 0.04 * L), 6)
    bands = [(near, surf_level), (far, max(1, surf_level - 1))]

    resolve_angle = 25.0 if analysis.get("curvature_hi", 0) > 0.15 else 35.0
    return {
        "base_cell": float(base_cell),
        "surface_level": (int(surf_level), int(surf_level)),
        "feature_level": int(feat_level),
        "feature_level_true": int(feat_level_true),
        "afford_level": int(afford),
        "budget_capped": bool(feat_level_true > feat_level
                              or _level(diag / body_cells) > afford),
        "distance_bands": bands,
        "resolve_feature_angle": float(resolve_angle),
        "min_feature": float(minf), "surface_area": area,
        "cells_across_min_feature": int(cells_per_feature),
    }
