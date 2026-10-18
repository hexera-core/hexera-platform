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


def census(polymesh, n_layers: int, patches) -> tuple[dict, str]:
    """(per-patch figures, "") - or ({}, why) when the mesh cannot be measured. Figures per named
    wall patch: faces, area_m2, area_with_any_layer, area_with_full_layers, mean_layers (each face
    capped at `n_layers`), mean_layers_found (uncapped), coverage_pct, layers_by_area {count: area
    share}, first_layer_m and stack_m (area-weighted medians over the faces with a layer), growth
    (median ratio of the second layer's height to the first's)."""
    pm = Path(polymesh)
    if n_layers <= 0:
        return {}, "no layers were asked for"
    try:
        owner = _labels(pm / "owner")
        n_cells = int(owner.max()) + 1 if len(owner) else 0
        if n_cells > CENSUS_MAX_CELLS:
            return {}, f"{n_cells:,} cells, above the census limit of {CENSUS_MAX_CELLS:,}"
        neigh = _labels(pm / "neighbour")
        P = _points(pm / "points")
        offs, dat = _faces(pm / "faces")
        bnd = _boundary(pm / "boundary")
    except NotMeasurable as exc:
        return {}, str(exc)
    except (OSError, ValueError, IndexError) as exc:
        return {}, f"the polyMesh could not be read ({type(exc).__name__})"
    geo = _Geometry(P, offs, dat)
    nF, nI = len(offs) - 1, len(neigh)
    n_cells = max(n_cells, int(neigh.max()) + 1 if nI else 0)
    cf_cell = np.concatenate([owner, neigh])
    cf_face = np.concatenate([np.arange(nF), np.arange(nI)])
    order = np.argsort(cf_cell, kind="stable")
    cf_cell, cf_face = cf_cell[order], cf_face[order]
    del order
    cstart = np.searchsorted(cf_cell, np.arange(n_cells + 1))
    del cf_cell
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
        cur_cell = owner[wall].copy()
        cur_c = wc.copy()                                     # centre of the face entered through
        prev_h = np.full(nf, np.inf)
        alive = np.ones(nf, dtype=bool)
        for step in range(depth):
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
            ufc, ufa = geo.of(uniq)
            d = np.einsum("ijk,ik->ij", ufc[inv] - cur_c[idx][:, None, :], -n0[idx])
            d = np.where(valid, d, -np.inf)
            j = np.argmax(d, axis=1)
            rows = np.arange(len(idx))
            best_f, h = faces[rows, j], d[rows, j]
            nj = ufa[inv[rows, j]]
            nj = nj / np.maximum(np.linalg.norm(nj, axis=1), 1e-300)[:, None]
            par = np.abs((nj * n0[idx]).sum(axis=1))
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
            cur_c[nxt] = ufc[inv[rows[go], j[go]]]
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
        }
    return res, ""


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
    re.compile(_NUM + r"\s+(?:prism(?:atic)?|boundary|inflation|near[- ]wall|wall)[\s-]*layers?\b", re.I),
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


__all__ = ["CENSUS_MAX_CELLS", "GROWTH_MAX", "census", "layer_census", "requested_layers",
           "requested_layers_for", "stated_layers"]
