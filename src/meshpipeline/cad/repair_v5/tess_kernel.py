# Responsibility: TESSELLATED volume measurement for shapes OCCT's Booleans cannot be trusted on (v5 step 8, owner
#                 decision A, 2026-11-25): a self-intersecting solid. Triangles, their signed volume, generalised
#                 winding numbers, and the interior-point sample both the loss guard and the fragment repair read.
# Owns: `triangles`, `mesh_volume`, `winding_numbers`, `halton_points`, `kept_by_sampling`.
# Boundaries: `triangles` runs INSIDE a worker (OCP imported lazily); the rest is pure arithmetic (numpy when it is
#             importable, plain Python otherwise), so it is unit-tested without a kernel. It measures; the 10% rule
#             stays in `repair_v4.loss`.
"""Why tessellate: `BRepAlgoAPI_Common` on a SELF-INTERFERING argument returns ~0 (b25999e0f8be: the loss guard read
"part kept 0.0" for a 1e-5 volume change and refused four repairs). A triangulation has no such failure mode: the
GENERALISED WINDING NUMBER of a point against a closed triangle mesh is 1 inside, 0 outside, and 2 where two lobes of
a self-overlapping solid cover it - so `|w| >= 0.5` is "inside" for a self-intersecting solid too, where a ray parity
test would call the doubly covered region outside.

KEPT, measured by sampling: points of a deterministic low-discrepancy sequence (Halton) in the input body's box; the
ones inside the input body are the body; the fraction of THOSE inside the output is what the output kept of it. The
same counts give Monte-Carlo volumes of the input body and of the kept part ("compare volumes").
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

#: sample size: points in the box, and the inside points wanted (the sample grows until it has them or hits the cap)
SAMPLE_START = 2000
SAMPLE_CAP = 16000
INSIDE_WANTED = 400
#: triangulation size cap per mesh; past it the deflection is coarsened (x4) and the mesh redone
MAX_TRIANGLES = 30000
INSIDE_W = 0.5


def triangles(shape: Any, *, rel_deflection: float = 2e-3, max_triangles: int = MAX_TRIANGLES) -> list[Tri]:
    """Every face's triangulation of `shape`, outward-oriented (a REVERSED face's triangles flipped), in world
    coordinates. Meshes a COPY (the caller's shape keeps its own triangulation state). Coarsens until under the cap."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return []
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = math.dist((x0, y0, z0), (x1, y1, z1)) or 1.0
    deflection = rel_deflection * diag
    out: list[Tri] = []
    for _attempt in range(4):
        copy = BRepBuilderAPI_Copy(shape, True, False).Shape()
        BRepMesh_IncrementalMesh(copy, deflection, False, 0.5, True)
        out = []
        exp = TopExp_Explorer(copy, TopAbs_FACE)
        while exp.More():
            face = TopoDS.Face_s(exp.Current())
            exp.Next()
            loc = TopLoc_Location()
            tri = BRep_Tool.Triangulation_s(face, loc)
            if tri is None:
                continue
            trsf = loc.Transformation()
            nodes = []
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                nodes.append((p.X(), p.Y(), p.Z()))
            flip = face.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                out.append((nodes[a - 1], nodes[c - 1], nodes[b - 1]) if flip else
                           (nodes[a - 1], nodes[b - 1], nodes[c - 1]))
        if len(out) <= max_triangles:
            return out
        deflection *= 4.0
    return out


def mesh_volume(tris: Sequence[Tri]) -> float:
    """Signed volume enclosed by a closed, outward triangle mesh (positive for an outward one)."""
    total = 0.0
    for a, b, c in tris:
        total += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                  + a[2] * (b[0] * c[1] - b[1] * c[0]))
    return total / 6.0


def box_of(tris: Sequence[Tri]) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    if not tris:
        return None
    xs = [p[0] for t in tris for p in t]
    ys = [p[1] for t in tris for p in t]
    zs = [p[2] for t in tris for p in t]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _radical_inverse(i: int, base: int) -> float:
    f, r = 1.0, 0.0
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def halton_points(lo: Sequence[float], hi: Sequence[float], start: int, count: int) -> list[tuple[float, float, float]]:
    """Points `start .. start+count-1` of the 3-D Halton sequence (bases 2, 3, 5) scaled into the box: deterministic,
    evenly spread, and extendable (the next batch continues the same sequence)."""
    out = []
    for i in range(start + 1, start + count + 1):
        u = (_radical_inverse(i, 2), _radical_inverse(i, 3), _radical_inverse(i, 5))
        out.append(tuple(lo[k] + (hi[k] - lo[k]) * u[k] for k in range(3)))
    return out  # type: ignore[return-value]


def winding_numbers(points: Sequence[Sequence[float]], tris: Sequence[Tri]) -> list[float]:
    """The generalised winding number of each point against the triangle mesh (Van Oosterom-Strackee solid angles):
    ~1 inside an outward closed mesh, ~0 outside, ~k where k lobes overlap."""
    if not points or not tris:
        return [0.0] * len(points)
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - the worker image carries numpy; this keeps the math testable anywhere
        return [_winding_py(p, tris) for p in points]
    t = np.asarray(tris, dtype=float)                       # (T, 3, 3)
    out = np.zeros(len(points))
    pts = np.asarray(points, dtype=float)
    chunk = max(1, int(500_000 // max(len(t), 1)))         # bounds the (P, T, 3, 3) work array to ~36 MB
    for s in range(0, len(pts), chunk):
        p = pts[s:s + chunk][:, None, None, :]               # (P, 1, 1, 3)
        d = t[None, :, :, :] - p                             # (P, T, 3, 3)
        a, b, c = d[:, :, 0, :], d[:, :, 1, :], d[:, :, 2, :]
        la, lb, lc = (np.linalg.norm(v, axis=-1) for v in (a, b, c))
        num = np.einsum("ptk,ptk->pt", a, np.cross(b, c))
        den = (la * lb * lc + np.einsum("ptk,ptk->pt", a, b) * lc + np.einsum("ptk,ptk->pt", b, c) * la
               + np.einsum("ptk,ptk->pt", c, a) * lb)
        out[s:s + chunk] = (2.0 * np.arctan2(num, den)).sum(axis=1) / (4.0 * math.pi)
    return [float(v) for v in out]


def _winding_py(p: Sequence[float], tris: Sequence[Tri]) -> float:
    total = 0.0
    for v0, v1, v2 in tris:
        a = (v0[0] - p[0], v0[1] - p[1], v0[2] - p[2])
        b = (v1[0] - p[0], v1[1] - p[1], v1[2] - p[2])
        c = (v2[0] - p[0], v2[1] - p[1], v2[2] - p[2])
        la, lb, lc = math.hypot(*a), math.hypot(*b), math.hypot(*c)
        num = (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
               + a[2] * (b[0] * c[1] - b[1] * c[0]))
        den = (la * lb * lc + (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) * lc
               + (b[0] * c[0] + b[1] * c[1] + b[2] * c[2]) * la + (c[0] * a[0] + c[1] * a[1] + c[2] * a[2]) * lb)
        total += 2.0 * math.atan2(num, den)
    return total / (4.0 * math.pi)


def inside(w: float) -> bool:
    return abs(w) >= INSIDE_W


def kept_by_sampling(body: Sequence[Tri], output: Sequence[Tri], *, wanted: int = INSIDE_WANTED,
                     start: int = SAMPLE_START, cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """What `output` kept of `body`, both as triangle meshes: `{kept, inside, sampled, volume_mc_mm3,
    kept_volume_mc_mm3, mesh_volume_mm3, output_mesh_volume_mm3}`. `kept` is None when no sample point fell inside
    the body (nothing measured - the caller claims nothing)."""
    return kept_from_sample(inside_sample(body, wanted=wanted, start=start, cap=cap), output)


def inside_sample(body: Sequence[Tri], *, wanted: int = INSIDE_WANTED, start: int = SAMPLE_START,
                  cap: int = SAMPLE_CAP) -> dict[str, Any]:
    """The INPUT half of `kept_by_sampling`: the body's interior sample points and what they say about its volume
    (`{inner, sampled, box_volume, mesh_volume_mm3}`). It depends on the input body only, so the loss guard computes
    it ONCE per file and body and reuses it for every candidate (CRS v5 r2h); plain JSON-able data."""
    box = box_of(body)
    out: dict[str, Any] = {"mesh_volume_mm3": mesh_volume(body), "inner": [], "sampled": 0, "box_volume": 0.0}
    if box is None:
        return out
    lo, hi = box
    out["box_volume"] = max((hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]), 0.0)
    inner: list[tuple[float, float, float]] = []
    sampled = 0
    batch = start
    while sampled < cap and len(inner) < wanted:
        pts = halton_points(lo, hi, sampled, min(batch, cap - sampled))
        sampled += len(pts)
        inner.extend(p for p, w in zip(pts, winding_numbers(pts, body), strict=True) if inside(w))
        batch *= 2
    out["inner"], out["sampled"] = inner, sampled
    return out


def kept_from_sample(sample: dict[str, Any], output: Sequence[Tri]) -> dict[str, Any]:
    """The OUTPUT half of `kept_by_sampling`: what `output` kept of the body `sample` was drawn from."""
    inner, sampled = sample.get("inner") or [], int(sample.get("sampled") or 0)
    base = {"mesh_volume_mm3": sample.get("mesh_volume_mm3", 0.0), "output_mesh_volume_mm3": mesh_volume(output)}
    if not inner:
        return {**base, "kept": None, "inside": 0, "sampled": sampled}
    hits = sum(1 for w in winding_numbers(inner, output) if inside(w))
    vol = float(sample.get("box_volume") or 0.0) * len(inner) / sampled
    return {**base, "kept": hits / len(inner), "inside": len(inner), "sampled": sampled, "volume_mc_mm3": vol,
            "kept_volume_mc_mm3": vol * hits / len(inner)}


__all__ = ["box_of", "halton_points", "inside", "inside_sample", "kept_by_sampling", "kept_from_sample",
           "mesh_volume", "triangles", "winding_numbers"]
