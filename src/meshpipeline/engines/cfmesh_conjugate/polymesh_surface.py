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


#: The patch type OpenFOAM gives the faces that exist only because the mesh was cut up. It is the
#: ONLY thing that tells a processor patch from a real one: the names are `procBoundaryM_toN`
#: today and nothing may depend on that spelling.
PROCESSOR_PATCH_TYPE = "processor"


class DecomposedBoundaryError(ValueError):
    """The per-rank boundary files do not agree on what the mesh's patches are."""


def processor_dirs(case: Path) -> list[Path]:
    """`case/processor0..N-1`, in RANK order rather than the lexical order glob gives.

    `sorted(glob("processor*"))` puts processor10 before processor2, which silently reorders
    every rank-major traversal built on it once a run uses ten or more ranks. The engine's
    ceiling is eight today (determinism.CONJUGATE_RANKS_MAX), so this is a trap that is not yet
    reachable and would be invisible when it became so.
    """
    out = []
    for d in Path(case).glob("processor*"):
        if d.is_dir() and d.name[9:].isdigit():
            out.append((int(d.name[9:]), d))
    return [d for _n, d in sorted(out)]


def decomposed_region_boundary(case: Path, region: str = "") -> list[dict]:
    """The REAL patches of a decomposed mesh, summed over its ranks, as read_boundary_patches
    would have returned them from the reconstructed mesh.

    Same shape as `read_boundary_patches` - {name, type, n_faces, start_face} - so it is a
    drop-in for every reader that only wants the patch table. `start_face` is renumbered as a
    running cursor over the summed counts, because the per-rank offsets mean nothing once the
    patches are added together, and NO reader of this function may use it to seek into a file.

    THREE THINGS THIS DOES THAT THE HAND-RUN SCRIPT IT REPLACES DOES NOT.

    The patch list is the UNION over ranks, not processor0's. `par_boundary_index.py` takes the
    order from processor0 alone, and a patch absent there is dropped with no error at all. A rank
    is SUPPOSED to list every global patch at `nFaces 0` - measured on a real 4-rank case - but
    "supposed to" is what a union costs nothing to stop depending on.

    A disagreement RAISES. Two ranks giving one name two types means the files are not one mesh,
    and summing them would produce a confident wrong answer.

    Processor patches are dropped, and that is the whole reason the counts come out right: each
    face on a cut was one internal face of the whole mesh and is a BOUNDARY face on both of the
    ranks that share it. Summing the real patches alone reproduces the reconstructed mesh exactly
    - measured on a real 4-rank case: fluid {fluid_to_solid 93600, ymax 128, ymin 128}, solid
    {solid_to_fluid 93600, xmax 67604, xmin 67604, ymax 404, ymin 404, zmax 13000, zmin 3900},
    with 840 and 2,484 processor faces excluded.
    """
    ranks = processor_dirs(Path(case))
    if not ranks:
        raise DecomposedBoundaryError(f"no processor* directories under {case}")
    order: list[str] = []
    types: dict[str, str] = {}
    totals: dict[str, int] = {}
    dropped = 0
    seen_a_boundary = False
    for d in ranks:
        polymesh = (d / "constant" / region / "polyMesh") if region else (d / "constant" / "polyMesh")
        if not (polymesh / "boundary").is_file():
            continue
        seen_a_boundary = True
        for p in read_boundary_patches(polymesh):
            if p["type"] == PROCESSOR_PATCH_TYPE:
                dropped += int(p["n_faces"])
                continue
            name = p["name"]
            if name not in types:
                order.append(name)
                types[name] = p["type"]
                totals[name] = 0
            elif types[name] != p["type"]:
                raise DecomposedBoundaryError(
                    f"{d.name} calls patch {name!r} a {p['type']!r} and an earlier rank calls it "
                    f"a {types[name]!r}; these per-rank files are not one mesh")
            totals[name] += int(p["n_faces"])
    if not seen_a_boundary:
        raise DecomposedBoundaryError(
            f"none of the {len(ranks)} processor directories under {case} holds a "
            f"{'constant/' + region if region else 'constant'}/polyMesh/boundary")
    logger.info("decomposed boundary: %s patches over %d ranks%s, %d processor faces excluded",
                len(order), len(ranks), f" for region {region}" if region else "", dropped)
    out: list[dict] = []
    cursor = 0
    for name in order:
        out.append({"name": name, "type": types[name], "n_faces": totals[name],
                    "start_face": cursor})
        cursor += totals[name]
    return out


def rank_of(path: Path) -> str:
    """The `processorN` component of `path`, or "" when it is not under one.

    COUNTING `.parent`s IS HOW A RANK GETS NAMED WRONG. `processorN/constant/<region>/polyMesh` is
    four levels up and `processorN/constant/polyMesh` is three, so an expression tuned to one of
    them names the case directory on the other - in a refusal whose whole job is to say which
    rank could not be read. The component is found by what it IS rather than by where it sits.
    """
    for part in Path(path).parts:
        if part.startswith("processor") and part[9:].isdigit():
            return part
    return ""


def decomposed_rank_spans(case: Path, region: str = "") -> list[tuple]:
    """Each rank's real boundary patches and the span of faces that covers them, in rank order.

    Returns [(polymesh, rows, first_face, n_faces), ...] - one entry per rank that holds any real
    boundary face, with `rows` in the rank's own file order and `first_face`/`n_faces` the single
    contiguous span of its `faces` file that those patches occupy.

    WHY THIS IS ONE FUNCTION AND NOT TWO LOOPS. Both decomposed sidecars are assembled from these
    spans: `write_index_decomposed` reads the geometry out of them, and `write_quality_decomposed`
    reads checkMesh's numbers out of the same faces and is NUMBERED AGAINST THE INDEX. They have
    to walk the ranks in the same order and cut each patch at the same place, and nothing about
    the counts would catch it if they did not: two arrays of the same length in a different rank
    order sum to the same patch totals and pass every gate there is to write. The hand-run scripts
    this replaces had the loop twice, and said so in their own docstring - "THE ORDER IS THE THING
    THAT CAN SILENTLY GO WRONG... A quality array off by one patch would paint the metal's numbers
    on the coolant and look entirely plausible". So it is written once and read twice, and the
    thing that could go wrong is gone rather than guarded.

    A RANK WITH NO REAL BOUNDARY IS LEFT OUT, not reported as empty: an interior rank of a big
    decomposition can hold cells whose every outside face is a cut to another rank.

    THE SPAN IS CHECKED TO BE EXACTLY THE REAL PATCHES. In a decomposed mesh the `processor`
    patches are appended after the real ones, so one read covers the real ones and nothing else -
    but a hole in the middle would take faces on a cut into the delivered surface, where they draw
    as a wall that is not there. Checked rather than assumed.
    """
    out: list[tuple] = []
    for d in processor_dirs(Path(case)):
        polymesh = (d / "constant" / region / "polyMesh") if region else (d / "constant" / "polyMesh")
        if not (polymesh / "boundary").is_file():
            continue
        rows = [p for p in read_boundary_patches(polymesh)
                if p["type"] != PROCESSOR_PATCH_TYPE and int(p["n_faces"]) > 0]
        if not rows:
            continue
        first = min(int(p["start_face"]) for p in rows)
        last = max(int(p["start_face"]) + int(p["n_faces"]) for p in rows)
        if sum(int(p["n_faces"]) for p in rows) != last - first:
            raise DecomposedBoundaryError(
                f"{d.name}: the real patches are not contiguous, so one read of faces "
                f"{first:,}..{last:,} would take in faces on a cut between ranks")
        out.append((polymesh, rows, first, last - first))
    return out


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
        patch_areas = _face_areas(points, patch_faces)
        all_areas.append(patch_areas)
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
            # THE AREA, which was already being computed here and thrown away. On a conjugate
            # mesh the interface area IS the heat-transfer area, so it is the one geometric
            # number a thermal answer depends on most directly - and the one that staircasing
            # takes away: measured on the first customer plate, 48,430.54 mm2 delivered against
            # 60,588.07 mm2 of wetted CAD, a 20.07% deficit nobody could see from a face count.
            "area_m2": float(patch_areas.sum()),
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
