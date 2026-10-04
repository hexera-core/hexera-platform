# Responsibility: Find the spaces inside an external body that the far field reaches only through
# gaps too narrow for the planned mesh to carry flow, and say which faces of the body the far field
# actually wets.
# Boundaries: measurement only - it reads triangles and a planned cell size, and names points and
# per-face sides. Whether a mesher seals those spaces, and how, is the engine's decision.
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

#: Gaps narrower than this many planned wall cells are closed for the reading: a mesh carries no
#: resolved flow through a passage one or two cells across, but snappyHexMesh's castellation still
#: leaks through it - and then meshes everything behind it.
SEAL_GAP_CELLS = 2.0

#: A sealed space smaller than this many wall cells is not worth a point: no mesh worth the name
#: sits inside it, and a point that close to the walls is not reliably clear of them.
MIN_CAVITY_CELLS = 64

#: The voxel copy: half the planned wall cell, coarsened only to stay within this many voxels.
MAX_VOXELS = 8_000_000

#: At most this many sealed spaces are named (largest first).
MAX_CAVITIES = 32

#: The rasteriser's sample budget (points laid on the triangles); a surface needing more is not
#: read - the case is then authored exactly as before.
MAX_SAMPLES = 150_000_000


@dataclass(frozen=True)
class SealedCavity:
    """One space inside the body that the far field reaches only through narrow gaps."""

    #: a point deep inside it (metres) - clear of every wall by `clearance_m`
    point: tuple[float, float, float]
    volume_m3: float
    clearance_m: float


@dataclass(frozen=True)
class CavityReading:
    """What the voxel copy of an external body found.

    `cavities`: the spaces to keep out of the mesh, largest first (empty when there are none).
    `wet_sides`: per input triangle, which of its sides the far field wets after the narrow gaps
    are closed: +1 the side its normal points to, -1 the other, 2 both (a sheet thinner than the
    reading), 0 neither (it faces only a sealed space or the body's own inside).
    `voxel_m`, `seal_gap_m`: the scale of the reading - gaps narrower than seal_gap_m count as
    closed."""

    cavities: tuple[SealedCavity, ...]
    wet_sides: np.ndarray
    voxel_m: float
    seal_gap_m: float


def read_cavities(tris, *, cell_m: float, gap_cells: float = SEAL_GAP_CELLS,
                  min_cells: float = MIN_CAVITY_CELLS, max_voxels: int = MAX_VOXELS,
                  budget_s: float | None = 120.0) -> CavityReading | None:
    """The sealed spaces of an EXTERNAL body and the far-field-wetted side of each face.

    A hollow model - a 3D-printing shell, a car body modelled as skins with panel gaps, a
    housing with a seam - is a closed space wrapped in a thin wall with slits in it. The far
    field reaches that space only through the slits, and snappyHexMesh's castellation follows
    any slit at least a cell across - the feature-edge refinement at the slit's own edges makes
    one - then meshes the whole inside. The wall, thinner than a cell, is then wetted on both
    sides: it cannot be castellated, it snaps to whichever skin is nearer, and prism layers
    collapse on most of the body (job 9548829e: a 128 mm car shell with 0.29 mm walls and
    0.4-0.8 mm panel gaps, 17-18 % layer coverage over three passes).

    How: the triangles are rasterised into voxels of half the wall cell; the wall voxels are
    grown by half the sealing gap, which closes every gap narrower than gap_cells wall cells;
    the free space is split into regions, and the far field is the region at the grid's corner.
    A region other than the far field that WAS joined to the far field before the growing is a
    space reached only through narrow gaps: it is named with its deepest point. A region that
    was already apart (the inside of a solid, a properly closed void) is left alone - the mesher
    never reaches it. Each face's sides are then read a little past the grown wall.

    None when the reading cannot be made (no triangles, a degenerate cell, no scipy, over
    budget): the caller then meshes exactly as before."""
    try:
        return _read_cavities(tris, cell_m=cell_m, gap_cells=gap_cells, min_cells=min_cells,
                              max_voxels=max_voxels, budget_s=budget_s)
    except Exception:  # noqa: BLE001 - a reading is an improvement, never a prerequisite
        logger.warning("sealed-cavity reading failed; meshing without it", exc_info=True)
        return None


def _read_cavities(tris, *, cell_m: float, gap_cells: float, min_cells: float, max_voxels: int,
                   budget_s: float | None) -> CavityReading | None:
    from scipy import ndimage

    from meshpipeline.engines.passage_flow import _rasterize

    deadline = (time.monotonic() + float(budget_s)) if budget_s else None
    T = np.asarray(tris, dtype=float)
    n = int(len(T))
    cell = float(cell_m)
    if n == 0 or T.ndim != 3 or not (math.isfinite(cell) and cell > 0.0):
        return None
    finite = np.isfinite(T).all(axis=(1, 2))
    if not bool(finite.any()):
        return None
    P = T[finite].reshape(-1, 3)
    lo, hi = P.min(axis=0), P.max(axis=0)
    seal_gap = float(gap_cells) * cell
    h = 0.5 * cell
    for _ in range(8):
        r = max(1, int(round(0.5 * seal_gap / h)))
        dims = np.ceil((hi - lo) / h).astype(np.int64) + 2 * (r + 4)
        if int(np.prod(dims)) <= int(max_voxels):
            break
        h *= 1.05 * (float(np.prod(dims)) / float(max_voxels)) ** (1.0 / 3.0)
    r = max(1, int(round(0.5 * seal_gap / h)))
    dims = np.ceil((hi - lo) / h).astype(np.int64) + 2 * (r + 4)
    if int(np.prod(dims)) > int(max_voxels):
        return None
    shape = tuple(int(d) for d in dims)
    origin = lo - (r + 4) * h
    # the rasteriser samples every triangle every h/3: bound that work up front, since it checks
    # its deadline only between batches of similar triangles
    tf_ = T[finite]
    area = 0.5 * float(np.linalg.norm(np.cross(tf_[:, 1] - tf_[:, 0], tf_[:, 2] - tf_[:, 0]),
                                      axis=1).sum())
    if 2.0 * area / (h / 3.0) ** 2 + 4.0 * len(tf_) > MAX_SAMPLES:
        logger.info("sealed cavities: surface too large to read within budget - skipped")
        return None
    blocked = _rasterize(origin, h, shape, tf_, deadline=deadline)

    six = ndimage.generate_binary_structure(3, 1)
    raw, _ = ndimage.label(~blocked, structure=six)
    far_raw = int(raw[0, 0, 0])
    grown = ndimage.binary_dilation(blocked, structure=ndimage.generate_binary_structure(3, 2),
                                    iterations=r)
    closed, n_closed = ndimage.label(~grown, structure=six)
    far = int(closed[0, 0, 0])
    if far == 0 or far_raw == 0:
        return None
    if deadline is not None and time.monotonic() > deadline:
        return None

    cavities: list[SealedCavity] = []
    min_vox = float(min_cells) * (cell / h) ** 3
    sizes = np.bincount(closed.ravel(), minlength=n_closed + 1)
    objects = ndimage.find_objects(closed)
    for rid in np.argsort(-sizes):
        rid = int(rid)
        if rid in (0, far) or sizes[rid] < min_vox:
            continue
        sl = objects[rid - 1]
        if sl is None:
            continue
        region = closed[sl] == rid
        # reached by the far field before the narrow gaps were closed? A space that was already
        # apart is the inside of a solid or a properly closed void - the mesher never gets there
        joined = raw[sl][region] == far_raw
        if not bool(joined.any()) or float(joined.mean()) < 0.5:
            continue
        # padded with one wall voxel all round: the region fills its own bounding box, and the
        # distance must be measured to the walls around it, not to nothing
        depth = ndimage.distance_transform_edt(np.pad(region, 1))[1:-1, 1:-1, 1:-1]
        at = np.unravel_index(int(np.argmax(depth)), depth.shape)
        ijk = np.array([s.start + a for s, a in zip(sl, at)], dtype=float)
        point = origin + (ijk + 0.5) * h
        cavities.append(SealedCavity(point=(float(point[0]), float(point[1]), float(point[2])),
                                     volume_m3=float(sizes[rid]) * h ** 3,
                                     clearance_m=float(depth[at]) * h + r * h))
        if len(cavities) >= MAX_CAVITIES:
            break

    # each face's wetted side. A step along a normal can jump a wall thinner than the step, so a
    # face of a CLOSED shell is read on its outer side only - normals turned out of the shell's
    # own material first (the shell's signed volume says which way they point) - where the step
    # walks away from that material, never through it. A face of an open sheet has no outside of
    # its own and is read both ways.
    wet = np.zeros(n, dtype=np.int8)
    fi = np.flatnonzero(finite)
    tf = T[finite]
    nrm = np.cross(tf[:, 1] - tf[:, 0], tf[:, 2] - tf[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    good = ln > 0.0
    unit = np.zeros_like(nrm)
    unit[good] = nrm[good] / ln[good, None]
    c = tf.mean(axis=1)
    hi_idx = np.asarray(shape) - 1
    out_sign = outward_signs(tf)

    def _side(sgn: np.ndarray) -> np.ndarray:
        """Per face, the first labelled region a step along sgn * normal reaches (0 = none)."""
        got = np.zeros(len(tf), dtype=closed.dtype)
        for k in (r + 1.0, r + 2.0, r + 3.0):
            todo = got == 0
            if not bool(todo.any()):
                break
            p = c[todo] + (sgn[todo] * k * h)[:, None] * unit[todo]
            ijk = np.clip(np.floor((p - origin) / h).astype(np.int64), 0, hi_idx)
            got[todo] = closed[ijk[:, 0], ijk[:, 1], ijk[:, 2]]
        return got

    closed_shell = out_sign != 0
    ahead = _side(np.where(closed_shell, out_sign, 1).astype(float))
    behind = _side(-np.ones(len(tf)))
    # closed shell: wetted on its outer side unless that side is a space kept out of the mesh (or
    # another part's inside); a step that reached no region at all (a gap tighter than the reading)
    # stays wetted - the fluid may well be there
    w_closed = np.where((ahead == far) | (ahead == 0), out_sign, 0)
    plus, minus = ahead == far, behind == far
    w_open = np.where(plus & minus, 2, np.where(plus, 1, np.where(minus, -1, 0)))
    w = np.where(closed_shell, w_closed, w_open)
    w[~good] = 0
    wet[fi] = w.astype(np.int8)
    logger.info("sealed cavities: %d space(s) reached only through gaps under %.3g m (voxel %.3g m"
                ", largest %.3g m3); %d of %d faces wetted by the far field", len(cavities),
                seal_gap, h, cavities[0].volume_m3 if cavities else 0.0,
                int((wet != 0).sum()), n)
    return CavityReading(cavities=tuple(cavities), wet_sides=wet, voxel_m=float(h),
                         seal_gap_m=float(seal_gap))


#: A shell counts as closed - it has an inside of its own - when at most this fraction of its edges
#: are open. A tessellated STEP solid may carry a few cracks along its seams.
CLOSED_SHELL_OPEN_EDGES = 0.01


#: Material sides are read by ray parity for at most this many closed shells (largest first) and
#: this many faces each; any further shell falls back to its signed volume.
PARITY_SHELLS = 200
PARITY_FACES = 3


def _crossings(points: np.ndarray, d: np.ndarray, T: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """How many triangles the ray from each point along d crosses (Moller-Trumbore)."""
    count = np.zeros(len(points), dtype=np.int64)
    for s in range(0, len(T), chunk):
        tri = T[s:s + chunk]
        v0, e1, e2 = tri[:, 0], tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        p = np.cross(d, e2)                                   # (m, 3)
        det = (e1 * p).sum(-1)
        ok = np.abs(det) > 1e-30
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        sv = points[:, None, :] - v0[None, :, :]              # (R, m, 3)
        u = (sv * p[None]).sum(-1) * inv[None]
        q = np.cross(sv, e1[None])
        v = (q * d).sum(-1) * inv[None]
        t = (q * e2[None]).sum(-1) * inv[None]
        hit = ok[None] & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
        count += hit.sum(axis=1)
    return count


def outward_signs(tris) -> np.ndarray:
    """Per triangle, +1 or -1: the sign that turns its normal OUT of the material of the closed
    shell it belongs to, or 0 for a face of an open sheet, which has no inside. Shells are the
    edge-connected pieces of the surface; coincident corners are welded first.

    Which side is material is read by RAY PARITY over the whole surface: a point just off a face
    is inside material when a ray from it crosses the surface an odd number of times. A shell's
    own signed volume cannot tell a solid modelled inside-out from the skin of a void nested in
    another part (both read negative) - the inner skin of a sealed hollow body is the second kind,
    and its outward side is the void. Signed volume is the fallback past PARITY_SHELLS shells."""
    T = np.asarray(tris, dtype=float)
    n = int(len(T))
    out = np.zeros(n, dtype=np.int8)
    if n == 0:
        return out
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    V = T.reshape(-1, 3)
    scale = float(np.abs(V).max()) or 1.0
    key = np.round(V / (scale * 1e-9)).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    F = np.asarray(inv).reshape(n, 3)
    E = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    owner = np.tile(np.arange(n), 3)
    order = np.lexsort((E[:, 1], E[:, 0]))
    Es, ts = E[order], owner[order]
    same = np.all(Es[1:] == Es[:-1], axis=1)
    a, b = ts[:-1][same], ts[1:][same]
    _, comp = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)),
                                   directed=False)
    # open edges: an edge key used by exactly one triangle
    uniq, first, counts = np.unique(Es, axis=0, return_index=True, return_counts=True)
    open_owner = ts[first[counts == 1]]
    n_comp = int(comp.max()) + 1
    edges_per = np.bincount(comp, minlength=n_comp) * 3
    open_per = np.bincount(comp[open_owner], minlength=n_comp)
    vol6 = np.einsum("ij,ij->i", T[:, 0], np.cross(T[:, 1], T[:, 2]))
    vol_per = np.bincount(comp, weights=vol6, minlength=n_comp)
    closed = (open_per <= CLOSED_SHELL_OPEN_EDGES * edges_per) & (vol_per != 0.0)
    sign = np.where(closed, np.sign(vol_per), 0.0).astype(np.int8)

    # ray parity for the largest closed shells: a point a hair in front of a face (along its own
    # normal) inside material means the normal points INTO material, so the outward sign is -1
    fn = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    area2 = np.linalg.norm(fn, axis=1)
    area_per = np.bincount(comp, weights=area2, minlength=n_comp)
    shells = [int(k) for k in np.argsort(-area_per) if closed[k]][:PARITY_SHELLS]
    if shells:
        good = area2 > 0
        diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0))) or 1.0
        probes, owner_shell = [], []
        for k in shells:
            faces = np.flatnonzero((comp == k) & good)
            if len(faces) == 0:
                continue
            pick = faces[np.argsort(-area2[faces])[:PARITY_FACES]]
            for f in pick:
                u = fn[f] / area2[f]
                # a hair off the face: far below any wall thickness, far above round-off
                probes.append(T[f].mean(axis=0) + u * 1e-6 * diag)
                owner_shell.append(k)
        if probes:
            d = np.array([0.5773503, 0.5773807, 0.5772199])     # off-axis: misses edges
            d = d / np.linalg.norm(d)
            odd = _crossings(np.asarray(probes), d, T[np.isfinite(T).all(axis=(1, 2))]) % 2 == 1
            votes: dict[int, list[int]] = {}
            for k, o in zip(owner_shell, odd):
                votes.setdefault(k, []).append(-1 if o else 1)
            for k, vs in votes.items():
                sign[k] = 1 if sum(vs) > 0 else -1
    return sign[comp]


__all__ = ["CLOSED_SHELL_OPEN_EDGES", "CavityReading", "MAX_SAMPLES", "MAX_VOXELS",
           "MIN_CAVITY_CELLS", "SEAL_GAP_CELLS", "SealedCavity", "outward_signs", "read_cavities"]
