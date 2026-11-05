# Responsibility: Load polylines and count the boundary loops a surface has.
# Boundaries: geometry reading for review evidence.
from __future__ import annotations

from pathlib import Path

Point = tuple[float, float, float]
Polyline = tuple[Point, ...]


def _points(poly) -> list[Point]:
    return [(float(p[0]), float(p[1]), float(p[2])) for p in poly.points]


def load_polylines(path: str | Path) -> tuple[Polyline, ...]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return ()
    import pyvista as pv

    mesh = pv.read(str(p))
    poly = mesh.extract_surface() if not hasattr(mesh, "lines") else mesh
    pts = _points(poly)
    lines = getattr(poly, "lines", None)
    if lines is None or len(lines) == 0:
        return ()
    out: list[Polyline] = []
    i = 0
    flat = [int(v) for v in lines]
    n = len(flat)
    while i < n:
        count = flat[i]
        if count <= 0 or i + 1 + count > n:
            break
        idxs = flat[i + 1 : i + 1 + count]
        out.append(tuple(pts[j] for j in idxs if 0 <= j < len(pts)))
        i += 1 + count
    return tuple(pl for pl in out if pl)


def count_boundary_loops(path: str | Path) -> int:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return -1
    try:
        import pyvista as pv

        surf = pv.read(str(p)).extract_surface()
        edges = surf.extract_feature_edges(
            boundary_edges=True, feature_edges=False,
            manifold_edges=False, non_manifold_edges=False,
        )
        if not edges.n_cells:
            return 0
        return int(edges.connectivity().split_bodies().n_blocks)
    except Exception:  # noqa: BLE001 - an unreadable auxiliary artifact is an unknown, not a crash
        return -1
