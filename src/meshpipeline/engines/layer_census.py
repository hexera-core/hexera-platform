# Responsibility: Count, on any OpenFOAM polyMesh, how many near-wall prism layers each wall face actually got - an engine-neutral measure for meshers that do not report it themselves.
# Boundaries: reads an ASCII polyMesh (points, faces, owner, neighbour, boundary); it meshes nothing. Above CENSUS_MAX_CELLS, or on a binary mesh, it measures nothing and says why.
# Collaborates with: engines/cfmesh/finalize.py (cfMesh writes no layer log), the manifest's layer_coverage_pct that the reviewer and the report read.
"""HOW MANY LAYERS DID EACH WALL FACE GET?

snappyHexMesh prints its layer coverage; cfMesh does not, so a cfMesh mesh shipped with no layer
figure and the reviewer could not judge the layers it asked for. This walks from every wall face
into the mesh. A cell counts as a layer cell while

  - it is flat against the wall (height under FLAT_RATIO of its wall face's width),
  - its far face lies parallel to the face it was entered through (|cos| over PARALLEL_COS), and
  - it is no more than GROWTH_MAX times as tall as the layer cell below it

- the shape an extruded prism stack has and a background cell does not. The last rule matters: on
a cfMesh mesh the first octree cell past the stack can itself read flat and parallel (be014's
5-layer stack is followed by a cell at 0.59 of the face width, 3.4 times the last layer's height),
and without it a mesh with ONE layer could read as two.

It is reported the way snappy reports layers: the share of the requested layer cells that exist
(coverage_pct), per wall patch, plus area shares, the mean count, how the counts are spread, and the
first-layer height and stack thickness. The REQUESTED count is the user's (the request's "5 prism
layers", requested_layers_for) - not only what the mesher was told: the e2e meshes asked cfMesh for
2 where the request said 5, and "2.0 of 2 layers, 100%" hid "2 of 5".

On the HOME-TURF lab (2026-10-05) it read cfMesh's internal meshes at 99.9% of the wall area with a
layer and 98-99.6% with both requested layers. Until 2026-10-17 it refused every mesh over 4M cells
(the 5.5M-cell Circle of Willis among them), so the biggest meshes shipped with no layer figure at
all; the parsing and the face geometry are now lean enough for CENSUS_MAX_CELLS.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: a layer cell's height against the square root of its wall face's area
FLAT_RATIO = 0.6
#: |cos| between the far face and the wall face for the cell to be a layer
PARALLEL_COS = 0.8
#: a layer cell is at most this many times as tall as the layer cell below it: prism stacks grow
#: by their expansion ratio (1.1-1.5); the jump to the background cell is far larger
GROWTH_MAX = 2.0
#: meshes above this are not walked (time and memory where the mesh was built: a 6.7M-cell cfMesh
#: tee took 43 s and peaked at 5.5 GB, so 12M cells is about 10 GB of the mesh job's 32)
CENSUS_MAX_CELLS = 12_000_000
#: the deepest stack walked, whatever was asked
MAX_STACK = 30
#: CFDREQ-14: a wall face has the layer asked when its first cell is at most this many times the first layer the
#: mesher was given (the same 1.5 the resolved-wall target reads the first cell by)
PLAN_FIRST_TOL = 1.5

_ASCII = re.compile(rb"format\s+ascii\s*;")
_PARENS = bytes.maketrans(b"()", b"  ")


class NotMeasurable(ValueError):
    """The polyMesh cannot be walked: why, in a few words."""


def _body(path: Path) -> bytes:
    raw = path.read_bytes()
    if not _ASCII.search(raw[:4096]):
        raise NotMeasurable(f"{path.name} is not written in ascii")
    raw = re.sub(rb"/\*.*?\*/", b" ", raw[:200000], flags=re.S) + raw[200000:]
    i = raw.find(b"FoamFile")
    if i >= 0:
        raw = raw[raw.find(b"}", i) + 1:]
    return re.sub(rb"//[^\n]*", b" ", raw[:200000]) + raw[200000:]


def _list_start(t: bytes) -> tuple[int, int]:
    i = t.find(b"(")
    return int(t[:i].split()[-1]), i


def _numbers(t: bytes, dtype) -> np.ndarray:
    """Every number in the text, parsed in C (the parentheses read as spaces)."""
    return np.fromstring(t.translate(_PARENS), dtype=dtype, sep=" ")


def _labels(path: Path) -> np.ndarray:
    t = _body(path)
    n, i = _list_start(t)
    return _numbers(t[i + 1:t.rfind(b")")], np.int64)[:n]


def _points(path: Path) -> np.ndarray:
    t = _body(path)
    n, i = _list_start(t)
    return _numbers(t[i + 1:t.rfind(b")")], float)[: 3 * n].reshape(n, 3)


def _faces(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(offsets, vertex labels) of every face: face f is dat[offs[f]:offs[f+1]]."""
    t = _body(path)
    head = path.read_bytes()[:4096]
    if b"faceCompactList" in head:
        n_off, i = _list_start(t)
        j = t.find(b")", i)
        offs = _numbers(t[i + 1:j], np.int64)[:n_off]
        rest = t[j + 1:]
        n_dat, k = _list_start(rest)
        return offs, _numbers(rest[k + 1:rest.rfind(b")")], np.int64)[:n_dat]
    n, i = _list_start(t)
    nums = _numbers(t[i + 1:t.rfind(b")")], np.int64)
    # "k(v1 .. vk)" read as numbers is k v1 .. vk: walk the headers once
    heads = np.empty(n, dtype=np.int64)
    p, flat = 0, nums.tolist()
    for f in range(n):
        heads[f] = p
        p += flat[p] + 1
    del flat
    sizes = nums[heads]
    keep = np.ones(len(nums), dtype=bool)
    keep[heads] = False
    return np.concatenate([[0], np.cumsum(sizes)]), nums[keep]


def _boundary(path: Path) -> dict[str, tuple[int, int]]:
    t = _body(path).decode(errors="replace")
    out = {}
    for name, blk in re.findall(r"(\w[\w.:-]*)\s*\{([^}]*)\}", t):
        nf, sf = re.search(r"nFaces\s+(\d+)", blk), re.search(r"startFace\s+(\d+)", blk)
        if nf and sf:
            out[name] = (int(sf.group(1)), int(nf.group(1)))
    return out


class _Geometry:
    """Face centres and area vectors, worked out only for the faces asked about."""

    def __init__(self, P: np.ndarray, offs: np.ndarray, dat: np.ndarray) -> None:
        self.P, self.offs, self.dat = P, offs, dat

    def of(self, ids) -> tuple[np.ndarray, np.ndarray]:
        ids = np.asarray(ids, dtype=np.int64)
        fc = np.zeros((len(ids), 3))
        fa = np.zeros((len(ids), 3))
        sizes = self.offs[ids + 1] - self.offs[ids]
        for s in np.unique(sizes):
            rows = np.flatnonzero(sizes == s)
            pts = self.P[self.dat[self.offs[ids[rows]][:, None] + np.arange(s)[None, :]]]
            fc[rows] = pts.mean(axis=1)
            a = np.zeros((len(rows), 3))
            for k in range(1, int(s) - 1):
                a += 0.5 * np.cross(pts[:, k] - pts[:, 0], pts[:, k + 1] - pts[:, 0])
            fa[rows] = a
        return fc, fa


def _median(values: np.ndarray, weights: np.ndarray) -> float | None:
    if not len(values):
        return None
    order = np.argsort(values)
    c = np.cumsum(weights[order])
    return float(values[order][int(np.searchsorted(c, 0.5 * c[-1]))])


def _quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float | None:
    if not len(values):
        return None
    order = np.argsort(values)
    c = np.cumsum(weights[order])
    return float(values[order][min(len(values) - 1, int(np.searchsorted(c, q * c[-1])))])


def _far_face(owner, cstart, cf_face, geo, cells, centres, n0):
    """One step out from the wall: for each cell (entered through the face centred at `centres`, the wall's outward
    normal `n0`), its face farthest in from that face - (face, height, how parallel it is to the wall, its centre)."""
    cnt = cstart[cells + 1] - cstart[cells]
    kmax = int(cnt.max())
    slot = cstart[cells][:, None] + np.arange(kmax)[None, :]
    valid = np.arange(kmax)[None, :] < cnt[:, None]
    faces = cf_face[np.minimum(slot, len(cf_face) - 1)]
    uniq, inv = np.unique(faces, return_inverse=True)
    inv = inv.reshape(faces.shape)
    ufc, ufa = geo.of(uniq)
    d = np.einsum("ijk,ik->ij", ufc[inv] - centres[:, None, :], -n0)
    d = np.where(valid, d, -np.inf)
    j = np.argmax(d, axis=1)
    rows = np.arange(len(cells))
    nj = ufa[inv[rows, j]]
    nj = nj / np.maximum(np.linalg.norm(nj, axis=1), 1e-300)[:, None]
    par = np.abs((nj * n0).sum(axis=1))
    return faces[rows, j], d[rows, j], par, ufc[inv[rows, j]]


def census(polymesh, n_layers: int, patches, planned_first_m: float | None = None, *,
           plain_cell_m: float | None = None) -> tuple[dict, str]:
    """(per-patch figures, "") - or ({}, why) when the mesh cannot be measured. Figures per named
    wall patch: faces, area_m2, area_with_any_layer, area_with_full_layers, mean_layers (each face
    capped at `n_layers`), mean_layers_found (uncapped), coverage_pct, layers_by_area {count: area
    share}, first_layer_m and stack_m (area-weighted medians over the faces with a layer), growth
    (median ratio of the second layer's height to the first's), and the wall's FIRST CELL on every
    face, layer or not (first_cell_m; first_cell_p10_m and first_cell_p90_m).

    BY HEIGHT (CFDREQ-14): with `planned_first_m` (the first layer the mesher was given) a layer counts only
    where the wall's first cell is at most PLAN_FIRST_TOL times that height: area_at_plan (share of the wall
    with such a layer), coverage_at_plan_pct (the layer cells asked that stand on such a first layer) and
    first_vs_plan (the median first cell over the plan). Counted by shape alone, a cfMesh mesh asked a
    0.021 mm first layer under 5.9 mm cells read "layers on 54% of the wall" where its first cell was
    3.2 mm (static_mixer_004, resolved wall): a stack 150 times the height asked is not the layer asked.

    A STACK SIZED ON EACH FACE (CFDREQ-33): with `plain_cell_m` (the plain wall cell of a relative snappyHexMesh
    stack, planned_plain_cell_of) a face k levels finer than that cell (its width, to the nearest level) was planned
    a first layer 2^k times thinner, and is judged against that: the first cell 4 times thinner on a face 4 times
    finer is the plan, not a miss."""
    if n_layers <= 0:
        return {}, "no layers were asked for"
    mesh, why = _read_mesh(polymesh)
    if mesh is None:
        return {}, why
    owner, neigh, geo, bnd, cstart, cf_face, nI = mesh
    depth = min(MAX_STACK, int(n_layers) + 2)
    res = {}
    for name in patches:
        if name not in bnd:
            continue
        s0, nf = bnd[name]
        if nf == 0:
            continue
        wall = np.arange(s0, s0 + nf)
        wc, wa = geo.of(wall)
        A = np.linalg.norm(wa, axis=1)
        n0 = wa / np.maximum(A, 1e-300)[:, None]             # out of the domain
        width = np.sqrt(A)
        count = np.zeros(nf, dtype=np.int64)
        heights = np.zeros((nf, depth))
        first_cell = np.zeros(nf)                             # the wall's own first cell, layer or not
        first_par = np.zeros(nf)                              # how parallel its far face is to the wall
        cur_cell = owner[wall].copy()
        cur_c = wc.copy()                                     # centre of the face entered through
        prev_h = np.full(nf, np.inf)
        alive = np.ones(nf, dtype=bool)
        first_far = np.full(nf, -1, dtype=np.int64)          # the first cell's far face (CFDREQ-24)
        first_far_c = np.zeros((nf, 3))
        for step in range(depth):
            if not alive.any():
                break
            idx = np.flatnonzero(alive)
            best_f, h, par, far_c = _far_face(owner, cstart, cf_face, geo, cur_cell[idx], cur_c[idx], n0[idx])
            rows = np.arange(len(idx))
            if step == 0:
                first_cell[idx] = np.maximum(h, 0.0)
                first_par[idx] = par
                first_far[idx], first_far_c[idx] = best_f, far_c
            layer = ((h > 0) & (h < FLAT_RATIO * width[idx]) & (par > PARALLEL_COS)
                     & (h <= GROWTH_MAX * prev_h[idx]))
            hit = idx[layer]
            count[hit] += 1
            heights[hit, step] = h[layer]
            prev_h[hit] = h[layer]
            go = layer & (best_f < nI)
            alive[idx[~go]] = False
            nxt = idx[go]
            bf = best_f[go]
            o, nb = owner[bf], neigh[bf]
            cur_cell[nxt] = np.where(o == cur_cell[nxt], nb, o)
            cur_c[nxt] = far_c[rows[go]]
        capped = np.minimum(count, n_layers)
        total = float(A.sum())
        with_any = count >= 1
        two = count >= 2
        spread = {int(k): round(float(A[count == k].sum() / total), 4)
                  for k in range(0, int(count.max()) + 1) if (count == k).any()}
        res[name] = {
            "faces": int(nf), "area_m2": total,
            "area_with_any_layer": round(float(A[with_any].sum() / total), 4),
            "area_with_full_layers": round(float(A[count >= n_layers].sum() / total), 4),
            "mean_layers": round(float((capped * A).sum() / total), 3),
            "mean_layers_found": round(float((count * A).sum() / total), 3),
            "coverage_pct": round(float(100.0 * capped.sum() / (n_layers * nf)), 2),
            "layers_by_area": spread,
            "first_layer_m": _median(heights[with_any, 0], A[with_any]),
            "stack_m": _median(heights[with_any].sum(axis=1), A[with_any]),
            "growth": _median(heights[two, 1] / np.maximum(heights[two, 0], 1e-300), A[two]),
            "first_cell_m": _median(first_cell, A),
            "first_cell_p10_m": _quantile(first_cell, A, 0.10),
            "first_cell_p90_m": _quantile(first_cell, A, 0.90),
        }
        if planned_first_m and planned_first_m > 0.0:
            plan = np.full(nf, float(planned_first_m))
            if plain_cell_m and plain_cell_m > 0.0:
                finer = np.clip(np.round(np.log2(float(plain_cell_m) / np.maximum(width, 1e-300))), 0, None)
                plan = plan / 2.0 ** finer
            tall_ok = first_cell <= PLAN_FIRST_TOL * plan
            # CFDREQ-21: ONE LAYER AT THE HEIGHT ASKED IS TOLD BY ITS HEIGHT. A wall-function layer is built about
            # half the wall cell high (cfMesh's whole stack; snappyHexMesh's cap 0.6): past FLAT_RATIO of a face's
            # width on faces narrower than the cell, so the shape test read a single full layer as 0-3% of the wall
            # while its first cell sat at y+ 41-63 (yplab_14f21bdd). With a plan, a first cell at the planned height,
            # its far face parallel to the wall and no taller than the face is wide, is that layer
            one = (first_cell > 0) & (first_par > PARALLEL_COS) & (first_cell < width) & tall_ok
            at_plan = (with_any | one) & tall_ok
            capped_p = np.minimum(np.where(one & (count == 0), 1, count), n_layers)
            med = _median(first_cell, A)
            # CFDREQ-24: A FIRST CELL AT THE PLANNED HEIGHT, AND THE CELL ABOVE IT. A wall-function layer as tall as
            # its wall faces are wide fails both shape tests, so a 1.92 mm layer built exactly at plan read "layers
            # on 0% of the wall" (yplab_e11_7821cb5b, three snappyHexMesh parts) - and hid what was wrong there: the
            # cells above it were 0.35 mm, a jump of five times going away from the wall. With a plan, every face
            # whose first cell is at the planned height (within PLAN_FIRST_TOL either way, its far face parallel) is
            # read with the cell next out: under a cell more than GROWTH_MAX times thinner it stands under a jump
            at_height = ((first_cell > 0) & (first_par > PARALLEL_COS) & tall_ok
                         & (first_cell >= plan / PLAN_FIRST_TOL))
            ratio = np.full(nf, np.nan)
            look = np.flatnonzero((at_height | at_plan) & (first_far >= 0) & (first_far < nI))
            if len(look):
                bf = first_far[look]
                o, nb = owner[bf], neigh[bf]
                beyond = np.where(o == owner[wall][look], nb, o)
                _f, h2, _p, _c = _far_face(owner, cstart, cf_face, geo, beyond, first_far_c[look], n0[look])
                ratio[look] = np.maximum(h2, 0.0) / np.maximum(first_cell[look], 1e-300)
            jump = (at_height | at_plan) & np.isfinite(ratio) & (ratio < 1.0 / GROWTH_MAX)
            # the share: CFDREQ-21's layers at plan, and a first cell at the planned height taller than its faces are
            # wide - each only where no jump sits above it. CFDREQ-24: a layer at plan under a cell more than
            # GROWTH_MAX times thinner is not the wall-function cell, whatever its shape (yplab_wf_0e83ed87: a part
            # read the y+ met with 71% of its wall under a 3.3x jump); the jump is said, never counted as the layer
            y_share = (at_height | at_plan) & ~jump
            seen = np.isfinite(ratio) & (at_height | at_plan)
            if plain_cell_m:
                res[name]["planned_plain_cell_m"] = float(plain_cell_m)
            res[name].update({
                "planned_first_m": float(planned_first_m),
                "area_at_plan": round(float(A[at_plan].sum() / total), 4),
                "coverage_at_plan_pct": round(float(100.0 * capped_p[at_plan].sum() / (n_layers * nf)), 2),
                "first_vs_plan": (None if med is None else round(float(med) / float(planned_first_m), 2)
                                  if not plain_cell_m else round(float(_median(first_cell / plan, A) or 0.0), 2)),
                "area_first_at_plan": round(float(A[y_share].sum() / total), 4),
                "area_under_jump": round(float(A[jump].sum() / total), 4),
                "next_over_first": (round(nof, 3) if (nof := _median(ratio[seen], A[seen])) is not None else None),
            })
    return res, ""


def _read_mesh(polymesh) -> tuple[tuple | None, str]:
    """The polyMesh read for a walk out from the wall: ((owner, neighbour, geometry, boundary, cell -> face
    starts, the faces in cell order, internal face count), "") - or (None, why) when it cannot be read."""
    pm = Path(polymesh)
    try:
        owner = _labels(pm / "owner")
        n_cells = int(owner.max()) + 1 if len(owner) else 0
        if n_cells > CENSUS_MAX_CELLS:
            return None, f"{n_cells:,} cells, above the census limit of {CENSUS_MAX_CELLS:,}"
        neigh = _labels(pm / "neighbour")
        P = _points(pm / "points")
        offs, dat = _faces(pm / "faces")
        bnd = _boundary(pm / "boundary")
    except NotMeasurable as exc:
        return None, str(exc)
    except (OSError, ValueError, IndexError) as exc:
        return None, f"the polyMesh could not be read ({type(exc).__name__})"
    geo = _Geometry(P, offs, dat)
    # cfMesh writes a neighbour entry (-1) for every boundary face too: the internal faces are the
    # ones before the first patch, never the length of that list
    starts = [s for s, n in bnd.values() if n > 0]
    if starts and len(neigh) > min(starts):
        neigh = neigh[:min(starts)]
    nF, nI = len(offs) - 1, len(neigh)
    n_cells = max(n_cells, int(neigh.max()) + 1 if nI else 0)
    cf_cell = np.concatenate([owner, neigh])
    cf_face = np.concatenate([np.arange(nF), np.arange(nI)])
    order = np.argsort(cf_cell, kind="stable")
    cf_cell, cf_face = cf_cell[order], cf_face[order]
    del order
    cstart = np.searchsorted(cf_cell, np.arange(n_cells + 1))
    del cf_cell
    return (owner, neigh, geo, bnd, cstart, cf_face, nI), ""


#: CFDREQ-22: the most cells a walk across a boundary layer counts (well past the 10 a resolved one is planned with)
BL_WALK_MAX = 40


def bl_cells(polymesh, patches, delta_m: float, *, met_at: float) -> tuple[dict, str]:
    """CFDREQ-22: THE CELLS ACROSS A BOUNDARY LAYER, COUNTED ON THE BUILT MESH. From every face of the wall
    `patches`, cell after cell straight out (each cell's far face along the wall's normal, the walk census() makes
    up a layer stack) - layer cells and core cells alike - the whole cells under `delta_m` and the share of the one
    its edge cuts; a walk that meets another boundary first counts what it crossed. Summed over the patches by area:
    ({"bl_delta_m", "bl_cells_median", "bl_cells_p10", "bl_area_met_pct" (the share of the wall with at least
    `met_at` cells across), "bl_faces"}, "") - or ({}, why) when the mesh cannot be read or has no such wall."""
    delta = float(delta_m)
    if not delta > 0.0:
        return {}, "no boundary layer thickness"
    mesh, why = _read_mesh(polymesh)
    if mesh is None:
        return {}, why
    owner, neigh, geo, bnd, cstart, cf_face, nI = mesh
    ks, As = [], []
    for name in patches:
        if name not in bnd or bnd[name][1] == 0:
            continue
        s0, nf = bnd[name]
        wall = np.arange(s0, s0 + nf)
        wc, wa = geo.of(wall)
        A = np.linalg.norm(wa, axis=1)
        n0 = wa / np.maximum(A, 1e-300)[:, None]             # out of the domain
        k, z = np.zeros(nf), np.zeros(nf)
        cur_cell, cur_c = owner[wall].copy(), wc.copy()
        alive = np.ones(nf, dtype=bool)
        for _ in range(BL_WALK_MAX):
            if not alive.any():
                break
            idx = np.flatnonzero(alive)
            cells = cur_cell[idx]
            cnt = cstart[cells + 1] - cstart[cells]
            kmax = int(cnt.max())
            slot = cstart[cells][:, None] + np.arange(kmax)[None, :]
            valid = np.arange(kmax)[None, :] < cnt[:, None]
            faces = cf_face[np.minimum(slot, len(cf_face) - 1)]
            uniq, inv = np.unique(faces, return_inverse=True)
            inv = inv.reshape(faces.shape)
            ufc, _ufa = geo.of(uniq)
            d = np.einsum("ijk,ik->ij", ufc[inv] - cur_c[idx][:, None, :], -n0[idx])
            d = np.where(valid, d, -np.inf)
            j = np.argmax(d, axis=1)
            rows = np.arange(len(idx))
            best_f, h = faces[rows, j], d[rows, j]
            step = h > 0
            reach = step & (z[idx] + h >= delta)
            k[idx] += np.where(reach, (delta - z[idx]) / np.maximum(h, 1e-300), np.where(step, 1.0, 0.0))
            z[idx] += np.where(step, h, 0.0)
            go = step & ~reach & (best_f < nI)
            alive[idx[~go]] = False
            nxt, bf = idx[go], best_f[go]
            o, nb = owner[bf], neigh[bf]
            cur_cell[nxt] = np.where(o == cur_cell[nxt], nb, o)
            cur_c[nxt] = ufc[inv[rows[go], j[go]]]
        ks.append(k)
        As.append(A)
    if not ks:
        return {}, "no wall faces"
    k, A = np.concatenate(ks), np.concatenate(As)
    total = float(A.sum()) or 1.0
    return {"bl_delta_m": delta, "bl_cells_median": round(float(_median(k, A) or 0.0), 2),
            "bl_cells_p10": round(float(_quantile(k, A, 0.10) or 0.0), 2),
            "bl_area_met_pct": round(float(100.0 * A[k >= float(met_at)].sum() / total), 2),
            "bl_faces": int(len(k))}, ""


#: where each mesher's case carries the first layer height it was given, and the key (CFDREQ-6's reading too)
PLANNED_FIRST_IN = (("system/snappyHexMeshDict", "firstLayerThickness"), ("system/meshDict", "maxFirstLayerThickness"))


#: CFDREQ-33: the comment a relative snappyHexMesh stack sized on the plain wall cell carries in its dictionary
#: (cfd_requirements writes it): "/* plainWallCell <metres>; */" - the cell its firstLayerThickness is a share of
PLAIN_CELL_KEY = "plainWallCell"


def planned_plain_cell_of(workspace) -> float | None:
    """CFDREQ-33: the plain wall cell (metres) a relative snappyHexMesh stack was sized on, or None (an absolute
    stack, another mesher, or a relative stack the plan did not size on a height)."""
    try:
        text = (Path(workspace) / "system" / "snappyHexMeshDict").read_text(errors="replace")
    except OSError:
        return None
    if not re.search(r"relativeSizes\s+(true|on|yes)\s*;", text):
        return None
    m = re.search(rf"\b{PLAIN_CELL_KEY}\s+([-+\d.eE]+)\s*;", text)
    try:
        v = float(m.group(1)) if m else 0.0
    except ValueError:
        return None
    return v if v > 0.0 else None


def planned_first_of(workspace) -> float | None:
    """The first layer height the mesher's dictionary was given (metres), or None when it was given none. A
    snappyHexMesh case with relativeSizes on reads None (its height is a share of each cell, not a height) -
    unless the plan sized it on a plain wall cell (CFDREQ-33): then the height on that cell, the share times it."""
    ws = Path(workspace)
    for rel, key in PLANNED_FIRST_IN:
        try:
            text = (ws / rel).read_text(errors="replace")
        except OSError:
            continue
        plain = None
        if key == "firstLayerThickness" and re.search(r"relativeSizes\s+(true|on|yes)\s*;", text):
            plain = planned_plain_cell_of(ws)
            if plain is None:
                return None
        m = re.search(rf"\b{key}\s+([-+\d.eE]+)\s*;", text)
        if m:
            try:
                v = float(m.group(1)) * (plain or 1.0)
            except ValueError:
                return None
            return v if v > 0.0 else None
    return None


def height_quality(per: dict) -> dict:
    """CFDREQ-14: the quality record's figures for the wall's first cell against the planned first layer, from
    census() with a plan: the layer coverage counted at that height (layer_coverage_pct, with the count by shape
    kept as layer_coverage_shape_pct), the first cell (median), the plan, their ratio and the share of the wall
    at the plan. {} when the census had no plan."""
    s = wall_summary(per)
    if "planned_first_m" not in s:
        return {k: v for k, v in (("layer_first_cell_m", s.get("first_cell_m")),) if v is not None}
    out = {"layer_coverage_basis": "height", "layer_first_planned_m": s["planned_first_m"],
           "layer_area_at_plan_pct": s["area_at_plan_pct"], "layer_coverage_at_plan_pct": s["coverage_at_plan_pct"]}
    if s.get("first_cell_m") is not None:
        out["layer_first_cell_m"] = s["first_cell_m"]
        out["layer_first_vs_plan"] = s.get("first_vs_plan")
    for k in ("area_first_at_plan_pct", "area_under_jump_pct", "next_over_first"):
        if s.get(k) is not None:
            out[f"layer_{k}"] = s[k]
    return out


def wall_summary(per: dict) -> dict:
    """The figures of census() over all its wall patches, weighted by area (faces for the coverage): the
    first cell (median of the patches' medians by area), and, with a plan, the share of the wall at it,
    the coverage there and the first cell over the plan. {} when nothing was measured."""
    rows = [v for v in (per or {}).values() if isinstance(v, dict) and v.get("area_m2")]
    if not rows:
        return {}
    area = sum(float(v["area_m2"]) for v in rows) or 1.0
    faces = sum(int(v["faces"]) for v in rows) or 1
    meds = [v for v in rows if v.get("first_cell_m") is not None]
    out: dict = {}
    if meds:
        meds.sort(key=lambda v: float(v["first_cell_m"]))
        c, half = 0.0, 0.5 * sum(float(v["area_m2"]) for v in meds)
        for v in meds:
            c += float(v["area_m2"])
            if c >= half:
                out["first_cell_m"] = float(v["first_cell_m"])
                break
    planned = [v for v in rows if v.get("planned_first_m")]
    if planned:
        p = float(planned[0]["planned_first_m"])
        out["planned_first_m"] = p
        out["area_at_plan_pct"] = round(100.0 * sum(float(v.get("area_at_plan") or 0.0) * float(v["area_m2"])
                                                    for v in planned) / area, 2)
        out["coverage_at_plan_pct"] = round(sum(float(v.get("coverage_at_plan_pct") or 0.0) * int(v["faces"])
                                                for v in planned) / faces, 2)
        if out.get("first_cell_m") is not None:
            out["first_vs_plan"] = round(out["first_cell_m"] / p, 2)
        # CFDREQ-24: the wall share at the planned height with no jump to the cell above, the share under one, and
        # the cell above over the first cell (the largest patch's median)
        if all("area_first_at_plan" in v for v in planned):
            for k in ("area_first_at_plan", "area_under_jump"):
                out[f"{k}_pct"] = round(100.0 * sum(float(v.get(k) or 0.0) * float(v["area_m2"])
                                                    for v in planned) / area, 2)
            ratios = [v for v in planned if v.get("next_over_first") is not None]
            if ratios:
                out["next_over_first"] = float(max(ratios, key=lambda v: float(v["area_m2"]))["next_over_first"])
    return out


def layer_census(polymesh, n_layers: int, patches) -> dict:
    """census() without the reason: {} when the mesh cannot be measured or no layers were asked."""
    out, why = census(polymesh, n_layers, patches)
    if why:
        logger.info("layer census: not measured - %s", why)
    return out


def requested_layers(meshdict) -> int:
    """nLayers asked for in a cfMesh meshDict (the largest, if several); 0 when none."""
    try:
        text = Path(meshdict).read_text(errors="replace")
    except OSError:
        return 0
    found = [int(n) for n in re.findall(r"nLayers\s+(\d+)\s*;", text)]
    return max(found) if found else 0


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}
_NUM = r"\b(\d{1,2}|" + "|".join(_WORDS) + r")"
#: a layer word near a bare "N layers": only then is it a near-wall layer count
_LAYER_CONTEXT = re.compile(r"prism|boundary|inflation|near[- ]wall|y\+|\bwall", re.I)
_STATED = (
    # "5 prism layers", "five boundary layers", "10 inflation layers", "5 near-wall layers"
    # "5 prism boundary layers", "five wall boundary layers": up to three layer words in a row
    re.compile(_NUM + r"\s+(?:(?:prism(?:atic)?|boundary|inflation|near[- ]wall|wall)[\s-]*){1,3}"
               r"layers?\b", re.I),
    # "5 layers" - counted only with a layer word around it
    re.compile(_NUM + r"\s+layers\b", re.I),
    # "n_layers = 5", "nLayers 5"
    re.compile(r"\bn_?layers\s*[:=]?\s*(\d{1,2})\b", re.I),
)


def stated_layers(text: str) -> int:
    """The number of near-wall layers a request states ("Resolve the near-wall flow with 5 prism
    layers"), or 0 when it states none. The largest stated count wins: a request that asks for
    "5 prism layers" is not met by 2."""
    text = str(text or "")
    found: list[int] = []
    for i, rx in enumerate(_STATED):
        for m in rx.finditer(text):
            if i == 1 and not _LAYER_CONTEXT.search(text[max(0, m.start() - 80):m.end() + 80]):
                continue
            tok = m.group(1).lower()
            n = _WORDS[tok] if tok in _WORDS else int(tok)
            if 0 < n <= 50:
                found.append(n)
    return max(found) if found else 0


def requested_layers_for(workspace) -> tuple[int, int, int]:
    """(target, stated in the request, written in the meshDict) for a cfMesh workspace. The target
    is the request's count when it states one, else the meshDict's: what the user asked for is
    what the delivered layers are measured against."""
    from meshpipeline.engines.workspace_facts import read_request_txt

    ws = Path(workspace)
    stated = stated_layers(read_request_txt(ws))
    in_dict = requested_layers(ws / "system" / "meshDict")
    return (stated or in_dict), stated, in_dict


__all__ = ["CENSUS_MAX_CELLS", "GROWTH_MAX", "PLAIN_CELL_KEY", "PLANNED_FIRST_IN", "PLAN_FIRST_TOL", "census",
           "height_quality", "layer_census", "planned_first_of", "planned_plain_cell_of", "requested_layers", "requested_layers_for", "stated_layers",
           "wall_summary"]
