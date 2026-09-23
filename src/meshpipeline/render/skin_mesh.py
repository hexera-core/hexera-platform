# Responsibility: Prepare a part's triangle skin for a CAD-style view - shared vertices, smooth
# normals that break at sharp corners, and the sharp edges themselves as lines - so the console
# draws it the way a CAD viewer does rather than as a soup of flat facets.
# Boundaries: numpy over triangles. No OpenCASCADE, no rendering, no storage; the caller decides
# what to do with the arrays.
from __future__ import annotations

import base64
import math

import numpy as np

#: Two faces meeting at more than this angle share an edge that is drawn, and a corner that is
#: not smoothed across. The usual CAD-viewer value.
FEATURE_ANGLE_DEG = 35.0


def prepare_skin(tris: np.ndarray, *, feature_angle_deg: float = FEATURE_ANGLE_DEG) -> dict:
    """From an (n, 3, 3) triangle soup: `points` (m, 3), `polys` in VTK's [3, a, b, c, ...] form,
    per-point `normals` (m, 3) averaged only across faces that meet gently, so a cylinder reads
    smooth and a box keeps its corners; and the sharp edges as `edge_points` / `edge_lines`
    ([2, a, b, ...]) over the welded vertices, boundary edges included."""
    tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if len(tris) == 0:
        raise ValueError("no triangles to prepare")
    verts, faces = _weld(tris)
    fn = _face_normals(verts, faces)
    cos_lim = math.cos(math.radians(feature_angle_deg))

    # faces around each vertex
    incident: list[list[int]] = [[] for _ in range(len(verts))]
    for f, (a, b, c) in enumerate(faces):
        incident[a].append(f); incident[b].append(f); incident[c].append(f)

    # ONE OUTPUT VERTEX PER (vertex, smoothing group): a corner's normal averages the incident
    # faces that meet its own face gently; corners whose averages differ become separate points,
    # which is what keeps a box's edges crisp under smooth shading.
    out_pts: list[tuple[float, float, float]] = []
    out_nrm: list[tuple[float, float, float]] = []
    key_to_id: dict[tuple, int] = {}
    polys = np.empty(len(faces) * 4, dtype=np.uint32)
    for f, corners in enumerate(faces):
        polys[f * 4] = 3
        for k, v in enumerate(corners):
            group = [g for g in incident[v] if float(np.dot(fn[g], fn[f])) >= cos_lim]
            n = fn[group].sum(axis=0)
            L = float(np.linalg.norm(n)) or 1.0
            n = n / L
            key = (int(v), round(float(n[0]), 3), round(float(n[1]), 3), round(float(n[2]), 3))
            pid = key_to_id.get(key)
            if pid is None:
                pid = len(out_pts)
                key_to_id[key] = pid
                out_pts.append((float(verts[v][0]), float(verts[v][1]), float(verts[v][2])))
                out_nrm.append((float(n[0]), float(n[1]), float(n[2])))
            polys[f * 4 + 1 + k] = pid

    # SHARP EDGES: an interior edge whose two faces meet at more than the feature angle, or a
    # boundary edge with one face only
    e = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    e = np.sort(e, axis=1)
    owner = np.tile(np.arange(len(faces)), 3)
    uniq, inverse, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    first = np.full(len(uniq), -1, dtype=np.int64)
    second = np.full(len(uniq), -1, dtype=np.int64)
    for k in range(len(inverse)):
        u = inverse[k]
        if first[u] < 0:
            first[u] = owner[k]
        elif second[u] < 0:
            second[u] = owner[k]
    sharp = counts == 1
    both = (first >= 0) & (second >= 0)
    dots = np.ones(len(uniq))
    dots[both] = np.einsum("ij,ij->i", fn[first[both]], fn[second[both]])
    sharp |= both & (dots < cos_lim)
    edge_lines = np.empty(int(sharp.sum()) * 3, dtype=np.uint32)
    edge_lines[0::3] = 2
    edge_lines[1::3] = uniq[sharp][:, 0]
    edge_lines[2::3] = uniq[sharp][:, 1]

    return {
        "points": np.asarray(out_pts, dtype=np.float32).reshape(-1, 3),
        "normals": np.asarray(out_nrm, dtype=np.float32).reshape(-1, 3),
        "polys": polys,
        "edge_points": verts.astype(np.float32),
        "edge_lines": edge_lines,
        "tri_count": int(len(faces)),
    }


def skin_patch(prepared: dict, name: str = "skin") -> dict:
    """The prepared skin as one viewer patch, base64 like the mesh viewer's polyMesh form, plus
    normals; the edges ride beside it."""
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")  # noqa: E731
    return {
        "name": name, "type": "", "face_count": prepared["tri_count"], "tri_count": prepared["tri_count"],
        "points_b64": b64(prepared["points"]), "polys_b64": b64(prepared["polys"]),
        "normals_b64": b64(prepared["normals"]),
    }


def edges_block(prepared: dict) -> dict:
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")  # noqa: E731
    return {"points_b64": b64(prepared["edge_points"]), "lines_b64": b64(prepared["edge_lines"]),
            "count": int(len(prepared["edge_lines"]) // 3)}


def _weld(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pts = tris.reshape(-1, 3)
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    key = np.round(pts / (1e-6 * span)).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = pts[first]
    faces = inverse.reshape(-1, 3)
    keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return verts, faces[keep]


def _face_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    cross = np.cross(b - a, c - a)
    L = np.linalg.norm(cross, axis=1)
    return cross / np.where(L > 0, L, 1.0)[:, None]


__all__ = ["FEATURE_ANGLE_DEG", "edges_block", "prepare_skin", "skin_patch"]
