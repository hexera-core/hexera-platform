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

#: A mesh above this many faces is not measured for the viewer. The arrays below are a few hundred
#: bytes per thousand faces, but the edge-expanded temporaries of the geometry pass are not, and the
#: worker computes this while it still owns a workspace it must also upload. Past the cap the user
#: keeps the mesh and the summary figures; only the per-face colouring is absent, and the log says so.
MAX_FACES_FOR_FIELDS = 12_000_000

#: How many worst spots ship per metric. Bad faces are rare on a mesh worth delivering (single
#: digits to a few hundred), so the list is small by nature; the cap only bounds a bad mesh.
MAX_HOTSPOTS_PER_METRIC = 60

#: checkMesh's own skewness bars (maxInternalSkewness / maxBoundarySkewness, the standard
#: meshQualityControls values the engine criteria cite). A boundary face is a half-cell measurement
#: and checkMesh allows it five times more before it counts. The numbers are OpenFOAM's, not ours.
INTERNAL_SKEW_LIMIT = 4.0
BOUNDARY_SKEW_LIMIT = 20.0

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


def _tokens(buf: bytes, dtype) -> np.ndarray:
    if not buf.strip():
        return np.empty(0, dtype=dtype)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return np.fromstring(buf, dtype=dtype, sep=" ")  # noqa: NPY201 - fastest stdlib-free path


def _read_points(path: Path) -> np.ndarray:
    count, body = _body(path)
    arr = _tokens(body.translate(_PAREN_TABLE), np.float64)
    if arr.size != count * 3:
        raise UnreadableMesh(f"{path.name}: expected {count * 3} coordinates, parsed {arr.size}")
    return arr.reshape(count, 3)


def _read_labels(path: Path) -> np.ndarray:
    count, body = _body(path)
    arr = _tokens(body, np.int64)
    if arr.size != count:
        raise UnreadableMesh(f"{path.name}: expected {count} labels, parsed {arr.size}")
    return arr


def _read_faces(path: Path) -> tuple[np.ndarray, np.ndarray]:
    count, body = _body(path)
    arr = _tokens(body.translate(_PAREN_TABLE), np.int64)
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
    d_own = fCtrs - cEst[owner]
    pyr3_own = np.einsum("ij,ij->i", fAreas, d_own)
    pc_own = 0.75 * fCtrs + 0.25 * cEst[owner]
    d_nei = fCtrs[:ni] - cEst[neighbour]
    pyr3_nei = -np.einsum("ij,ij->i", fAreas[:ni], d_nei)
    pc_nei = 0.75 * fCtrs[:ni] + 0.25 * cEst[neighbour]
    cVols3 = (np.bincount(owner, weights=pyr3_own, minlength=n_cells)
              + np.bincount(neighbour, weights=pyr3_nei, minlength=n_cells))
    cCtrs = np.zeros((n_cells, 3))
    for k in range(3):
        cCtrs[:, k] = (np.bincount(owner, weights=pyr3_own * pc_own[:, k], minlength=n_cells)
                       + np.bincount(neighbour, weights=pyr3_nei * pc_nei[:, k],
                                     minlength=n_cells))
    with np.errstate(invalid="ignore", divide="ignore"):
        cCtrs /= cVols3[:, None]
    tiny = np.abs(cVols3) <= _ROOTVSMALL
    if tiny.any():
        cCtrs[tiny] = cEst[tiny]
    return cCtrs, nfc


def _non_orthogonality(cCtrs, fAreas, owner, neighbour):
    ni = len(neighbour)
    d = cCtrs[neighbour] - cCtrs[owner[:ni]]
    s = fAreas[:ni]
    den = np.linalg.norm(d, axis=1) * np.linalg.norm(s, axis=1) + _ROOTVSMALL
    return np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", d, s) / den, -1.0, 1.0)))


def _skewness(points, face_flat, face_off, fCtrs, fAreas, cCtrs, owner, neighbour):
    nf = len(face_off) - 1
    ni = len(neighbour)
    own_c = cCtrs[owner]
    Cpf = fCtrs - own_c
    d = np.empty((nf, 3))
    d[:ni] = cCtrs[neighbour] - own_c[:ni]
    nrm = fAreas[ni:]
    nhat = nrm / (np.linalg.norm(nrm, axis=1)[:, None] + _ROOTVSMALL)
    d[ni:] = nhat * np.einsum("ij,ij->i", nhat, Cpf[ni:])[:, None]
    sf_cpf = np.einsum("ij,ij->i", fAreas, Cpf)
    sf_d = np.einsum("ij,ij->i", fAreas, d)
    sv = Cpf - (sf_cpf / (sf_d + _ROOTVSMALL))[:, None] * d
    svmag = np.linalg.norm(sv, axis=1)
    svHat = sv / (svmag[:, None] + _ROOTVSMALL)
    fd = np.empty(nf)
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        e0, e1 = face_off[f0], face_off[f1]
        off = face_off[f0:f1 + 1] - e0
        sizes = np.diff(off)
        pv = points[face_flat[e0:e1]]
        proj = np.abs(np.einsum("ij,ij->i", np.repeat(svHat[f0:f1], sizes, axis=0),
                                pv - np.repeat(fCtrs[f0:f1], sizes, axis=0)))
        fd[f0:f1] = np.maximum.reduceat(proj, off[:-1])
    dmag = np.linalg.norm(d, axis=1)
    floor = np.empty(nf)
    floor[:ni] = 0.2 * dmag[:ni]
    floor[ni:] = 0.4 * dmag[ni:]
    return svmag / np.maximum(fd, floor + _ROOTVSMALL)


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
                          skew_limit: float = INTERNAL_SKEW_LIMIT) -> None:
    """Add `quality_fields` to a viewer response, or leave it untouched.

    The colouring is an addition to a mesh the user already has. Nothing that goes wrong while
    measuring it may cost them the mesh, so every failure is logged and swallowed here, and the
    viewer simply has no heatmap for that job."""
    try:
        names = [p.get("name") for p in (resp.get("patches") or []) if isinstance(p, dict)]
        fields = quality_fields(polymesh_dir, non_ortho_limit=non_ortho_limit,
                                skew_limit=skew_limit, patch_names=names)
        if fields:
            resp["quality_fields"] = fields
    except Exception:  # noqa: BLE001 - a heatmap is never worth a delivery
        logger.exception("viewer quality fields unavailable - the mesh ships without a heatmap")


def quality_fields(polymesh_dir, *, non_ortho_limit: float,
                   skew_limit: float = INTERNAL_SKEW_LIMIT, patch_names=None) -> dict | None:
    """Per-face quality fields for the viewer, or None when the mesh cannot or should not be measured.

    A boundary face carries the WORST value of the cell behind it, over that cell's internal
    faces (`basis: owner_cell_max`): the viewer paints the boundary surface, and the question a
    user asks of a red face is "what is wrong with the cell there", not "what is this face's own
    angle" (a boundary face has no non-orthogonality of its own - the metric lives on the faces
    between two cells, and those are the faces the limit applies to).

    `non_ortho_limit` and `skew_limit` are the engine's bars; they are reported back verbatim so
    the viewer's red line is the same line the quality gate drew.
    """
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

    ni = len(mesh.neighbour)
    nc = mesh.n_cells
    fCtrs, fAreas = _face_geometry(mesh.points, mesh.face_flat, mesh.face_off)
    cCtrs, nfc = _cell_geometry(fCtrs, fAreas, mesh.owner, mesh.neighbour, nc)
    no = _non_orthogonality(cCtrs, fAreas, mesh.owner, mesh.neighbour)   # (Fi,)
    sk = _skewness(mesh.points, mesh.face_flat, mesh.face_off, fCtrs, fAreas, cCtrs,
                   mesh.owner, mesh.neighbour)                             # (F,)
    no = np.nan_to_num(no, nan=0.0, posinf=0.0, neginf=0.0)
    sk = np.nan_to_num(sk, nan=0.0, posinf=0.0, neginf=0.0)

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
            "cell_faces_b64": _b64(np.clip(nfc[own], 0, 255).astype(np.uint8)),
            "count": int(len(ids)),
        }
    if not per_patch:
        return None

    # skew is judged against checkMesh's two bars: the engine's limit inside, 20 on the boundary
    sk_limits = np.full(mesh.n_faces, float(skew_limit))
    sk_limits[ni:] = BOUNDARY_SKEW_LIMIT
    no_limits = np.full(ni, float(non_ortho_limit))
    hot = (_hotspots("non_ortho", no, no_limits, fCtrs, mesh.owner, nfc, patch_of_face, names)
           + _hotspots("skewness", sk, sk_limits, fCtrs, mesh.owner, nfc, patch_of_face, names))

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
        },
        "patches": per_patch,
        "hotspots": hot,
    }
