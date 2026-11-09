# Responsibility: Fit a DELIVERED polyMesh boundary into what a browser can carry and draw, and
# say in the payload exactly how much of it is being shown.
# Boundaries: numpy over the compact (points, polys) pair that a boundary reader already produced.
# It opens no files, knows no engine, and never changes what the mesh IS - only what is drawn.
# Collaborates with: render/viewer_pack.py (packs what comes out), engines/*/viewer.py (call it).
from __future__ import annotations

import logging
import math
import os

import numpy as np

logger = logging.getLogger(__name__)

#: THE TRIANGLE BUDGET FOR A DELIVERED MESH, and why it is not the input skin's 400,000.
#: `skin_mesh.STAGE_MAX_TRIANGLES` governs the tessellated INPUT skin, which is a picture of the
#: part. This governs the mesh that was BUILT, which is the thing a reader is deciding about, so it
#: is worth more of their patience: about 24 MB of base64 against the input skin's 12 MB, and one
#: to three seconds of JSON parse.
#:
#: MEASURED, not chosen in the abstract. The first real customer cold plate delivers 13,022,400
#: boundary faces on the coolant alone (2026-10-28). Packed whole by `viewer_pack` that is 416.8 MB
#: raw and 555.8 MB base64 - past the 512 MiB cap `sandbox.safe_extract` puts on a workspace
#: archive before any browser is asked to parse it. At this budget the same mesh packs to about
#: 24 MB.
#: OVERRIDABLE PER BUILD, because 800,000 is a transport figure and the transport changed.
#: Measured 2026-11-07, one actor at 1:1: 1,904,636 faces / 3,810,902 triangles drew in 0.30 ms of
#: CPU per frame at 55.2 fps on Intel UHD via ANGLE/D3D11, and the binary path carried the same
#: payload in 574 ms against base64's 1,379 ms. What actually binds is VRAM, about 92 B per
#: delivered face, so a whole 36 M-face boundary would be ~3.3 GB and is out of reach - but several
#: million faces is not. The default is unchanged so no existing page moves; a build that has been
#: measured sets HEXERA_DRAWN_MAX_TRIANGLES.
DRAWN_MAX_TRIANGLES = int(os.environ.get("HEXERA_DRAWN_MAX_TRIANGLES") or 800_000)

#: A patch is never reduced below this. A six-face block side would otherwise vanish entirely at a
#: coarse cluster size, and a boundary that is not drawn reads to a user as a boundary that is not
#: there.
MIN_TRIANGLES_PER_PATCH = 64

#: How many times the cluster size is allowed to grow before the result is accepted as it stands.
#: Each pass costs a `np.unique` over every point, so this is the one place the cost is bounded.
_MAX_PASSES = 6


def _decode(patch: dict) -> tuple[np.ndarray, np.ndarray]:
    """A patch's stored bytes back as (points (n, 3) float32, polys uint32 in VTK's form)."""
    pts = np.frombuffer(patch["points"], dtype=np.float32).reshape(-1, 3)
    polys = np.frombuffer(patch["polys"], dtype=np.uint32)
    return pts, polys


def _triangles(polys: np.ndarray) -> np.ndarray:
    """VTK's [n, a, b, c, ...] stream as an (m, 3) fan triangulation.

    A cartesian mesh's boundary is 99.9% quads with a handful of n-gons where the octree graded, so
    the sizes present are few and each is handled as one vectorised block. A fan is correct for
    these because every face is planar and convex: they are cell faces of an axis-aligned mesh.
    """
    if polys.size == 0:
        return np.empty((0, 3), dtype=np.int64)
    # walk the stream once to find where each face starts
    starts: list[int] = []
    i = 0
    n = int(polys.size)
    while i < n:
        starts.append(i)
        i += int(polys[i]) + 1
    idx = np.asarray(starts, dtype=np.int64)
    sizes = polys[idx].astype(np.int64)

    out: list[np.ndarray] = []
    for k in np.unique(sizes):
        sel = idx[sizes == k]
        # (faces, k) of point ids, without a python loop over faces
        ring = polys[(sel[:, None] + 1 + np.arange(int(k))[None, :])].astype(np.int64)
        for t in range(int(k) - 2):
            out.append(np.stack([ring[:, 0], ring[:, t + 1], ring[:, t + 2]], axis=1))
    return np.concatenate(out, axis=0) if out else np.empty((0, 3), dtype=np.int64)


def _facing(pts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Which of the six axis sectors each vertex's surface normal points into.

    The normal is the area-weighted sum of the incident triangles' normals, which is the usual
    vertex normal and needs no connectivity beyond the triangle list. `bincount` rather than
    `np.add.at`: the same scatter-add, and on 13 million vertices it is the difference between
    seconds and minutes.

    Six sectors is deliberate. It is the coarsest split that puts two opposing sheets in different
    buckets, which is all this has to do; a finer split would start separating a merely curved
    surface from itself and cost detail rather than save it.
    """
    v0, v1, v2 = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)          # length is twice the triangle's area
    flat = tris.ravel()
    acc = np.empty((len(pts), 3), dtype=np.float64)
    for k in range(3):
        acc[:, k] = np.bincount(flat, weights=np.repeat(fn[:, k], 3), minlength=len(pts))
    axis = np.argmax(np.abs(acc), axis=1)
    sign = (acc[np.arange(len(acc)), axis] >= 0).astype(np.int64)
    # a vertex no triangle touches has a zero normal; it lands in sector 1 and is harmless,
    # because no surviving triangle refers to it
    return axis * 2 + sign


def _cluster_once(pts: np.ndarray, tris: np.ndarray, h: float) -> tuple[np.ndarray, np.ndarray]:
    """One pass of vertex clustering: every point snapped to the centre of the `h` cell it falls
    in, triangles reindexed, collapsed and repeated triangles dropped.

    THE CLUSTERING IS DONE ON THE POINTS, not on a triangle soup, and that is the whole reason this
    is affordable here. `skin_mesh.simplified` clusters (n, 3, 3) soup, so for this mesh it would
    sort 78 million rows where this sorts 13 million.
    """
    lo = pts.min(axis=0)
    cell = np.floor((pts - lo) / h).astype(np.int64)
    # WHICH WAY THE VERTEX FACES, as a fourth key column. Without it a fin thinner than `h` puts
    # both of its faces in one cell, every triangle on it collapses and the fin is DROPPED - the
    # reducer does not blur a fine structure, it perforates it. Opposing sheets point opposite
    # ways, so this separates them however thin the fin is.
    key = np.concatenate([cell, _facing(pts, tris)[:, None]], axis=1)
    _, inverse, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    sums = np.zeros((len(counts), 3), dtype=np.float64)
    np.add.at(sums, inverse, pts.astype(np.float64))
    reps = (sums / counts[:, None]).astype(np.float32)

    t = inverse[tris]
    keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    t = t[keep]
    if len(t):
        _, first = np.unique(np.sort(t, axis=1), axis=0, return_index=True)
        t = t[np.sort(first)]
    return reps, t


def _repack(pts: np.ndarray, tris: np.ndarray) -> tuple[bytes, bytes, int]:
    """(points bytes, polys bytes, triangle count), carrying only the points still referenced."""
    if len(tris) == 0:
        return b"", b"", 0
    uniq, inv = np.unique(tris.reshape(-1), return_inverse=True)
    tri = inv.reshape(-1, 3).astype(np.uint32)
    polys = np.empty((len(tri), 4), dtype=np.uint32)
    polys[:, 0] = 3
    polys[:, 1:] = tri
    return pts[uniq].astype(np.float32).tobytes(), polys.reshape(-1).tobytes(), len(tri)


def reduce_patch(patch: dict, *, max_triangles: int) -> tuple[dict, int, float]:
    """`patch` drawn with at most about `max_triangles` triangles.

    Returns (the patch, the triangles it came in with, the cluster size used in metres; 0.0 when
    nothing was merged). The patch is returned UNCHANGED when it already fits, so a small boundary
    keeps its exact faces and its quads.
    """
    pts, polys = _decode(patch)
    tris = _triangles(polys)
    came_with = len(tris)
    if came_with <= max_triangles or came_with == 0 or len(pts) == 0:
        return patch, came_with, 0.0

    # the cluster size that lands near the budget, from the patch's own area: two triangles per
    # h-by-h square is what a surface made of cell faces gives back
    v = pts[tris]
    area = 0.5 * float(np.linalg.norm(
        np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1).sum())
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    h = max(math.sqrt(2.0 * area / max(max_triangles, 1)), 1e-9 * span)

    best_pts, best_tris, used = pts, tris, 0.0
    for _ in range(_MAX_PASSES):
        got_pts, got_tris = _cluster_once(pts, tris, h)
        if len(got_tris) == 0:
            break                        # the grid swallowed the patch: keep the last that stood
        best_pts, best_tris, used = got_pts, got_tris, h
        if len(got_tris) <= max_triangles:
            break
        h *= 1.1 * math.sqrt(len(got_tris) / max_triangles)

    points_b, polys_b, n = _repack(best_pts, best_tris)
    if n == 0:
        return patch, came_with, 0.0
    out = dict(patch)
    out["points"] = points_b
    out["polys"] = polys_b
    out["face_count"] = n
    return out, came_with, used


def fit_to_budget(raw: list[dict], *,
                  max_triangles: int = DRAWN_MAX_TRIANGLES) -> tuple[list[dict], dict | None]:
    """Every patch drawn within one budget, and the record of what that cost.

    The budget is shared out in proportion to how much of the surface each patch is, with a floor,
    so a mesh whose faces are nearly all on one patch - which is exactly what a conjugate split
    gives, the coolant being one interface - does not spend its whole allowance on the block sides.

    Returns (patches, note). `note` is None when nothing was reduced; otherwise it is the block the
    payload carries so the viewer can say what it is showing:

        {"delivered_faces": 13504416, "drawn_triangles": 636713,
         "resolution_m": 0.00054, "patches": {"fluid/fluid_to_solid": {...}, ...}}

    `delivered_faces` counts the mesh's OWN faces - the quads and n-gons the mesher wrote - and
    `drawn_triangles` counts triangles, because that is what is on screen. The two are deliberately
    named apart: a first version of this summed the fan triangulation into `delivered_faces` and
    reported 27,020,802 for a mesh whose boundary is 13,504,416 faces, which is the mesh's own
    measurement of itself doubled.

    A reduced payload that does not say it is reduced is the thing this must never produce: the
    faces are the mesh's own measurement of itself, and a viewer that merges them silently invites
    a reader to conclude the mesh is coarser than it is.
    """
    if not raw:
        return raw, None
    counts = [max(int(p.get("face_count") or 0), 1) for p in raw]
    total = sum(counts)
    if total <= 0:
        return raw, None

    out: list[dict] = []
    per_patch: dict[str, dict] = {}
    delivered = 0
    drawn = 0
    coarsest = 0.0
    for patch, share in zip(raw, counts):
        allow = max(int(max_triangles * share / total), MIN_TRIANGLES_PER_PATCH)
        got, _came_with, used = reduce_patch(patch, max_triangles=allow)
        out.append(got)
        delivered += int(patch.get("face_count") or 0)
        drawn += int(got.get("face_count") or 0)
        if used > 0.0:
            coarsest = max(coarsest, used)
            per_patch[str(patch.get("name"))] = {
                "delivered_faces": int(patch.get("face_count") or 0),
                "drawn_triangles": int(got.get("face_count") or 0),
                "resolution_m": used,
            }

    if not per_patch:
        return out, None
    note = {"delivered_faces": delivered, "drawn_triangles": drawn,
            "resolution_m": coarsest, "patches": per_patch}
    logger.info("viewer: drawing %s of %s faces, merged at %.4f mm",
                f"{drawn:,}", f"{delivered:,}", coarsest * 1000.0)
    return out, note


__all__ = ["DRAWN_MAX_TRIANGLES", "MIN_TRIANGLES_PER_PATCH", "fit_to_budget", "reduce_patch"]
