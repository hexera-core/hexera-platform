# Responsibility: Write the delivered boundary and its per-face quality fields as a legacy VTK
# PolyData file, so the heatmap can be opened in ParaView with the numbers the viewer shows.
# Boundaries: packing only. It reads the viewer payload (render/viewer_pack + face_quality) and
# nothing else - no workspace, no engine. A field that does not align with its patch's polygons is
# left out for that patch, as the viewer leaves it out; it is never shipped against the wrong faces.
from __future__ import annotations

import base64

import numpy as np

#: The per-face fields the viewer colours by, in the order they are written.
FIELDS = ("non_ortho", "skewness", "aspect_ratio")

_TITLE_MAX = 255      # the legacy header's title line is limited to 256 characters


def _b64(text: str, dtype) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype=dtype)


def surface_to_vtk(surface: dict) -> bytes:
    """Legacy VTK PolyData, binary (big-endian, as the format requires): one polygon per drawn
    boundary face, in the order the viewer draws them. CELL_DATA carries `patch_id` (an index into
    the patch list named in the title line) and one float per quality field the payload holds -
    the viewer's own arrays, byte for byte."""
    patches = [p for p in (surface.get("patches") or [])
               if p.get("points_b64") and p.get("polys_b64")]
    if not patches:
        raise ValueError("the surface carries no polyMesh patches to export")
    qf = surface.get("quality_fields") or {}
    qpatches = qf.get("patches") or {}
    metrics = qf.get("metrics") or {}

    pts_parts: list[np.ndarray] = []
    poly_parts: list[np.ndarray] = []
    pid_parts: list[np.ndarray] = []
    field_parts: dict[str, list[np.ndarray]] = {f: [] for f in FIELDS}
    have: dict[str, bool] = {f: False for f in FIELDS}
    n_points = 0
    n_polys = 0
    for i, p in enumerate(patches):
        pts = _b64(p["points_b64"], np.float32).reshape(-1, 3)
        polys = _b64(p["polys_b64"], np.uint32).astype(np.int64)
        # records are [n, v0 .. vn-1]; walk them to find the size slots, then move every vertex
        # id up by the points already written
        starts = []
        k = 0
        while k < len(polys):
            starts.append(k)
            k += int(polys[k]) + 1
        if k != len(polys):
            raise ValueError(f"patch {p.get('name')!r}: polygon stream is inconsistent")
        is_size = np.zeros(len(polys), dtype=bool)
        is_size[starts] = True
        shifted = polys.copy()
        shifted[~is_size] += n_points
        n_here = len(starts)
        pts_parts.append(pts.astype(">f4"))
        poly_parts.append(shifted.astype(">i4"))
        pid_parts.append(np.full(n_here, i, dtype=">i4"))
        q = qpatches.get(p.get("name")) or {}
        for f in FIELDS:
            raw = q.get(f + "_b64")
            arr = _b64(raw, np.float32) if raw else None
            if arr is not None and len(arr) == n_here:
                field_parts[f].append(arr.astype(">f4"))
                have[f] = True
            else:
                field_parts[f].append(np.full(n_here, np.nan, dtype=">f4"))
        n_points += len(pts)
        n_polys += n_here

    title = "hexera mesh quality - patches: " + " ".join(
        f"{i}={p.get('name')}" for i, p in enumerate(patches))
    lims = [f"{f}<={metrics[f]['limit']:g}" for f in FIELDS
            if have[f] and isinstance((metrics.get(f) or {}).get("limit"), (int, float))]
    if lims:
        title += " - bars: " + " ".join(lims)
    title = title.encode("ascii", "replace")[:_TITLE_MAX]

    out = bytearray()
    out += b"# vtk DataFile Version 3.0\n" + title + b"\nBINARY\nDATASET POLYDATA\n"
    out += f"POINTS {n_points} float\n".encode()
    out += b"".join(a.tobytes() for a in pts_parts) + b"\n"
    total = sum(len(a) for a in poly_parts)
    out += f"POLYGONS {n_polys} {total}\n".encode()
    out += b"".join(a.tobytes() for a in poly_parts) + b"\n"
    out += f"CELL_DATA {n_polys}\n".encode()
    out += b"SCALARS patch_id int 1\nLOOKUP_TABLE default\n"
    out += b"".join(a.tobytes() for a in pid_parts) + b"\n"
    for f in FIELDS:
        if not have[f]:
            continue
        out += f"SCALARS {f} float 1\nLOOKUP_TABLE default\n".encode()
        out += b"".join(a.tobytes() for a in field_parts[f]) + b"\n"
    return bytes(out)
