# Responsibility: Answer the geometric preconditions an engine's input contract asks about.
# Boundaries: measurement for admission - self-intersection, deviation, thickness; it repairs nothing.
from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def surface_analysis_for(spec, surface) -> dict | None:
    from pathlib import Path as _Path
    stl_path = _Path(surface.path)
    ic = getattr(spec, "input_contract", None)
    if ic is None or not (ic.require_no_self_intersection or ic.min_thickness_ratio > 0):
        return None
    try:
        from meshpipeline.cad.analysis import analyze_surface
        a = analyze_surface(surface)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not masquerade as bad geometry
        logger.warning("surface_analysis_for: analyze_surface failed (%s) - no measured "
                       "evidence, deferring", exc)
        return None
    if ic.require_no_self_intersection:
        # ONE scan answers whether AND where: the refusal names the place, so the user can find it
        report = self_intersection_report(stl_path)
        a["self_intersecting"] = report is not None
        if report is not None:
            a["self_intersection"] = report
    return a


def surface_deviation(snapped_surface_path, reference_stl_path) -> dict | None:
    try:
        import pyvista as pv
        wall = pv.read(str(Path(snapped_surface_path))).extract_surface()
        stl = pv.read(str(Path(reference_stl_path))).extract_surface().triangulate()
        if wall.n_cells == 0 or stl.n_cells == 0:
            return None
        dev = np.abs(np.asarray(
            wall.compute_implicit_distance(stl).point_data["implicit_distance"]))
        e = wall.extract_all_edges()
        ep = e.lines.reshape(-1, 3)[:, 1:]
        p = np.asarray(e.points)
        cell = float(np.median(np.linalg.norm(p[ep[:, 0]] - p[ep[:, 1]], axis=1)))
        if cell <= 0:
            return None
        return {
            "mean_ratio": round(float(dev.mean()) / cell, 4),
            "p95_ratio": round(float(np.percentile(dev, 95)) / cell, 4),
            "max_ratio": round(float(dev.max()) / cell, 3),
            "frac_beyond_one_cell": round(float((dev > cell).mean()), 5),
            "cell_size_m": round(cell, 8),
        }
    except Exception as exc:  # noqa: BLE001 - best-effort evidence, never fatal
        logger.warning("surface_deviation: %s - skipping", exc)
        return None


def self_intersects(surface_path, *, max_faces: int = 400_000) -> bool:
    return self_intersection_report(surface_path, max_faces=max_faces, max_pairs=1) is not None


def self_intersection_report(surface_path, *, max_faces: int = 400_000,
                             max_pairs: int = 50) -> dict | None:
    """Where the surface passes through itself, or None when it does not (or cannot be told).

    {"pairs": crossing triangle pairs found (at most max_pairs; "more" when the search stopped
    there), "first_at_m": the first crossing's centre, "region_m": the size of the box holding
    every crossing found, "triangle_m": the typical triangle size there} - in the staged surface's
    metres, so a refusal can say where to look and how big the damage is."""
    try:
        import pyvista as pv
        from vtkmodules.vtkCommonDataModel import vtkTriangle
    except Exception as exc:  # noqa: BLE001 - never let a probe crash the build
        logger.warning("self_intersects: render stack unavailable (%s) - skipping check", exc)
        return None

    try:
        # clean() MERGES coincident points so a shared vertex is a shared INDEX - the only
        # way to tell adjacency (legitimate touching) from a real crossing on a triangle soup.
        surf = pv.read(str(Path(surface_path))).extract_surface().triangulate().clean()
        n = surf.n_cells
        if n == 0:
            return None
        if n > max_faces:
            # the broad phase is ~linear in faces; guard a pathological input rather than hang
            logger.warning("self_intersects: %d faces exceeds %d - skipping (too large to "
                           "check cheaply)", n, max_faces)
            return None
        tris = surf.faces.reshape(-1, 4)[:, 1:]
        pts = np.asarray(surf.points, dtype=float)
        tp = pts[tris]                                   # (n,3,3)
        lo = tp.min(axis=1)
        hi = tp.max(axis=1)

        span = hi - lo
        cell = float(np.median(span[span > 0])) if np.any(span > 0) else float(span.max() or 1.0)
        cell = max(cell, 1e-9)
        gl = np.floor(lo / cell).astype(np.int64)
        gh = np.floor(hi / cell).astype(np.int64)

        buckets: dict[tuple, list] = defaultdict(list)
        for i in range(len(tris)):
            for x in range(gl[i, 0], gh[i, 0] + 1):
                for y in range(gl[i, 1], gh[i, 1] + 1):
                    for z in range(gl[i, 2], gh[i, 2] + 1):
                        buckets[(x, y, z)].append(i)

        triset = [frozenset(t.tolist()) for t in tris]
        tested: set[tuple[int, int]] = set()
        found: list[tuple[int, int]] = []
        for ids in buckets.values():
            m = len(ids)
            if m < 2:
                continue
            for a in range(m):
                i = ids[a]
                for b in range(a + 1, m):
                    j = ids[b]
                    key = (i, j) if i < j else (j, i)
                    if key in tested:
                        continue
                    tested.add(key)
                    if triset[i] & triset[j]:            # shares a vertex → adjacency, not a defect
                        continue
                    if (lo[i] > hi[j]).any() or (lo[j] > hi[i]).any():
                        continue                          # AABBs miss
                    if vtkTriangle.TrianglesIntersect(tp[i][0], tp[i][1], tp[i][2],
                                                      tp[j][0], tp[j][1], tp[j][2]):
                        found.append(key)
                        if len(found) >= max_pairs:
                            return _crossing_report(found, tp, more=True)
        return _crossing_report(found, tp, more=False) if found else None
    except Exception as exc:  # noqa: BLE001 - a probe must never be the reason a build dies
        logger.warning("self_intersects: check failed (%s) - treating as inconclusive", exc)
        return None


def _crossing_report(pairs: list[tuple[int, int]], tp, *, more: bool) -> dict:
    idx = sorted({i for p in pairs for i in p})
    pts = tp[idx].reshape(-1, 3)
    first = tp[list(pairs[0])].reshape(-1, 3).mean(axis=0)
    spans = (tp[idx].max(axis=1) - tp[idx].min(axis=1)).max(axis=1)
    return {"pairs": len(pairs), "more": bool(more),
            "first_at_m": [round(float(v), 6) for v in first],
            "region_m": [round(float(v), 6) for v in (pts.max(axis=0) - pts.min(axis=0))],
            "triangle_m": round(float(np.median(spans)), 6)}
