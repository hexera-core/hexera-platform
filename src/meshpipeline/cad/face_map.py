"""NOTE-17: THE FACE MAP - every face of an upload as labelled triangles, named the way the engines name them
(a CAD file: Face_k, or Part_k_Face_j for several solids - cad/fea_measure, cad/fea_assembly; a triangle file:
Region_k, its smooth regions), with what each face is (its kind, radius, area, centre, normal and size), so a
click on the model can say which face it hit, in plain words, and a run can check the face is still there.

Pure: numpy only (OpenCASCADE inside from_cad). Stored and served by application/face_pick."""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np

VERSION = 1
#: A face map larger than this is not made (a scanned body of millions of triangles): no face pick on it.
MAX_TRIANGLES = 2_000_000
#: How far (a share of the part's diagonal) a click may sit from a face: the viewer draws a simplified skin.
PICK_TOL_SHARE = 0.02
#: A face still exists at run time when its area and centre agree within these shares.
SAME_AREA_SHARE = 0.02
SAME_CENTRE_SHARE = 0.01
#: A triangle-file region is flat when its triangles' normals stay within this angle of their mean.
FLAT_DEG = 2.0
MAX_LABEL = 80


@dataclass
class FaceMap:
    xyz: np.ndarray          # (n, 3) float32, metres in the unit `scale_to_m` (the skin's unit when it was made)
    tris: np.ndarray         # (m, 3) int32
    label: np.ndarray        # (m,) int32, index into names
    names: list[str]
    kind: list[str]          # plane / cylinder / cone / sphere / torus / bspline ... ; "" on a triangle file
    radius_mm: np.ndarray    # (f,) nan when the face does not bend
    area_m2: np.ndarray      # (f,)
    centroid_m: np.ndarray   # (f, 3)
    normal: np.ndarray       # (f, 3) unit, area weighted ([0,0,0] for a closed face)
    extent_mm: np.ndarray    # (f, 2) the face's two largest sizes along its own axes
    flat: np.ndarray         # (f,) bool
    scale_to_m: float
    basis: str               # "cad" | "triangles"

    @property
    def diag_m(self) -> float:
        if not len(self.xyz):
            return 0.0
        return float(np.linalg.norm(self.xyz.max(axis=0) - self.xyz.min(axis=0)))


def from_surface(surface, *, kinds: list[str] | None = None, radii_mm: list[float | None] | None = None,
                 scale_to_m: float, basis: str = "cad") -> FaceMap:
    """The face map of a labelled surface in millimetres (cad/fea_features.Surface: xyz, tris, label, names)."""
    xyz = np.asarray(surface.xyz, dtype=float).reshape(-1, 3) / 1000.0
    tris = np.asarray(surface.tris, dtype=np.int64).reshape(-1, 3)
    label = np.asarray(surface.label, dtype=np.int64).reshape(-1)
    names = [str(n) for n in surface.names]
    if len(tris) > MAX_TRIANGLES:
        raise ValueError(f"too many triangles for a face map ({len(tris)})")
    nf = len(names)
    a, b, c = (xyz[tris[:, k]] for k in range(3))
    cross = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cross, axis=1)
    cen = (a + b + c) / 3.0
    face_area = np.bincount(label, weights=area, minlength=nf)
    centroid = np.stack([np.bincount(label, weights=area * cen[:, k], minlength=nf) for k in range(3)], axis=1)
    centroid = centroid / np.maximum(face_area, 1e-300)[:, None]
    nsum = np.stack([np.bincount(label, weights=0.5 * cross[:, k], minlength=nf) for k in range(3)], axis=1)
    nlen = np.linalg.norm(nsum, axis=1)
    normal = np.where(nlen[:, None] > 1e-12 * np.maximum(face_area, 1e-300)[:, None], nsum / np.maximum(nlen, 1e-300)[:, None], 0.0)
    unit = np.where(area[:, None] > 0, cross / np.maximum(2 * area, 1e-300)[:, None], 0.0)
    extent = np.zeros((nf, 2))
    flat = np.zeros(nf, dtype=bool)
    order = np.argsort(label, kind="stable")
    bounds = np.searchsorted(label[order], np.arange(nf + 1))
    cos_flat = math.cos(math.radians(FLAT_DEG))
    for k in range(nf):
        sel = order[bounds[k]:bounds[k + 1]]
        if not len(sel):
            continue
        pts = xyz[np.unique(tris[sel])]
        if len(pts) >= 3:
            centred = pts - pts.mean(axis=0)
            _u, _s, vt = np.linalg.svd(centred, full_matrices=False)
            proj = centred @ vt[:2].T
            extent[k] = (proj.max(axis=0) - proj.min(axis=0)) * 1000.0
        if nlen[k] > 0:
            flat[k] = bool(np.all(unit[sel] @ normal[k] >= cos_flat))
    kind = [str(x) for x in (kinds or [""] * nf)]
    if kinds is not None:
        flat = np.array([kd == "plane" for kd in kind], dtype=bool)
    radius = np.array([float(r) if r else np.nan for r in (radii_mm or [None] * nf)], dtype=float)
    return FaceMap(xyz=xyz.astype(np.float32), tris=tris.astype(np.int32), label=label.astype(np.int32), names=names,
                   kind=kind, radius_mm=radius, area_m2=face_area, centroid_m=centroid, normal=normal,
                   extent_mm=extent, flat=flat, scale_to_m=float(scale_to_m), basis=basis)


def from_cad(shape, *, mm_per_unit: float, scale_to_m: float) -> FaceMap:
    """A CAD shape's face map: its faces as cad/fea_measure tessellates them, Part_k_Face_j when it has several
    solids (cad/fea_assembly), each bent face's radius (cad/fea_measure.face_radius)."""
    from meshpipeline.cad.fea_assembly import global_face_names, solids
    from meshpipeline.cad.fea_measure import _brep_surface

    surface, faces, kinds = _brep_surface(shape, mm_per_unit)
    if len(solids(shape)) > 1:
        names = global_face_names(shape)
        if len(names) == len(surface.names):
            surface.names = list(names)
    radii = [_radius(f, kd, mm_per_unit) for f, kd in zip(faces, kinds, strict=True)]
    return from_surface(surface, kinds=list(kinds), radii_mm=radii, scale_to_m=scale_to_m, basis="cad")


def _radius(face, kind: str, mm: float) -> float | None:
    """A round face's own radius in mm, read off its surface (a cylinder, a sphere; a torus's tube), else None - the
    label then says the face's size. Read exactly and cheaply: no curvature sampling."""
    if kind not in ("cylinder", "sphere", "torus"):
        return None
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface

        a = BRepAdaptor_Surface(face, True)
        if kind == "cylinder":
            return float(a.Cylinder().Radius()) * mm
        if kind == "sphere":
            return float(a.Sphere().Radius()) * mm
        return float(a.Torus().MinorRadius()) * mm
    except Exception:  # noqa: BLE001 - no radius: the label says the size
        return None


def from_triangles(tris_mm, *, scale_to_m: float) -> FaceMap:
    """A triangle file's face map: its smooth regions, Region_k (cad/fea_measure.triangle_regions)."""
    from meshpipeline.cad.fea_measure import triangle_regions

    tris = np.asarray(tris_mm, dtype=float).reshape(-1, 3, 3)
    if len(tris) > MAX_TRIANGLES:
        raise ValueError(f"too many triangles for a face map ({len(tris)})")
    surface, _pairs, _vol, _depth = triangle_regions(tris)
    return from_surface(surface, scale_to_m=scale_to_m, basis="triangles")


# ------------------------------------------------------------------------------------- storage ----
def to_bytes(fm: FaceMap) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, version=np.int32(VERSION), xyz=fm.xyz, tris=fm.tris, label=fm.label,
                        names=np.array(fm.names, dtype=str), kind=np.array(fm.kind, dtype=str),
                        radius_mm=fm.radius_mm, area_m2=fm.area_m2, centroid_m=fm.centroid_m, normal=fm.normal,
                        extent_mm=fm.extent_mm, flat=fm.flat, scale_to_m=np.float64(fm.scale_to_m),
                        basis=np.array(fm.basis))
    return buf.getvalue()


def from_bytes(data: bytes) -> FaceMap:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return FaceMap(xyz=z["xyz"], tris=z["tris"], label=z["label"], names=[str(n) for n in z["names"]],
                       kind=[str(k) for k in z["kind"]], radius_mm=z["radius_mm"], area_m2=z["area_m2"],
                       centroid_m=z["centroid_m"], normal=z["normal"], extent_mm=z["extent_mm"], flat=z["flat"],
                       scale_to_m=float(z["scale_to_m"]), basis=str(z["basis"]))


# ------------------------------------------------------------------------------------- picking ----
def _closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The closest point to p on each triangle (a, b, c), vectorised (Ericson, Real-Time Collision Detection 5.1.5)."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    denom = va + vb + vc
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.where(denom != 0, vb / denom, 0.0)
        w = np.where(denom != 0, vc / denom, 0.0)
        out = a + ab * v[:, None] + ac * w[:, None]
        # the seven regions, last wins: edges and corners override the inside
        e_bc = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0)
        t = np.where(e_bc, (d4 - d3) / ((d4 - d3) + (d5 - d6)), 0.0)
        out = np.where(e_bc[:, None], b + (c - b) * t[:, None], out)
        e_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        t = np.where(e_ac, d2 / (d2 - d6), 0.0)
        out = np.where(e_ac[:, None], a + ac * t[:, None], out)
        e_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        t = np.where(e_ab, d1 / (d1 - d3), 0.0)
        out = np.where(e_ab[:, None], a + ab * t[:, None], out)
    out = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, out)
    out = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, out)
    out = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, out)
    return out


def face_at(fm: FaceMap, point_m, normal=None, *, scale: float = 1.0) -> int | None:
    """The index of the face a click at `point_m` (the viewer's ray hit, on the skin) lies on, or None when no face
    is within PICK_TOL_SHARE of the part's size. `scale`: the skin's unit over the map's (a unit confirmed since).
    `normal`: the skin's normal there; of two faces equally near (an edge), the one facing the same way wins."""
    if not len(fm.tris):
        return None
    p = np.asarray(point_m, dtype=float).reshape(3) / float(scale)
    if not np.all(np.isfinite(p)):
        return None
    xyz = fm.xyz.astype(float)
    tol = PICK_TOL_SHARE * max(fm.diag_m, 1e-12)
    a, b, c = (xyz[fm.tris[:, k]] for k in range(3))
    lo = np.minimum(np.minimum(a, b), c) - tol
    hi = np.maximum(np.maximum(a, b), c) + tol
    near = np.nonzero(np.all((lo <= p) & (p <= hi), axis=1))[0]
    if not len(near):
        return None
    q = _closest_on_triangles(p, a[near], b[near], c[near])
    dist = np.linalg.norm(q - p, axis=1)
    score = dist.copy()
    if normal is not None:
        n = np.asarray(normal, dtype=float).reshape(3)
        nn = float(np.linalg.norm(n))
        if nn > 0 and math.isfinite(nn):
            cross = np.cross(b[near] - a[near], c[near] - a[near])
            unit = cross / np.maximum(np.linalg.norm(cross, axis=1), 1e-300)[:, None]
            # a face turned away from the viewer's normal costs a little distance, never a near face's win
            score = dist + 0.1 * tol * (1.0 - np.abs(unit @ (n / nn)))
    best = int(np.argmin(score))
    if dist[best] > tol:
        return None
    return int(fm.label[near[best]])


def _n(v: float) -> str:
    v = float(v)
    if v >= 100:
        return f"{v:.0f}"
    return f"{v:.3g}"


def label(fm: FaceMap, k: int) -> str:
    """The chip's words for face k: its name as a person reads it and what it is - "Face 12 · round Ø20 mm",
    "Face 7 · flat 40 × 25 mm", "Part 2 face 5 · curved R3 mm", "Region 4 · flat 120 × 80 mm"."""
    name = fm.names[k]
    head, sep, tail = name.rpartition("_Face_")
    if sep:
        who = f"{head.replace('_', ' ')} face {tail}"
    else:
        who = name.replace("_", " ")
    kind = fm.kind[k] if k < len(fm.kind) else ""
    r = float(fm.radius_mm[k])
    e1, e2 = (float(x) for x in fm.extent_mm[k])
    size = f"{_n(e1)} × {_n(e2)} mm"
    if kind == "plane" or (not kind and bool(fm.flat[k])):
        what = f"flat {size}"
    elif kind == "cylinder" and math.isfinite(r):
        what = f"round Ø{_n(2 * r)} mm"
    elif kind == "sphere" and math.isfinite(r):
        what = f"ball Ø{_n(2 * r)} mm"
    elif kind == "cone":
        what = f"tapered {size}"
    elif kind == "torus" and math.isfinite(r):
        what = f"rounded edge R{_n(r)} mm"
    elif math.isfinite(r):
        what = f"curved R{_n(r)} mm"
    else:
        what = f"curved {size}"
    return f"{who} · {what}"[:MAX_LABEL]


def signature(fm: FaceMap, k: int, *, scale: float = 1.0) -> dict:
    """What a run re-checks to know face k is still the face the user picked: its area, centre and normal, in the
    skin's unit (`scale` as in face_at)."""
    return {"area_m2": float(fm.area_m2[k]) * scale * scale,
            "centroid_m": [float(v) * scale for v in fm.centroid_m[k]],
            "normal": [float(v) for v in fm.normal[k]]}


def same_face(fm: FaceMap, name: str, sig: dict, *, scale: float = 1.0) -> bool:
    """True when face `name` exists in this map with the area and centre `sig` recorded (NOTE-17: a picked face
    that changed is told, never guessed)."""
    try:
        k = fm.names.index(str(name))
        area = float(sig["area_m2"])
        cen = np.asarray(sig["centroid_m"], dtype=float).reshape(3)
    except (ValueError, KeyError, TypeError):
        return False
    mine = signature(fm, k, scale=scale)
    if abs(mine["area_m2"] - area) > SAME_AREA_SHARE * max(area, 1e-300):
        return False
    return float(np.linalg.norm(np.asarray(mine["centroid_m"]) - cen)) <= SAME_CENTRE_SHARE * fm.diag_m * scale


def highlight(fm: FaceMap, k: int, *, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Face k's own triangles for the viewer to tint: (points float32 (n, 3) in the skin's unit, tris uint32)."""
    sel = fm.tris[fm.label == k]
    used, local = np.unique(sel.ravel(), return_inverse=True)
    pts = fm.xyz[used].astype(np.float64) * scale
    return pts.astype(np.float32), local.reshape(-1, 3).astype(np.uint32)
