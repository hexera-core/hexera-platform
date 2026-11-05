# Responsibility: Inspect triangulated surface files for repair-relevant defects.
# Boundaries: diagnostics only; never mutates geometry and never infers physical units.
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairReport,
)
from meshpipeline.cad.stl_io import read_stl_triangles


def _triangles_from_stl(path: Path) -> np.ndarray:
    tris = read_stl_triangles(path)
    return (
        np.asarray(tris, dtype=float).reshape((-1, 3, 3))
        if tris
        else np.empty((0, 3, 3))
    )


def _triangles_from_vtp(path: Path) -> np.ndarray:
    import pyvista as pv

    mesh = pv.read(str(path)).extract_surface().triangulate()
    if mesh.n_cells == 0:
        return np.empty((0, 3, 3))
    faces = mesh.faces.reshape(-1, 4)[:, 1:]
    pts = np.asarray(mesh.points, dtype=float)
    return pts[faces]


def _coord_key(coord: float) -> float:
    value = float(coord)
    return 0.0 if value == 0.0 else value


def _point_key(point: np.ndarray) -> tuple[float, float, float]:
    return (_coord_key(point[0]), _coord_key(point[1]), _coord_key(point[2]))


def _triangle_key(tri: np.ndarray) -> tuple:
    return tuple(sorted(_point_key(p) for p in tri))


def _edge_key(a: np.ndarray, b: np.ndarray) -> tuple:
    pa = _point_key(a)
    pb = _point_key(b)
    return tuple(sorted((pa, pb)))


def _non_manifold_vertices(tris: np.ndarray) -> int:
    """Count vertices whose incident triangles form more than one fan.

    Two fans meeting at a single vertex are non-manifold there even when every
    edge is used by at most two triangles, so edge counts alone cannot see it.
    """
    corners: list[tuple[tuple, tuple, tuple]] = [
        (_point_key(tri[0]), _point_key(tri[1]), _point_key(tri[2])) for tri in tris
    ]
    incident: dict[tuple, list[int]] = {}
    for index, keys in enumerate(corners):
        for key in set(keys):
            incident.setdefault(key, []).append(index)

    count = 0
    for vertex, triangle_indices in incident.items():
        if len(triangle_indices) < 2:
            continue
        parent = {index: index for index in triangle_indices}

        def find(index: int, parent: dict[int, int] = parent) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        seen_edges: dict[tuple, int] = {}
        for index in triangle_indices:
            for other in corners[index]:
                if other == vertex:
                    continue
                edge = tuple(sorted((vertex, other)))
                previous = seen_edges.setdefault(edge, index)
                if previous != index:
                    left, right = find(previous), find(index)
                    if left != right:
                        parent[left] = right
        if len({find(index) for index in triangle_indices}) > 1:
            count += 1
    return count


def _surface_metrics(tris: np.ndarray) -> dict:
    if len(tris) == 0:
        return {
            "n_triangles": 0,
            "n_points": 0,
            "bbox_min": [],
            "bbox_max": [],
            "degenerate_faces": 0,
            "duplicate_faces": 0,
            "boundary_edges": 0,
            "non_manifold_edges": 0,
            "non_manifold_vertices": 0,
        }

    points = tris.reshape(-1, 3)
    unique_points = {_point_key(p) for p in points}
    areas = 0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1
    )
    face_counts = Counter(_triangle_key(tri) for tri in tris)
    edge_counts: Counter[
        tuple[tuple[float, float, float], tuple[float, float, float]]
    ] = Counter()
    for tri in tris:
        edge_counts[_edge_key(tri[0], tri[1])] += 1
        edge_counts[_edge_key(tri[1], tri[2])] += 1
        edge_counts[_edge_key(tri[2], tri[0])] += 1

    return {
        "n_triangles": int(len(tris)),
        "n_points": int(len(unique_points)),
        "bbox_min": [_coord_key(c) for c in points.min(axis=0)],
        "bbox_max": [_coord_key(c) for c in points.max(axis=0)],
        "degenerate_faces": int((areas <= 0.0).sum()),
        "duplicate_faces": int(sum(v - 1 for v in face_counts.values() if v > 1)),
        "boundary_edges": int(sum(1 for v in edge_counts.values() if v == 1)),
        "non_manifold_edges": int(sum(1 for v in edge_counts.values() if v > 2)),
        "non_manifold_vertices": _non_manifold_vertices(tris),
    }


def _defects(metrics: dict) -> tuple[RepairDefect, ...]:
    defects: list[RepairDefect] = []
    if metrics["degenerate_faces"]:
        defects.append(
            RepairDefect(
                code=DefectCode.degenerate_edge,
                severity=DefectSeverity.error,
                message="Degenerate surface triangles were found.",
                count=metrics["degenerate_faces"],
                details={"degenerate_faces": metrics["degenerate_faces"]},
            )
        )
    if metrics["duplicate_faces"]:
        defects.append(
            RepairDefect(
                code=DefectCode.duplicate_surface_data,
                severity=DefectSeverity.warning,
                message="Duplicate surface triangles were found.",
                count=metrics["duplicate_faces"],
                details={"duplicate_faces": metrics["duplicate_faces"]},
            )
        )
    if metrics["non_manifold_edges"]:
        defects.append(
            RepairDefect(
                code=DefectCode.non_manifold_surface,
                severity=DefectSeverity.error,
                message="Surface edges used by more than two triangles were found.",
                count=metrics["non_manifold_edges"],
                details={"non_manifold_edges": metrics["non_manifold_edges"]},
            )
        )
    if metrics["non_manifold_vertices"]:
        defects.append(
            RepairDefect(
                code=DefectCode.non_manifold_surface,
                severity=DefectSeverity.error,
                message="Surface vertices joining separate triangle fans were found.",
                count=metrics["non_manifold_vertices"],
                details={"non_manifold_vertices": metrics["non_manifold_vertices"]},
            )
        )
    return tuple(defects)


def _fatal_read_report(path: Path, exc: Exception) -> RepairReport:
    suffix = path.suffix.lower()
    defect = RepairDefect(
        code=DefectCode.engine_staging_failure,
        severity=DefectSeverity.fatal,
        message="Surface file could not be inspected.",
        details={
            "path_suffix": suffix,
            "error": type(exc).__name__,
        },
    )
    return RepairReport(
        defects=(defect,),
        measurements=(RepairMeasurement(name="format", value=suffix.lstrip(".")),),
        operations=({"name": "inspect_surface", "mutated": False},),
        summary="Surface file could not be inspected.",
        diagnostics={"path_suffix": suffix},
    )


def _unusable_surface_report(path: Path, reason: str, message: str) -> RepairReport:
    suffix = path.suffix.lower()
    defect = RepairDefect(
        code=DefectCode.engine_staging_failure,
        severity=DefectSeverity.fatal,
        message=message,
        details={"path_suffix": suffix, "reason": reason},
    )
    return RepairReport(
        defects=(defect,),
        measurements=(RepairMeasurement(name="format", value=suffix.lstrip(".")),),
        operations=({"name": "inspect_surface", "mutated": False},),
        summary=message,
        diagnostics={"path_suffix": suffix, "reason": reason},
    )


def inspect_surface_file(path: Path) -> RepairReport:
    p = Path(path)
    suffix = p.suffix.lower()
    if not p.exists():
        raise FileNotFoundError(p)
    try:
        tris = _triangles_from_vtp(p) if suffix == ".vtp" else _triangles_from_stl(p)
    except (FileNotFoundError, ImportError):
        raise
    except Exception as exc:
        return _fatal_read_report(p, exc)
    if len(tris) == 0:
        return _unusable_surface_report(
            p,
            "empty_surface",
            "Surface file contains no triangles.",
        )
    if not bool(np.isfinite(tris).all()):
        return _unusable_surface_report(
            p,
            "non_finite_coordinates",
            "Surface file contains non-finite vertex coordinates.",
        )
    metrics = _surface_metrics(tris)
    metrics["format"] = suffix.lstrip(".")
    defects = _defects(metrics)
    summary = "Repair recommended before meshing." if defects else "No repair needed."
    measurements = tuple(RepairMeasurement(name=k, value=v) for k, v in metrics.items())
    return RepairReport(
        defects=defects,
        measurements=measurements,
        operations=({"name": "inspect_surface", "mutated": False},),
        summary=summary,
        diagnostics={"path_suffix": suffix},
    )
