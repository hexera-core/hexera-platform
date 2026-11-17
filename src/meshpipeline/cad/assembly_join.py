# Responsibility: Join an assembly's parts for the mesh (PLAN-fea-35): only parts that touch are fused, each group that touches in its own fuse, each fuse in a child process under a time limit scaled to it; a group that runs out is joined pair by pair, and what still cannot be fused is left apart and tied where it meets - so the run never waits on one fuse without a bound.
# Boundaries: geometry only, deterministic for the same parts and limits, no model call, no persistence. It finds which parts touch and fuses them (cad/fea_assembly.fuse, run in the child); what a joint is, and how it is written, stays with cad/fea_assembly.stage and cad/fea_joins.
# Collaborates with: cad/bounded_steps.py (the child processes), cad/fea_assembly.py (fuse, Fused, the stage and the check's record), engines/fea_driver.py (the decision record and the run's step line).
"""JOIN ONLY WHAT TOUCHES (PLAN-fea-35).

The general fuse of every part at once cuts each part against every other. A turbocharger of ten parts (two bladed
wheels inside their housings) sat in that one fuse for about five hours, one thread, with no limit (lab, 16-17 Nov),
though every pair of its parts that touch fuses in under six seconds on its own; two parts that do not touch at all
(an inlet and a mount 0.1 mm apart) were still past 300 s fused together. So:

1. WHICH PARTS TOUCH, measured: two parts whose boxes, grown by twice the fuse's fuzzy distance and both parts'
   tolerances (reach()), meet are measured by OpenCASCADE's exact distance between the two solids (0 when one
   lies inside the other). Parts within that reach touch. A check that runs out of its time counts as touching -
   the safe side: it only puts more parts in one fuse.
2. EACH CONNECTED GROUP of touching parts is fused on its own (a wheel that touches only its shaft is never in an
   operation with the housings); a part that touches nothing is left as it is. Parts of different groups are
   further apart than anything the fuse would merge, so the groups' fuses together are the fuse of all the parts:
   the same shared faces, from the same faces of the same parts.
3. EACH FUSE HAS A TIME LIMIT (budget_s: a floor and a share per face, capped) and runs in a child process, killed
   when it runs out. A group that runs out (or fails) is fused PAIR BY PAIR: each pair of its parts that touch,
   each under its own limit; the pairs that fused make smaller groups, each fused again under its limit (a group
   of two is its pair's fuse). What still cannot be fused - a pair past its limit, a smaller group past its
   limit - is left apart: those parts are meshed on their own and TIED where they meet (cad/fea_assembly.stage),
   their faces there from the pair's own fuse when it finished, else from the surfaces (cad/fea_joins), said in
   words. The whole join is held to TOTAL_S; past it, nothing more is fused and the rest is tied.

The answer is cad/fea_assembly.Fused: the pieces in one compound (each fused group, each untouched part), every
face's and solid's origins, `unfused` (the touching pairs no fuse joined) and `join` (the decision record).
"""
from __future__ import annotations

import logging
import math
import tempfile
import time
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from meshpipeline.cad import bounded_steps as BS

logger = logging.getLogger(__name__)

RULE = "PLAN-fea-35"
MODULE = "meshpipeline.cad.assembly_join"
#: THE TIME A FUSE MAY TAKE, seconds: FUSE_FLOOR_S, and FUSE_S_PER_FACE for each face of the parts it fuses, at
#: most FUSE_CAP_S. (Measured: see docs/RULES.md PLAN-fea-35.)
FUSE_FLOOR_S = 120.0
FUSE_S_PER_FACE = 0.25
FUSE_CAP_S = 600.0
#: THE TOUCH CHECK of one pair of parts may take this long; past it, the pair counts as touching.
TOUCH_S = 60.0
#: THE WHOLE JOIN (touch checks and every fuse) is held to this; past it nothing more is fused.
TOTAL_S = 1800.0
#: OpenCASCADE's Precision::Confusion (the fuse's own floor on the fuzzy distance)
_CONFUSION = 1e-7

FUSED, TIED = "fused", "tied"


def budget_s(faces: int) -> float:
    """PLAN-fea-35: the time limit of a fuse of parts with `faces` faces in all."""
    return float(min(FUSE_CAP_S, FUSE_FLOOR_S + FUSE_S_PER_FACE * max(int(faces), 0)))


# #
# which parts touch
# #

def _tolerance(shape) -> float:
    """The largest tolerance of the shape's vertices, edges and faces (how far OpenCASCADE takes it to reach)."""
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance

    try:
        return float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1))
    except Exception:  # noqa: BLE001 - the fuse's own floor, then
        return _CONFUSION


def reach(fuzzy: float, tol_a: float, tol_b: float) -> float:
    """How near two parts must come for the fuse to join them, twice over (the safe side)."""
    return 2.0 * (max(float(fuzzy), _CONFUSION) + float(tol_a) + float(tol_b))


def _box(shape, pad: float = 0.0) -> np.ndarray:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, True, False)
    if b.IsVoid():
        return np.array([-math.inf] * 3 + [math.inf] * 3)
    v = np.array(b.Get(), dtype=float)
    return v + np.array([-pad] * 3 + [pad] * 3)


def _meet(a: np.ndarray, b: np.ndarray) -> bool:
    return not (np.any(a[3:] < b[:3]) or np.any(b[3:] < a[:3]))


def near_pairs(parts: list, fuzzy: float) -> tuple[list[tuple[int, int]], list[float]]:
    """The pairs of parts (0-based) whose boxes come within reach(), and each pair's reach."""
    tols = [_tolerance(s) for s in parts]
    boxes = [_box(s) for s in parts]
    out, pads = [], []
    for i, j in combinations(range(len(parts)), 2):
        pad = reach(fuzzy, tols[i], tols[j])
        if _meet(boxes[i] + np.array([-pad] * 3 + [pad] * 3), boxes[j]):
            out.append((i, j))
            pads.append(pad)
    return out, pads


def touches(a, b, pad: float) -> dict:
    """Whether solids `a` and `b` come within `pad`: OpenCASCADE's exact distance between the two solids (on every
    core; 0 when one lies inside the other), never their boxes. {"touch": bool, "how": "surface" | "inside" |
    "apart" | "unknown", "distance": d}. A distance that cannot be measured is "unknown" and counts as touching."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.Extrema import Extrema_ExtFlag

    d = BRepExtrema_DistShapeShape()
    # every core (lab, 17 Nov: a bladed wheel and its housing, 175 s on one thread, 28-32 s on sixteen)
    d.SetMultiThread(True)
    d.SetFlag(Extrema_ExtFlag.Extrema_ExtFlag_MIN)
    d.LoadS1(a)
    d.LoadS2(b)
    d.Perform()
    if not d.IsDone():
        return {"touch": True, "how": "unknown", "distance": None}
    value = float(d.Value())
    if value <= pad:
        return {"touch": True, "how": "inside" if d.InnerSolution() else "surface", "distance": value}
    return {"touch": False, "how": "apart", "distance": value}


def groups(n: int, pairs: list[tuple[int, int]]) -> list[list[int]]:
    """The connected groups of parts 0..n-1 joined by `pairs`, each sorted, in order of their first part."""
    parent = list(range(n))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs:
        a, b = root(i), root(j)
        if a != b:
            parent[max(a, b)] = min(a, b)
    out: dict[int, list[int]] = {}
    for k in range(n):
        out.setdefault(root(k), []).append(k)
    return sorted(out.values(), key=lambda g: g[0])


# #
# the child's side
# #

def _load(payload: dict) -> list:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad.fea_assembly import solids

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(payload["brep"]), BRep_Builder()):
        raise RuntimeError("the parts could not be read back")
    return solids(shape)


def _handle(parts: list, step: dict, work: Path, k: int) -> Any:
    if step["op"] == "touch":
        return touches(parts[int(step["a"])], parts[int(step["b"])], float(step["pad"]))
    if step["op"] == "fuse":
        from OCP.BRepTools import BRepTools

        from meshpipeline.cad.fea_assembly import fuse

        done = fuse([parts[int(p)] for p in step["parts"]], fuzzy=float(step["fuzzy"]),
                    mm_per_unit=float(step["mm_per_unit"]))
        if done.error or done.shape is None:
            return {"error": done.error or "no shape", "seconds": done.seconds}
        path = work / f"{k}.brep"
        if not BRepTools.Write_s(done.shape, str(path)):
            return {"error": "the fused parts could not be written", "seconds": done.seconds}
        return {"error": "", "brep": path.name, "faces": [[list(x) for x in o] for o in done.face_origins],
                "solids": done.solid_parts, "seconds": done.seconds}
    raise ValueError(f"unknown step {step.get('op')}")


# #
# the join
# #

def _read(path: Path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    return shape if BRepTools.Read_s(shape, str(path), BRep_Builder()) else None


class _Piece:
    """One piece of the joined assembly: a fused group (its shape and origins, parts 1-based in the whole
    assembly's numbering) or one part as it is."""

    def __init__(self, parts: list[int], shape, faces: list[list[tuple[int, int]]], solids: list[list[int]]):
        self.parts, self.shape, self.faces, self.solids = parts, shape, faces, solids


def _piece_of(step: BS.Step, members: list[int], work: Path) -> _Piece | None:
    """The fused piece a fuse step made (members 0-based), or None when it did not finish with one."""
    v = step.value if step.ok else None
    if not isinstance(v, dict) or v.get("error") or not v.get("brep"):
        return None
    shape = _read(work / str(v["brep"]))
    if shape is None:
        return None
    glob = [m + 1 for m in members]
    faces = [sorted((glob[int(p) - 1], int(j)) for p, j in o) for o in v["faces"]]
    solids = [sorted(glob[int(p) - 1] for p in o) for o in v["solids"]]
    return _Piece(glob, shape, faces, solids)


def _single(parts: list, k: int) -> _Piece:
    from meshpipeline.cad.fea_assembly import _faces

    return _Piece([k + 1], parts[k], [[(k + 1, j)] for j in range(1, len(_faces(parts[k])) + 1)], [[k + 1]])


def _outcome(step: BS.Step) -> str:
    if step.ok and isinstance(step.value, dict) and not step.value.get("error"):
        return FUSED
    if step.ok:
        return "error"
    return step.why or BS.NOT_RUN


def join(parts: list, *, fuzzy: float, mm_per_unit: float = 1.0, total_s: float | None = None,
         fuse_budget: float | None = None, touch_s: float | None = None):
    """PLAN-fea-35 (the module text): the parts joined group by group, each fuse time-boxed. Returns
    cad/fea_assembly.Fused with `unfused` and `join` set. `fuse_budget` sets every fuse's limit (tests); else
    budget_s() of the faces it fuses."""
    from meshpipeline.cad.fea_assembly import Fused, _faces, compound

    t0 = time.time()
    total = TOTAL_S if total_s is None else float(total_s)
    n = len(parts)
    nfaces = [len(_faces(s)) for s in parts]

    def limit(members: list[int]) -> float:
        return float(fuse_budget) if fuse_budget is not None else budget_s(sum(nfaces[m] for m in members))

    def left() -> float:
        return max(total - (time.time() - t0), 0.0)

    record: dict[str, Any] = {"rule": RULE, "parts": n, "groups": [], "pairs": [], "regroups": [], "unfused": [],
                              "touch": {}, "limits": {"floor_s": FUSE_FLOOR_S, "per_face_s": FUSE_S_PER_FACE,
                                                      "cap_s": FUSE_CAP_S, "total_s": total}}
    with tempfile.TemporaryDirectory(prefix="asm_join_") as tmp:
        work = Path(tmp)
        brep = work / "parts.brep"
        from OCP.BRepTools import BRepTools

        if not BRepTools.Write_s(compound(parts), str(brep)):
            return _apart(parts, [], record, t0, "the parts could not be written for the fuse")
        payload = {"brep": str(brep)}
        # 1. WHICH PARTS TOUCH
        near, pads = near_pairs(parts, fuzzy)
        steps = [{"op": "touch", "a": i, "b": j, "pad": pad} for (i, j), pad in zip(near, pads, strict=True)]
        got = BS.run(MODULE, payload, steps, [float(touch_s or TOUCH_S)] * len(steps), total_s=left(),
                     workdir=work / "touch")
        touching = [pr for pr, s in zip(near, got, strict=True)
                    if not (s.ok and isinstance(s.value, dict) and not s.value.get("touch"))]
        unknown = [pr for pr, s in zip(near, got, strict=True) if not s.ok]
        record["touch"] = {"box_pairs": len(near), "touching": len(touching), "unknown": len(unknown),
                           "seconds": round(sum(s.seconds for s in got), 2)}
        if unknown and all(s.why == BS.STOPPED for s in got):
            # the child cannot run here at all: nothing is fused, every touching pair is tied
            return _apart(parts, near, record, t0, "the fuse could not be run")
        # 2. EACH GROUP ON ITS OWN
        grouped = groups(n, touching)
        multi = [g for g in grouped if len(g) > 1]
        steps = [{"op": "fuse", "parts": g, "fuzzy": fuzzy, "mm_per_unit": mm_per_unit} for g in multi]
        got = BS.run(MODULE, payload, steps, [limit(g) for g in multi], total_s=left(), workdir=work / "groups")
        pieces: list[_Piece] = [_single(parts, g[0]) for g in grouped if len(g) == 1]
        unfused: list[dict] = []
        for g, s in zip(multi, got, strict=True):
            piece = _piece_of(s, g, work / "groups")
            record["groups"].append({"parts": [m + 1 for m in g], "faces": sum(nfaces[m] for m in g),
                                     "limit_s": round(limit(g), 1), "seconds": s.seconds, "outcome": _outcome(s)})
            if piece is not None:
                pieces.append(piece)
                continue
            # 3. A GROUP THAT RAN OUT: pair by pair, then the smaller groups the pairs make
            sub, lost = _by_pairs(parts, g, [pr for pr in touching if pr[0] in g and pr[1] in g], payload, work,
                                  limit, left, record, nfaces, fuzzy, mm_per_unit)
            pieces.extend(sub)
            unfused.extend(lost)
    return _combined(pieces, unfused, record, t0, Fused, compound)


def _by_pairs(parts: list, g: list[int], pairs: list[tuple[int, int]], payload: dict, work: Path, limit, left,
              record: dict, nfaces: list[int], fuzzy: float, mm_per_unit: float) -> tuple[list, list[dict]]:
    """The group `g` that ran out, joined pair by pair (the module text, 3): its pieces and its unfused pairs."""
    tag = "g" + "_".join(str(m + 1) for m in g)
    steps = [{"op": "fuse", "parts": list(pr), "fuzzy": fuzzy, "mm_per_unit": mm_per_unit} for pr in pairs]
    got = BS.run(MODULE, payload, steps, [limit(list(pr)) for pr in pairs], total_s=left(),
                 workdir=work / f"{tag}_pairs")
    fused_pairs: dict[tuple[int, int], _Piece] = {}
    for pr, s in zip(pairs, got, strict=True):
        piece = _piece_of(s, list(pr), work / f"{tag}_pairs")
        record["pairs"].append({"parts": [pr[0] + 1, pr[1] + 1], "limit_s": round(limit(list(pr)), 1),
                                "seconds": s.seconds, "outcome": _outcome(s)})
        if piece is not None:
            fused_pairs[pr] = piece
    pieces: list[_Piece] = []
    joined: set[tuple[int, int]] = set()
    sub = [c for c in groups(len(parts), list(fused_pairs)) if set(c) <= set(g)]
    # a smaller group of three or more is fused again - never the group itself (the very fuse that ran out)
    multi = [c for c in sub if 2 < len(c) < len(g)]
    steps = [{"op": "fuse", "parts": c, "fuzzy": fuzzy, "mm_per_unit": mm_per_unit} for c in multi]
    got = BS.run(MODULE, payload, steps, [limit(c) for c in multi], total_s=left(), workdir=work / f"{tag}_regroups")
    again = dict(zip([tuple(c) for c in multi], got, strict=True))
    if any(len(c) == len(g) for c in sub):
        # every pair fused, and they make the whole group again: its parts are left apart, each pair tied
        record["regroups"].append({"parts": [m + 1 for m in g], "faces": sum(nfaces[m] for m in g),
                                   "limit_s": round(limit(g), 1), "seconds": 0.0, "outcome": "not tried again"})
        again[tuple(g)] = BS.Step(index=-1, why=BS.NOT_RUN)
    for c in sub:
        if len(c) == 1:
            pieces.append(_single(parts, c[0]))
        elif len(c) == 2:
            pieces.append(fused_pairs[(c[0], c[1])])
            joined.add((c[0], c[1]))
        else:
            s = again[tuple(c)]
            piece = _piece_of(s, c, work / f"{tag}_regroups") if s.index >= 0 else None
            if s.index >= 0:
                record["regroups"].append({"parts": [m + 1 for m in c], "faces": sum(nfaces[m] for m in c),
                                           "limit_s": round(limit(c), 1), "seconds": s.seconds,
                                           "outcome": _outcome(s)})
            if piece is not None:
                pieces.append(piece)
                joined.update(pr for pr in pairs if pr[0] in c and pr[1] in c)
            else:
                pieces.extend(_single(parts, m) for m in c)
    lost = [{"parts": [i + 1, j + 1], "pair": fused_pairs.get((i, j))} for i, j in pairs if (i, j) not in joined]
    return pieces, lost


def _apart(parts: list, pairs: list[tuple[int, int]], record: dict, t0: float, why: str):
    """Every part as it is, and every pair whose boxes meet left to be tied (the join could not run at all)."""
    from meshpipeline.cad.fea_assembly import Fused, compound

    record["error"] = why
    pieces = [_single(parts, k) for k in range(len(parts))]
    lost = [{"parts": [i + 1, j + 1], "pair": None} for i, j in pairs]
    return _combined(pieces, lost, record, t0, Fused, compound)


def _combined(pieces: list[_Piece], unfused: list[dict], record: dict, t0: float, fused_cls, compound):
    """The pieces in one compound (in order of their first part), with every face's and solid's origins."""
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Iterator

    from meshpipeline.cad.fea_assembly import _faces, _map, solids

    pieces = sorted(pieces, key=lambda p: min(p.parts))
    if len(pieces) == 1:
        shape = pieces[0].shape
    else:
        tops = []
        for p in pieces:
            if p.shape.ShapeType() == TopAbs_COMPOUND:
                it = TopoDS_Iterator(p.shape)
                while it.More():
                    tops.append(it.Value())
                    it.Next()
            else:
                tops.append(p.shape)
        shape = compound(tops)
    fmap, smap = _map(shape, TopAbs_FACE), _map(shape, TopAbs_SOLID)
    f_orig: list[list[tuple[int, int]]] = [[] for _ in range(fmap.Extent())]
    s_orig: list[list[int]] = [[] for _ in range(smap.Extent())]
    for p in pieces:
        for f, fo in zip(_faces(p.shape), p.faces, strict=False):
            k = fmap.FindIndex(f)
            if k > 0:
                f_orig[k - 1] = sorted(set(f_orig[k - 1]) | {(int(a), int(b)) for a, b in fo})
        for s, so in zip(solids(p.shape), p.solids, strict=False):
            k = smap.FindIndex(s)
            if k > 0:
                s_orig[k - 1] = sorted(set(s_orig[k - 1]) | {int(x) for x in so})
    record["unfused"] = [u["parts"] for u in unfused]
    record["seconds"] = round(time.time() - t0, 2)
    # each unfused pair with its own fuse when that finished (where its faces meet), else None
    lost = [{"parts": list(u["parts"]),
             "pair": (fused_cls(shape=u["pair"].shape, face_origins=u["pair"].faces, solid_parts=u["pair"].solids)
                      if u.get("pair") is not None else None)} for u in unfused]
    return fused_cls(shape=shape, face_origins=[[(int(p), int(j)) for p, j in o] for o in f_orig],
                     solid_parts=[[int(x) for x in o] for o in s_orig], seconds=record["seconds"],
                     unfused=lost, join=record)


def words(record: dict | None, name_of=None) -> list[str]:
    """PLAN-fea-35 in plain words: the parts no fuse could join, tied where they meet (none: nothing to say)."""
    rec = record or {}
    lost = [list(u) for u in rec.get("unfused") or []]
    if not lost:
        return []

    def nm(p: int) -> str:
        got = name_of(p) if name_of else ""
        return f"{got} (Part_{p})" if got else f"Part_{p}"

    listed = "; ".join(f"{nm(a)} and {nm(b)}" for a, b in lost[:6]) + (" and more" if len(lost) > 6 else "")
    if rec.get("error"):
        return [f"The parts could not be joined in the CAD ({rec['error']}), so each part is meshed on its own and "
                f"tied where it meets another: {listed}."]
    head = "Two parts" if len(lost) == 1 else f"{len(lost)} pairs of parts"
    return [f"{head} could not be joined in the CAD in the time set for it, so {'they are' if len(lost) == 1 else 'each pair is'} "
            f"meshed apart and tied where they meet: {listed}."]


def say_words(record: dict | None, name_of=None) -> str:
    """The same in one short line for the run's live stream (no part ids: "Turbine Wheel and part 3"); "" when
    every touching pair was fused."""
    lost = [list(u) for u in (record or {}).get("unfused") or []]
    if not lost:
        return ""

    def nm(p: int) -> str:
        return (name_of(p) if name_of else "") or f"part {p}"

    if len(lost) == 1:
        a, b = lost[0]
        return (f"Two parts could not be joined in the CAD in time, so they are tied where they meet: {nm(a)} and "
                f"{nm(b)}.")
    return (f"{len(lost)} pairs of parts could not be joined in the CAD in time, so each pair is tied where it meets "
            "(listed in the mesh notes).")


if __name__ == "__main__":
    import sys as _sys

    if BS.is_child(_sys.argv):
        _sys.exit(BS.child_main(_sys.argv[2:], _load, _handle))
