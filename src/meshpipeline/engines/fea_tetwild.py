# Responsibility: The FEA ladder's LAST RUNG (LAD-fea-9): fTetWild meshes the part(s) every gmsh option left with no valid mesh - its parameters derived from the plan and the measured part, its tetrahedra made quadratic, its boundary faces mapped back to the CAD's named faces by nearest surface, and how far its surface sits from the CAD measured and said.
# Boundaries: the driver side (rung_parts, params, words) is pure over the plan, the record and the passes; the runner side (mesh_into) runs inside the gmsh FEA adapter's process, runs the pinned fTetWild binary one part at a time in its own process (one thread, a seeded insertion order: the same input gives the same mesh), and hands the adapter's own checks, gates and writers the same gmsh model every other FEA mesh is read from. It decides no lever and never runs before gmsh's options are exhausted (engines/fea_driver.py decides that).
# Collaborates with: engines/fea_driver.py (fires the rung), engines/fea_ladder.py (LAD-fea-9), engines/gmsh/fea_adapter.py (meshes the rest with gmsh, then writes and measures everything), engines/fea_size_field.py (the plan's sizes as fTetWild's sizing field), cad/fea_measure.py (the CAD tessellation), Dockerfile (the pinned fTetWild build).
"""THE LAST RUNG: fTetWild (LAD-fea-9).

gmsh's options come first and stay as they are - Delaunay, then HXT, MeshAdapt on the surfaces that
failed, and for an assembly the tie (PLAN-fea-17). Only when every one of them has left a part with no
valid mesh does fTetWild (Hu et al., "Fast Tetrahedral Meshing in the Wild", SIGGRAPH 2020; MPL-2.0)
mesh THAT part: it fills the region a surface encloses within an envelope round it, so a surface gmsh
cannot recover as a boundary (faces that cross or touch, a fill that crashes) still gives a valid
tetrahedral mesh. The other parts keep their gmsh mesh and are tied to it as PLAN-fea-17 ties parts.

WHAT IS FIXED, AND WHY (never guessed):
* the ideal edge length is the plan's base size for the part (PLAN-fea-14 / PLAN-fea-17), and the
  plan's zones are fTetWild's sizing field (its background mesh), so the mesh is refined where the plan
  refines it;
* the envelope (epsilon): fTetWild's own default, a thousandth of the part's diagonal - never more than
  a tenth of the part's thinnest measured wall (a thinner wall must keep its section), never less than
  EPS_FLOOR_SHARE of the diagonal (the run time grows as the envelope shrinks);
* the CAD is tessellated within half the envelope, so the mesh's surface is within 1.5 envelopes of the
  CAD itself;
* one thread, and the insertion order's shuffle seeded by a constant (the image's build patches the
  hardware seed upstream uses): the same input gives the same mesh, byte for byte.

THE MESH: fTetWild's tetrahedra (straight), made quadratic by a node at the middle of every edge - a
straight 10-node tetrahedron has the Jacobian of its 4-node one, so every element valid stays valid.
Its boundary triangles are mapped to the CAD face nearest to each (Part_k_Face_j / Face_k), so every
load and support still has its face; a face too small to keep a triangle of its own is said.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

ENGINE = "ftetwild"
#: THE PINNED BUILD (Dockerfile ARG FTETWILD_COMMIT): wildmeshing/fTetWild at this commit, MPL-2.0.
COMMIT = "8118f810478e0e65a7bf2d8cecdc5e203a876e97"
VERSION = f"fTetWild {COMMIT[:12]}"
#: where the mesh image installs it (and the name on PATH)
BIN_NAME = "fTetWild"
BIN_PATH = "/opt/ftetwild/bin/fTetWild"
#: ONE THREAD: TBB's parallel optimisation is not repeatable
THREADS = 1
#: the envelope: fTetWild's own default share of the diagonal ...
EPS_DIAG_SHARE = 1e-3
#: ... never more than this share of the thinnest measured wall ...
EPS_WALL_SHARE = 0.1
#: ... never less than this share of the diagonal (below it the run time grows past use)
EPS_FLOOR_SHARE = 2e-5
#: the CAD is tessellated within this share of the envelope
TESSELLATION_SHARE = 0.5
#: the tessellation's angular deflection (radians)
ANGULAR_DEFLECTION = 0.5
#: fTetWild's own optimisation stop (its default): the worst conformal AMIPS energy
STOP_ENERGY = 10.0
#: the sizing field's background mesh: this many cells along the part's longest side
BG_DIVISIONS = 40
#: fTetWild's log level (2 = its info lines: progress the runner's watchdog reads)
LOG_LEVEL = 2
#: at most this many rung passes: the failing parts, then (if that pass fails too) every part
MAX_RUNG_PASSES = 2
#: how many triangles the nearest-face search splits the CAD surface into, at most
MAX_SEARCH_TRIANGLES = 3_000_000
#: a face's surface counts as MOVED when it is further than this share of the envelope from the CAD
MOVED_SHARE = 0.5
#: the line the driver reads which parts went to fTetWild
RUNG_LINE = "[FTETWILD]"


def binary() -> str | None:
    """The fTetWild binary the mesh image ships, or None where it is not installed."""
    found = shutil.which(BIN_NAME)
    if found:
        return found
    return BIN_PATH if Path(BIN_PATH).exists() else None


# #
# the driver's side: which parts, with what (pure)
# #

def failed_parts(passes: list[dict]) -> list[int]:
    """The assembly parts the failed passes named (their bad faces, Part_k_Face_j), sorted."""
    from meshpipeline.cad.fea_assembly import part_of_face

    out: set[int] = set()
    for p in passes:
        for f in p.get("bad_faces") or []:
            k = part_of_face(str(f))
            if k is not None:
                out.add(int(k))
    return sorted(out)


def rung_parts(record: Mapping, passes: list[dict], rung: int) -> list[int] | None:
    """The parts the rung's pass `rung` (1, 2) gives to fTetWild: None = every part (a single part is
    always whole). The first pass takes only the assembly parts the failed passes named, when they
    named some; the second (the first failed too), or a first with none named, takes every part."""
    if int(record.get("bodies") or 1) <= 1 or rung > 1:
        return None
    named = failed_parts(passes)
    return named or None


def _part_faces(plan: Mapping, part: int | None) -> list[dict]:
    from meshpipeline.cad.fea_assembly import part_of_face

    faces = list(plan.get("faces") or [])
    if part is None:
        return faces
    return [f for f in faces if part_of_face(str(f.get("name"))) == part]


def _part_base(plan: Mapping, part: int | None) -> float:
    """The part's base size (PLAN-fea-17: its own zone of kind "part"), else the plan's base."""
    base = float(plan["base_size_m"])
    if part is None:
        return base
    for z in plan.get("zones") or []:
        if z.get("part") == f"Part_{part}" or z.get("part") == part:
            return min(base, float(z["size_m"]))
    return base


#: LAD-fea-10: the rule that holds the rung's ideal edge to the part's own size
EDGE_RULE = "LAD-fea-10"


def rung_edge(plan: Mapping, part: int | None, diag_m: float) -> float:
    """LAD-fea-10: fTetWild's ideal edge for a part - its base (PLAN-fea-17), never longer than the part's own
    diagonal. A plan coarsened to fit (PLAN-fea-8) floors every part's base at its zone floor, and on a big
    assembly that floor passes the small parts' own size (ox_mc_lid: 61 mm on parts of 3 to 20 mm). gmsh is
    held there by the parts' face zones; fTetWild, asked for edges twenty times its part with an envelope a
    thousandth of it, ran past 37 GB and an hour on a 3.2 mm part (Part_64), and past 41 GB and two hours on
    a 6 mm one (lab, 26 Oct). Held to the part's diagonal: 0.21 GB, 4.4 minutes. A part whose base is within
    its own size (every part of a plan not coarsened that far) is unchanged."""
    base = _part_base(plan, part)
    return min(base, float(diag_m)) if diag_m and diag_m > 0 else base


def _part_diag_m(record: Mapping, diag_m: float, part: int | None) -> float:
    if part is not None:
        for b in record.get("part_bases") or []:
            if int(b.get("n") or 0) == part and b.get("diag_mm"):
                return float(b["diag_mm"]) * 1e-3
    return float(diag_m)


def envelope_m(diag_m: float, thinnest_m: float | None) -> float:
    """fTetWild's envelope for a part (see the module's words): its default share of the diagonal,
    never more than a tenth of the thinnest wall, never less than the floor."""
    eps = EPS_DIAG_SHARE * diag_m
    if thinnest_m and thinnest_m > 0:
        eps = min(eps, EPS_WALL_SHARE * thinnest_m)
    return max(eps, EPS_FLOOR_SHARE * diag_m)


def params(plan: Mapping, record: Mapping, diag_m: float, parts: list[int] | None) -> dict:
    """The rung's settings, every number from the plan and the measured part (plan["mesh"]["ftetwild"])."""
    assembly = int(record.get("bodies") or 1) > 1
    every = [int(b["n"]) for b in record.get("part_bases") or []] if assembly else []
    chosen = list(parts) if parts is not None else (every or [None])  # type: ignore[list-item]
    per: dict[str, dict] = {}
    for p in chosen:
        faces = _part_faces(plan, p)
        walls = [float(f["thickness_min_mm"]) * 1e-3 for f in faces if f.get("thickness_min_mm")]
        thinnest = min(walls) if walls else None
        d = _part_diag_m(record, diag_m, p)
        eps = envelope_m(d, thinnest)
        per["whole" if p is None else f"Part_{p}"] = {
            "edge_length_m": _sig(rung_edge(plan, p, d)), "epsilon_m": _sig(eps), "diag_m": _sig(d),
            "thinnest_wall_m": _sig(thinnest) if thinnest else None,
            "tessellation_m": _sig(TESSELLATION_SHARE * eps),
            **({"edge_rule": EDGE_RULE, "plan_base_m": _sig(_part_base(plan, p))}
               if rung_edge(plan, p, d) < _part_base(plan, p) else {})}
    return {"engine": ENGINE, "version": VERSION, "threads": THREADS, "stop_energy": STOP_ENERGY,
            "parts": None if parts is None or not assembly else sorted(int(p) for p in parts),
            "per_part": per, "rule": "LAD-fea-9",
            "basis": {"eps_diag_share": EPS_DIAG_SHARE, "eps_wall_share": EPS_WALL_SHARE,
                      "eps_floor_share": EPS_FLOOR_SHARE, "tessellation_share": TESSELLATION_SHARE,
                      "sizing": "the plan's zones (fea_size_field) as fTetWild's background mesh"}}


def _sig(v: float | None, digits: int = 6) -> float | None:
    if v is None:
        return None
    v = float(v)
    if v == 0 or not math.isfinite(v):
        return v
    return round(v, digits - 1 - int(math.floor(math.log10(abs(v)))))


def words(info: Mapping) -> str:
    """The delivery's note (quality.json fea.ftetwild): which parts fTetWild meshed, how close its surface
    is to the CAD, and which faces it affected."""
    parts = info.get("parts") or []
    who = ("the part" if not parts or parts == ["whole"] else
           ("part " if len(parts) == 1 else "parts ") + ", ".join(str(p) for p in parts[:8])
           + (", ..." if len(parts) > 8 else ""))
    dev = info.get("deviation_mm")
    eps = info.get("epsilon_mm")
    said = [f"gmsh could not build a valid mesh of {who}, so fTetWild meshed it (the last rung, LAD-fea-9)"
            + ("; the other parts are gmsh's, tied to it where they touch" if info.get("tied") else "") + "."]
    if dev is not None and eps is not None:
        said.append(f"fTetWild builds its surface inside an envelope of ±{_mm(eps)} round the CAD's; measured on "
                    f"the mesh, its furthest point is {_mm(dev)} from the CAD"
                    + (" (on the faces listed next)" if dev > eps and info.get("moved_faces") else "")
                    + ".")
    moved = info.get("moved_faces") or []
    lost = info.get("lost_faces") or []
    if moved:
        said.append(f"Faces where the surface moved by more than half the envelope: {_names(moved)}.")
    if lost:
        said.append(f"Faces too small to keep an element face of their own (not in the solver files): "
                    f"{_names(lost)} - their loads belong on the faces round them.")
    if not moved and not lost:
        said.append("Every face kept its name; loads and supports apply to the same faces.")
    return " ".join(said)


def _mm(v: float) -> str:
    v = float(v)
    return f"{v:.3g} mm" if v >= 0.01 else f"{v * 1000:.3g} µm"


def _names(names: list[str]) -> str:
    return ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")


# #
# geometry: the CAD's triangles, the nearest face, quadratic tetrahedra (pure numpy)
# #

def closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """The closest point of each triangle (a, b, c) to each point p (rows paired) - Ericson's regions."""
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    out = np.empty_like(p)
    done = np.zeros(len(p), dtype=bool)

    def put(mask, value):
        nonlocal done
        m = mask & ~done
        out[m] = value[m] if value.ndim == 2 else value
        done |= m

    with np.errstate(divide="ignore", invalid="ignore"):
        put((d1 <= 0) & (d2 <= 0), a)
        put((d3 >= 0) & (d4 <= d3), b)
        put((d6 >= 0) & (d5 <= d6), c)
        v = np.nan_to_num(d1 / (d1 - d3))
        put((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + v[:, None] * ab)
        w = np.nan_to_num(d2 / (d2 - d6))
        put((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + w[:, None] * ac)
        w2 = np.nan_to_num((d4 - d3) / ((d4 - d3) + (d5 - d6)))
        put((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), b + w2[:, None] * (c - b))
        den = va + vb + vc
        den = np.where(den == 0, 1.0, den)
        put(np.ones(len(p), dtype=bool), a + (vb / den)[:, None] * ab + (vc / den)[:, None] * ac)
    return out


def split_long(xyz: np.ndarray, tris: np.ndarray, label: np.ndarray, spacing: float,
               cap: int = MAX_SEARCH_TRIANGLES) -> tuple[np.ndarray, np.ndarray]:
    """The triangles as a soup ((T, 3, 3) corners, (T,) labels) split 1 -> 4 until no edge is longer
    than `spacing` (or the cap is reached): a nearest-face search over small triangles needs only
    their nearest centroids."""
    corners = np.asarray(xyz, dtype=float)[np.asarray(tris, dtype=np.int64)]
    lab = np.asarray(label)
    for _ in range(12):
        e = np.stack([np.linalg.norm(corners[:, 1] - corners[:, 0], axis=1),
                      np.linalg.norm(corners[:, 2] - corners[:, 1], axis=1),
                      np.linalg.norm(corners[:, 0] - corners[:, 2], axis=1)], axis=1).max(axis=1)
        long = e > spacing
        if not long.any() or len(corners) + 3 * int(long.sum()) > cap:
            break
        a, b, c = corners[long, 0], corners[long, 1], corners[long, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        kids = np.concatenate([np.stack(t, axis=1) for t in ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
        corners = np.concatenate([corners[~long], kids])
        lab = np.concatenate([lab[~long], np.tile(lab[long], 4)])
    return corners, lab


class Nearest:
    """The nearest triangle of a labelled surface to many points: (label, distance) - EXACT. The k nearest
    centroids give a first answer; wherever a triangle not among them could still be nearer (its centroid
    within that distance plus the largest triangle's reach), every triangle that close is measured too
    (fc_glass_frames, 25 Oct: a pane's 10 mm edge face, split into many small triangles, crowded the big
    face out of the nearest centroids, and points ON the big face read 7 mm off)."""

    def __init__(self, xyz: np.ndarray, tris: np.ndarray, label: np.ndarray, spacing: float, k: int = 16):
        from scipy.spatial import cKDTree

        self.corners, self.label = split_long(xyz, tris, label, spacing)
        centre = self.corners.mean(axis=1)
        self.tree = cKDTree(centre)
        self.k = int(min(k, len(self.corners)))
        #: how far any triangle reaches from its centroid
        self.reach = float(np.linalg.norm(self.corners - centre[:, None, :], axis=2).max(initial=0.0))

    def _exact(self, q: np.ndarray, t: np.ndarray) -> np.ndarray:
        cpt = closest_on_triangles(q, self.corners[t, 0], self.corners[t, 1], self.corners[t, 2])
        return np.linalg.norm(cpt - q, axis=1)

    def query(self, p: np.ndarray, chunk: int = 200_000) -> tuple[np.ndarray, np.ndarray]:
        p = np.asarray(p, dtype=float).reshape(-1, 3)
        labels = np.empty(len(p), dtype=np.int64)
        dist = np.empty(len(p))
        for s in range(0, len(p), chunk):
            q = p[s:s + chunk]
            dk, idx = self.tree.query(q, k=self.k)
            idx = np.asarray(idx).reshape(len(q), -1)
            dk = np.asarray(dk).reshape(len(q), -1)
            best = np.full(len(q), np.inf)
            pick = np.zeros(len(q), dtype=np.int64)
            for j in range(idx.shape[1]):
                t = idx[:, j]
                d = self._exact(q, t)
                better = d < best                       # strict: the nearer centroid wins a tie
                best[better] = d[better]
                pick[better] = t[better]
            # A NEARER TRIANGLE NOT AMONG THE k: possible only where the k-th centroid is closer than the
            # answer plus the reach of a triangle - there, every triangle within that is measured
            if self.k < len(self.corners):
                open_ = np.flatnonzero(dk[:, -1] < best + self.reach)
                if len(open_):
                    lists = self.tree.query_ball_point(q[open_], best[open_] + self.reach, return_sorted=True)
                    rows = np.repeat(open_, [len(c) for c in lists])
                    cand = np.fromiter((t for c in lists for t in c), dtype=np.int64, count=len(rows))
                    for a in range(0, len(rows), 2_000_000):
                        r, t = rows[a:a + 2_000_000], cand[a:a + 2_000_000]
                        d = self._exact(q[r], t)
                        # the nearest per point; among equals the lower triangle index (sorted lists)
                        order = np.lexsort((t, d, r))
                        r, t, d = r[order], t[order], d[order]
                        first = np.ones(len(r), dtype=bool)
                        first[1:] = r[1:] != r[:-1]
                        r, t, d = r[first], t[first], d[first]
                        better = d < best[r]
                        best[r[better]] = d[better]
                        pick[r[better]] = t[better]
            labels[s:s + chunk] = self.label[pick]
            dist[s:s + chunk] = best
        return labels, dist


#: the 4 faces of a tetrahedron (corner indices), outward for a positive one
TET_FACES = ((1, 2, 3), (0, 3, 2), (0, 1, 3), (0, 2, 1))


def orient(xyz: np.ndarray, tets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(tets with a positive volume each - the two last corners swapped where negative, the signed volumes)."""
    t = np.asarray(tets, dtype=np.int64).copy()
    a, b, c, d = (xyz[t[:, i]] for i in range(4))
    vol = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0
    neg = vol < 0
    t[neg, 2], t[neg, 3] = t[neg, 3].copy(), t[neg, 2].copy()
    return t, np.abs(vol)


#: LAD-fea-12: a tetrahedron whose volume is under this share of its longest edge cubed is all but flat (a regular
#: one has 0.118). On ox_mc_lid's Part_239 that is SICN about 5e-5 and under, the worst element the quality record
#: (four decimals) still reads as above 0; the flattest, 3e-16, gmsh measured inside out.
FLAT_SHARE = 1e-6
FLAT_RULE = "LAD-fea-12"
#: ...and a sliver still under this SICN after the peel, with a face on the surface, is set aside from the floor and
#: named (the gmsh adapter's DEGENERATE_SICN: the record would read it as 0.0)
SLIVER_SICN = 5e-5


def _odd_edges(tris: np.ndarray) -> int:
    """How many edges of the boundary triangles are not the edge of exactly two of them."""
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    _u, count = np.unique(e, axis=0, return_counts=True)
    return int((count != 2).sum())


def peel_flat(xyz: np.ndarray, tets: np.ndarray) -> tuple[np.ndarray, int, int]:
    """LAD-fea-12: fTetWild's flat slivers ON THE SURFACE taken off. fTetWild can leave a tetrahedron with no
    volume lying on the part's surface, two or three of its faces outside (Part_239 of ox_mc_lid: ten of them in
    one 50-micron spot, the flattest measured inside out). Such a sliver holds no material: it is dropped, one at a
    time, flattest first, whenever the surface without it has no more edges off two triangles than it had (fTetWild's
    own surface can have a few already), and again while dropping one lays bare another. A flat one inside the part
    is left for the gate.
    `tets` oriented (orient). Returns (the tets kept, how many were dropped, how many flat ones are left)."""
    t = np.asarray(tets, dtype=np.int64)
    edges = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))

    def flat(tt):
        a, b, c, d = (xyz[tt[:, i]] for i in range(4))
        vol = np.abs(np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)) / 6.0
        lmax = np.max([np.linalg.norm(xyz[tt[:, i]] - xyz[tt[:, j]], axis=1) for i, j in edges], axis=0)
        rel = vol / np.maximum(lmax, 1e-300) ** 3
        idx = np.where(rel < FLAT_SHARE)[0]
        return idx[np.argsort(rel[idx], kind="stable")]

    dropped = 0
    while True:
        cand = flat(t)
        if not len(cand):
            return t, dropped, 0
        surface = boundary(t)
        outside = {tuple(sorted(f)) for f in surface.tolist()}
        odd = _odd_edges(surface)
        took = False
        for i in cand.tolist():
            if sum(tuple(sorted(t[i, list(c)].tolist())) in outside for c in TET_FACES) < 2:
                continue
            rest = np.delete(t, i, axis=0)
            if _odd_edges(boundary(rest)) <= odd:
                t, dropped, took = rest, dropped + 1, True
                break
        if not took:
            return t, dropped, int(len(cand))


#: LAD-fea-13: one refinement pass after the last rung, on the parts whose zones fell short
REFINE_RULE = "LAD-fea-13"


def _zone_part(z: Mapping) -> str:
    """The part a zone is on ("Part_k"), or "whole" for a single part's zone."""
    from meshpipeline.cad.fea_assembly import part_of_face

    for f in z.get("faces") or []:
        k = part_of_face(str(f))
        if k is not None:
            return f"Part_{k}"
    return "whole"


def refine_targets(plan: Mapping, short: list[str], walls: list[Mapping], parts: list[int] | None) -> dict[str, dict]:
    """LAD-fea-13: the parts the refinement pass re-meshes, each with its short zones, split by who meshes them:
    {"ftetwild": {"Part_k" | "whole": [zone ids]}, "gmsh": {"Part_k": [zone ids]}}. A wall short of its elements is
    on the part of the face the check measured (zone ids repeat across an assembly's parts: T5_1 is the fifth face
    of every part); any other short zone (a hot spot) is on the parts of its own faces. `parts`: the parts the
    rung's pass gave fTetWild (None: every part)."""
    found: dict[str, set[str]] = {}
    walled = set()
    for w in walls:
        zid, face = str(w.get("zone") or ""), str(w.get("face") or "")
        if zid and zid in set(short):
            found.setdefault(_face_part(face), set()).add(zid)
            walled.add(zid)
    for z in plan.get("zones") or []:
        zid = str(z.get("id"))
        if zid in set(short) and zid not in walled:
            found.setdefault(_zone_part(z), set()).add(zid)
    out: dict[str, dict] = {"ftetwild": {}, "gmsh": {}}
    for key, zids in sorted(found.items()):
        by = "ftetwild" if (parts is None or key == "whole" or int(key.split("_")[1]) in set(parts)) else "gmsh"
        out[by][key] = sorted(zids)
    return out


def _face_part(face: str) -> str:
    from meshpipeline.cad.fea_assembly import part_of_face

    k = part_of_face(face)
    return f"Part_{k}" if k is not None else "whole"


def tighten_zones(zones: list, targets: Mapping[str, list[str]], factor: float) -> list[dict]:
    """The plan's zones with each target part's own short zone entries `factor` finer (LAD-fea-3's way), every
    other entry as it was."""
    out = []
    for z in zones:
        key = _zone_part(z)
        if str(z.get("id")) in set(targets.get(key) or []):
            z = {**z, "size_m": float(z["size_m"]) * factor, "tightened": factor}
        out.append(z)
    return out


def refined_plan(plan: Mapping, refine: Mapping, key: str) -> tuple[Mapping, list[dict]]:
    """(the plan this part is sized by, the zones held through every cell): a part the refinement names gets its
    short zones LAD-fea-3 finer and held (LAD-fea-13); every other part the plan as it was - so its run, and its
    mesh, are the ones before."""
    mine = (refine.get("parts") or {}).get(key) or []
    if not mine:
        return plan, []
    zones = tighten_zones(list(plan.get("zones") or []), {key: list(mine)}, float(refine.get("factor") or 1.0))
    band = [z for z in zones if z.get("tightened") and _zone_part(z) == key]
    return {**plan, "zones": zones}, band


def boundary(tets: np.ndarray) -> np.ndarray:
    """The triangles that are the face of one tetrahedron only, oriented outward, in a fixed order."""
    f = np.concatenate([tets[:, list(c)] for c in TET_FACES])
    key = np.sort(f, axis=1)
    _u, first, count = np.unique(key, axis=0, return_index=True, return_counts=True)
    return f[np.sort(first[count == 1])]


def quadratic(xyz: np.ndarray, tets: np.ndarray, tris: np.ndarray):
    """Straight 10-node tetrahedra and 6-node triangles: a node at the middle of every edge, numbered
    after the corners in the sorted order of the edges (the same mesh gives the same numbers).
    Returns (xyz, tet10 in gmsh's node order, tri6 in gmsh's node order)."""
    tets = np.asarray(tets, dtype=np.int64)
    # gmsh's 10-node tet: corners 0-3, then edges 0-1, 1-2, 2-0, 3-0, 3-2, 3-1 (its own reference order)
    tet_edges = ((0, 1), (1, 2), (2, 0), (3, 0), (3, 2), (3, 1))
    e = np.concatenate([np.sort(tets[:, list(p)], axis=1) for p in tet_edges])
    uniq, inv = np.unique(e, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    mid = len(xyz) + np.arange(len(uniq))
    out_xyz = np.vstack([xyz, (xyz[uniq[:, 0]] + xyz[uniq[:, 1]]) / 2.0])
    tet10 = np.hstack([tets, mid[inv].reshape(len(tet_edges), len(tets)).T])
    # the boundary triangles' midside nodes, looked up in the same edge list
    tri_edges = ((0, 1), (1, 2), (2, 0))
    te = np.concatenate([np.sort(np.asarray(tris, dtype=np.int64)[:, list(p)], axis=1) for p in tri_edges])
    pos = _rows_in(uniq, te)
    tri6 = np.hstack([np.asarray(tris, dtype=np.int64), mid[pos].reshape(3, len(tris)).T])
    return out_xyz, tet10, tri6


def _rows_in(table: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """The index in the lexicographically sorted unique `table` of each of `rows` (all present)."""
    w = int(max(table.max(initial=0), rows.max(initial=0))) + 1
    tk = table[:, 0] * w + table[:, 1]
    rk = rows[:, 0] * w + rows[:, 1]
    pos = np.searchsorted(tk, rk)
    if not np.all(tk[np.minimum(pos, len(tk) - 1)] == rk):
        raise ValueError("a boundary edge is no element's edge")
    return pos


# #
# the runner's side: the CAD's faces, the sizing field, one fTetWild run per part
# #

def cad_faces(staged: Path):
    """(the staged shape, its faces in MapShapes order (gmsh's tag order), its solids in MapShapes order,
    for each solid the 0-based indexes of its faces)."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.scout import read_cad

    shape = read_cad(staged)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    smap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, smap)
    own: list[list[int]] = []
    for s in range(1, smap.Extent() + 1):
        ex = TopExp_Explorer(smap.FindKey(s), TopAbs_FACE)
        idx: list[int] = []
        while ex.More():
            k = fmap.FindIndex(ex.Current())
            if k > 0 and k - 1 not in idx:
                idx.append(k - 1)
            ex.Next()
        own.append(sorted(idx))
    return shape, fmap, smap, own


def frame_scale(gmsh, shape) -> float:
    """The factor from the OpenCASCADE reader's unit to the gmsh model's (a power of ten: a B-rep is read
    as written, a STEP in millimetres - the staged solid is in metres either way)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    occ = math.dist((x0, y0, z0), (x1, y1, z1))
    gx0, gy0, gz0, gx1, gy1, gz1 = gmsh.model.getBoundingBox(-1, -1)
    ours = math.dist((gx0, gy0, gz0), (gx1, gy1, gz1))
    if occ <= 0 or ours <= 0:
        return 1.0
    return float(10.0 ** round(math.log10(ours / occ)))


def tessellate(fmap, faces: list[int], deflection: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The faces' triangles within `deflection` (the shape's own unit), welded, outward as the faces
    are oriented: (xyz, tris, label = the 0-based face index)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.fea_measure import _weld
    from meshpipeline.cad.occ_box import incremental_mesh

    blocks, tri_blocks, labels = [], [], []
    base = 0
    for k in faces:
        face = TopoDS.Face_s(fmap.FindKey(k + 1))
        incremental_mesh(face, float(deflection), ANGULAR_DEFLECTION)
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None or tri.NbTriangles() == 0:
            continue
        trsf = loc.Transformation()
        nodes = np.array([(lambda q: (q.X(), q.Y(), q.Z()))(tri.Node(i).Transformed(trsf))
                          for i in range(1, tri.NbNodes() + 1)], dtype=float)
        flip = face.Orientation() == TopAbs_REVERSED
        t = np.empty((tri.NbTriangles(), 3), dtype=np.int64)
        for i in range(1, tri.NbTriangles() + 1):
            g = tri.Triangle(i)
            a, b, c = g.Value(1), g.Value(2), g.Value(3)
            t[i - 1] = (a, c, b) if flip else (a, b, c)
        blocks.append(nodes)
        tri_blocks.append(t - 1 + base)
        labels.append(np.full(len(t), k, dtype=np.int64))
        base += len(nodes)
    if not blocks:
        raise ValueError("the part's faces could not be tessellated")
    xyz, tris = _weld(np.vstack(blocks), np.vstack(tri_blocks))
    label = np.concatenate(labels)
    keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    return xyz, tris[keep], label[keep]


def sizing_grid(xyz: np.ndarray, tris: np.ndarray, label: np.ndarray, names: list[str], plan: Mapping,
                edge: float, pad: float = 0.0, band: list[dict] | tuple = ()) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """fTetWild's background mesh: the part's box in BG_DIVISIONS cells along EACH side (a thin pane's
    thickness is resolved as finely as its length - fc_glass_frames, 25 Oct: one 57 mm cell across a
    10 mm pane let its 7.5 mm wall zone fall through, and its tets ran 7 mm off the CAD), a cell or
    `pad` beyond it whichever is more, six tetrahedra a cell, the plan's zone size at every node (the
    part's edge length where no zone reaches, never above it). None when no zone is finer than it."""
    from meshpipeline.cad.fea_features import Surface
    from meshpipeline.engines.fea_size_field import SizeField

    zones = [z for z in plan.get("zones") or [] if float(z.get("size_m") or math.inf) < edge]
    if not zones:
        return None
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    span = hi - lo
    if float(span.max()) <= 0:
        return None
    # every side in BG_DIVISIONS cells (a flat face's own zero extent gets the longest side's cell)
    step = np.where(span > 0, span, float(span.max())) / BG_DIVISIONS
    margin = np.maximum(step, float(pad))
    lo, hi = lo - margin, hi + margin
    n = np.maximum(1, np.ceil((hi - lo) / step).astype(int))
    axes = [np.linspace(lo[i], lo[i] + n[i] * step[i], n[i] + 1) for i in range(3)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    surface = Surface(xyz=xyz, tris=tris, label=label, names=names)
    field = SizeField(surface, zones, growth=float(plan.get("growth") or 1.3), scale_to_m=1.0)
    h = np.minimum(field.zone_size(pts), edge)
    h = np.where(np.isfinite(h) & (h > 0), h, edge)
    if band:
        # LAD-fea-13: THE ZONES BEING REFINED, HELD THROUGH EVERY CELL. A zone's size rises away from it at most
        # (growth - 1) per metre, so a node one cell diagonal from it may read a size that much coarser than the
        # zone itself, and fTetWild, reading sizes between the nodes, lets a wall thinner than a cell fall through
        # (g_c0152's 0.29 mm wall in a 5 mm cell). Lowered by exactly that much, the grid never reads coarser than
        # the zones anywhere in a cell; never finer than the finest of them
        bfield = SizeField(surface, list(band), growth=float(plan.get("growth") or 1.3), scale_to_m=1.0)
        hb = bfield.zone_size(pts)
        floor = min(float(z["size_m"]) for z in band)
        slack = (float(plan.get("growth") or 1.3) - 1.0) * float(np.linalg.norm(step))
        hb = np.where(np.isfinite(hb), np.maximum(floor, hb - slack), np.inf)
        h = np.minimum(h, hb)
    # six tetrahedra a cube (Kuhn's split along the main diagonal: conforming between cubes)
    def vid(i, j, k):
        return (i * (n[1] + 1) + j) * (n[2] + 1) + k
    ii, jj, kk = np.meshgrid(np.arange(n[0]), np.arange(n[1]), np.arange(n[2]), indexing="ij")
    ii, jj, kk = ii.ravel(), jj.ravel(), kk.ravel()
    c = [vid(ii + dx, jj + dy, kk + dz) for dx, dy, dz in
         ((0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0), (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1))]
    kuhn = ((0, 1, 3, 7), (0, 1, 5, 7), (0, 2, 3, 7), (0, 2, 6, 7), (0, 4, 5, 7), (0, 4, 6, 7))
    tets = np.concatenate([np.stack([c[a], c[b], c[cc], c[d]], axis=1) for a, b, cc, d in kuhn])
    return pts, tets, h


def _write_obj(path: Path, xyz: np.ndarray, tris: np.ndarray) -> None:
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.writelines(f"v {x:.17g} {y:.17g} {z:.17g}\n" for x, y, z in xyz.tolist())
        fh.writelines(f"f {a + 1} {b + 1} {c + 1}\n" for a, b, c in tris.tolist())


def _write_bg(path: Path, pts: np.ndarray, tets: np.ndarray, values: np.ndarray) -> None:
    """The background mesh in gmsh's 2.2 ASCII format, its sizes as the node field "values"
    (fTetWild's --bg-mesh reads exactly that)."""
    with open(path, "w", encoding="ascii", newline="\n") as fh:
        fh.write("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n")
        fh.write(f"{len(pts)}\n")
        fh.writelines(f"{i + 1} {x:.17g} {y:.17g} {z:.17g}\n" for i, (x, y, z) in enumerate(pts.tolist()))
        fh.write(f"$EndNodes\n$Elements\n{len(tets)}\n")
        fh.writelines(f"{i + 1} 4 0 {a + 1} {b + 1} {c + 1} {d + 1}\n" for i, (a, b, c, d) in enumerate(tets.tolist()))
        fh.write("$EndElements\n$NodeData\n1\n\"values\"\n1\n0.0\n3\n0\n1\n")
        fh.write(f"{len(values)}\n")
        fh.writelines(f"{i + 1} {v:.17g}\n" for i, v in enumerate(values.tolist()))
        fh.write("$EndNodeData\n")


def _read_tets(path: Path) -> tuple[np.ndarray, np.ndarray]:
    import meshio

    m = meshio.read(str(path))
    tets = [c.data for c in m.cells if c.type == "tetra"]
    if not tets:
        raise ValueError("fTetWild wrote no tetrahedra")
    t = np.concatenate(tets).astype(np.int64)
    used, inv = np.unique(t, return_inverse=True)
    return np.asarray(m.points, dtype=float)[used], inv.reshape(t.shape)


def run_part(job: dict) -> dict:
    """One fTetWild run (its own process, one thread): job = {"dir", "xyz", "tris", "edge", "epsilon",
    "bg": (pts, tets, values) | None, "name"}. Returns {"xyz", "tets"} or {"error"}; writes nothing
    outside job["dir"]."""
    exe = binary()
    if exe is None:
        return {"error": "fTetWild is not installed in this mesh image"}
    d = Path(job["dir"])
    d.mkdir(parents=True, exist_ok=True)
    src, out = d / "input.obj", d / "output.msh"
    _write_obj(src, job["xyz"], job["tris"])
    xyz = job["xyz"]
    # fTetWild's envelope is a share of ITS bounding box: the input's own box, as it computes it
    diag = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0)))
    cmd = [exe, "-i", str(src), "-o", str(out), "--la", repr(float(job["edge"])),
           "-e", repr(float(job["epsilon"]) / diag), "--max-threads", str(THREADS),
           "--stop-energy", repr(STOP_ENERGY), "--level", str(LOG_LEVEL), "--no-color"]
    if job.get("bg") is not None:
        bg = d / "sizing.msh"
        _write_bg(bg, *job["bg"])
        cmd += ["--bg-mesh", str(bg)]
    else:
        (d / "sizing.msh").unlink(missing_ok=True)
    # LAD-fea-13: THE SAME INPUTS, THE SAME MESH. fTetWild runs on one thread with constant seeds, so a part whose
    # surface, settings and sizing are byte for byte those of a run already in its folder gets that run's mesh
    # without running again (the rung's refinement pass re-meshes only the parts it refines)
    key = _run_key(d, cmd)
    done = d / "run.json"
    if out.exists() and done.exists():
        try:
            was = json.loads(done.read_text())
            if was.get("key") == key:
                pts, tets = _read_tets(out)
                return {"xyz": pts, "tets": tets, "seconds": was.get("seconds"), "peak_gib": was.get("peak_gib"),
                        "command": was.get("command"), "reused": True}
        except Exception:  # noqa: BLE001 - a record that cannot be read is run again
            pass
    done.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    t0 = time.monotonic()
    tail: list[str] = []
    peak_kb = 0
    hwm_kb = 0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(d)) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            # fTetWild's own lines are the runner's sign of progress (the FEA watchdog reads output)
            print(f"{RUNG_LINE} {job['name']}: {line.rstrip()}", file=sys.stderr, flush=True)
            tail = (tail + [line.rstrip()])[-20:]
            hwm_kb = max(hwm_kb, _hwm_kb(proc.pid))
        hwm_kb = max(hwm_kb, _hwm_kb(proc.pid))
        try:
            _pid, status, usage = os.wait4(proc.pid, 0)
            rc = os.waitstatus_to_exitcode(status)
            proc.returncode = rc
            peak_kb = int(usage.ru_maxrss)
        except ChildProcessError:
            rc = proc.wait()
    # ITS OWN PEAK: the kernel's high-water mark of fTetWild's own memory, read while it ran. The reaped
    # rusage's ru_maxrss also holds the forking process's resident memory at the fork (it carries across
    # fork and exec): beside a 1.5 GB parent every small part read 1.5 GB (lab, 26 Oct), so it stands
    # only where the high-water mark cannot be read
    peak_kb = hwm_kb or peak_kb
    seconds = round(time.monotonic() - t0, 1)
    peak_gib = round(peak_kb / 1024.0 ** 2, 2)
    if rc != 0 or not out.exists():
        return {"error": f"fTetWild exited {rc}: " + " | ".join(tail[-4:]), "seconds": seconds, "rc": rc,
                "peak_gib": peak_gib}
    try:
        pts, tets = _read_tets(out)
    except Exception as exc:  # noqa: BLE001 - said as this part's failure
        return {"error": f"fTetWild's mesh could not be read ({exc})", "seconds": seconds}
    command = [Path(cmd[0]).name, *cmd[5:]]
    done.write_text(json.dumps({"key": key, "seconds": seconds, "peak_gib": peak_gib, "command": command}))
    return {"xyz": pts, "tets": tets, "seconds": seconds, "peak_gib": peak_gib, "command": command}


def _run_key(d: Path, cmd: list[str]) -> str:
    """What decides a run's mesh: fTetWild's version, its settings (not the paths) and the bytes of its input
    surface and sizing mesh."""
    import hashlib

    h = hashlib.sha256()
    h.update(f"{VERSION}|{cmd[5:]}".replace(str(d), "").encode())
    for name in ("input.obj", "sizing.msh"):
        f = d / name
        if f.exists():
            h.update(name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _hwm_kb(pid: int) -> int:
    """A running process's peak resident memory so far (kB: /proc's VmHWM); 0 where it cannot be read."""
    try:
        with open(f"/proc/{int(pid)}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return 0
    return 0


#: RUN-6: fTetWild's MEASURED memory, each part's own peak (VmHWM) on the lab (26 Oct: 23 of ox_mc_lid's parts run
#: as the rung runs them, at LAD-fea-10 edges, 1,090 to 2.39M tetrahedra): 0.19-0.26 GB however small, and at most
#: 2.1 GB per million tetrahedra above that (the steepest: a 13 mm part, 0.26M in 0.75 GB; the 651 mm plate, 0.77M
#: in 1.78 GB; the others 0.3-1.2 GB per million)
FTW_BASE_GIB = 0.25
FTW_GIB_PER_ELEMENT = 2.1e-6
#: the rule that sizes the last rung's machine from fTetWild's own model
MEMORY_RULE = "RUN-6"
#: the Cloud Run mesh job's CPUs (deploy MESH_CPU): how many parts it runs at once
CLOUD_RUN_CPUS = 8


def ftetwild_gib(elements: float, workers: int) -> float:
    """RUN-6: fTetWild's memory for a rung pass that builds `elements` tetrahedra with `workers` parts at once:
    each running part's own base, and every element once (a part's elements are its own: the parts at once
    build at most all of them)."""
    return max(1, int(workers)) * FTW_BASE_GIB + FTW_GIB_PER_ELEMENT * max(0.0, float(elements))


def rung_machine(elements: float, gmsh_gib: float, files_gib: float, *, margin: float) -> dict:
    """RUN-6: WHERE THE LAST RUNG'S PASS RUNS. The pass holds gmsh's model of every part (the gmsh memory model,
    `gmsh_gib`, with its margin) while fTetWild runs one part per CPU beside it - its own measured model, with
    the same margin, for as many parts at once as the machine has CPUs. The smallest place that holds both:
    the Cloud Run mesh job, else the smallest VM (contracts/mesh_machine). #319 sized it by gmsh's model alone;
    fTetWild's 24-40 GB parts were LAD-fea-10's runaway edges, and its recorded peaks were the parent's."""
    from meshpipeline.contracts import mesh_machine as MM

    def need(cpus: int) -> float:
        return float(gmsh_gib) + float(margin) * ftetwild_gib(elements, cpus)

    cloud = MM.choose(process_gib=need(CLOUD_RUN_CPUS), workspace_gib=files_gib, why="fTetWild memory model")
    if cloud["where"] == "cloud_run":
        decision = cloud
    else:
        fits = next((m for m in MM.MACHINES if need(m.cpus) <= MM.usable_gib(m)), MM.CEILING)
        decision = MM.choose(process_gib=need(fits.cpus), workspace_gib=files_gib, why="fTetWild memory model")
    cpus = int(decision.get("cpus") or CLOUD_RUN_CPUS)
    decision["ftetwild"] = {"rule": MEMORY_RULE, "elements": int(max(0.0, float(elements))), "workers": cpus,
                            "ftetwild_gib": round(float(margin) * ftetwild_gib(elements, cpus), 2),
                            "gmsh_gib": round(float(gmsh_gib), 2),
                            "model": {"base_gib": FTW_BASE_GIB, "gib_per_element": FTW_GIB_PER_ELEMENT}}
    return decision


#: a part is started beside others only while the job keeps this share of its memory free
MEMORY_ROOM_SHARE = 0.35


def memory_room() -> float:
    """The share of the job's memory still free: its cgroup's limit and use where it has one (Cloud Run, a
    container), else the machine's MemAvailable; 1.0 when neither can be read."""
    def read(path: str) -> str:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""

    limit, used = read("/sys/fs/cgroup/memory.max"), read("/sys/fs/cgroup/memory.current")
    if limit.isdigit() and used.isdigit() and int(limit) > 0:
        return max(0.0, 1.0 - int(used) / int(limit))
    info = {ln.split(":")[0]: ln.split()[1] for ln in read("/proc/meminfo").splitlines() if ":" in ln}
    try:
        return int(info["MemAvailable"]) / int(info["MemTotal"])
    except (KeyError, ValueError, ZeroDivisionError):
        return 1.0


def run_parts(jobs: list[dict], workers: int) -> list[dict]:
    """Every part's run, at most `workers` at once and a new one started beside others only while the job
    keeps MEMORY_ROOM_SHARE of its memory free; a part killed for memory (beside others) runs again alone.
    The results are in `jobs` order whatever the timing: each part's mesh is its own (the same input, the
    same mesh), so how they were scheduled changes nothing delivered."""
    from concurrent.futures import FIRST_COMPLETED, Future, wait

    results: list[dict | None] = [None] * len(jobs)
    pending = list(range(len(jobs)))
    running: dict[int, Future] = {}
    alone: list[int] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        while pending or running:
            while pending and len(running) < max(1, int(workers)) and (not running or memory_room() >= MEMORY_ROOM_SHARE):
                i = pending.pop(0)
                running[i] = pool.submit(run_part, jobs[i])
            done, _ = wait(list(running.values()), timeout=5.0, return_when=FIRST_COMPLETED)
            for i, fut in list(running.items()):
                if fut in done:
                    r = fut.result()
                    del running[i]
                    if r.get("rc") in (-9, 137):
                        alone.append(i)
                    else:
                        results[i] = r
    for i in alone:
        print(f"{RUNG_LINE} {jobs[i]['name']}: killed for memory beside other parts - run again alone",
              file=sys.stderr, flush=True)
        results[i] = run_part(jobs[i])
    return [r if r is not None else {"error": "not run"} for r in results]


def mesh_into(gmsh, ws: Path, plan: Mapping, staged: Path, volumes: list[int], *, volume_index: Mapping[int, int],
              surface_tag_of: Mapping[int, int], names: list[str], order: int = 2, workers: int = 1,
              model_scale: float = 1.0) -> dict:
    """fTetWild's mesh of each of `volumes` (gmsh volume tags of the staged model), written INTO that
    model: its nodes on the volume, its 10-node tetrahedra in the volume, each boundary triangle (6-node)
    on the CAD surface it is nearest to. `volume_index`: gmsh volume tag -> 0-based solid index (MapShapes
    order); `surface_tag_of`: 0-based face index -> gmsh surface tag; `names`: each face index's name
    (Face_k / Part_k_Face_j). Returns the record quality.json keeps (fea.ftetwild); raises when a part
    could not be meshed."""
    from meshpipeline.engines.gmsh.fea_adapter import note_stage

    settings = (plan.get("mesh") or {}).get("ftetwild") or {}
    per = settings.get("per_part") or {}
    refine = settings.get("refine") or {}
    shape, fmap, _smap, own = cad_faces(staged)
    # the reader's unit to the mesh's: a model built `model_scale` times its metres whose mesh is back in metres
    # (MESH-4, LAD-fea-11) takes fTetWild's parts in metres too
    scale = frame_scale(gmsh, shape) / float(model_scale or 1.0)
    jobs, meta = [], []
    from meshpipeline.cad.fea_assembly import part_of_face

    for v in volumes:
        faces = own[int(volume_index[int(v)])]
        part = next((part_of_face(names[k]) for k in faces if part_of_face(names[k]) is not None), None)
        key = f"Part_{part}" if part is not None and f"Part_{part}" in per else "whole"
        cfg = per.get(key) or next(iter(per.values()), None)
        if cfg is None:
            raise ValueError("the plan holds no fTetWild settings for this part")
        edge, eps = float(cfg["edge_length_m"]), float(cfg["epsilon_m"])
        defl = float(cfg.get("tessellation_m") or TESSELLATION_SHARE * eps)
        xyz, tris, label = tessellate(fmap, faces, defl / scale)
        xyz = xyz * scale
        own_plan, band = refined_plan(plan, refine, key)
        bg = sizing_grid(xyz, tris, label, list(names), own_plan, edge, pad=4.0 * eps, band=band)
        jobs.append({"dir": str(ws / "_ftetwild" / f"volume_{int(v)}"), "xyz": xyz, "tris": tris, "edge": edge,
                     "epsilon": eps, "bg": bg, "name": key if key != "whole" else "part"})
        meta.append({"volume": int(v), "key": key, "sized": bg is not None, "part": part, "faces": faces, "xyz": xyz, "tris": tris,
                     "label": label, "edge": edge, "eps": eps, "deflection": defl,
                     "refined": sorted(z["id"] for z in band)})
    note_stage(ws, "ftetwild", f"meshing {len(jobs)} part(s) with fTetWild (the last rung)")
    results = run_parts(jobs, workers)
    failed = [(m["key"], r["error"]) for m, r in zip(meta, results, strict=True) if "error" in r]
    if failed:
        raise RuntimeError("fTetWild could not mesh " + "; ".join(f"{k}: {e}" for k, e in failed))
    note_stage(ws, "ftetwild_faces", "naming fTetWild's faces after the CAD's")
    node_next = int(gmsh.model.mesh.getMaxNodeTag()) + 1
    elem_next = int(gmsh.model.mesh.getMaxElementTag()) + 1
    out_parts = []
    worst_dev = 0.0
    eps_max = 0.0
    aside_tags: list[int] = []
    aside_slivers: list[dict] = []
    moved_all: list[str] = []
    lost_all: list[str] = []
    for m, r in zip(meta, results, strict=True):
        tets, _vol = orient(r["xyz"], r["tets"])
        tets, peeled, flat_left = peel_flat(r["xyz"], tets)
        if peeled:
            print(f"[FTETWILD] {m['key']}: {peeled} flat sliver(s) on the surface taken off ({FLAT_RULE})",
                  file=sys.stderr)
        tris = boundary(tets)
        if order > 1:
            xyz10, tet10, tri6 = quadratic(r["xyz"], tets, tris)
            tet_type, tri_type = 11, 9
        else:
            xyz10, tet10, tri6 = r["xyz"], tets, tris
            tet_type, tri_type = 4, 2
        # EVERY BOUNDARY TRIANGLE ON ITS NEAREST CAD FACE (by its centroid), and how far the surface is
        spacing = max(m["edge"] / 4.0, m["eps"])
        to_cad = Nearest(m["xyz"], m["tris"], m["label"], spacing)
        cent = r["xyz"][tris].mean(axis=1)
        tri_face, _ = to_cad.query(cent)
        bverts = np.unique(tris)
        _vl, vdist = to_cad.query(r["xyz"][bverts])
        # and the other way: how far the CAD's surface is from the mesh's (a feature the envelope lost)
        to_mesh = Nearest(r["xyz"], tris, np.zeros(len(tris), dtype=np.int64), spacing)
        cad_pts = np.vstack([m["xyz"], to_cad.corners.mean(axis=1)])
        _cl, cdist = to_mesh.query(cad_pts)
        dev = float(max(vdist.max(initial=0.0), cdist.max(initial=0.0)))
        vertex_dev = np.zeros(len(r["xyz"]))
        vertex_dev[bverts] = vdist
        tri_dev = vertex_dev[tris].max(axis=1)
        faces_dev: dict[str, float] = {}
        for k in m["faces"]:
            sel = tri_face == k
            if sel.any():
                faces_dev[names[k]] = float(tri_dev[sel].max())
        lost = [names[k] for k in m["faces"] if names[k] not in faces_dev]
        moved = [n for n, d in faces_dev.items() if d > MOVED_SHARE * m["eps"] + m["deflection"]]
        # INTO THE MODEL: nodes on the volume, tets in it, each triangle on its face's surface
        tags = node_next + np.arange(len(xyz10), dtype=np.int64)
        node_next += len(xyz10)
        gmsh.model.mesh.addNodes(3, m["volume"], tags.tolist(), xyz10.ravel().tolist())
        etags = elem_next + np.arange(len(tet10), dtype=np.int64)
        elem_next += len(tet10)
        gmsh.model.mesh.addElementsByType(m["volume"], tet_type, etags.tolist(), tags[tet10].ravel().tolist())
        # LAD-fea-12: the slivers the peel could not take off (a face on the surface, the rest inside) - set aside
        # from the floor and named, as a detail too small for the mesh (PLAN-fea-12); a flat one wholly inside is
        # left for the gate
        q = np.asarray(gmsh.model.mesh.getElementQualities(etags.tolist(), "minSICN"), dtype=float)
        thin = np.where(q < SLIVER_SICN)[0]
        if len(thin):
            outside = {tuple(sorted(f)): j for j, f in enumerate(tris.tolist())}
            onface = []
            for i in thin.tolist():
                out_faces = [outside[k] for c in TET_FACES if (k := tuple(sorted(tets[i, list(c)].tolist()))) in outside]
                if out_faces:
                    onface.append(i)
                    # each one named: its part, the CAD face its outside face lies on, its SICN and where
                    aside_slivers.append({"part": m["key"], "face": names[int(tri_face[out_faces[0]])],
                                          "sicn": float(f"{q[i]:.3g}"),
                                          "at_mm": np.round(r["xyz"][tets[i]].mean(axis=0) * 1000.0, 3).tolist()})
            aside_tags += [int(etags[i]) for i in onface]
            if onface:
                print(f"[FTETWILD] {m['key']}: {len(onface)} sliver(s) on the surface under SICN {SLIVER_SICN:g} set aside "
                      f"({FLAT_RULE})", file=sys.stderr)
        for k in m["faces"]:
            sel = tri_face == k
            if not sel.any():
                continue
            ftags = elem_next + np.arange(int(sel.sum()), dtype=np.int64)
            elem_next += int(sel.sum())
            gmsh.model.mesh.addElementsByType(int(surface_tag_of[k]), tri_type, ftags.tolist(), tags[tri6[sel]].ravel().tolist())
        out_parts.append({"part": m["key"], "volume": m["volume"], "elements": int(len(tet10)),
                          "fTetWild_s": r.get("seconds"), "peak_gib": r.get("peak_gib"), "edge_length_mm": _sig(m["edge"] * 1e3),
                          "epsilon_mm": _sig(m["eps"] * 1e3), "deviation_mm": _sig(dev * 1e3),
                          "mesh_to_cad_mm": _sig(float(vdist.max(initial=0.0)) * 1e3),
                          "cad_to_mesh_mm": _sig(float(cdist.max(initial=0.0)) * 1e3),
                          "sizing_field": m["sized"],
                          "faces": len(m["faces"]), "moved_faces": moved, "lost_faces": lost,
                          **({"refined_zones": m["refined"], "refine_rule": REFINE_RULE} if m["refined"] else {}),
                          **({"reused": True} if r.get("reused") else {}),
                          **({"flat_slivers_dropped": peeled, "flat_rule": FLAT_RULE} if peeled else {}),
                          **({"flat_inside": flat_left} if flat_left else {}),
                          "command": r.get("command")})
        worst_dev = max(worst_dev, dev)
        eps_max = max(eps_max, m["eps"] + m["deflection"])
        moved_all += moved
        lost_all += lost
    info = {"engine": ENGINE, "version": VERSION, "rule": "LAD-fea-9", "threads": THREADS,
            "parts": [p["part"] for p in out_parts], "per_part": out_parts,
            "epsilon_mm": _sig(eps_max * 1e3), "deviation_mm": _sig(worst_dev * 1e3),
            "moved_faces": moved_all, "lost_faces": lost_all,
            "tied": bool(settings.get("tied")),
            # LAD-fea-12: the slivers set aside (the adapter holds the floor without them, the delivery names them)
            **({"set_aside": {"rule": FLAT_RULE, "elements": len(aside_tags), "slivers": aside_slivers,
                              "tags": aside_tags}} if aside_tags else {})}
    info["words"] = words(info)
    print(f"{RUNG_LINE} meshed {', '.join(str(p['part']) for p in out_parts)}: "
          + ", ".join(f"{p['part']} {p['elements']} elements in {p['fTetWild_s']} s, within {p['deviation_mm']} mm"
                      for p in out_parts), file=sys.stderr, flush=True)
    return info


__all__ = ["BIN_PATH", "COMMIT", "ENGINE", "MAX_RUNG_PASSES", "Nearest", "VERSION", "binary", "boundary",
           "closest_on_triangles", "envelope_m", "failed_parts", "FLAT_RULE", "FLAT_SHARE", "mesh_into", "orient",
           "params", "peel_flat", "quadratic", "REFINE_RULE", "SLIVER_SICN", "refine_targets", "refined_plan", "tighten_zones", "rung_parts", "run_part", "sizing_grid", "split_long", "tessellate",
           "words"]
