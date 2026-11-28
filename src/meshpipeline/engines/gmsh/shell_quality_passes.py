# Responsibility: SHMESH-100 - the shell mesh's extra quality passes: when the mesh is made with a measured figure past
# its limit in force (the user's quality line, our usual shell target while they set none, or Nastran's own limit)
# and the run has time left under its limit, the elements past it are worked on where they stand - their free nodes
# moved in their flat face's plane (along it, on a straight stretch of a face's boundary), two triangles' shared
# diagonal turned - and each pass is kept only when the worst
# figure past its limit improves and no figure in force gets worse past its limit.
# Boundaries: numpy over engines/shell_mesh.ShellMesh and the plan as applied; the CAD only through a Projector given
# (no gmsh here, no file). Element kinds, element numbers, faces, walls and groups are never changed; a node on a
# face's boundary moves only along a straight stretch of it or on its CAD curve, a node of a curved face only on its
# CAD surface, and a node at a corner of the boundary or of a place the user sized never moves. A mesh with no figure
# past its limit is returned untouched.
# Collaborates with: engines/gmsh/shell_adapter.py (mesh_shell runs it on the mesh made, with the run's clock),
# engines/shell_quality.py (the measures and the limits), engines/shell_requirements.py (the user's lines and the
# report rows the verdict reads), engines/shell_run.py (says the record's one line).
"""THE EXTRA QUALITY PASSES (SHMESH-100).

The shell mesh's time limit follows the size of its work (SHMESH-21e); what a run does not need of it goes to the
mesh's quality, never to an earlier delivery. A mesh made with a figure past its limit in force - the lines the
verdict judges (engines/shell_delivery.limits): the user's own quality lines while they set any (SHMESH-26), our usual
shell targets (shell_quality.LIMITS) while they set none, and Nastran's own GEOMCHECK limits always - is worked on
while time is left:

* each element past such a line, the furthest past first, has its free nodes (inside one flat face, in no place the
  user sized) tried at a few steps round where they stand, in the face's plane, and its nodes on a straight stretch of
  a face's boundary (a CAD edge, a free edge, a junction: two boundary edges in one line, every face round it flat)
  tried along that line, where they stay on every face round them;
  a node goes where that element's figure improves most and no element round the node comes out past a line in force
  it was inside, or further past one it was past, folded, or with an edge newly past the user's shortest or longest;
* a triangle past such a line and the triangle across one of its sides on the same flat face have their shared
  diagonal turned where the worse of the two improves on that line under the same guard (two triangles stay two).

One pass is one sweep of the elements past. It is measured again whole, as the verdict measures it, and kept only
when a figure past its limit improves (its worst, or fewer past it with the worst no worse), no line of the user's
that was met goes unmet, no line counts more elements outside it, no figure past its limit is worse, and no more
elements are past Nastran's limits or invalid. A pass is kept patch by patch (a patch: the changes round one element
past a line): when the pass measured whole is worse on a row, the longest run of its patches that is not is kept, and
the patch after it is set aside for the passes that follow; a pass no worse but with no gain is kept too. Passes go
on: they stop after STALL_MAX passes in a row with no gain, at the run's clock, or once they have spent PASS_SHARE of
the time left when they began. At the end the mesh is measured whole again against the one made: it is handed back
only when it is better and no row of it worse, else the mesh as made.

On the CAD (`project`, the mid-surface B-rep read again by engines/gmsh/shell_cad_projector): a free node of a curved
face whose nodes all stand on one CAD surface is tried in its tangent plane and put back on that surface, and a node
on a curved stretch of a face's boundary (a hole's rim: it and its two boundary neighbours on one CAD curve) is tried
along the curve and put back on it, under the same guard; a face made again (on no CAD surface) is left as made. The
record (`quality.quality_passes`) and one plain line say what was done."""
from __future__ import annotations

import copy
import dataclasses
import math
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import numpy as np

from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines.shell_mesh import ShellMesh

RULE = "SHMESH-100"
#: SHMESH-100: off, a mesh is delivered as made, as before
QUALITY_PASSES = True
#: SHMESH-100: passes go on while each one is kept and time is left: they stop at the first pass not kept, at the
#: run's clock, or once they have spent PASS_SHARE of the time left under the run's limit when they began (the rest
#: stays for what follows the mesh); PASS_MAX is only a backstop for a run with no clock
PASS_SHARE = 0.5
PASS_MAX = 500
#: SHMESH-100: a node on a curved face, or on a curved stretch of a face's boundary (a hole's rim), moves ON the CAD
#: surface or curve it stands on (the mid-surface B-rep read again: `project`); off, only flat faces and straight
#: stretches are worked on, as before
ON_CAD = True
#: a face's (a boundary stretch's) nodes are on a CAD surface (curve) when within this share of their shortest edge
ON_TOL = 1e-3
#: the steps a node is tried at, as shares of its shortest edge, in TURNS directions in its face's plane
STEPS = (0.4, 0.2, 0.1, 0.05, 0.02)
TURNS = 8
#: SHMESH-100: a node none of STEPS helps is tried again at these finer steps (same directions, same guard)
FINE_STEPS = (0.01, 0.005, 0.002)
#: SHMESH-100: an element no single node of which can move is tried with two neighbouring nodes of it moved together,
#: at these steps, in TURNS directions each (or along their line or curve), under the same guard
PAIR_STEPS = (0.2, 0.1, 0.05, 0.02, 0.01, 0.005)
PAIRS = True
#: a face is flat when its nodes stand within this share of its size (or 1e-9 mm) of their best plane
PLANE_TOL = 1e-7
#: the most elements one pass works on (the furthest past first)
TARGET_MAX = 4000
#: a corner of an element moved is kept turned the way it was by at least this sine of its angle
CORNER_SIN = 1e-3
#: a node on its faces' boundary slides along it where its two boundary edges are one straight line within this sine
#: (it stays on every face round it: the line is theirs)
SLIDE_SIN = 1e-8
#: SHMESH-100: a pass is kept patch by patch (a patch: the moves and the turn round one element past a limit): when
#: the pass measured whole is worse on a row, the longest run of its patches that is not is kept (found by halving,
#: at most BISECT_MAX whole measures) and the patch after it is set aside for the passes that follow; a pass not worse
#: but with no gain is kept too, and the passes stop after STALL_MAX of those in a row
BISECT_MAX = 14
STALL_MAX = 3
#: SHMESH-100 (rulebook 133): the passes stop when they no longer help, the idea of MMG's rounds that stop under a 10%
#: gain. A pass helps when, on some line past its limit as the pass began, it brings the line inside, or takes away at
#: least GAIN_SHARE of GAIN_OF (what the passes have gained on that line so far, this pass included: "gained"; or what
#: was still past as the pass began: "left") - in elements past it, or in its worst figure. GAIN_RUN passes in a row
#: that do not help (a pass with no gain is one) stop the passes; the best mesh so far is kept as before. None: off.
#: Chosen on the round 13 pass log (98 parts, 589 passes): 5% of what was gained, 4 in a row, is the rule that saves
#: the most while leaving no line worse than the full passes (74 s); 10% and 3 in a row saved 532 s but left 10 parts
#: worse (17 more elements past, 6 worst figures worse); 10% and 1 saved 1,595 s and left 43 parts worse
GAIN_SHARE: float | None = 0.05
GAIN_OF = "gained"
GAIN_RUN = 4
#: SHMESH-100: on the CAD, a node's candidates are judged first in its tangent plane (along its curve's tangent); only
#: the PROJECT_MAX best that pass the guard there are put on the CAD and judged again (a node with none costs no CAD)
PROJECT_MAX = 4
#: SHMESH-100: LOCAL RE-MESH - where an element is still past a line once the passes end and no node can move, the
#: small patch round it (the elements sharing a node with it, then one ring more: REMESH_RINGS) on one known face is
#: made again on that face by a `Remesher` with the patch's outline fixed (every neighbour untouched); a patch is kept
#: only when it is better and no figure in it worse, and the whole mesh only when better with no row worse.
#: REMESH_MAX: the most elements one run tries; REMESH_ON: a new node put back on a curved face's CAD surface moves
#: at most this share of the patch's edge size
REMESH = True
REMESH_RINGS = (1, 2)
REMESH_MAX = 400
REMESH_ON = 0.25
#: (SHMESH-100, measuring only: the census of the elements still past a line, by cause, in the record; off in the
#: product, set on in a lab tree)
CENSUS = False
CENSUS_MAX = 20000
CENSUS_KEEP = 40
#: SHMESH-89e's margin: an element within it of a solver limit is on it
_SOLVER_TOL = SQ.MARGIN

class Projector(Protocol):
    """The CAD a mesh was made on (mm): sewn face k's surface, and the boundary curves round given faces."""

    def face(self, k: int, nodes: np.ndarray, tol: float) -> bool:
        """Whether sewn face k's nodes `nodes` (n, 3) stand on one CAD surface within `tol` mm (a sample of them may be
        read; not: the face was made again, or is not known); that surface is then face k's. A node is checked again
        by itself (`surface`) before it moves."""
        ...

    def surface(self, k: int, pts: np.ndarray) -> np.ndarray:
        """`pts` (n, 3) put on the CAD surface of sewn face k (one `face` said is known)."""
        ...

    def curve(self, pts: np.ndarray, tol: float) -> int | None:
        """The CAD curve every point of `pts` (n, 3) stands on within `tol` mm, or None."""
        ...

    def on_curve(self, c: int, pts: np.ndarray) -> np.ndarray:
        """`pts` put on CAD curve c."""
        ...


class Remesher(Protocol):
    """A small polygon of a face meshed again with no node added on its outline."""

    def patch(self, ring: np.ndarray, normal: np.ndarray, size: float) -> list[tuple[np.ndarray, list[list[int]]]]:
        """Ways to mesh the polygon `ring` (m, 3, mm; in order round `normal`) at about `size` mm: each (the new
        nodes (n, 3), the elements as numbers into the ring's nodes then the new ones). None: an empty list."""
        ...


_PLAIN = {"jacobian":"worst element shape (Jacobian)", "jacobian_ratio": "worst element shape (Jacobian)",
          "aspect": "worst aspect ratio", "warp_deg": "worst warpage", "skew_deg": "worst skew",
          "taper": "worst taper", "min_angle_quad": "smallest quad corner angle",
          "max_angle_quad": "largest quad corner angle", "min_angle_tria": "smallest triangle corner angle",
          "max_angle_tria": "largest triangle corner angle", "min_angle": "smallest corner angle",
          "max_angle": "largest corner angle"}
_DEGREES = ("warp_deg", "skew_deg", "min_angle_quad", "max_angle_quad", "min_angle_tria", "max_angle_tria",
            "min_angle", "max_angle")
_QUAD_KEYS = ("aspect", "warp_deg", "skew_deg", "taper", "min_angle_quad", "max_angle_quad", "jacobian_ratio")
_TRIA_KEYS = ("aspect", "skew_deg", "min_angle_tria", "max_angle_tria")


def _name_of(shapes: Mapping[str, str]) -> tuple[str, bool]:
    """A line's figure in plain words, and whether it is in degrees."""
    keys = sorted(set(shapes.values()))
    key = keys[0] if len(keys) == 1 else ("min_angle" if all(k.startswith("min_angle") for k in keys) else
                                           "max_angle" if all(k.startswith("max_angle") for k in keys) else keys[0])
    return _PLAIN.get(key, key.replace("_", " ")), key in _DEGREES


def lines_in_force(applied: Mapping, requirements: Mapping | None) -> list[dict]:
    """The lines the verdict judges this mesh by (engines/shell_delivery.limits), each {"kind": "user" | "ours" |
    "solver", "id", "shapes": {shape: figure}, "op", "bound" (an element is past it beyond this), "name", "deg"}."""
    rows = {r.get("id"): r for r in ((applied.get("requirements") or {}).get("rows") or [])
            if isinstance(r, Mapping)}
    out: list[dict] = []
    for lim in (requirements or {}).get("limits") or []:
        figs = SR.figures_of(lim)
        row = rows.get(lim.get("id"))
        if not figs or row is None or row.get("applied") is False:
            continue
        name, deg = _name_of(figs)
        out.append({"kind": "user", "id": lim.get("id"), "shapes": figs, "op": str(lim["op"]),
                    "bound": float(lim["target"]), "name": name, "deg": deg})
    if not out:
        # (SHMESH-26: our usual targets are judged only while the user's own limits are not in force)
        for key, op, limit, _source in SQ.LIMITS:
            shapes = {s: key for s, keys in (("quad", _QUAD_KEYS), ("tria", _TRIA_KEYS)) if key in keys}
            name, deg = _name_of(shapes)
            out.append({"kind": "ours", "id": key, "shapes": shapes, "op": op, "bound": float(limit), "name": name,
                        "deg": deg})
    for shape, key, op, limit, card in SQ.NASTRAN_LIMITS:
        out.append({"kind": "solver", "id": card, "shapes": {shape: key}, "op": op, "bound": float(limit),
                    "name": f"elements past Nastran's {card}", "deg": False})
    return out


def _tol(line: Mapping) -> float:
    """How far past its bound an element must be to be past the line (the measures' own snapping)."""
    if line["kind"] == "solver":
        return _SOLVER_TOL
    if line["kind"] == "user":
        return SR.AT_LIMIT * max(abs(float(line["bound"])), 1.0)
    return 0.0


def _dev(values: np.ndarray, line: Mapping) -> np.ndarray:
    """How far past the line's bound each value is (positive: past)."""
    v = np.asarray(values, dtype=float)
    return float(line["bound"]) - v if line["op"] == ">=" else v - float(line["bound"])


def _deviations(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, lines: list[dict]) -> np.ndarray:
    """(quads + trias, lines): each element's deviation past each line (NaN where the line reads no such element)."""
    nq, nt = len(quads), len(trias)
    D = np.full((nq + nt, len(lines)), np.nan)
    mq = SQ.quad_measures(xyz, quads, extra=True) if nq else {}
    mt = SQ.tria_measures(xyz, trias, extra=True) if nt else {}
    for j, ln in enumerate(lines):
        if nq and "quad" in ln["shapes"]:
            D[:nq, j] = _dev(mq[ln["shapes"]["quad"]], ln)
        if nt and "tria" in ln["shapes"]:
            D[nq:, j] = _dev(mt[ln["shapes"]["tria"]], ln)
    return D


def _measure(mesh: ShellMesh, applied: Mapping, requirements: Mapping | None) -> dict:
    """The mesh measured as the verdict reads it: the figures, Nastran's counts, validity and the report rows."""
    q = SQ.measure(mesh)
    rep = SR.report(mesh, requirements, applied.get("sizing"), places=applied.get("places")) if requirements else None
    return {"q": q, "rep": rep, "rows": {r.get("id"): r for r in ((rep or {}).get("rows") or [])}}


def _summary(state: Mapping, lines: list[dict]) -> list[dict]:
    """Per line: whether it is past (as the verdict says), its worst, and how many elements are past it."""
    q, rows = state["q"], state["rows"]
    out = []
    seen_cards: set = set()
    for ln in lines:
        if ln["kind"] == "user":
            r = rows.get(ln["id"]) or {}
            out.append({"past": r.get("met") is False, "worst": r.get("worst"), "n": int(r.get("outside") or 0),
                        "beyond": int(r.get("beyond") or 0)})
        elif ln["kind"] == "ours":
            w = ((q.get("figures") or {}).get(ln["id"]) or {}).get("worst")
            n = int((q.get("past_limits") or {}).get(ln["id"]) or 0)
            out.append({"past": n > 0, "worst": w, "n": n, "beyond": n})
        else:
            n = 0 if ln["id"] in seen_cards else int((q.get("nastran_past") or {}).get(ln["id"]) or 0)
            seen_cards.add(ln["id"])
            out.append({"past": n > 0, "worst": None, "n": n, "beyond": n})
    return out


def _better(w_new: Any, w_old: Any, op: str) -> bool:
    if w_new is None or w_old is None:
        return False
    return float(w_new) > float(w_old) + 1e-12 if op == ">=" else float(w_new) < float(w_old) - 1e-12


def _worse(w_new: Any, w_old: Any, op: str) -> bool:
    if w_new is None or w_old is None:
        return False
    return _better(w_old, w_new, op)


def _judged(old: Mapping, new: Mapping, lines: list[dict]) -> tuple[bool, bool]:
    """(no row worse, a figure past its limit better) of mesh state `new` over `old`, both measured whole."""
    qo, qn = old["q"], new["q"]
    if int(qn.get("invalid") or 0) > int(qo.get("invalid") or 0):
        return False, False
    po, pn = qo.get("nastran_past") or {}, qn.get("nastran_past") or {}
    if any(int(pn.get(k) or 0) > int(po.get(k) or 0) for k in set(po) | set(pn)):
        return False, False
    for rid, ro in old["rows"].items():
        rn = new["rows"].get(rid) or {}
        if ro.get("met") is True and rn.get("met") is not True:
            return False, False
        if int(rn.get("outside") or 0) > int(ro.get("outside") or 0) or \
                int(rn.get("beyond") or 0) > int(ro.get("beyond") or 0):
            return False, False
    so, sn = _summary(old, lines), _summary(new, lines)
    gain = False
    for ln, a, b in zip(lines, so, sn, strict=True):
        if b["n"] > a["n"] or b["beyond"] > a["beyond"]:
            return False, False
        if b["past"] and not a["past"]:
            return False, False
        if a["past"] and _worse(b["worst"], a["worst"], ln["op"]):
            return False, False
        if ln["kind"] == "ours" and not a["past"] and b["worst"] is not None and \
                _worse(b["worst"], ln["bound"], ln["op"]):
            return False, False
        if a["past"] and (_better(b["worst"], a["worst"], ln["op"]) or b["n"] < a["n"]):
            gain = True
    return True, gain


def kept(old: Mapping, new: Mapping, lines: list[dict]) -> bool:
    """SHMESH-100: whether a pass's mesh is kept over the one before it - a figure past its limit improves (its worst,
    or fewer elements past it with the worst no worse) and no figure in force gets worse past its limit."""
    ok, gain = _judged(old, new, lines)
    return ok and gain


def _not_worse(old: Mapping, new: Mapping, lines: list[dict]) -> bool:
    """SHMESH-100: no row of mesh state `new` worse than `old` (a gain or not)."""
    return _judged(old, new, lines)[0]


def _gap(worst: Any, ln: Mapping) -> float:
    """How far a line's worst figure is past its bound (0 inside, or no figure)."""
    if worst is None:
        return 0.0
    b = float(ln["bound"])
    return max(0.0, b - float(worst)) if ln["op"] == ">=" else max(0.0, float(worst) - b)


def _pass_helps(first: list[dict], prev: list[dict], now: list[dict], lines: list[dict]) -> bool:
    """SHMESH-100 stop rule: whether one pass (summaries `prev` -> `now`; `first` as the passes began) helped - on some
    line past as it began, the line brought inside, or at least GAIN_SHARE of GAIN_OF taken away in elements past it
    or in its worst figure."""
    share = float(GAIN_SHARE or 0.0)
    for ln, a0, a, b in zip(lines, first, prev, now, strict=True):
        if not a["past"]:
            continue
        if not b["past"]:
            return True
        dn = int(a["n"]) - int(b["n"])
        dw = _gap(a["worst"], ln) - _gap(b["worst"], ln) if _better(b["worst"], a["worst"], ln["op"]) else 0.0
        if GAIN_OF == "gained":
            base_n = int(a0["n"]) - int(b["n"])
            base_w = _gap(a0["worst"], ln) - _gap(b["worst"], ln) if a0["worst"] is not None else dw
        else:
            base_n, base_w = int(a["n"]), _gap(a["worst"], ln)
        if dn > 0 and dn >= share * base_n:
            return True
        if dw > 0 and dw >= share * base_w:
            return True
    return False


def _flat_faces(xyz: np.ndarray, elem: np.ndarray, face_of: np.ndarray) -> dict[int, np.ndarray]:
    """{face: unit normal} of each sewn face whose nodes lie in one plane."""
    out: dict[int, np.ndarray] = {}
    for k in np.unique(face_of).tolist():
        nodes = np.unique(elem[face_of == k])
        nodes = nodes[nodes >= 0]
        p = xyz[nodes]
        if len(p) < 3:
            continue
        c = p.mean(axis=0)
        _u, s, vt = np.linalg.svd(p - c, full_matrices=False)
        size = float(np.linalg.norm(p.max(axis=0) - p.min(axis=0)))
        n = vt[-1]
        if float(np.abs((p - c) @ n).max()) <= max(PLANE_TOL * size, 1e-9):
            out[int(k)] = n / max(float(np.linalg.norm(n)), 1e-300)
    return out


def _frozen_elements(mesh: ShellMesh, applied: Mapping) -> np.ndarray:
    """The elements of a place the user sized (SHMESH-52): their nodes never move."""
    frozen = np.zeros(mesh.n_elements, dtype=bool)
    plan = applied.get("places") or {}
    places = list(plan.get("places") or []) if isinstance(plan, Mapping) else []
    if not places:
        return frozen
    from meshpipeline.engines import shell_places as SP

    mid = np.asarray(mesh.mid, dtype=object)
    for p in places:
        if not isinstance(p, Mapping):
            continue
        if p.get("edge"):
            frozen |= np.asarray(SP._edge_elements(mesh, p.get("rim_mm") or [], float(p.get("set_mm") or 0.0)),
                                 dtype=bool)
        elif p.get("mids"):
            frozen |= np.isin(mid, list(p["mids"]))
    return frozen


#: SHMESH-102: a mapped strip's nodes move in the passes like any free node, under the same guard (round 9 census: of
#: the element-rows held by a mapped strip's nodes, 67% - 19,276 of 28,578 - were on free faces beside the strip, held
#: only by the strip's nodes on their shared edge; mostly triangle angle rows). A node next to any other structure's
#: node (a ring, band, grid cell, block, step, closing) stays fixed. False: every held node fixed, as before.
STRIP_NODES_MOVE = True
STRIP_NODES_RULE = "SHMESH-102"


def _move_pins(mesh: ShellMesh, held: np.ndarray, elem: np.ndarray, of: dict[int, list[int]]) -> np.ndarray:
    """SHMESH-102: the held nodes the passes' moves keep fixed - all of them less a mapped strip's own nodes whose
    elements touch no node of another structure (held_kind; a mesh without it keeps every held node fixed)."""
    hk = getattr(mesh, "held_kind", None)
    if hk is None or len(hk) != len(held) or not held.any():
        return held
    from meshpipeline.engines.gmsh.shell_adapter import HELD_KINDS
    kind = np.asarray(hk, dtype=np.int64)
    strip = HELD_KINDS.index("mapped strip")
    other = held & (kind != strip) & (kind != 0)
    pins = held.copy()
    for v in np.flatnonzero(held & (kind == strip)).tolist():
        round_ = {int(x) for e in of.get(v, []) for x in elem[e].tolist() if x >= 0}
        if not any(bool(other[x]) for x in round_):
            pins[v] = False
    return pins


class _Sweep:
    """One mesh's elements and nodes as the passes work on them (positions and triangles change, nothing else)."""

    def __init__(self, mesh: ShellMesh, face_of: np.ndarray, lines: list[dict], frozen: np.ndarray,
                 low: float | None, high: float | None, held: np.ndarray | None = None,
                 project: Projector | None = None):
        self.project = project if ON_CAD else None
        self.X = np.array(mesh.xyz, dtype=float)
        self.Q = np.asarray(mesh.quads, dtype=np.int64).reshape(-1, 4)
        self.T = np.array(mesh.trias, dtype=np.int64).reshape(-1, 3)
        self.nq = len(self.Q)
        self.face_of = np.asarray(face_of, dtype=np.int64)
        self.lines, self.low, self.high = lines, low, high
        self.scale = np.array([max(abs(float(ln["bound"])), 1.0) for ln in lines])
        self.tols = np.array([_tol(ln) for ln in lines])
        elem = np.full((self.nq + len(self.T), 4), -1, dtype=np.int64)
        elem[:self.nq] = self.Q
        elem[self.nq:, :3] = self.T
        self.flat = _flat_faces(self.X, elem, self.face_of)
        self.of: dict[int, list[int]] = {}
        all_n: dict[tuple[int, int], int] = {}
        face_n: dict[tuple[int, int, int], int] = {}
        for e, row in enumerate(elem.tolist()):
            r = [v for v in row if v >= 0]
            k = int(self.face_of[e])
            for i, v in enumerate(r):
                self.of.setdefault(v, []).append(e)
                w = r[(i + 1) % len(r)]
                a, b = min(v, w), max(v, w)
                all_n[(a, b)] = all_n.get((a, b), 0) + 1
                face_n[(a, b, k)] = face_n.get((a, b, k), 0) + 1
        # a node moves when every element round it is of one flat face, none frozen, and it is on no edge that face
        # does not share between two of its own elements (its boundary: a CAD edge, a hole, a free edge, a junction)
        self._all_n, self._face_n = all_n, face_n
        on_edge: set[int] = set()
        for (a, b, _k), n in face_n.items():
            if n != 2 or all_n[(a, b)] != 2:
                on_edge.update((a, b))
        # (a structure's nodes - rings, bands, grid cells, mapped strips: SHMESH-83's pinned - never move)
        self.held = np.zeros(len(self.X), dtype=bool) if held is None else np.asarray(held, dtype=bool)
        # SHMESH-102: what the moves hold fixed - the held nodes less a mapped strip's own (self.held stays the
        # census' and the turns' and re-meshes' pin)
        self.pinned = _move_pins(mesh, self.held, elem, self.of) if STRIP_NODES_MOVE else self.held
        on_edge |= {int(v) for v in np.flatnonzero(self.pinned)}
        self.movable = np.zeros(len(self.X), dtype=bool)
        self.normal = np.zeros((len(self.X), 3))
        for v, els in self.of.items():
            faces = {int(self.face_of[e]) for e in els}
            if len(faces) != 1 or v in on_edge or any(bool(frozen[e]) for e in els):
                continue
            k = faces.pop()
            if k in self.flat:
                self.movable[v] = True
                self.normal[v] = self.flat[k]
        # a node on a straight stretch of the boundary (two boundary edges in one line, every face round it flat, none
        # frozen) slides along that line
        feat: dict[int, set[int]] = {}
        for (a, b, _k), n in face_n.items():
            if n != 2 or all_n[(a, b)] != 2:
                feat.setdefault(a, set()).add(b)
                feat.setdefault(b, set()).add(a)
        self.slide: dict[int, np.ndarray] = {}
        for v in sorted(on_edge):
            if self.pinned[v]:
                continue
            els = self.of.get(v, [])
            ends = sorted(feat.get(v, ()))
            if len(ends) != 2 or any(bool(frozen[e]) for e in els) or                     any(int(self.face_of[e]) not in self.flat for e in els):
                continue
            d1, d2 = self.X[ends[0]] - self.X[v], self.X[ends[1]] - self.X[v]
            l1, l2 = float(np.linalg.norm(d1)), float(np.linalg.norm(d2))
            if l1 <= 0 or l2 <= 0 or float(d1 @ d2) >= 0 or                     float(np.linalg.norm(np.cross(d1, d2))) > SLIDE_SIN * l1 * l2:
                continue
            self.slide[v] = (d2 - d1) / float(np.linalg.norm(d2 - d1))
        # each element's normal: its flat face's, else its own as made (a curved face's corners are kept turned the
        # way they were round it)
        self.en = np.zeros((len(elem), 3))
        for e in range(len(elem)):
            k = int(self.face_of[e])
            if k in self.flat:
                self.en[e] = self.flat[k]
                continue
            c = self._conn(e)
            p = self.X[c]
            nn = np.cross(p[2] - p[0], p[3] - p[1]) if len(c) == 4 else np.cross(p[1] - p[0], p[2] - p[0])
            self.en[e] = nn / max(float(np.linalg.norm(nn)), 1e-300)
        # (SHMESH-100, on the CAD: a free node of one curved face, and a node on a curved stretch of the boundary -
        # its two boundary neighbours and it on one CAD curve - are worked on once their face is known on the CAD)
        self.on_face: dict[int, int] = {}
        self.arc_ends: dict[int, list[int]] = {}
        if self.project is not None:
            for v, els in self.of.items():
                if self.pinned[v] or self.movable[v] or v in self.slide or any(bool(frozen[e]) for e in els):
                    continue
                faces = {int(self.face_of[e]) for e in els}
                if v not in on_edge:
                    if len(faces) == 1:
                        self.on_face[v] = faces.pop()
                elif len(feat.get(v, ())) == 2:
                    self.arc_ends[v] = sorted(feat[v])
        self._face_known: dict[int, bool] = {}
        self._arc: dict[int, int | None] = {}
        self._on: dict[int, bool] = {}
        self.frozen = frozen
        self.moved = 0
        self.turned = 0
        self.paired = 0
        self.cad_s = 0.0
        # (SHMESH-100, patch by patch: the pass's changes in order, where each patch begins, its element; and the
        # elements set aside, never worked on again)
        self.log: list[tuple] = []
        self.pos = 0
        self.marks: list[tuple[int, int]] = []
        self.banned: set[int] = set()
        # (SHMESH-100: why a node's last try was refused, per node; and how often each reason was met)
        self.why: dict[int, str] = {}
        self.refused: dict[str, int] = {}

    def _no(self, v: int | None, code: str) -> bool:
        """A try refused for reason `code` (kept per node and counted); False."""
        if v is not None:
            self.why[v] = code
        self.refused[code] = self.refused.get(code, 0) + 1
        return False

    def _cad(self, fn: Callable, *args: Any) -> Any:
        """A call on the CAD, timed."""
        t = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.cad_s += time.perf_counter() - t

    def begin(self) -> None:
        """A pass begins: its journal is empty."""
        self.log, self.pos, self.marks = [], 0, []

    def patches(self) -> list[tuple[int, int, int]]:
        """The pass's patches that changed something: (first change, one past the last, element worked on)."""
        out = []
        for i, (start, e) in enumerate(self.marks):
            end = self.marks[i + 1][0] if i + 1 < len(self.marks) else len(self.log)
            if end > start:
                out.append((start, end, e))
        return out

    def _turn_rows(self, ent: tuple, forward: bool) -> None:
        _t, e, f, (a, b, c, d), old_e, old_f = ent
        if forward:
            self.T[e - self.nq], self.T[f - self.nq] = [a, d, c], [d, b, c]
            self.of[a].remove(f)
            self.of[b].remove(e)
            self.of[d].append(e)
            self.of[c].append(f)
        else:
            self.T[e - self.nq], self.T[f - self.nq] = old_e, old_f
            self.of[a].append(f)
            self.of[b].append(e)
            self.of[d].remove(e)
            self.of[c].remove(f)

    def goto(self, n: int) -> None:
        """The mesh as it stood after the pass's first n changes."""
        while self.pos > n:
            self.pos -= 1
            ent = self.log[self.pos]
            if ent[0] == "x":
                self.X[ent[1]] = ent[2]
            else:
                self._turn_rows(ent, False)
        while self.pos < n:
            ent = self.log[self.pos]
            if ent[0] == "x":
                self.X[ent[1]] = ent[3]
            else:
                self._turn_rows(ent, True)
            self.pos += 1

    def done(self) -> tuple[int, int]:
        """(nodes moved, diagonals turned) of the pass's changes as it stands."""
        kinds = [ent[0] for ent in self.log[:self.pos]]
        return kinds.count("x"), kinds.count("t")

    def _known(self, k: int) -> bool:
        """Face k is flat, or its nodes stand on one CAD surface (SHMESH-100: they may move on it)."""
        if k in self.flat:
            return True
        if self.project is None:
            return False
        if k not in self._face_known:
            sel = self.face_of == k
            nodes = np.unique(np.concatenate([self.Q[sel[:self.nq]].ravel(), self.T[sel[self.nq:]].ravel()]))
            self._face_known[k] = bool(self._cad(self.project.face, k, self.X[nodes], ON_TOL * self._short(nodes)))
        return self._face_known[k]

    def _stands(self, v: int) -> bool:
        """Node v of a known curved face stands on its CAD surface itself (checked once, when it is first a candidate
        to move: the face's own check reads a sample of its nodes)."""
        if v not in self._on:
            k = self.on_face[v]
            tol = ON_TOL * self._short(np.array([v]))
            put = self._cad(self.project.surface, k, self.X[[v]]) if self.project is not None else None
            self._on[v] = put is not None and tol > 0 and bool(np.isfinite(put).all()) and \
                float(np.linalg.norm(put[0] - self.X[v])) <= tol
        return self._on[v]

    def _short(self, nodes: np.ndarray) -> float:
        """The shortest edge among elements round `nodes` (mm)."""
        best = math.inf
        for v in nodes.tolist()[:64]:
            for e in self.of.get(int(v), [])[:4]:
                p = self.X[self._conn(e)]
                best = min(best, float(np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1).min()))
        return best if math.isfinite(best) else 0.0

    def _curve_of(self, v: int) -> int | None:
        """The CAD curve node v and its two boundary neighbours stand on (every face round them known), or None."""
        if v not in self._arc:
            c = None
            faces = {int(self.face_of[e]) for e in self.of[v]}
            if self.project is not None and all(self._known(k) for k in faces):
                pts = self.X[[self.arc_ends[v][0], v, self.arc_ends[v][1]]]
                h = float(min(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[1])))
                if h > 0:
                    c = self._cad(self.project.curve, pts, ON_TOL * h)
            self._arc[v] = c
        return self._arc[v]

    def _can(self, v: int) -> bool:
        """Whether node v may be tried: free on a flat face, sliding on a straight stretch, or (on the CAD) free on a
        known curved face or on a CAD curve."""
        if self.movable[v] or v in self.slide:
            return True
        if v in self.on_face:
            return self._known(self.on_face[v]) and self._stands(v)
        if v in self.arc_ends:
            return self._curve_of(v) is not None
        return False

    def _conn(self, e: int) -> list[int]:
        return self.Q[e].tolist() if e < self.nq else self.T[e - self.nq].tolist()

    def _batch(self, XX: np.ndarray, conns: list[list[int]], n: np.ndarray, sign: np.ndarray | None
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """For C copies of a few nodes' positions XX (C, L, 3) and elements `conns` on them (local numbers, quads
        first): each copy's deviations (C, elements, lines) and its corners' sines signed by each element's face normal
        `n` (elements, 3) (C, elements, 4;
        +inf where a triangle has no fourth corner), times `sign` (per element, the sign it was turned before), and each
        element's area signed by `n` (C, elements)."""
        C, L = XX.shape[0], XX.shape[1]
        flat = XX.reshape(-1, 3)
        off = np.arange(C, dtype=np.int64) * L
        qc = np.array([c for c in conns if len(c) == 4], dtype=np.int64).reshape(-1, 4)
        tc = np.array([c for c in conns if len(c) == 3], dtype=np.int64).reshape(-1, 3)
        Qb = (qc[None] + off[:, None, None]).reshape(-1, 4)
        Tb = (tc[None] + off[:, None, None]).reshape(-1, 3)
        D = _deviations(flat, Qb, Tb, self.lines)
        nqs, nts = len(qc), len(tc)
        nl = len(self.lines)
        Dc = np.concatenate([D[:C * nqs].reshape(C, nqs, nl), D[C * nqs:].reshape(C, nts, nl)], axis=1)
        S = np.full((C, nqs + nts, 4), np.inf)
        A = np.zeros((C, nqs + nts))
        for conn, lo, k in ((qc, 0, 4), (tc, nqs, 3)):
            if not len(conn):
                continue
            p = XX[:, conn]                                  # (C, m, k, 3)
            nxt = np.roll(p, -1, axis=2) - p
            prv = np.roll(p, 1, axis=2) - p
            nn = n[lo:lo + len(conn)]
            cr = np.einsum("cmki,mi->cmk", SQ._cross(nxt, prv), nn)
            s = cr / np.maximum(np.linalg.norm(nxt, axis=3) * np.linalg.norm(prv, axis=3), 1e-300)
            if sign is not None:
                s = s * sign[None, lo:lo + len(conn), None]
            S[:, lo:lo + len(conn), :k] = s
            A[:, lo:lo + len(conn)] = 0.5 * np.einsum("cmi,mi->cm", SQ._cross(p, np.roll(p, -1, axis=2)).sum(axis=2), nn)
        return Dc, S, A

    def _guard(self, d_old: np.ndarray, d_new: np.ndarray) -> np.ndarray:
        """Per copy (d_new: C, elements, lines): no element comes out past a line it was inside, nor further past one
        it was past (NaN where a line reads no such element: never a failure)."""
        tol = self.tols[None, None, :]
        inside = d_old[None] <= tol
        newly = inside & (d_new > tol)
        further = (~inside) & (d_new > d_old[None] + 1e-12)
        return ~(newly | further).any(axis=(1, 2))

    def move(self, v: int, e: int, j: int) -> bool:
        """Node v of element e tried round where it stands, in its face's plane (along its straight boundary line,
        for a node on one); kept where e's deviation on line j falls most under the guard."""
        els = sorted(self.of[v], key=lambda x: (x >= self.nq, x))
        conns = [self._conn(x) for x in els]
        loc = sorted({u for c in conns for u in c})
        idx = {u: i for i, u in enumerate(loc)}
        lc = [[idx[u] for u in c] for c in conns]
        ie = els.index(e)
        ns = self.en[els]
        n = self.normal[v]
        if not self.movable[v] and v not in self.slide:
            n = ns.sum(axis=0)
            n = n / max(float(np.linalg.norm(n)), 1e-300)
        X = self.X
        p0 = X[v].copy()
        nbr = sorted({c[i - 1] for c in conns for i, u in enumerate(c) if u == v}
                     | {c[(i + 1) % len(c)] for c in conns for i, u in enumerate(c) if u == v})
        if not nbr:
            return self._no(v, "no_neighbour")
        h = float(np.linalg.norm(X[nbr] - p0, axis=1).min())
        if h <= 0:
            return self._no(v, "zero_edge")
        for steps in (STEPS, FINE_STEPS):
            if self._move_at(v, e, j, steps, els, conns, loc, idx, lc, ie, ns, n, p0, nbr, h):
                self.why.pop(v, None)
                return True
        return False

    def _move_at(self, v: int, e: int, j: int, steps: tuple, els: list, conns: list, loc: list, idx: dict,
                 lc: list, ie: int, ns: np.ndarray, n: np.ndarray, p0: np.ndarray, nbr: list, h: float) -> bool:
        """`move` at the steps given (shares of h); True when node v moved."""
        X = self.X
        put: Callable[[np.ndarray], np.ndarray] | None = None
        if v in self.slide:
            line = self.slide[v]
            cands = [p0 + sg * s * h * line for s in steps for sg in (1.0, -1.0)]
        elif v in self.arc_ends:
            # (on a CAD curve: tried along its tangent there; the best put back on the curve)
            c_ = self._curve_of(v)
            if c_ is None or self.project is None:
                return self._no(v, "no_cad_curve")
            ends = self.arc_ends[v]
            tan = X[ends[1]] - X[ends[0]]
            tan = tan / max(float(np.linalg.norm(tan)), 1e-300)
            cands = [p0 + sg * s * h * tan for s in steps for sg in (1.0, -1.0)]
            on_curve = self.project.on_curve
            put = lambda P: self._cad(on_curve, c_, P)  # noqa: E731
        else:
            a = np.cross(n, [1.0, 0.0, 0.0])
            if np.linalg.norm(a) < 0.5:
                a = np.cross(n, [0.0, 1.0, 0.0])
            a = a / np.linalg.norm(a)
            b = np.cross(n, a)
            mid = X[nbr].mean(axis=0)
            cands = [mid - float((mid - p0) @ n) * n] if steps is STEPS else []
            for s in steps:
                for t in range(TURNS):
                    ang = 2.0 * math.pi * t / TURNS
                    cands.append(p0 + s * h * (math.cos(ang) * a + math.sin(ang) * b))
            if v in self.on_face and self.project is not None:
                # (on a curved face: tried in its tangent plane; the best put back on the CAD surface)
                surface, k_ = self.project.surface, self.on_face[v]
                put = lambda P: self._cad(surface, k_, P)  # noqa: E731
        P = np.array(cands)
        if not np.isfinite(P).all():
            return self._no(v, "not_finite")
        base = X[loc][None]
        d_old, _s_old, a_old = self._batch(base, lc, ns, None)
        # (each element kept turned the way its area is: a folded corner may unfold, none may fold)
        sign = np.sign(a_old[0])
        if (sign == 0).any():
            return self._no(v, "flat_element")
        why = [""]

        def judged(P: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            XX = np.repeat(base, len(P), axis=0)
            XX[:, idx[v]] = P
            d_new, s_new, _a = self._batch(XX, lc, ns, sign)
            gain = d_new[:, ie, j]
            better = gain < float(d_old[0, ie, j]) - 1e-9 * float(self.scale[j])
            shape = (s_new > CORNER_SIN).all(axis=(1, 2))
            guard = self._guard(d_old[0], d_new)
            ok = shape & guard
            if self.low is not None or self.high is not None:
                l0 = np.linalg.norm(X[nbr] - p0, axis=1)
                l1 = np.linalg.norm(P[:, None, :] - X[nbr][None], axis=2)
                if self.low is not None:
                    ok &= ~((l0[None] >= self.low) & (l1 < self.low)).any(axis=1)
                if self.high is not None:
                    ok &= ~((l0[None] <= self.high) & (l1 > self.high)).any(axis=1)
            ok &= better
            why[0] = _reason(better, shape, guard, ok)
            return ok, gain

        ok, gain = judged(P)
        if put is not None:
            # (on the CAD: only the best few that pass in the tangent plane are put on it, then judged again there)
            if not ok.any():
                return self._no(v, why[0])
            best = np.flatnonzero(ok)[np.argsort(gain[ok], kind="stable")][:PROJECT_MAX]
            P = np.asarray(put(P[best]), dtype=float).reshape(-1, 3)
            P = P[np.isfinite(P).all(axis=1)]
            if not len(P):
                return self._no(v, "off_cad")
            ok, gain = judged(P)
            if not ok.any():
                return self._no(v, "on_cad_" + why[0])
        if not ok.any():
            return self._no(v, why[0])
        pick = int(np.flatnonzero(ok)[np.argmin(gain[ok])])
        self.log.append(("x", v, X[v].copy(), P[pick].copy()))
        self.pos = len(self.log)
        X[v] = P[pick]
        self.moved += 1
        return True

    def _dirs(self, v: int) -> tuple[np.ndarray, Callable[[np.ndarray], np.ndarray] | None] | None:
        """Node v's directions to be tried in (unit, (k, 3)) and how a position is put back on its CAD (or None)."""
        if v in self.slide:
            return np.array([self.slide[v], -self.slide[v]]), None
        if v in self.arc_ends:
            c_ = self._curve_of(v)
            if c_ is None or self.project is None:
                return None
            ends = self.arc_ends[v]
            tan = self.X[ends[1]] - self.X[ends[0]]
            tan = tan / max(float(np.linalg.norm(tan)), 1e-300)
            on_curve = self.project.on_curve
            return np.array([tan, -tan]), (lambda P: self._cad(on_curve, c_, P))
        n = self.normal[v] if self.movable[v] else self.en[self.of[v]].sum(axis=0)
        n = n / max(float(np.linalg.norm(n)), 1e-300)
        a = np.cross(n, [1.0, 0.0, 0.0])
        if np.linalg.norm(a) < 0.5:
            a = np.cross(n, [0.0, 1.0, 0.0])
        a = a / np.linalg.norm(a)
        b = np.cross(n, a)
        ang = 2.0 * np.pi * np.arange(TURNS) / TURNS
        dirs = np.cos(ang)[:, None] * a[None] + np.sin(ang)[:, None] * b[None]
        if v in self.on_face and self.project is not None:
            surface, k_ = self.project.surface, self.on_face[v]
            return dirs, (lambda P: self._cad(surface, k_, P))
        return dirs, None

    def pair(self, v: int, w: int, e: int, j: int) -> bool:
        """SHMESH-100: two neighbouring nodes v, w of element e moved together (each in its own plane, line or curve),
        kept where e's deviation on line j falls most under the same guard as one node's move."""
        dv, dw = self._dirs(v), self._dirs(w)
        if dv is None or dw is None:
            return False
        els = sorted(set(self.of[v]) | set(self.of[w]), key=lambda x: (x >= self.nq, x))
        conns = [self._conn(x) for x in els]
        loc = sorted({u for c in conns for u in c})
        idx = {u: i for i, u in enumerate(loc)}
        lc = [[idx[u] for u in c] for c in conns]
        ie = els.index(e)
        ns = self.en[els]
        X = self.X
        edges = sorted({(min(c[i], c[(i + 1) % len(c)]), max(c[i], c[(i + 1) % len(c)])) for c in lc
                        for i in range(len(c)) if idx[v] in (c[i], c[(i + 1) % len(c)])
                        or idx[w] in (c[i], c[(i + 1) % len(c)])})
        ea, eb = np.array([x[0] for x in edges]), np.array([x[1] for x in edges])
        base = X[loc][None]
        l0 = np.linalg.norm(base[0, ea] - base[0, eb], axis=1)
        h = float(l0.min()) if len(l0) else 0.0
        if h <= 0:
            return False
        d_old, _s_old, a_old = self._batch(base, lc, ns, None)
        sign = np.sign(a_old[0])
        if (sign == 0).any():
            return False
        iv, iw = idx[v], idx[w]

        why = [""]

        def judged(Pv: np.ndarray, Pw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            XX = np.repeat(base, len(Pv), axis=0)
            XX[:, iv], XX[:, iw] = Pv, Pw
            d_new, s_new, _a = self._batch(XX, lc, ns, sign)
            shape = (s_new > CORNER_SIN).all(axis=(1, 2))
            guard = self._guard(d_old[0], d_new)
            ok = shape & guard
            l1 = np.linalg.norm(XX[:, ea] - XX[:, eb], axis=2)
            if self.low is not None:
                ok &= ~((l0[None] >= self.low) & (l1 < self.low)).any(axis=1)
            if self.high is not None:
                ok &= ~((l0[None] <= self.high) & (l1 > self.high)).any(axis=1)
            gain = d_new[:, ie, j]
            better = gain < float(d_old[0, ie, j]) - 1e-9 * float(self.scale[j])
            ok &= better
            why[0] = _reason(better, shape, guard, ok)
            return ok, gain

        (av, put_v), (aw, put_w) = dv, dw
        steps = np.array(PAIR_STEPS)[:, None, None, None] * h
        Pv = (X[v][None, None] + steps * av[None, :, None, :] * np.ones((1, 1, len(aw), 1))).reshape(-1, 3)
        Pw = (X[w][None, None] + steps * aw[None, None, :, :] * np.ones((1, len(av), 1, 1))).reshape(-1, 3)
        ok, gain = judged(Pv, Pw)
        if not ok.any():
            return self._no(None, "pair_" + why[0])
        if put_v is not None or put_w is not None:
            best = np.flatnonzero(ok)[np.argsort(gain[ok], kind="stable")][:PROJECT_MAX]
            Pv = np.asarray(put_v(Pv[best]), dtype=float).reshape(-1, 3) if put_v is not None else Pv[best]
            Pw = np.asarray(put_w(Pw[best]), dtype=float).reshape(-1, 3) if put_w is not None else Pw[best]
            fin = np.isfinite(Pv).all(axis=1) & np.isfinite(Pw).all(axis=1)
            Pv, Pw = Pv[fin], Pw[fin]
            if not len(Pv):
                return self._no(None, "pair_off_cad")
            ok, gain = judged(Pv, Pw)
            if not ok.any():
                return self._no(None, "pair_on_cad_" + why[0])
        pick = int(np.flatnonzero(ok)[np.argmin(gain[ok])])
        self.why.pop(v, None)
        self.why.pop(w, None)
        for u, P in ((v, Pv), (w, Pw)):
            self.log.append(("x", u, X[u].copy(), P[pick].copy()))
            X[u] = P[pick]
        self.pos = len(self.log)
        self.moved += 2
        self.paired += 1
        return True

    def turn(self, e: int, j: int) -> bool:
        """Triangle e and the triangle across one of its sides on the same flat face: their diagonal turned where the
        worse of the two improves on line j under the guard."""
        if e < self.nq or self.frozen[e]:
            return False
        k = int(self.face_of[e])
        if not self._known(k):
            return False
        t = self.T[e - self.nq].tolist()
        for i in range(3):
            a, b, c = t[i], t[(i + 1) % 3], t[(i + 2) % 3]
            other = [x for x in set(self.of[a]) & set(self.of[b]) if x != e]
            if len(other) != 1 or other[0] < self.nq:
                continue
            f = other[0]
            if int(self.face_of[f]) != k or self.frozen[f]:
                continue
            u = self.T[f - self.nq].tolist()
            d = next((x for x in u if x not in (a, b)), None)
            if d is None or d in t or set(self.of[c]) & set(self.of[d]) or self.held[[a, b, c, d]].any():
                continue
            loc = [a, b, c, d]
            n = np.array([self.en[e], self.en[f]])
            if k not in self.flat:
                n[:] = (n[0] + n[1]) / max(float(np.linalg.norm(n[0] + n[1])), 1e-300)
            old = [[0, 1, 2], [loc.index(x) for x in u]]
            new = [[0, 3, 2], [3, 1, 2]]
            base = self.X[loc][None]
            d_old, _s_old, a_old = self._batch(base, old, n, None)
            sign = np.sign(a_old[0])
            if sign[0] == 0 or sign[0] != sign[1]:
                continue
            d_new, s_new, _a = self._batch(base, new, n, sign)
            if not (s_new[:, :, :3] > CORNER_SIN).all():
                continue
            if float(np.nanmax(d_new[0, :, j])) >= float(np.nanmax(d_old[0, :, j])) - 1e-9 * float(self.scale[j]):
                continue
            # the guard on the pair as a whole: per line, no more of the two past it, and the worse no worse past it
            tol = self.tols
            do_ = np.where(np.isnan(d_old[0]), -np.inf, d_old[0])
            dn_ = np.where(np.isnan(d_new[0]), -np.inf, d_new[0])
            wo, wn = do_.max(axis=0), dn_.max(axis=0)
            no_, nn_ = (d_old[0] > tol).sum(axis=0), (d_new[0] > tol).sum(axis=0)
            if (nn_ > no_).any() or ((wo <= tol) & (wn > tol)).any() or ((wo > tol) & (wn > wo + 1e-12)).any():
                continue
            if self.low is not None or self.high is not None:
                l_new = float(np.linalg.norm(self.X[c] - self.X[d]))
                l_old = float(np.linalg.norm(self.X[a] - self.X[b]))
                if self.low is not None and l_old >= self.low > l_new:
                    continue
                if self.high is not None and l_old <= self.high < l_new:
                    continue
            # turned: the two rows keep their numbers (their face, wall, groups); the node table follows
            ent = ("t", e, f, (a, b, c, d), list(t), list(u))
            self._turn_rows(ent, True)
            self.log.append(ent)
            self.pos = len(self.log)
            self.turned += 1
            return True
        return False

    def sweep(self, past_cols: list[int], deadline: float | None, clock: Callable[[], float]) -> bool:
        """One pass over the elements past the lines `past_cols`, the furthest past first. False when the clock
        stopped it."""
        D = _deviations(self.X, self.Q, self.T, self.lines)
        cols = np.array(past_cols, dtype=np.int64)
        sub = np.where(np.isnan(D[:, cols]), -np.inf, D[:, cols] / self.scale[cols])
        past = sub > (self.tols[cols] / self.scale[cols])
        hit = np.flatnonzero(past.any(axis=1))
        if not len(hit):
            return True
        how = np.where(past[hit], sub[hit], -np.inf)
        j_of = cols[np.argmax(how, axis=1)]
        order = np.argsort(-how.max(axis=1), kind="stable")[:TARGET_MAX]
        if len(hit) > TARGET_MAX:
            self.refused["not_reached"] = self.refused.get("not_reached", 0) + len(hit) - TARGET_MAX
        for i in order.tolist():
            if deadline is not None and clock() >= deadline:
                return False
            e, j = int(hit[i]), int(j_of[i])
            if e in self.banned:
                self._no(None, "set_aside")
                continue
            start = len(self.log)
            self.marks.append((start, e))
            conn = self._conn(e)
            free = [self._can(v) for v in conn]
            for v, f in zip(conn, free, strict=True):
                if f:
                    self.move(v, e, j)
            if e >= self.nq:
                self.turn(e, j)
            if len(self.log) == start and PAIRS:
                # (SHMESH-100: no single node helped - two neighbouring nodes of it moved together)
                for i in range(len(conn)):
                    if free[i] and free[(i + 1) % len(conn)] and self.pair(conn[i], conn[(i + 1) % len(conn)], e, j):
                        break
        return True


#: (every datum _rebuilt carries itself; round 10: held_kind too - the mesher writes it on every mesh since the
#: census, and a field missing here made _carried refuse every re-mesh, so no patch was re-made on any part)
_MESH_FIELDS = {f.name for f in dataclasses.fields(ShellMesh)} | {"face_of", "held", "held_kind"}


def _carried(mesh: ShellMesh) -> bool:
    """Whether a re-made patch can carry every per-node and per-element datum the mesh has (none unknown to it)."""
    sizes = {len(mesh.xyz), mesh.n_elements}
    for k, v in vars(mesh).items():
        if k not in _MESH_FIELDS and hasattr(v, "__len__") and not isinstance(v, (str, dict)) and len(v) in sizes:
            return False
    return True


def _rebuilt(mesh: ShellMesh, face_of: np.ndarray, held: np.ndarray, patches: list[dict]) -> ShellMesh:
    """The mesh with each patch's elements made again: kept elements keep their order (quads first, then triangles),
    the new ones follow each kind's, every datum copied from the patch's own element; nodes no element uses go."""
    nq0, ne0, n0 = len(mesh.quads), mesh.n_elements, len(mesh.xyz)
    gone: set[int] = set()
    newq: list[list[int]] = []
    newt: list[list[int]] = []
    srcq: list[int] = []
    srct: list[int] = []
    pts = [np.asarray(mesh.xyz, dtype=float)]
    nxt = n0
    for pt in patches:
        gone.update(pt["elems"])
        m = len(pt["ring"])
        ids = list(range(nxt, nxt + len(pt["new"])))
        nxt += len(pt["new"])
        pts.append(np.asarray(pt["new"], dtype=float).reshape(-1, 3))
        for c in pt["conns"]:
            g = [int(pt["ring"][i]) if i < m else ids[i - m] for i in c]
            (newq if len(g) == 4 else newt).append(g)
            (srcq if len(g) == 4 else srct).append(int(pt["tmpl"]))
    keep_q = [e for e in range(nq0) if e not in gone]
    keep_t = [e for e in range(nq0, ne0) if e not in gone]
    quads = np.vstack([np.asarray(mesh.quads, dtype=np.int64).reshape(-1, 4)[keep_q],
                       np.array(newq, dtype=np.int64).reshape(-1, 4)])
    trias = np.vstack([np.asarray(mesh.trias, dtype=np.int64).reshape(-1, 3)[[e - nq0 for e in keep_t]],
                       np.array(newt, dtype=np.int64).reshape(-1, 3)])
    src = np.array(keep_q + srcq + keep_t + srct, dtype=np.int64)
    is_new = np.r_[np.zeros(len(keep_q), bool), np.ones(len(srcq), bool), np.zeros(len(keep_t), bool),
                   np.ones(len(srct), bool)]
    xyz = np.vstack(pts)
    used = np.zeros(len(xyz), dtype=bool)
    used[quads.ravel()] = True
    used[trias.ravel()] = True
    remap = np.cumsum(used) - 1
    new_of_old = np.full(ne0, -1, dtype=np.int64)
    new_of_old[src[~is_new]] = np.flatnonzero(~is_new)
    groups = {}
    for name, idx in mesh.groups.items():
        idx = np.asarray(idx, dtype=np.int64)
        kept_idx = new_of_old[idx]
        member = np.zeros(ne0, dtype=bool)
        member[idx] = True
        groups[name] = np.sort(np.r_[kept_idx[kept_idx >= 0], np.flatnonzero(is_new & member[src])])
    node_wall = None
    if mesh.node_wall is not None:
        nw = np.asarray(mesh.node_wall, dtype=float)
        extra = [np.full(len(pt["new"]), float(nw[pt["ring"][0]])) for pt in patches]
        node_wall = np.r_[nw, *extra][used] if extra else nw[used]
    out = dataclasses.replace(
        mesh, xyz=xyz[used], quads=remap[quads], trias=remap[trias], wall=np.asarray(mesh.wall)[src],
        mid=[mesh.mid[i] for i in src.tolist()], part=[mesh.part[i] for i in src.tolist()], groups=groups,
        node_wall=node_wall, varies=None if mesh.varies is None else np.asarray(mesh.varies)[src],
        zoffs=None if mesh.zoffs is None else np.asarray(mesh.zoffs)[src], notes=list(mesh.notes),
        part_names=dict(mesh.part_names))
    for k, v in vars(mesh).items():
        if k not in _MESH_FIELDS:
            setattr(out, k, copy.copy(v))
    out.face_of = np.asarray(face_of, dtype=np.int64)[src]  # type: ignore[attr-defined]
    out.held = np.r_[np.asarray(held, dtype=bool), np.zeros(nxt - n0, dtype=bool)][used]  # type: ignore[attr-defined]
    hk = getattr(mesh, "held_kind", None)
    if hk is not None and len(hk) == n0:
        out.held_kind = np.r_[np.asarray(hk, dtype=np.int8), np.zeros(nxt - n0, dtype=np.int8)][used]  # type: ignore[attr-defined]
    elif hasattr(out, "held_kind"):
        del out.held_kind
    return out


def _outline(sw: _Sweep, P: set[int]) -> list[int] | None:
    """The patch P's outline as one loop of nodes in its elements' own order, or None (not one simple loop)."""
    seen: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for f in P:
        c = sw._conn(f)
        for i in range(len(c)):
            a, b = c[i], c[(i + 1) % len(c)]
            seen.setdefault((min(a, b), max(a, b)), []).append((a, b))
    nxt: dict[int, int] = {}
    for runs in seen.values():
        if len(runs) == 1:
            a, b = runs[0]
            if a in nxt:
                return None
            nxt[a] = b
    if len(nxt) < 3:
        return None
    start = next(iter(nxt))
    loop, v = [start], nxt[start]
    while v != start and len(loop) <= len(nxt):
        loop.append(v)
        v = nxt.get(v, -1)
        if v < 0:
            return None
    return loop if v == start and len(loop) == len(nxt) else None


def _remade(sw: _Sweep, mesh: ShellMesh, remesher: Remesher, project: Projector | None, past_cols: list[int],
            stop_at: float | None, clock: Callable[[], float], rec: dict,
            why_patch: dict | None = None) -> list[dict]:
    """SHMESH-100: the patches round the elements still past a line, each made again where that is better with no
    figure in it worse (patches share no element)."""
    why_patch = {} if why_patch is None else why_patch
    D = _deviations(sw.X, sw.Q, sw.T, sw.lines)
    cols = np.array(past_cols, dtype=np.int64)
    sub = np.where(np.isnan(D[:, cols]), -np.inf, D[:, cols] / sw.scale[cols])
    past = sub > (sw.tols[cols] / sw.scale[cols])
    hit = np.flatnonzero(past.any(axis=1))
    how = np.where(past[hit], sub[hit], -np.inf)
    j_of = cols[np.argmax(how, axis=1)]
    order = np.argsort(-how.max(axis=1), kind="stable")[:REMESH_MAX]
    if len(hit) > REMESH_MAX:
        rec.setdefault("refused_patch", {})["not_reached"] = len(hit) - REMESH_MAX
    member: dict[int, frozenset] = {}
    for name, idx in mesh.groups.items():
        for x in np.asarray(idx, dtype=np.int64).tolist():
            member[x] = member.get(x, frozenset()) | {name}
    wall = np.asarray(mesh.wall, dtype=float)
    zoffs = None if mesh.zoffs is None else np.asarray(mesh.zoffs, dtype=float)
    varies = None if mesh.varies is None else np.asarray(mesh.varies, dtype=bool)
    nw = None if mesh.node_wall is None else np.asarray(mesh.node_wall, dtype=float)
    used: set[int] = set()
    out: list[dict] = []
    refused = rec.setdefault("refused_patch", {})

    def no(e: int, code: str) -> None:
        # (SHMESH-100: why the patch round element e was not re-made, kept by where it stands, and counted)
        why_patch[_key(sw.X[sw._conn(e)].mean(axis=0))] = code
        refused[code] = refused.get(code, 0) + 1

    for i in order.tolist():
        if stop_at is not None and clock() >= stop_at:
            break
        e, j = int(hit[i]), int(j_of[i])
        if e in used:
            no(e, "in_a_patch_made")
            continue
        k = int(sw.face_of[e])
        if not sw._known(k) or (k not in sw.flat and project is None):
            no(e, "face_not_known")
            continue
        code = ""
        for rings in REMESH_RINGS:
            P = {e}
            for _r in range(rings):
                P = {x for f in P for v in sw._conn(f) for x in sw.of[v]}
            Pl = sorted(P, key=lambda x: (x >= sw.nq, x))
            if P & used:
                code = code or "next_to_a_patch_made"
                break
            if any(int(sw.face_of[f]) != k for f in Pl):
                code = code or "crosses_a_face_edge"
                break
            if any(bool(sw.frozen[f]) for f in Pl):
                code = code or "typed_place"
                break
            if len({(float(wall[f]), mesh.mid[f], mesh.part[f], member.get(f, frozenset()),
                     None if zoffs is None else float(zoffs[f])) for f in Pl}) != 1 or \
                    (varies is not None and bool(varies[Pl].any())):
                code = code or "mixed_data"
                break
            ring = _outline(sw, P)
            if ring is None:
                code = "outline_not_one_loop"
                continue
            nodes = {v for f in Pl for v in sw._conn(f)}
            inner = sorted(nodes - set(ring))
            if any(sw.held[v] for v in inner):
                code = code or "held_node_inside"
                break
            if any(not set(sw.of[v]) <= P for v in inner):
                code = code or "inner_node_open"
                break
            if nw is not None and float(np.ptp(nw[sorted(nodes)])) > 0:
                code = code or "wall_varies"
                break
            rec["remesh_tried"] = int(rec.get("remesh_tried") or 0) + 1
            got, why = _patch_best(sw, Pl, ring, j, k, remesher, project)
            if got is not None:
                got.update(elems=Pl, tmpl=e)
                out.append(got)
                used |= P
                code = ""
                break
            code = why + ("" if inner else "/no_inner_node")
        if code:
            no(e, code)
    return out


#: (SHMESH-100: how far a patch's best way got, in order; the furthest is why the patch was not re-made)
_PATCH_STAGES = ("no_way", "more_triangles", "off_cad", "fold", "another_figure_worse", "edge_past_typed",
                 "not_better")


def _patch_best(sw: _Sweep, Pl: list[int], ring: list[int], j: int, k: int, remesher: Remesher,
                project: Projector | None) -> tuple[dict | None, str]:
    """Of the remesher's ways to mesh patch Pl's outline `ring`, the one that betters Pl most on line j with no
    figure in it worse (no fold, no more triangles, no edge newly past the user's shortest or longest), or None;
    and, when None, how far its best way got (_PATCH_STAGES)."""
    got = _patch_best_of(sw, Pl, ring, j, k, remesher, project)
    return (got, "") if isinstance(got, dict) else (None, got)


def _patch_best_of(sw: _Sweep, Pl: list[int], ring: list[int], j: int, k: int, remesher: Remesher,
                   project: Projector | None) -> dict | str:
    conns = [sw._conn(f) for f in Pl]
    loc = sorted({u for c in conns for u in c})
    idx = {u: i for i, u in enumerate(loc)}
    lc = [[idx[u] for u in c] for c in conns]
    n = sw.en[Pl].sum(axis=0)
    n = n / max(float(np.linalg.norm(n)), 1e-300)
    base = sw.X[loc][None]
    d_old, s_old, a_old = sw._batch(base, lc, np.repeat(n[None], len(Pl), axis=0), None)
    if not (a_old[0] > 0).all():
        if not (a_old[0] < 0).all():
            return "patch_folded"
        n = -n
    edges_old = np.array([np.linalg.norm(base[0, c[i]] - base[0, c[(i + 1) % len(c)]]) for c in lc
                          for i in range(len(c))])
    ring_xyz = sw.X[ring]
    size = float(np.linalg.norm(ring_xyz - np.roll(ring_xyz, -1, axis=0), axis=1).mean())
    tri_old = sum(1 for c in conns if len(c) == 3)
    tol = sw.tols
    do_ = np.where(np.isnan(d_old[0]), -np.inf, d_old[0])
    wo, no_ = do_.max(axis=0), (do_ > tol).sum(axis=0)
    best, best_w = None, float(wo[j]) - 1e-9 * float(sw.scale[j])
    far = 0
    for new, elems in remesher.patch(ring_xyz, n, size):
        new = np.asarray(new, dtype=float).reshape(-1, 3)
        # (a patch with quads in it gets no more triangles; one of triangles only stays triangles)
        if not elems or (tri_old < len(conns) and sum(1 for c in elems if len(c) == 3) > tri_old):
            far = max(far, 1 if elems else 0)
            continue
        far = max(far, 2)
        if len(new) and k not in sw.flat:
            if project is None:
                continue
            put = np.asarray(sw._cad(project.surface, k, new), dtype=float).reshape(-1, 3)
            if not np.isfinite(put).all() or float(np.linalg.norm(put - new, axis=1).max()) > REMESH_ON * size:
                continue
            new = put
        far = max(far, 3)
        XX = np.vstack([ring_xyz, new])[None]
        el = sorted(elems, key=len, reverse=True)
        d_new, s_new, a_new = sw._batch(XX, el, np.repeat(n[None], len(el), axis=0), None)
        if not ((a_new[0] > 0).all() and (s_new[0] > CORNER_SIN).all()):
            continue
        far = max(far, 4)
        dn_ = np.where(np.isnan(d_new[0]), -np.inf, d_new[0])
        wn, nn_ = dn_.max(axis=0), (dn_ > tol).sum(axis=0)
        if (nn_ > no_).any() or ((wo <= tol) & (wn > tol)).any() or ((wo > tol) & (wn > wo + 1e-12)).any():
            continue
        far = max(far, 5)
        ln = np.array([np.linalg.norm(XX[0, c[i]] - XX[0, c[(i + 1) % len(c)]]) for c in el for i in range(len(c))])
        if sw.low is not None and float(ln.min()) < min(sw.low, float(edges_old.min())) - 1e-12:
            continue
        if sw.high is not None and float(ln.max()) > max(sw.high, float(edges_old.max())) + 1e-12:
            continue
        far = max(far, 6)
        if float(wn[j]) < best_w or (best is None and nn_[j] < no_[j] and float(wn[j]) <= float(wo[j])):
            best, best_w = {"ring": list(ring), "new": new, "conns": [list(c) for c in el]}, float(wn[j])
    return best if best is not None else _PATCH_STAGES[far]


def _remesh(mesh: ShellMesh, state: dict, face_of: np.ndarray, held: np.ndarray, frozen: np.ndarray,
            lines: list[dict], applied: Mapping, requirements: Mapping | None, low: float | None,
            high: float | None, project: Projector | None, remesher: Remesher, stop_at: float | None,
            clock: Callable[[], float], rec: dict, known: dict[int, bool],
            why_patch: dict | None = None) -> tuple[ShellMesh, dict] | None:
    """SHMESH-100: the local re-mesh on the passes' mesh; (the mesh, its state) when kept, else None."""
    past_cols = [j for j, s in enumerate(_summary(state, lines)) if s["past"]]
    if not past_cols:
        return None
    if not _carried(mesh):
        rec.setdefault("refused_patch", {})["mesh_data_not_carried"] = 1
        return None
    sw = _Sweep(mesh, face_of, lines, frozen, low, high, held, project=project)
    sw._face_known.update(known)
    patches = _remade(sw, mesh, remesher, sw.project, past_cols, stop_at, clock, rec, why_patch)
    rec["cad_s"] = round(float(rec.get("cad_s") or 0.0) + sw.cad_s, 2)
    if not patches:
        return None
    m = _rebuilt(mesh, face_of, held, patches)
    st = _measure(m, applied, requirements)
    if kept(state, st, lines):
        rec["remeshed"] = len(patches)
        return m, st
    rec.setdefault("refused_patch", {})["worse_measured_whole"] = len(patches)
    lo, hi, got = 0, len(patches), None
    probes = 0
    while hi - lo > 1 and probes < BISECT_MAX and not (stop_at is not None and clock() >= stop_at):
        mid = (lo + hi) // 2
        m_mid = _rebuilt(mesh, face_of, held, patches[:mid])
        s_mid = _measure(m_mid, applied, requirements)
        probes += 1
        if _not_worse(state, s_mid, lines):
            lo, got = mid, (m_mid, s_mid)
        else:
            hi = mid
    rec["probes"] = int(rec.get("probes") or 0) + probes
    if got is not None and kept(state, got[1], lines):
        rec["remeshed"] = lo
        return got
    return None


def _key(p: Any) -> tuple:
    """A place as a key (rounded to 1e-6 mm): a node or an element found again after a re-make renumbered them."""
    return tuple(np.round(np.asarray(p, dtype=float), 6).tolist())


def _reason(better: np.ndarray, shape: np.ndarray, guard: np.ndarray, ok: np.ndarray) -> str:
    """Why no candidate of a try was taken, as a short code ("" when one was)."""
    if ok.any():
        return ""
    if not better.any():
        return "no_gain"
    if not (better & shape).any():
        return "would_fold"
    if not (better & shape & guard).any():
        return "another_figure_worse"
    return "edge_past_typed"


#: (SHMESH-100 census: the causes, first named first: an element is counted under the first that holds for it)
CAUSES = ("f_typed", "f_held", "b_cad_corner", "c_sliver_face", "a_strip", "b_corner_type", "d_transition",
          "e_curved", "g_free", "h_locked")
_CURVED_DEG = 10.0
_SPREAD = 2.0


def _ideal_ok(w_deg: float, shape: str, lines: list[dict], cache: dict) -> bool:
    """Whether some element of `shape` with its corner in a CAD corner of w_deg degrees (the corner shared by 1 to 4
    elements; the best-shaped such element) is inside every line in force."""
    key = (round(w_deg, 1), shape)
    if key in cache:
        return cache[key]
    tols = np.array([_tol(ln) for ln in lines])
    ok = False
    for n in range(1, 5):
        a = math.radians(w_deg / n)
        if not 0.0 < a < math.pi:
            continue
        c, s = math.cos(a / 2), math.sin(a / 2)
        if shape == "tria":
            cand = [[[0, 0, 0], [r * c, -r * s, 0], [c, s, 0]] for r in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)]
        else:
            cand = [[[0, 0, 0], [c, -s, 0], [x, 0, 0], [c, s, 0]] for x in c + np.linspace(0.05, 3.0, 40)]
        k = 3 if shape == "tria" else 4
        xyz = np.array(cand, dtype=float).reshape(-1, 3)
        conn = np.arange(len(cand) * k, dtype=np.int64).reshape(-1, k)
        empty3, empty4 = np.zeros((0, 3), dtype=np.int64), np.zeros((0, 4), dtype=np.int64)
        D = _deviations(xyz, conn if k == 4 else empty4, conn if k == 3 else empty3, lines)
        if np.where(np.isnan(D), True, D <= tols[None]).all(axis=1).any():
            ok = True
            break
    cache[key] = ok
    return ok


def _corner_faces(applied: Mapping, face_of: np.ndarray, nq: int, sw, bedge: set, bnb: dict, wedge_of) -> dict:
    """SHMESH-78/79 census, measuring only: for each face the mesher mapped on its corners (quality.corner_census, by
    face index) what the mesh holds - its quads and triangles, its corner nodes (the boundary nodes turning most, as
    many as the rule's corners), its sides' segment counts walked round its boundary, and where each triangle sits
    (at a corner, on a side, inside). Held elements are added by census()."""
    q = applied.get("quality") if isinstance(applied, Mapping) else None
    cc = (q.get("corner_census") if isinstance(q, Mapping) else None) or {}
    out: dict[int, dict] = {}
    if not cc:
        return out
    edges: dict[int, list[tuple[int, int]]] = {}
    for a, b, k in bedge:
        if str(k) in cc:
            edges.setdefault(int(k), []).append((int(a), int(b)))
    for ks, plan in cc.items():
        k = int(ks)
        el = np.flatnonzero(face_of == k)
        tr = el[el >= nq]
        n_c = int(plan.get("corners") or 4)
        bn = sorted({v for e_ in edges.get(k, []) for v in e_})
        turn = sorted(((abs(180.0 - wedge_of(v, k)), v) for v in bn if len(bnb.get((v, k), ())) == 2), reverse=True)
        corners = {v for _t, v in turn[:n_c]}
        # (the sides' segment counts: the boundary walked as one loop from a corner, cut at each corner)
        sides: list[int] = []
        adj: dict[int, list[int]] = {}
        for a, b in edges.get(k, []):
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        if corners and adj and all(len(x) == 2 for x in adj.values()):
            start = min(corners)
            prev, cur, run, steps = -1, start, 0, 0
            while steps <= len(adj):
                nxt = adj[cur][0] if adj[cur][0] != prev else adj[cur][1]
                prev, cur, run, steps = cur, nxt, run + 1, steps + 1
                if cur in corners:
                    sides.append(run)
                    run = 0
                if cur == start:
                    break
            if steps != len(adj):
                sides = []              # (more than one loop: not walked)
        at = {"corner": 0, "side": 0, "inside": 0}
        for e in tr.tolist():
            conn = sw._conn(e)
            at["corner" if any(v in corners for v in conn) else
               "side" if any((v, k) in bnb for v in conn) else "inside"] += 1
        opp = (len(sides) == 4 and (sides[0] != sides[2] or sides[1] != sides[3]))
        out[k] = {**dict(plan), "quads": int(len(el) - len(tr)), "trias": int(len(tr)),
                  "trias_at": at, "mesh_counts": sides, "opposite_differ": bool(opp), "held_elems": 0, "held": {},
                  "corner_turn_deg": [round(t_, 1) for t_, _v in turn[:n_c]], "_corners": corners}
    return out


def census(mesh: ShellMesh, applied: Mapping, lines: list[dict], project: Projector | None,
           why_node: Mapping | None = None, why_patch: Mapping | None = None, deadline: float | None = None,
           clock: Callable[[], float] = time.monotonic) -> dict:
    """SHMESH-100, measuring only: every element of `mesh` still past a line in force, put under one cause (CAUSES,
    the first that holds), measured from the mesh: (f) a place the user sized / a structure's held node; (b) at a CAD
    corner whose angle on the face (the face's elements' angles there, summed) no element of either kind can meet the
    lines in, or (b_corner_type) only the other kind can; (c) on a face thinner than the element size (2 x area /
    perimeter under the median element size); (a) in a strip one element wide between two runs of the face's edge;
    (d) where the sizes round it differ over 2 times, or next to a sized place or held node; (e) on a curved face,
    warped or turned over 10 degrees from an element by it; (g) a free element, with why its nodes' moves and its
    patch were refused; (h) every node locked (on a CAD edge or a face not on the CAD). Bounded: counts, and at most
    CENSUS_KEEP measures per cause."""
    t0 = clock()
    face_of = np.asarray(mesh.face_of, dtype=np.int64)  # type: ignore[attr-defined]
    held = getattr(mesh, "held", None)
    held = np.asarray(held, dtype=bool) if held is not None and len(held) == len(mesh.xyz) else None
    hkind = getattr(mesh, "held_kind", None)
    hkind = np.asarray(hkind, dtype=np.int64) if hkind is not None and len(hkind) == len(mesh.xyz) else None
    from meshpipeline.engines.gmsh.shell_adapter import HELD_KINDS
    held_by: dict[str, int] = {}
    held_by_row: dict[str, dict[str, int]] = {}
    held_face_rows: dict[str, dict[str, int]] = {}
    frozen = _frozen_elements(mesh, applied)
    sizing = applied.get("sizing") or {}
    low = sizing.get("low_mm") if isinstance(sizing, Mapping) else None
    high = sizing.get("high_mm") if isinstance(sizing, Mapping) else None
    sw = _Sweep(mesh, face_of, lines, frozen, float(low) if low else None, float(high) if high else None, held,
                project=project)
    why_node, why_patch = why_node or {}, why_patch or {}
    X, nq = sw.X, sw.nq
    ne = nq + len(sw.T)
    D = _deviations(X, sw.Q, sw.T, lines)
    past = np.where(np.isnan(D), False, D > sw.tols[None])
    hit = np.flatnonzero(past.any(axis=1))
    out: dict = {"elements_past": int(len(hit)), "by_cause": {}, "by_row": {}, "any_label": {}, "subs": {},
                 "measures": {}, "cut": False}
    if not len(hit):
        out["seconds"] = round(clock() - t0, 2)
        return out
    elem = np.full((ne, 4), -1, dtype=np.int64)
    elem[:nq] = sw.Q
    elem[nq:, :3] = sw.T
    P = X[np.maximum(elem, 0)]
    quad = np.arange(ne) < nq
    d2 = np.where(quad[:, None], P[:, 2] - P[:, 0], P[:, 1] - P[:, 0])
    d3 = np.where(quad[:, None], P[:, 3] - P[:, 1], P[:, 2] - P[:, 0])
    area = 0.5 * np.linalg.norm(np.cross(d2, d3), axis=1)
    nxt = np.where(quad[:, None, None], np.roll(P, -1, axis=1), np.concatenate([np.roll(P[:, :3], -1, axis=1),
                                                                             P[:, 3:]], axis=1))
    el = np.linalg.norm(nxt - P, axis=2)
    size = np.where(quad, el.mean(axis=1), el[:, :3].mean(axis=1))
    h0 = float(np.median(size))
    # each face's boundary (an edge not shared by two of its own elements, or shared by more than two in all)
    bnb: dict[tuple[int, int], set[int]] = {}
    perim: dict[int, float] = {}
    bedge: set[tuple[int, int, int]] = set()
    for (a, b, k), n in sw._face_n.items():
        if n != 2 or sw._all_n[(a, b)] != 2:
            bedge.add((a, b, k))
            bnb.setdefault((a, k), set()).add(b)
            bnb.setdefault((b, k), set()).add(a)
            perim[k] = perim.get(k, 0.0) + float(np.linalg.norm(X[a] - X[b]))
    farea = np.bincount(face_of, weights=area)
    wedge: dict[tuple[int, int], float] = {}

    def wedge_of(v: int, k: int) -> float:
        if (v, k) not in wedge:
            tot = 0.0
            for f in sw.of.get(v, []):
                if int(face_of[f]) != k:
                    continue
                c = sw._conn(f)
                i = c.index(v)
                u, w = X[c[i - 1]] - X[v], X[c[(i + 1) % len(c)]] - X[v]
                cs = float(u @ w) / max(float(np.linalg.norm(u) * np.linalg.norm(w)), 1e-300)
                tot += math.degrees(math.acos(min(1.0, max(-1.0, cs))))
            wedge[(v, k)] = tot
        return wedge[(v, k)]

    cface = _corner_faces(applied, face_of, nq, sw, bedge, bnb, wedge_of)
    cache: dict = {}
    ids = [str(ln["id"]) for ln in lines]
    warp = {j for j, ln in enumerate(lines) if any("warp" in str(f) for f in ln["shapes"].values())}
    by_cause: dict[str, int] = {}
    any_label: dict[str, int] = {}
    by_row: dict[str, dict[str, int]] = {}
    subs: dict[str, dict[str, int]] = {}
    meas: dict[str, list] = {}
    order = hit[np.argsort(-np.nanmax(np.where(past[hit], D[hit] / sw.scale[None], -np.inf), axis=1),
                           kind="stable")][:CENSUS_MAX]
    if len(hit) > CENSUS_MAX:
        out["not_classified"] = int(len(hit) - CENSUS_MAX)
    for n_done, e in enumerate(order.tolist()):
        if n_done % 256 == 0 and deadline is not None and clock() >= deadline:
            out["cut"] = True
            out["not_classified"] = int(len(order) - n_done)
            break
        conn = sw._conn(e)
        k = int(face_of[e])
        shape = "quad" if e < nq else "tria"
        labels: list[str] = []
        sub: dict[str, str] = {}
        if bool(frozen[e]):
            labels.append("f_typed")
        if bool(sw.held[conn].any()):
            labels.append("f_held")
        width = 2.0 * float(farea[k]) / max(perim.get(k, 0.0), 1e-300)
        if width < h0:
            labels.append("c_sliver_face")
            meas.setdefault("c_sliver_face", []).append(round(width / h0, 3))
        bn = [v for v in conn if (v, k) in bnb]
        worst_w, impossible, other_kind = math.inf, False, False
        for v in bn:
            if len(bnb[(v, k)]) != 2:
                continue
            w = wedge_of(v, k)
            if abs(w - 180.0) <= 1.0:
                continue
            mine = _ideal_ok(w, shape, lines, cache)
            if mine:
                continue
            if not _ideal_ok(w, "quad" if shape == "tria" else "tria", lines, cache):
                impossible = True
                worst_w = min(worst_w, w)
            else:
                other_kind = True
        if impossible:
            labels.append("b_cad_corner")
            meas.setdefault("b_cad_corner", []).append(round(worst_w, 1))
        if len(bn) == len(conn):
            parent = {v: v for v in conn}

            def root(v: int, parent: dict[int, int] = parent) -> int:
                while parent[v] != v:
                    v = parent[v]
                return v
            lens = []
            for i, v in enumerate(conn):
                w = conn[(i + 1) % len(conn)]
                if (min(v, w), max(v, w), k) in bedge:
                    parent[root(v)] = root(w)
                    lens.append(float(np.linalg.norm(X[v] - X[w])))
            if len({root(v) for v in conn}) >= 2:
                labels.append("a_strip")
                across = (2.0 * float(area[e]) / max(lens)) if shape == "tria" and lens else \
                    float(area[e]) / max(float(np.mean(lens)) if lens else float(size[e]), 1e-300)
                meas.setdefault("a_strip", []).append(round(across / h0, 3))
        if other_kind:
            labels.append("b_corner_type")
        ring = sorted({f for v in conn for f in sw.of.get(v, [])})
        sr = size[ring]
        if float(sr.max()) > _SPREAD * max(float(sr.min()), 1e-300):
            labels.append("d_transition")
            sub["d_transition"] = "sizes_differ"
            meas.setdefault("d_transition", []).append(round(float(sr.max() / max(float(sr.min()), 1e-300)), 2))
        elif bool(frozen[ring].any()) or bool(sw.held[sorted({v for f in ring for v in sw._conn(f)})].any()):
            labels.append("d_transition")
            sub["d_transition"] = "next_to_typed_or_held"
        if k not in sw.flat:
            same = [f for f in ring if int(face_of[f]) == k and f != e]
            turn = max((math.degrees(math.acos(min(1.0, abs(float(sw.en[e] @ sw.en[f]))))) for f in same),
                       default=0.0)
            if turn > _CURVED_DEG or any(past[e, j] for j in warp):
                labels.append("e_curved")
        if not labels:
            free = [v for v in conn if sw._can(v)]
            if free:
                labels.append("g_free")
                codes = sorted({str(why_node.get(_key(X[v]), "never_refused")) for v in free})
                sub["g_free"] = "+".join(codes) + " | patch: " + str(why_patch.get(_key(X[conn].mean(axis=0)),
                                                                                    "not_tried"))
            else:
                labels.append("h_locked")
                sub["h_locked"] = "face_not_on_cad" if not sw._known(k) else (
                    "every_node_on_the_face_edge" if len(bn) == len(conn) else "nodes_locked")
        if "f_held" in labels:
            sub["f_held"] = "+".join(x for x in labels if x != "f_held") or "only"
        first = next(c for c in CAUSES if c in labels)
        if first == "f_held":
            # (which structure holds it: the held nodes' own kinds, joined)
            ks = sorted({int(hkind[v]) for v in conn if bool(sw.held[v])}) if hkind is not None else []
            hb = "+".join(HELD_KINDS[k] if 0 < k < len(HELD_KINDS) else "unknown" for k in ks) or "unknown"
            held_by[hb] = held_by.get(hb, 0) + 1
            hr = held_by_row.setdefault(hb, {})
            for rid in sorted({ids[j] for j in np.flatnonzero(past[e]).tolist()}):
                hr[rid] = hr.get(rid, 0) + 1
            if "mapped strip" in hb:
                # (per face: the mapped strip's held element-rows, joined to quality.strip_census by face index)
                fr = held_face_rows.setdefault(str(k), {})
                for rid in sorted({ids[j] for j in np.flatnonzero(past[e]).tolist()}):
                    fr[rid] = fr.get(rid, 0) + 1
                if k in cface:
                    # (SHMESH-78/79 census: the held element's kind, where it sits on its corner-mapped face, rows)
                    cf = cface[k]
                    pos = ("corner" if any(v in cf["_corners"] for v in conn) else
                           "side" if any((v, k) in bnb for v in conn) else "inside")
                    cf["held_elems"] += 1
                    for rid in sorted({ids[j] for j in np.flatnonzero(past[e]).tolist()}):
                        key_ = f"{shape}|{pos}|{rid}"
                        cf["held"][key_] = cf["held"].get(key_, 0) + 1
        by_cause[first] = by_cause.get(first, 0) + 1
        for c in labels:
            any_label[c] = any_label.get(c, 0) + 1
        if first in sub:
            s = subs.setdefault(first, {})
            s[sub[first]] = s.get(sub[first], 0) + 1
        # (the solver's cards may stand on two lines: a card's elements are counted once)
        for rid in sorted({ids[j] for j in np.flatnonzero(past[e]).tolist()}):
            r = by_row.setdefault(rid, {})
            r[first] = r.get(first, 0) + 1
    out["rows"] = {str(ln["id"]): {"kind": ln["kind"], "name": ln["name"]} for ln in lines}
    if cface:
        out["corner_faces"] = {str(k_): {n_: v_ for n_, v_ in cf_.items() if not n_.startswith("_")}
                               for k_, cf_ in cface.items() if cf_["trias"] or cf_["held_elems"]}
        out["corner_faces_n"] = len(cface)
    out.update(by_cause=by_cause, any_label=any_label, by_row=by_row, subs=subs, h0=round(h0, 4),
               causes=sorted(by_cause), held_by=held_by, held_by_row=held_by_row, held_face_rows=held_face_rows)
    for c, vals in meas.items():
        vs = sorted(vals)
        out["measures"][c] = {"n": len(vs), "min": vs[0], "median": vs[len(vs) // 2], "max": vs[-1],
                              "smallest": vs[:CENSUS_KEEP]}
    out["seconds"] = round(clock() - t0, 2)
    return out


def _words(rec: Mapping) -> str:
    """The one plain line the result says."""
    done = [x for x in rec.get("lines") or [] if x.get("improved")]
    n, s = int(rec.get("kept") or 0), float(rec.get("seconds") or 0.0)
    if not done:
        why = rec.get("why") or "no change to the elements past it made it better"
        return (f"Extra quality passes with the time left did not improve the elements past a limit ({why}); the "
                "mesh is as first made.")
    parts = []
    for x in done:
        if x.get("kind") == "solver":
            parts.append(f"the elements past Nastran's {x['id']} from {x['n_before']:,} to {x['n_after']:,}")
            continue
        unit = " degrees" if x.get("deg") else ""
        if x.get("before") is not None and x.get("after") is not None and x["before"] != x["after"]:
            # (as many digits as show the change: 179.862 to 179.742 is not "180 to 180")
            d = next((k for k in range(3, 8) if f"{x['before']:.{k}g}" != f"{x['after']:.{k}g}"), 8)
            parts.append(f"the {x['name']} from {x['before']:.{d}g} to {x['after']:.{d}g}{unit}")
        else:
            parts.append(f"the elements past the {x['name']} line from {x['n_before']:,} to {x['n_after']:,}")
    said = "; ".join(parts)
    r = int(rec.get("remeshed") or 0)
    remade = (f", re-making {r} small patch{'es' if r != 1 else ''} of elements round the worst ones (the elements "
              "round each patch untouched)") if r else ""
    return f"Improved {said} in {n} extra pass{'es' if n != 1 else ''}{remade} ({s:.0f} s of the time left)."


def run(mesh: ShellMesh, applied: Mapping, requirements: Mapping | None, *, deadline: float | None,
        clock: Callable[[], float] = time.monotonic,
        project: Projector | None = None,
        remesher: Remesher | None = None) -> tuple[ShellMesh, dict, dict | None]:
    """SHMESH-100: the extra quality passes on a mesh made. Returns (the mesh kept, the plan as applied for it, the
    record) - the mesh and plan given, untouched, and record None when no figure is past its limit in force, or
    the passes are off, or there is no time left; the record without a mesh change when no pass was kept."""
    if not QUALITY_PASSES or mesh.n_elements == 0:
        return mesh, dict(applied), None
    face_of = getattr(mesh, "face_of", None)
    lines = lines_in_force(applied, requirements)
    t0 = clock()
    state = _measure(mesh, applied, requirements)
    summ = _summary(state, lines)
    past_cols = [j for j, s in enumerate(summ) if s["past"]]
    if not past_cols:
        return mesh, dict(applied), None
    rec: dict = {"rule": RULE, "tried": 0, "kept": 0, "moved": 0, "turned": 0, "seconds": 0.0, "stopped": ""}
    before = summ
    if deadline is not None and t0 >= deadline:
        return mesh, dict(applied), None
    if face_of is None or len(face_of) != mesh.n_elements:
        rec.update(why="the mesh carries no faces to work on", stopped="no faces")
    else:
        sizing = applied.get("sizing") or {}
        low = sizing.get("low_mm") if isinstance(sizing, Mapping) else None
        high = sizing.get("high_mm") if isinstance(sizing, Mapping) else None
        frozen = _frozen_elements(mesh, applied)
        kept_mesh, kept_state = mesh, state
        held = getattr(mesh, "held", None)
        held = np.asarray(held, dtype=bool) if held is not None and len(held) == len(mesh.xyz) else None
        held_kind = getattr(mesh, "held_kind", None)
        held_kind = np.asarray(held_kind, dtype=np.int8) if held_kind is not None and len(held_kind) == len(
            mesh.xyz) else None
        sw = _Sweep(mesh, face_of, lines, frozen, float(low) if low else None, float(high) if high else None, held,
                    project=project)
        rec["on_cad"] = sw.project is not None
        if not sw.movable.any() and not sw.slide and not len(sw.T) and not sw.on_face and not sw.arc_ends:
            rec.update(why="the elements past it are on curved faces or CAD edges, where these passes move nothing",
                       stopped="nothing to move")
        # (the passes' own stop: PASS_SHARE of the time left when they began; the run's clock in any case)
        stop_at = None if deadline is None else t0 + PASS_SHARE * max(float(deadline) - t0, 0.0)

        def candidate() -> ShellMesh:
            m = dataclasses.replace(kept_mesh, xyz=sw.X.copy(), trias=sw.T.copy(), notes=list(kept_mesh.notes),
                                    groups=dict(kept_mesh.groups), part_names=dict(kept_mesh.part_names))
            m.face_of = np.asarray(face_of, dtype=np.int64).copy()  # type: ignore[attr-defined]
            if held is not None:
                m.held = held.copy()  # type: ignore[attr-defined]
            if held_kind is not None:
                m.held_kind = held_kind.copy()  # type: ignore[attr-defined]
            return m

        best: tuple[ShellMesh, dict, int, int] | None = None
        stall = 0
        slow = 0                # (SHMESH-100 stop rule: passes in a row that did not help)
        rec.update(set_aside=0, probes=0, stalled=0)
        moved_n = turned_n = 0
        for _pass in range(PASS_MAX):
            if stop_at is not None and clock() >= stop_at:
                rec["stopped"] = "the clock" if clock() >= float(deadline or 0.0) else \
                    f"their share of the time left ({PASS_SHARE:.0%})"
                break
            moved0, turned0 = sw.moved, sw.turned
            prev_summ = summ
            sw.begin()
            finished = sw.sweep(past_cols, stop_at, clock)
            rec["tried"] += 1
            if sw.moved == moved0 and sw.turned == turned0:
                rec["stopped"] = rec["stopped"] or "nothing more to improve"
                if not rec["kept"]:
                    rec.setdefault("why", "no node of the elements past it could move, nor a diagonal turn, without "
                                          "another figure getting worse")
                break
            cand = candidate()
            st = _measure(cand, applied, requirements)
            ok = kept(kept_state, st, lines)
            if not ok and not _not_worse(kept_state, st, lines):
                # (SHMESH-100, patch by patch: the longest run of the pass's patches that is no worse measured whole
                # is kept, found by halving; the patch after it is set aside, and the passes go on)
                cuts = sw.patches()
                lo, hi = 0, len(cuts)
                lo_mesh, lo_state = kept_mesh, kept_state
                probes = 0
                while hi - lo > 1 and probes < BISECT_MAX and not (stop_at is not None and clock() >= stop_at):
                    mid = (lo + hi) // 2
                    sw.goto(cuts[mid - 1][1])
                    m_mid = candidate()
                    s_mid = _measure(m_mid, applied, requirements)
                    probes += 1
                    if _not_worse(kept_state, s_mid, lines):
                        lo, lo_mesh, lo_state = mid, m_mid, s_mid
                    else:
                        hi = mid
                rec["probes"] += probes
                if hi - lo == 1 and hi <= len(cuts):
                    sw.banned.add(cuts[hi - 1][2])
                    rec["set_aside"] += 1
                sw.goto(cuts[lo - 1][1] if lo else 0)
                cand, st = lo_mesh, lo_state
                ok = bool(lo) and kept(kept_state, st, lines)
            mv, tn = sw.done()
            moved_n, turned_n = moved_n + mv, turned_n + tn
            kept_mesh, kept_state = cand, st
            if ok:
                rec["kept"] += 1
                best = (kept_mesh, kept_state, moved_n, turned_n)
                stall = 0
            else:
                stall += 1
                rec["stalled"] += 1
            summ = _summary(kept_state, lines)
            past_cols = [j for j, s in enumerate(summ) if s["past"]]
            if not past_cols:
                rec["stopped"] = "every figure inside its limit"
                break
            if not finished:
                rec["stopped"] = f"their share of the time left ({PASS_SHARE:.0%})"
                break
            if GAIN_SHARE is not None:
                # (the stop rule: a pass that took away under GAIN_SHARE of what counts on every line)
                slow = 0 if (ok and _pass_helps(before, prev_summ, summ, lines)) else slow + 1
                if slow >= GAIN_RUN and rec["kept"]:
                    rec["stopped"] = f"gains under {GAIN_SHARE:.0%} in {slow} pass(es)"
                    rec["gain_stop"] = rec["tried"]
                    break
            if stall >= STALL_MAX:
                rec["stopped"] = "a pass not better" if not rec["kept"] else f"no gain in {STALL_MAX} passes"
                if not rec["kept"]:
                    rec.setdefault("why", "the changes it made did not improve the worst figure as measured whole")
                break
        else:
            rec["stopped"] = rec["stopped"] or f"{PASS_MAX} passes"
        rec["cad_s"] = round(sw.cad_s, 2)
        rec["paired"] = sw.paired
        rec.setdefault("remeshed", 0)
        # (SHMESH-100: why the node moves tried were refused, by reason: a few counts)
        rec["refused"] = dict(sorted(sw.refused.items()))
        why_patch: dict = {}
        if REMESH and remesher is not None and not (stop_at is not None and clock() >= stop_at):
            # (SHMESH-100: the local re-mesh, where the passes left an element past a line)
            got = _remesh(kept_mesh, kept_state, np.asarray(face_of, dtype=np.int64),
                          held if held is not None else np.zeros(len(mesh.xyz), dtype=bool), frozen, lines, applied,
                          requirements, sw.low, sw.high, sw.project, remesher, stop_at, clock, rec,
                          dict(sw._face_known), why_patch)
            if got is not None:
                kept_mesh, kept_state = got
                rec["kept"] += 1
                best = (kept_mesh, kept_state, moved_n, turned_n)
        # (the final gate: the mesh handed back is better than the one that came in, and no row of it worse; else
        # the last mesh that was, else the mesh as made)
        x_in = np.asarray(mesh.xyz, dtype=float)
        if kept(state, kept_state, lines):
            mesh, state = kept_mesh, kept_state
        elif best is not None and kept(state, best[1], lines):
            mesh, state, moved_n, turned_n = best
        else:
            rec["kept"] = 0
            rec.setdefault("why", "the changes it made did not improve the worst figure as measured whole")
        rec["moved"], rec["turned"] = (moved_n, turned_n) if rec["kept"] else (0, 0)
        freed = np.flatnonzero(sw.held & ~sw.pinned)
        if len(freed):
            # (SHMESH-102: the strip nodes the moves were let try, and those that stand moved in the mesh handed back -
            # None when a re-made patch renumbered the nodes)
            x_out = np.asarray(mesh.xyz, dtype=float)
            rec["strip_nodes"] = {"free": int(len(freed)), "moved": (
                int(np.any(np.abs(x_out[freed] - x_in[freed]) > 0.0, axis=1).sum()) if len(x_out) == len(x_in)
                else None)}
        if CENSUS:
            try:
                why_node = {_key(sw.X[v]): c for v, c in sw.why.items()}
                rec["census"] = census(mesh, applied, lines, sw.project, why_node, why_patch, deadline, clock)
            except Exception as exc:  # noqa: BLE001 - measuring only: never costs the mesh
                rec["census"] = {"failed": f"{type(exc).__name__}: {str(exc)[:200]}"}
    after = _summary(state, lines)
    rec["lines"] = []
    for ln, a, b in zip(lines, before, after, strict=True):
        if not a["past"]:
            continue
        rec["lines"].append({"kind": ln["kind"], "id": ln["id"], "name": ln["name"], "deg": ln["deg"],
                             "before": a["worst"], "after": b["worst"], "n_before": a["n"], "n_after": b["n"],
                             "still_past": b["past"],
                             "improved": bool(rec["kept"]) and (_better(b["worst"], a["worst"], ln["op"])
                                                                or b["n"] < a["n"])})
    rec["seconds"] = round(clock() - t0, 2)
    rec["words"] = _words(rec)
    out = copy.deepcopy(dict(applied))
    if rec["kept"]:
        q = out.setdefault("quality", {})
        q.update(state["q"])
        sizing = out.get("sizing") or {}
        q["per_face"] = SQ.per_face(mesh, sizing.get("low_mm"), sizing.get("high_mm"),
                                    aims=SR.limits_in_force(requirements).aims)
        if state["rep"] is not None:
            out["requirements"] = state["rep"]
    return mesh, out, rec
