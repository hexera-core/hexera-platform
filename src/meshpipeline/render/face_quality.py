# Responsibility: Measure per-face cell quality on a delivered OpenFOAM polyMesh and pack it for the viewer's heatmap.
# Boundaries: measurement and packing only. It judges nothing - the bars are passed in by the engine
# that owns them - and it degrades to "no fields" rather than guess when a mesh cannot be measured.
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: A mesh above this many faces is not measured for the viewer. Every pass over the mesh runs a
#: block of faces at a time (_CHUNK_FACES) and the labels are 32-bit, so what remains resident is
#: the mesh itself plus one block: measured on a delivered 7.4M-cell bundle (22,967,336 faces,
#: HEX-14) at 107 s and a 3.77 GB peak - about 165 bytes per face - against 565 s and 7.42 GB
#: before the passes were blocked. The cap is where that rate meets the worker's 8 GiB with room
#: for the worker itself: 40M faces is ~6.6 GB, about 13M hex-dominant cells. Past the cap the
#: user keeps the mesh and the summary figures; only the per-face colouring is absent, and the log
#: says so. Raising the worker's memory raises the cap in proportion.
MAX_FACES_FOR_FIELDS = 40_000_000

#: How many worst spots ship per metric. Bad faces are rare on a mesh worth delivering (single
#: digits to a few hundred), so the list is small by nature; the cap only bounds a bad mesh.
MAX_HOTSPOTS_PER_METRIC = 60

#: checkMesh's own skewness bars (maxInternalSkewness / maxBoundarySkewness, the standard
#: meshQualityControls values the engine criteria cite). A boundary face is a half-cell measurement
#: and checkMesh allows it five times more before it counts. The numbers are OpenFOAM's, not ours.
INTERNAL_SKEW_LIMIT = 4.0
BOUNDARY_SKEW_LIMIT = 20.0

#: checkMesh's aspect-ratio bar (primitiveMesh::aspectThreshold_): a cell past 1000 is reported as
#: high aspect ratio. Prism layers sit at 10 to 50 by design, so on a delivered mesh the bar is far
#: above everything the mesh has; the payload therefore also carries `floor` (a cube reads 1) and
#: `scale_to` (the mesh's own maximum), and the viewer spans its colours over that range, drawing
#: the bar as a tick only when it falls inside it.
ASPECT_RATIO_LIMIT = 1000.0

_ROOTVSMALL = 1.0e-18
_CHUNK_FACES = 1_500_000
_COMMENT_BLOCK = re.compile(rb"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(rb"//[^\n]*")
_PAREN_TABLE = bytes.maketrans(b"()", b"  ")
_PATCH_RE = re.compile(rb"(\w[\w.\-:]*)\s*\{([^{}]*)\}", re.S)


class UnreadableMesh(ValueError):
    """The polyMesh on disk cannot be measured (absent, binary, or inconsistent)."""


# ---------------------------------------------------------------- polyMesh reader ----
# ASCII only, like the surface reader the viewer already uses: a binary polyMesh is refused loudly
# rather than mis-parsed. Whole files are tokenised with numpy - a 2M-cell mesh reads in seconds.

@dataclass
class _Patch:
    name: str
    start: int
    n: int


@dataclass
class _Mesh:
    points: np.ndarray        # (P,3) float64
    face_flat: np.ndarray     # vertex ids, concatenated
    face_off: np.ndarray      # (F+1,) offsets into face_flat
    owner: np.ndarray         # (F,)
    neighbour: np.ndarray     # (Fi,) - the first Fi faces are internal
    patches: list[_Patch]

    @property
    def n_faces(self) -> int:
        return len(self.face_off) - 1

    @property
    def n_cells(self) -> int:
        n = int(self.owner.max()) + 1 if len(self.owner) else 0
        if len(self.neighbour):
            n = max(n, int(self.neighbour.max()) + 1)
        return n


def _strip(data: bytes) -> bytes:
    return _COMMENT_LINE.sub(b" ", _COMMENT_BLOCK.sub(b" ", data))


def _body(path: Path) -> tuple[int, bytes]:
    data = path.read_bytes()
    m = re.search(rb"format\s+(\w+)\s*;", data[:4096])
    if not m:
        raise UnreadableMesh(f"{path.name}: no FoamFile format entry")
    if m.group(1) != b"ascii":
        raise UnreadableMesh(f"{path.name}: format {m.group(1).decode()!r} - only ascii is read")
    if len(data) < 50 * 1024 * 1024:
        data = _strip(data)
    hdr_end = data.index(b"}") + 1
    m = re.search(rb"(\d+)\s*\(", data[hdr_end:hdr_end + 65536])
    if not m:
        raise UnreadableMesh(f"{path.name}: no list count after the header")
    return int(m.group(1)), data[hdr_end + m.end():data.rindex(b")")]


def declared_count(path: Path) -> int | None:
    """The list count a polyMesh file declares, read from its first 64 KiB only - so a mesh past
    the field cap is refused without loading it. A 22 M-face `faces` file is about a gigabyte of
    arrays once parsed; on shell_tube_bundle_009 (7.3 M cells) that load, in a worker already
    holding the mesh for its other checks, was part of what the kernel killed at 8 GiB.
    None when the header cannot be read or carries no count (the full reader then decides)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    head = _strip(head)
    hdr_end = head.find(b"}")
    if hdr_end < 0:
        return None
    m = re.search(rb"(\d+)\s*\(", head[hdr_end + 1:])
    return int(m.group(1)) if m else None


def _tokens(buf: bytes, dtype) -> np.ndarray:
    if not buf.strip():
        return np.empty(0, dtype=dtype)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return np.fromstring(buf, dtype=dtype, sep=" ")  # noqa: NPY201 - fastest stdlib-free path


def _translated_body(path: Path) -> bytes:
    """The list body with the parentheses blanked - parsed once, so the raw body is not kept
    beside its translated copy (each is the size of the file: ~0.7 GB for 23 M faces)."""
    _, body = _body(path)
    return body.translate(_PAREN_TABLE)


def _read_points(path: Path) -> np.ndarray:
    count, body = _body(path)
    arr = _tokens(body.translate(_PAREN_TABLE), np.float64)
    if arr.size != count * 3:
        raise UnreadableMesh(f"{path.name}: expected {count * 3} coordinates, parsed {arr.size}")
    return arr.reshape(count, 3)


def _read_labels(path: Path) -> np.ndarray:
    count, body = _body(path)
    arr = _tokens(body, np.int32)          # cell ids fit 32 bits; half the bytes of int64
    if arr.size != count:
        raise UnreadableMesh(f"{path.name}: expected {count} labels, parsed {arr.size}")
    return arr


def _read_faces(path: Path) -> tuple[np.ndarray, np.ndarray]:
    count, body = _body(path)
    del body                                # the translated copy is the one that lives on
    # point ids fit 32 bits: the token array is half the size the int64 parse made
    arr = _tokens(_translated_body(path), np.int32)
    sizes = np.empty(count, dtype=np.int64)
    idx = 0
    for i in range(count):
        if idx >= arr.size:
            raise UnreadableMesh(f"{path.name}: face stream ended at record {i} of {count}")
        sizes[i] = arr[idx]
        idx += int(arr[idx]) + 1
    if idx != arr.size:
        raise UnreadableMesh(f"{path.name}: face stream inconsistent ({idx} of {arr.size} used)")
    face_off = np.zeros(count + 1, dtype=np.int64)
    np.cumsum(sizes, out=face_off[1:])
    keep = np.ones(arr.size, dtype=bool)
    size_pos = np.zeros(count, dtype=np.int64)
    size_pos[1:] = np.cumsum(sizes[:-1] + 1)
    keep[size_pos] = False
    return arr[keep], face_off


def _read_boundary(path: Path) -> list[_Patch]:
    data = _strip(path.read_bytes())
    m = re.search(rb"format\s+(\w+)\s*;", data[:4096])
    if m and m.group(1) != b"ascii":
        raise UnreadableMesh(f"{path.name}: binary boundary file")
    body = data[data.index(b"}") + 1:]
    out = []
    for pm in _PATCH_RE.finditer(body):
        entries = dict(re.findall(rb"(\w+)\s+([^;]+);", pm.group(2)))
        if b"nFaces" not in entries or b"startFace" not in entries:
            continue
        out.append(_Patch(pm.group(1).decode(), int(entries[b"startFace"]),
                          int(entries[b"nFaces"])))
    return out


def read_polymesh(polymesh_dir: Path) -> _Mesh:
    d = Path(polymesh_dir)
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        if not (d / name).exists():
            raise UnreadableMesh(f"{d}: no {name} file")
    points = _read_points(d / "points")
    face_flat, face_off = _read_faces(d / "faces")
    owner = _read_labels(d / "owner")
    neighbour = _read_labels(d / "neighbour")
    nf = len(face_off) - 1
    if len(owner) != nf or len(neighbour) > nf:
        raise UnreadableMesh(f"{d}: owner/neighbour do not match {nf} faces")
    if face_flat.size and int(face_flat.max()) >= len(points):
        raise UnreadableMesh(f"{d}: a face names a point that does not exist")
    return _Mesh(points, face_flat, face_off, owner, neighbour, _read_boundary(d / "boundary"))


# --------------------------------------------------------------- metrics (OpenFOAM) ----
# The formulas are primitiveMesh / primitiveMeshTools (v2412): fan-decomposed face centres and
# areas, pyramid-decomposed cell centres, faceOrthogonality and faceSkewness. They are the ones
# the corpus scorer cross-validated against checkMesh; a "nicer" formula here would colour the
# mesh by a number the gate never measured.

def _face_geometry_block(points, face_flat, face_off):
    sizes = np.diff(face_off)
    pv = points[face_flat]
    fc_est = np.add.reduceat(pv, face_off[:-1]) / sizes[:, None]
    nxt = np.arange(1, len(face_flat) + 1)
    nxt[face_off[1:] - 1] = face_off[:-1]
    p2 = points[face_flat[nxt]]
    fce = np.repeat(fc_est, sizes, axis=0)
    n = np.cross(p2 - pv, fce - pv)
    c = pv + p2 + fce
    a = np.linalg.norm(n, axis=1)
    sumN = np.add.reduceat(n, face_off[:-1])
    sumA = np.add.reduceat(a, face_off[:-1])
    sumAc = np.add.reduceat(a[:, None] * c, face_off[:-1])
    fAreas = 0.5 * sumN
    with np.errstate(invalid="ignore", divide="ignore"):
        fCtrs = sumAc / (3.0 * sumA[:, None])
    bad = sumA < _ROOTVSMALL
    if bad.any():
        fCtrs[bad] = fc_est[bad]
    tri = sizes == 3
    if tri.any():
        s = face_off[:-1][tri]
        i0, i1, i2 = face_flat[s], face_flat[s + 1], face_flat[s + 2]
        fCtrs[tri] = (points[i0] + points[i1] + points[i2]) / 3.0
        fAreas[tri] = 0.5 * np.cross(points[i1] - points[i0], points[i2] - points[i0])
    return fCtrs, fAreas


def _face_geometry(points, face_flat, face_off):
    nf = len(face_off) - 1
    if nf <= _CHUNK_FACES:
        return _face_geometry_block(points, face_flat, face_off)
    fCtrs = np.empty((nf, 3))
    fAreas = np.empty((nf, 3))
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        e0, e1 = face_off[f0], face_off[f1]
        fCtrs[f0:f1], fAreas[f0:f1] = _face_geometry_block(
            points, face_flat[e0:e1], face_off[f0:f1 + 1] - e0)
    return fCtrs, fAreas


def _cell_geometry(fCtrs, fAreas, owner, neighbour, n_cells):
    ni = len(neighbour)
    nfc = np.bincount(owner, minlength=n_cells) + np.bincount(neighbour, minlength=n_cells)
    cEst = np.zeros((n_cells, 3))
    for k in range(3):
        cEst[:, k] = (np.bincount(owner, weights=fCtrs[:, k], minlength=n_cells)
                      + np.bincount(neighbour, weights=fCtrs[:ni, k], minlength=n_cells))
    cEst /= np.maximum(nfc, 1)[:, None]
    # The pyramid sums, a block of faces at a time: the per-face vectors (centre offsets, pyramid
    # volumes, pyramid centroids) exist only for the block, and the accumulators are per cell.
    nf = len(owner)
    cVols3 = np.zeros(n_cells)
    cCtrs = np.zeros((n_cells, 3))
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        own = owner[f0:f1]
        fc = fCtrs[f0:f1]
        fa = fAreas[f0:f1]
        ce = cEst[own]
        pyr3 = np.einsum("ij,ij->i", fa, fc - ce)
        pc = 0.75 * fc + 0.25 * ce
        cVols3 += np.bincount(own, weights=pyr3, minlength=n_cells)
        for k in range(3):
            cCtrs[:, k] += np.bincount(own, weights=pyr3 * pc[:, k], minlength=n_cells)
        m = min(f1, ni) - f0                       # the block's internal faces, if any
        if m > 0:
            nei = neighbour[f0:f0 + m]
            ce = cEst[nei]
            pyr3 = -np.einsum("ij,ij->i", fa[:m], fc[:m] - ce)
            pc = 0.75 * fc[:m] + 0.25 * ce
            cVols3 += np.bincount(nei, weights=pyr3, minlength=n_cells)
            for k in range(3):
                cCtrs[:, k] += np.bincount(nei, weights=pyr3 * pc[:, k], minlength=n_cells)
    with np.errstate(invalid="ignore", divide="ignore"):
        cCtrs /= cVols3[:, None]
    tiny = np.abs(cVols3) <= _ROOTVSMALL
    if tiny.any():
        cCtrs[tiny] = cEst[tiny]
    return cCtrs, nfc, cVols3 / 3.0


def _aspect_ratio(fAreas, owner, neighbour, cVols, n_cells):
    """checkMesh's cell aspect ratio (primitiveMeshTools::cellClosedness, three solved directions):
    the larger of the ratio of the largest to the smallest component of the cell's summed |Sf|,
    and (1/6) * the sum of those components / V^(2/3). A unit cube reads 1; a 10:1:1 brick reads
    10. A cell metric, so the boundary face carries its owner cell's value as it is."""
    ni = len(neighbour)
    nf = len(owner)
    sumMag = np.zeros((n_cells, 3))
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        mag = np.abs(fAreas[f0:f1])
        m = min(f1, ni) - f0
        for k in range(3):
            sumMag[:, k] += np.bincount(owner[f0:f1], weights=mag[:, k], minlength=n_cells)
            if m > 0:
                sumMag[:, k] += np.bincount(neighbour[f0:f0 + m], weights=mag[:m, k],
                                            minlength=n_cells)
    ratio = sumMag.max(axis=1) / (sumMag.min(axis=1) + _ROOTVSMALL)
    v = np.maximum(np.abs(cVols), _ROOTVSMALL)
    return np.maximum(ratio, sumMag.sum(axis=1) / (6.0 * np.power(v, 2.0 / 3.0)))


def _non_orthogonality(cCtrs, fAreas, owner, neighbour):
    ni = len(neighbour)
    out = np.empty(ni)
    for f0 in range(0, ni, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, ni)
        d = cCtrs[neighbour[f0:f1]] - cCtrs[owner[f0:f1]]
        s = fAreas[f0:f1]
        den = np.linalg.norm(d, axis=1) * np.linalg.norm(s, axis=1) + _ROOTVSMALL
        out[f0:f1] = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", d, s) / den, -1.0, 1.0)))
    return out


def _skewness(points, face_flat, face_off, fCtrs, fAreas, cCtrs, owner, neighbour):
    nf = len(face_off) - 1
    ni = len(neighbour)
    out = np.empty(nf)
    # One block of faces at a time, the internal/boundary split handled inside the block: the
    # full-length temporaries of the old pass (five face-vectors and six face-scalars, ~170 bytes
    # per face) were what set the 20 M-face cap.
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        fc = fCtrs[f0:f1]
        fa = fAreas[f0:f1]
        Cpf = fc - cCtrs[owner[f0:f1]]
        d = np.empty((f1 - f0, 3))
        m = max(0, min(f1, ni) - f0)                  # internal faces in this block
        if m > 0:
            d[:m] = cCtrs[neighbour[f0:f0 + m]] - cCtrs[owner[f0:f0 + m]]
        if m < f1 - f0:                               # boundary faces: project onto the normal
            nrm = fa[m:]
            nhat = nrm / (np.linalg.norm(nrm, axis=1)[:, None] + _ROOTVSMALL)
            d[m:] = nhat * np.einsum("ij,ij->i", nhat, Cpf[m:])[:, None]
        sf_cpf = np.einsum("ij,ij->i", fa, Cpf)
        sf_d = np.einsum("ij,ij->i", fa, d)
        sv = Cpf - (sf_cpf / (sf_d + _ROOTVSMALL))[:, None] * d
        svmag = np.linalg.norm(sv, axis=1)
        svHat = sv / (svmag[:, None] + _ROOTVSMALL)
        e0, e1 = face_off[f0], face_off[f1]
        off = face_off[f0:f1 + 1] - e0
        sizes = np.diff(off)
        pv = points[face_flat[e0:e1]]
        proj = np.abs(np.einsum("ij,ij->i", np.repeat(svHat, sizes, axis=0),
                                pv - np.repeat(fc, sizes, axis=0)))
        fd = np.maximum.reduceat(proj, off[:-1])
        dmag = np.linalg.norm(d, axis=1)
        floor = np.empty(f1 - f0)
        floor[:m] = 0.2 * dmag[:m]
        floor[m:] = 0.4 * dmag[m:]
        out[f0:f1] = svmag / np.maximum(fd, floor + _ROOTVSMALL)
    return out


# ------------------------------------------------------------------- the payload ----

def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def _patch_face_ids(mesh: _Mesh, patch: _Patch) -> np.ndarray:
    """The faces of a patch the viewer actually draws, in the order it draws them.

    The surface reader drops faces with fewer than three vertices, so the polygon index in the
    viewer is the index among the patch's faces that survive that filter. Mirror it exactly -
    an off-by-one here would colour every face with its neighbour's number."""
    ids = np.arange(patch.start, patch.start + patch.n)
    ids = ids[(ids >= 0) & (ids < mesh.n_faces)]
    return ids[np.diff(mesh.face_off)[ids] >= 3]


def _hotspots(metric: str, values: np.ndarray, limits: np.ndarray, fCtrs, owner, nfc,
              patch_of_face: np.ndarray, patch_names: list[str]) -> list[dict]:
    over = np.nonzero(values > limits)[0]
    if not len(over):
        return []
    excess = values[over] / limits[over]
    order = over[np.argsort(-excess)][:MAX_HOTSPOTS_PER_METRIC]
    out = []
    for f in order:
        f = int(f)
        pid = int(patch_of_face[f])
        out.append({"metric": metric, "value": float(values[f]),
                    "x": float(fCtrs[f, 0]), "y": float(fCtrs[f, 1]), "z": float(fCtrs[f, 2]),
                    "patch": patch_names[pid] if pid >= 0 else None,
                    "cell_faces": int(nfc[owner[f]])})
    return out


def attach_quality_fields(resp: dict, polymesh_dir, *, non_ortho_limit: float,
                          skew_limit: float = INTERNAL_SKEW_LIMIT,
                          aspect_limit: float = ASPECT_RATIO_LIMIT) -> None:
    """Add `quality_fields` to a viewer response, or leave it untouched.

    The colouring is an addition to a mesh the user already has. Nothing that goes wrong while
    measuring it may cost them the mesh, so every failure is logged and swallowed here, and the
    viewer simply has no heatmap for that job."""
    try:
        names = [p.get("name") for p in (resp.get("patches") or []) if isinstance(p, dict)]
        fields = quality_fields(polymesh_dir, non_ortho_limit=non_ortho_limit,
                                skew_limit=skew_limit, aspect_limit=aspect_limit, patch_names=names)
        if fields:
            resp["quality_fields"] = fields
    except Exception:  # noqa: BLE001 - a heatmap is never worth a delivery
        logger.exception("viewer quality fields unavailable - the mesh ships without a heatmap")


def quality_fields(polymesh_dir, *, non_ortho_limit: float,
                   skew_limit: float = INTERNAL_SKEW_LIMIT,
                   aspect_limit: float = ASPECT_RATIO_LIMIT, patch_names=None) -> dict | None:
    """Per-face quality fields for the viewer, or None when the mesh cannot or should not be measured.

    A boundary face carries the WORST value of the cell behind it, over that cell's internal
    faces (`basis: owner_cell_max`): the viewer paints the boundary surface, and the question a
    user asks of a red face is "what is wrong with the cell there", not "what is this face's own
    angle" (a boundary face has no non-orthogonality of its own - the metric lives on the faces
    between two cells, and those are the faces the limit applies to).

    `non_ortho_limit` and `skew_limit` are the engine's bars; they are reported back verbatim so
    the viewer's red line is the same line the quality gate drew. Aspect ratio is the third field:
    checkMesh's own number per cell, against checkMesh's bar (`aspect_limit`).
    """
    declared = declared_count(Path(polymesh_dir) / "faces")
    if declared is not None and declared > MAX_FACES_FOR_FIELDS:
        logger.info("viewer quality fields skipped: %d faces exceeds the %d cap (not loaded)",
                    declared, MAX_FACES_FOR_FIELDS)
        return None
    try:
        mesh = read_polymesh(Path(polymesh_dir))
    except UnreadableMesh as exc:
        logger.info("viewer quality fields skipped: %s", exc)
        return None
    if mesh.n_faces == 0 or mesh.n_cells == 0:
        return None
    if mesh.n_faces > MAX_FACES_FOR_FIELDS:
        logger.info("viewer quality fields skipped: %d faces exceeds the %d cap",
                    mesh.n_faces, MAX_FACES_FOR_FIELDS)
        return None

    import time as _time
    _t0 = _time.monotonic()
    ni = len(mesh.neighbour)
    nc = mesh.n_cells
    fCtrs, fAreas = _face_geometry(mesh.points, mesh.face_flat, mesh.face_off)
    cCtrs, nfc, cVols = _cell_geometry(fCtrs, fAreas, mesh.owner, mesh.neighbour, nc)
    no = _non_orthogonality(cCtrs, fAreas, mesh.owner, mesh.neighbour)   # (Fi,)
    sk = _skewness(mesh.points, mesh.face_flat, mesh.face_off, fCtrs, fAreas, cCtrs,
                   mesh.owner, mesh.neighbour)                             # (F,)
    no = np.nan_to_num(no, nan=0.0, posinf=0.0, neginf=0.0)
    sk = np.nan_to_num(sk, nan=0.0, posinf=0.0, neginf=0.0)
    ar = np.nan_to_num(_aspect_ratio(fAreas, mesh.owner, mesh.neighbour, cVols, nc),
                       nan=1.0, posinf=1.0, neginf=1.0)                    # (C,)

    # The worst value each cell touches, over its INTERNAL faces - both sides of every one. Both
    # metrics are judged against one bar in the viewer, and only internal faces share a bar: a
    # boundary face's own skewness is a half-cell measurement checkMesh allows up to 20, so
    # folding it in would paint a face red against the internal limit of 4 while the summary
    # said nothing was over. Boundary faces past THEIR bar still surface as hotspots below.
    cell_no = np.zeros(nc)
    np.maximum.at(cell_no, mesh.owner[:ni], no)
    np.maximum.at(cell_no, mesh.neighbour, no)
    cell_sk = np.zeros(nc)
    np.maximum.at(cell_sk, mesh.owner[:ni], sk[:ni])
    np.maximum.at(cell_sk, mesh.neighbour, sk[:ni])

    wanted = set(patch_names) if patch_names else None
    patches = [p for p in mesh.patches if wanted is None or p.name in wanted]
    names = [p.name for p in mesh.patches]
    patch_of_face = np.full(mesh.n_faces, -1, dtype=np.int64)
    for i, p in enumerate(mesh.patches):
        patch_of_face[p.start:p.start + p.n] = i

    per_patch: dict[str, dict] = {}
    for p in patches:
        ids = _patch_face_ids(mesh, p)
        if not len(ids):
            continue
        own = mesh.owner[ids]
        per_patch[p.name] = {
            "non_ortho_b64": _b64(cell_no[own].astype(np.float32)),
            "skewness_b64": _b64(cell_sk[own].astype(np.float32)),
            "aspect_ratio_b64": _b64(ar[own].astype(np.float32)),
            "cell_faces_b64": _b64(np.clip(nfc[own], 0, 255).astype(np.uint8)),
            "count": int(len(ids)),
        }
    if not per_patch:
        return None

    # skew is judged against checkMesh's two bars: the engine's limit inside, 20 on the boundary
    sk_limits = np.full(mesh.n_faces, float(skew_limit))
    sk_limits[ni:] = BOUNDARY_SKEW_LIMIT
    no_limits = np.full(ni, float(non_ortho_limit))
    # aspect ratio is a cell number; every face reads its owner cell's, judged against one bar
    ar_faces = ar[mesh.owner]
    ar_limits = np.full(mesh.n_faces, float(aspect_limit))
    hot = (_hotspots("non_ortho", no, no_limits, fCtrs, mesh.owner, nfc, patch_of_face, names)
           + _hotspots("skewness", sk, sk_limits, fCtrs, mesh.owner, nfc, patch_of_face, names)
           + _hotspots("aspect_ratio", ar_faces, ar_limits, fCtrs, mesh.owner, nfc, patch_of_face,
                       names))
    logger.info("viewer quality fields: %d faces (%d internal), %d boundary faces coloured, "
                "%d hotspot(s), %.1fs", mesh.n_faces, ni,
                sum(p["count"] for p in per_patch.values()), len(hot), _time.monotonic() - _t0)

    return {
        "basis": "owner_cell_max",
        "metrics": {
            "non_ortho": {"label": "non-orthogonality", "unit": "°",
                          "limit": float(non_ortho_limit),
                          "max": float(no.max()) if ni else 0.0,
                          "n_over": int(np.count_nonzero(no > non_ortho_limit)),
                          "n_faces": int(ni)},
            "skewness": {"label": "skewness", "unit": "",
                         "limit": float(skew_limit), "boundary_limit": BOUNDARY_SKEW_LIMIT,
                         "max": float(sk.max()),
                         "n_over": int(np.count_nonzero(sk > sk_limits)),
                         "n_faces": int(mesh.n_faces)},
            "aspect_ratio": {"label": "aspect ratio", "unit": "",
                             "limit": float(aspect_limit),
                             "floor": 1.0, "scale_to": float(max(ar.max(), 2.0)),
                             "max": float(ar.max()),
                             "n_over": int(np.count_nonzero(ar_faces > ar_limits)),
                             "n_faces": int(mesh.n_faces)},
        },
        "patches": per_patch,
        "hotspots": hot,
    }
