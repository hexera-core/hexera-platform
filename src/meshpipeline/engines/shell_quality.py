# Responsibility: Measure a shell mesh's element quality the way shell solvers and pre-processors judge it -
# validity (a positive Jacobian at every corner), aspect ratio, warp, skew, taper, interior angles, the
# Jacobian ratio, and the share of triangles - and hold each to its limit (SHMESH-8): validity is the only
# blocker; every other figure past its limit is a note.
# Boundaries: pure numpy over engines/shell_mesh.ShellMesh. It decides no delivery. The limits are the
# design note's (section 3): MSC Nastran's GEOMCHECK defaults as we read them, and common Femap targets.
# Collaborates with: engines/gmsh/shell_adapter.py (measures every mesh into shell_quality.json),
# engines/shell_writers.py (states the figures in each deck), docs/design/shell/README.md.
"""HOW EACH FIGURE IS MEASURED (quads; triangles where it applies).

* corner Jacobian: at each corner, the cross product of its two edges, along the element's mean normal,
  over the two edge lengths (1 for a right angle, 0 for a straight one, negative when folded). VALID: every
  corner positive. The Jacobian RATIO is the smallest corner value over the largest (1 for a parallelogram).
* aspect ratio: longest edge over shortest.
* warp (degrees): the larger of the angles between the normals of the two triangles each diagonal cuts the
  quad into (0 for a flat quad).
* skew (degrees): the smaller angle between the two lines joining opposite edge midpoints (90 is ideal);
  for a triangle, the smallest angle between a median and the line joining the other two edge midpoints.
* taper: the largest share by which a corner triangle (the corner and its two neighbours) departs from
  their mean, |Ti - mean| / mean (0 for a parallelogram).
* interior angles (degrees): the smallest and the largest.
"""
from __future__ import annotations

import math

import numpy as np

from meshpipeline.engines.shell_mesh import ShellMesh

#: (figure, op, limit, source). Validity is checked apart: it blocks.
LIMITS = (
    ("aspect", "<=", 5.0, "Femap / industry target"),
    ("warp_deg", "<=", 10.0, "industry target (5 is good, 10 the note)"),
    ("skew_deg", ">=", 45.0, "Femap target for quads (MSC GEOMCHECK Q4_SKEW warns under 30)"),
    ("taper", "<=", 0.5, "MSC GEOMCHECK Q4_TAPER default"),
    ("min_angle_quad", ">=", 45.0, "industry target (GEOMCHECK Q4_IAMIN 30)"),
    ("max_angle_quad", "<=", 135.0, "industry target (GEOMCHECK Q4_IAMAX 150)"),
    ("min_angle_tria", ">=", 20.0, "industry target"),
    ("max_angle_tria", "<=", 120.0, "industry target (GEOMCHECK T3_IAMAX 160)"),
    ("jacobian_ratio", ">=", 0.6, "industry target"),
)
#: the triangle share a quad-dominant mesh should keep under (a note past it)
TRIA_SHARE = 0.05
#: NASTRAN'S OWN LIMITS (SHMESH-8): the MSC Nastran GEOMCHECK defaults, past which the solver itself warns (MSC
#: Nastran Quick Reference Guide, GEOMCHECK). Every delivered element is held inside these; the tighter Femap /
#: industry targets above are notes. GEOMCHECK's Q4_WARP (0.05) is a height over the mean diagonal, about 5.7
#: degrees as an angle; we hold the angle at 5. (shape, figure, op, limit, GEOMCHECK name)
NASTRAN_LIMITS = (
    ("quad", "skew_deg", ">=", 30.0, "Q4_SKEW"),
    ("quad", "taper", "<=", 0.5, "Q4_TAPER"),
    ("quad", "warp_deg", "<=", 5.0, "Q4_WARP"),
    ("quad", "min_angle_quad", ">=", 30.0, "Q4_IAMIN"),
    ("quad", "max_angle_quad", "<=", 150.0, "Q4_IAMAX"),
    ("tria", "skew_deg", ">=", 10.0, "T3_SKEW"),
    ("tria", "max_angle_tria", "<=", 160.0, "T3_IAMAX"),
)


def nastran_past(mesh: ShellMesh) -> np.ndarray:
    """(elements,) bool: the element is past one of Nastran's own GEOMCHECK limits."""
    q = quad_measures(mesh.xyz, mesh.quads) if len(mesh.quads) else {}
    t = tria_measures(mesh.xyz, mesh.trias) if len(mesh.trias) else {}
    out = np.zeros(mesh.n_elements, dtype=bool)
    for shape, key, op, limit, _card in NASTRAN_LIMITS:
        d, off = (q, 0) if shape == "quad" else (t, len(mesh.quads))
        if key in d:
            v = d[key]
            out[off:off + len(v)] |= (v < limit) if op == ">=" else (v > limit)
    return out


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-300)


def _angles(p: np.ndarray) -> np.ndarray:
    """Interior angles (degrees) of polygons p (m, k, 3)."""
    k = p.shape[1]
    prev = p[:, [(i - 1) % k for i in range(k)]] - p
    nxt = p[:, [(i + 1) % k for i in range(k)]] - p
    c = np.einsum("mki,mki->mk", _unit(prev), _unit(nxt))
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


def quad_measures(xyz: np.ndarray, quads: np.ndarray) -> dict[str, np.ndarray]:
    p = xyz[quads]                                         # (m, 4, 3)
    n = _unit(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]))
    e_next = p[:, [1, 2, 3, 0]] - p
    e_prev = p[:, [3, 0, 1, 2]] - p
    cross = np.cross(e_next, e_prev)
    jac = np.einsum("mki,mi->mk", cross, n) / np.maximum(
        np.linalg.norm(e_next, axis=2) * np.linalg.norm(e_prev, axis=2), 1e-300)
    lengths = np.linalg.norm(e_next, axis=2)
    n1 = _unit(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]))
    n2 = _unit(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]))
    n3 = _unit(np.cross(p[:, 1] - p[:, 0], p[:, 3] - p[:, 0]))
    n4 = _unit(np.cross(p[:, 2] - p[:, 1], p[:, 3] - p[:, 1]))
    warp = np.maximum(np.degrees(np.arccos(np.clip(np.einsum("mi,mi->m", n1, n2), -1, 1))),
                      np.degrees(np.arccos(np.clip(np.einsum("mi,mi->m", n3, n4), -1, 1))))
    mids = 0.5 * (p + p[:, [1, 2, 3, 0]])
    a = _unit(mids[:, 2] - mids[:, 0])
    b = _unit(mids[:, 3] - mids[:, 1])
    ang = np.degrees(np.arccos(np.clip(np.abs(np.einsum("mi,mi->m", a, b)), 0, 1)))
    corner = 0.5 * np.linalg.norm(cross, axis=2)
    mean = corner.mean(axis=1)
    taper = np.max(np.abs(corner - mean[:, None]), axis=1) / np.maximum(mean, 1e-300)
    angles = _angles(p)
    jmax = jac.max(axis=1)
    return {"min_jacobian": jac.min(axis=1), "jacobian_ratio": jac.min(axis=1) / np.where(jmax > 0, jmax, 1.0),
            "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300), "warp_deg": warp,
            "skew_deg": ang, "taper": taper, "min_angle_quad": angles.min(axis=1),
            "max_angle_quad": angles.max(axis=1)}


def tria_measures(xyz: np.ndarray, trias: np.ndarray) -> dict[str, np.ndarray]:
    p = xyz[trias]
    lengths = np.linalg.norm(p[:, [1, 2, 0]] - p, axis=2)
    area2 = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
    angles = _angles(p)
    # skew: the smallest angle between a median and the mid-line opposite it, over the three corners
    skews = []
    for i in range(3):
        j, k = (i + 1) % 3, (i + 2) % 3
        med = _unit(0.5 * (p[:, j] + p[:, k]) - p[:, i])
        mid = _unit(0.5 * (p[:, i] + p[:, k]) - 0.5 * (p[:, i] + p[:, j]))
        skews.append(np.degrees(np.arccos(np.clip(np.abs(np.einsum("mi,mi->m", med, mid)), 0, 1))))
    return {"min_jacobian": area2 / np.maximum(lengths.max(axis=1) ** 2, 1e-300),
            "aspect": lengths.max(axis=1) / np.maximum(lengths.min(axis=1), 1e-300),
            "skew_deg": np.min(np.stack(skews), axis=0), "min_angle_tria": angles.min(axis=1),
            "max_angle_tria": angles.max(axis=1)}


def _stat(v: np.ndarray, worst: str) -> dict:
    if not len(v):
        return {}
    return {"worst": float(f"{float(v.min() if worst == 'min' else v.max()):.6g}"),
            "median": float(f"{float(np.median(v)):.6g}")}


def measure(mesh: ShellMesh) -> dict:
    """The figures, their worst and median, how many elements are past each limit, validity and the notes."""
    q = quad_measures(mesh.xyz, mesh.quads) if len(mesh.quads) else {}
    t = tria_measures(mesh.xyz, mesh.trias) if len(mesh.trias) else {}
    n = mesh.n_elements
    bad = int((q.get("min_jacobian", np.ones(0)) <= 0).sum() + (t.get("min_jacobian", np.ones(0)) <= 0).sum())
    out: dict = {"elements": n, "quads": int(len(mesh.quads)), "trias": int(len(mesh.trias)),
                 "tria_share": float(f"{len(mesh.trias) / n:.4g}") if n else 0.0,
                 "invalid": bad, "valid": bad == 0 and n > 0, "figures": {}, "past_limits": {}, "notes": []}
    for key, op, limit, source in LIMITS:
        vals = np.concatenate([d[key] for d in (q, t) if key in d]) if any(key in d for d in (q, t)) else np.zeros(0)
        if not len(vals):
            continue
        worst = "min" if op == ">=" else "max"
        out["figures"][key] = _stat(vals, worst)
        past = int((vals < limit).sum() if op == ">=" else (vals > limit).sum())
        out["past_limits"][key] = past
        if past:
            out["notes"].append(f"{past} of {len(vals)} elements are past the {key} limit ({op} {limit}; {source}).")
    out["nastran_past"] = {}
    for shape, key, op, limit, card in NASTRAN_LIMITS:
        d = q if shape == "quad" else t
        if key in d:
            past = int((d[key] < limit).sum() if op == ">=" else (d[key] > limit).sum())
            if past:
                out["nastran_past"][card] = past
    out["within_nastran"] = bool(out["valid"]) and not out["nastran_past"]
    if out["nastran_past"]:
        out["notes"].insert(0, "Past Nastran's own GEOMCHECK limits: " + ", ".join(
            f"{v} elements by {k}" for k, v in out["nastran_past"].items()) + ".")
    if n and len(mesh.trias) / n > TRIA_SHARE:
        out["notes"].append(f"{100 * len(mesh.trias) / n:.1f}% of elements are triangles (target under "
                            f"{100 * TRIA_SHARE:.0f}%).")
    if bad:
        out["notes"].insert(0, f"{bad} elements are folded or flat (a corner Jacobian not positive): the mesh is "
                               "not valid.")
    return out


def element_angle_deg(radius: float, size: float) -> float:
    """The angle one element of `size` turns through on a curve of `radius` (degrees): the curvature check."""
    return math.degrees(size / radius) if radius > 0 else 0.0


__all__ = ["LIMITS", "TRIA_SHARE", "measure", "quad_measures", "tria_measures"]
