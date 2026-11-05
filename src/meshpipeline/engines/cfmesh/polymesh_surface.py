# Responsibility: Extract the renderable boundary surface from a cfMesh polyMesh.
# Boundaries: geometry extraction for viewing and review.
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER_ASCII_RE = re.compile(rb"format\s+ascii\s*;")
_BOUNDARY_BLOCK_RE = re.compile(r"(\w[\w.:-]*)\s*\{([^}]*)\}", re.DOTALL)
# Face records come in two writer formats: compact `k(i0 i1 ...)` and, for
# large polygons (snappy polyhedra), EXPANDED - `k` on its own line, then `(`,
# one index per line, then `)`. \s* between count and paren covers both.
_FACE_LINE_RE = re.compile(rb"(\d+)\s*\(([^)]*)\)")


def _data_start(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(65536)
    if not _HEADER_ASCII_RE.search(head):
        raise ValueError(f"{path.name}: not an ascii foam file")
    offset = 0
    count = None
    for line in head.splitlines(keepends=True):
        stripped = line.strip()
        if count is None and stripped.isdigit():
            count = int(stripped)
        elif count is not None and stripped == b"(":
            offset += len(line)
            return offset, count
        offset += len(line)
    raise ValueError(f"{path.name}: could not locate list start in header")


def _offset_after_records(path: Path, start_offset: int, n_records: int) -> int:
    remaining = n_records
    pos = start_offset
    with path.open("rb") as fh:
        fh.seek(start_offset)
        while remaining > 0:
            chunk = fh.read(4 * 1024 * 1024)
            if not chunk:
                raise ValueError(f"{path.name}: EOF before record {n_records}")
            n = chunk.count(b")")
            if n < remaining:
                remaining -= n
                pos += len(chunk)
                continue
            # the target terminator is inside this chunk
            idx = -1
            for _ in range(remaining):
                idx = chunk.index(b")", idx + 1)
            return pos + idx + 1
    return pos


def read_boundary_patches(polymesh: Path) -> list[dict]:
    text = (polymesh / "boundary").read_text(errors="replace")
    # strip the FoamFile header block so it isn't parsed as a patch
    text = text.split("//", 1)[-1] if "FoamFile" in text.split("{", 1)[0] else text
    patches = []
    for name, body in _BOUNDARY_BLOCK_RE.findall(text):
        if name == "FoamFile":
            continue
        m_type = re.search(r"\btype\s+(\w+)\s*;", body)
        m_n = re.search(r"\bnFaces\s+(\d+)\s*;", body)
        m_s = re.search(r"\bstartFace\s+(\d+)\s*;", body)
        if not (m_type and m_n and m_s):
            continue
        patches.append({"name": name, "type": m_type.group(1),
                        "n_faces": int(m_n.group(1)), "start_face": int(m_s.group(1))})
    return patches


def read_points(polymesh: Path):
    import numpy as np
    path = polymesh / "points"
    offset, count = _data_start(path)
    with path.open("rb") as fh:
        fh.seek(offset)
        blob = fh.read()
    # records are '(x y z)' per line; strip parens → whitespace floats. The list
    # terminator ')' and trailing comment survive translate → cut at the last
    # record before fromstring by slicing count*3 values.
    blob = blob.translate(bytes.maketrans(b"()", b"  "))
    arr = np.fromstring(blob, dtype=np.float64, sep=" ", count=count * 3)  # noqa: NPY201
    return arr.reshape(-1, 3).astype(np.float32)


def read_boundary_faces(polymesh: Path, first_face: int, total_faces: int) -> list[list[int]]:
    path = polymesh / "faces"
    offset, count = _data_start(path)
    if first_face + total_faces > count:
        raise ValueError("boundary face range exceeds faces file count")
    tail_off = _offset_after_records(path, offset, first_face)
    with path.open("rb") as fh:
        fh.seek(tail_off)
        blob = fh.read()
    faces: list[list[int]] = []
    for m in _FACE_LINE_RE.finditer(blob):
        faces.append([int(t) for t in m.group(2).split()])
        if len(faces) >= total_faces:
            break
    if len(faces) < total_faces:
        raise ValueError(f"faces tail parse found {len(faces)} < {total_faces}")
    return faces


def _face_areas(points, patch_faces: list[list[int]]):
    import numpy as np
    sizes = np.fromiter((len(f) for f in patch_faces), dtype=np.int64,
                        count=len(patch_faces))
    areas = np.empty(len(patch_faces))
    for n in np.unique(sizes):
        sel = np.nonzero(sizes == n)[0]
        ids = np.array([patch_faces[i] for i in sel], dtype=np.int64)
        v = points[ids]                                        # (k, n, 3)
        cr = np.cross(v[:, 1:-1] - v[:, :1], v[:, 2:] - v[:, :1])
        areas[sel] = 0.5 * np.linalg.norm(cr.sum(axis=1), axis=1)
    return areas


def boundary_surface(polymesh: Path, skip_types: tuple = (),
                     skip_names: tuple = ()) -> tuple[list[dict], dict | None]:
    import numpy as np
    patches = [p for p in read_boundary_patches(polymesh)
               if p["type"] not in skip_types and p["name"] not in skip_names]
    if not patches:
        return [], None
    points = read_points(polymesh)
    first = min(p["start_face"] for p in patches)
    last = max(p["start_face"] + p["n_faces"] for p in patches)
    faces = read_boundary_faces(polymesh, first, last - first)

    out = []
    all_areas = []
    for p in patches:
        f0 = p["start_face"] - first
        patch_faces = [f for f in faces[f0:f0 + p["n_faces"]] if len(f) >= 3]
        if not patch_faces:
            continue
        all_areas.append(_face_areas(points, patch_faces))
        sizes = np.fromiter((len(f) for f in patch_faces), dtype=np.int64,
                            count=len(patch_faces))
        flat = np.fromiter((i for f in patch_faces for i in f), dtype=np.int64,
                           count=int(sizes.sum()))
        uniq, inv = np.unique(flat, return_inverse=True)
        # interleave [count, local ids...] without a python loop: counts live at
        # the running offsets, everything else is the reindexed connectivity
        polys = np.empty(len(flat) + len(sizes), dtype=np.uint32)
        starts = np.concatenate(([0], np.cumsum(sizes + 1)[:-1]))
        polys[starts] = sizes
        mask = np.ones(len(polys), dtype=bool)
        mask[starts] = False
        polys[mask] = inv
        out.append({
            "name": p["name"], "type": p["type"], "face_count": len(patch_faces),
            "points": points[uniq].astype(np.float32).tobytes(),
            "polys": polys.tobytes(),
        })

    stats = None
    if out and all_areas:
        areas = np.concatenate(all_areas)
        cell_sizes = np.sqrt(np.maximum(areas, 0.0))
        order = np.argsort(cell_sizes)
        cum = np.cumsum(areas[order])
        typ = float(cell_sizes[order][np.searchsorted(cum, cum[-1] * 0.5)]) \
            if cum[-1] > 0 else 0.0
        stats = {"p10": float(np.percentile(cell_sizes, 10)), "typ": typ}
    return out, stats
