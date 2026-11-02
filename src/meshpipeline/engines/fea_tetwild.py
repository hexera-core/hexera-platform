# Responsibility: The FEA ladder's LAST RUNG (LAD-fea-9): fTetWild meshes the part(s) every gmsh option left with no valid mesh - its parameters derived from the plan and the measured part, its tetrahedra made quadratic, its boundary faces mapped back to the CAD's named faces by nearest surface, and how far its surface sits from the CAD measured and said.
# Boundaries: the driver side (rung_parts, params, words) is pure over the plan, the record and the passes; the runner side (mesh_into) runs inside the gmsh FEA adapter's process, runs the pinned, patched fTetWild binary per part in its own process (every core of the job as its threads; seeded and thread-stable: the same input gives the same mesh), within the rung's time (LAD-fea-18), and hands the adapter's own checks, gates and writers the same gmsh model every other FEA mesh is read from. It decides no lever and never runs before gmsh's options are exhausted (engines/fea_driver.py decides that).
# Collaborates with: engines/fea_driver.py (fires the rung), engines/fea_ladder.py (LAD-fea-9), engines/gmsh/fea_adapter.py (meshes the rest with gmsh, then writes and measures everything), engines/fea_size_field.py (the plan's sizes as fTetWild's sizing field), cad/fea_measure.py (the CAD tessellation), Dockerfile (the pinned fTetWild build).
"""THE LAST RUNG: fTetWild (LAD-fea-9).

gmsh's options come first and stay as they are - Delaunay, then HXT, MeshAdapt on the surfaces that
failed, and for an assembly the tie (PLAN-fea-17). Only when every one of them has left a part with no
valid mesh does fTetWild (Hu et al., "Fast Tetrahedral Meshing in the Wild", SIGGRAPH 2020; MPL-2.0)
mesh THAT part: it fills the region a surface encloses within an envelope round it, so a surface gmsh
cannot recover as a boundary (faces that cross or touch, a fill that crashes) still gives a valid
tetrahedral mesh. The other parts keep their gmsh mesh and are tied to it as PLAN-fea-17 ties parts.

WHAT IS FIXED, AND WHY (never guessed):
* LAD-fea-17, fTetWild's OWN sizes: the ideal edge is the plan's base size for the part (PLAN-fea-14 /
  PLAN-fea-17, never past the part: LAD-fea-10) and the plan's zones are its sizing field, none finer than
  ZONE_FLOOR_EPS envelopes; the envelope is a thousandth of the part's diagonal, lowered to a tenth of its
  thinnest REAL wall (never a crossing face's, a sliver's, or two overlapping faces' reading - real_walls,
  walls_with_room), never under EPS_FLOOR_SHARE of the diagonal unless a real wall needs it (then never past
  WALL_KEEP_SHARE of that wall): fTetWild's time grows steeply as its envelope shrinks;
* the CAD is tessellated within half the envelope, so the mesh's surface is within 1.5 envelopes of the
  CAD itself; where the mesh leaves CAD material out, the part is filled again by flood fill;
* LAD-fea-18, BOUNDED: a time sized from the part; near its end the build's stop file has fTetWild finish the
  mesh it has, at its end it is stopped and said; its rounds go to the live line; every core is used, and the
  build is seeded and thread-stable, so the same input gives the same mesh, byte for byte.

THE MESH: fTetWild's tetrahedra (straight), made quadratic by a node at the middle of every edge - a
straight 10-node tetrahedron has the Jacobian of its 4-node one, so every element valid stays valid.
Its boundary triangles are mapped to the CAD face nearest to each (Part_k_Face_j / Face_k), so every
load and support still has its face; a face too small to keep a triangle of its own is said.
"""
from __future__ import annotations

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
VERSION = f"fTetWild {COMMIT[:12]} (patched: stop file, thread-stable)"
#: where the mesh image installs it (and the name on PATH)
BIN_NAME = "fTetWild"
BIN_PATH = "/opt/ftetwild/bin/fTetWild"
#: THE BUILD'S OWN PATCHES (Dockerfile ftetwild-build, each rewrite checked): the two shuffles seeded by a
#: constant; a stop file ends the optimisation where it is (LAD-fea-18); the tets a triangle may cut,
#: gathered by TBB threads, taken in index order, and the smoothing split into the same sets whatever the
#: thread count - so the same input gives the same mesh on any number of threads
BUILD = "seeded, stop file, thread-stable"
#: LAD-fea-18: every core of the job: fTetWild's own TBB threads (one part: all of them; parts at once:
#: shared out). The patched build gives the same mesh on 1 thread or on many
THREAD_RULE = "LAD-fea-18"
#: LAD-fea-17: THE RUNG'S OWN SIZES (rule id)
SIZE_RULE = "LAD-fea-17"
#: the envelope: fTetWild's own default share of the diagonal ...
EPS_DIAG_SHARE = 1e-3
#: ... never more than this share of the thinnest REAL wall (LAD-fea-17: real_walls) ...
EPS_WALL_SHARE = 0.1
#: ... never less than this share of the diagonal (LAD-fea-17: below it the run time grows past use)
EPS_FLOOR_SHARE = 3e-4
#: ... except that it never rises past this share of a real wall to reach that floor (under half the wall,
#: its two faces stay apart)
WALL_KEEP_SHARE = 0.4
#: ... and never under this share of the diagonal whatever the wall (the old floor)
EPS_MIN_SHARE = 2e-5
#: LAD-fea-17: a wall reading counts for the envelope only on a face at least this many walls across
#: (its area at least (WALL_FACE_SPAN x the wall)^2): a sliver face reading a thin "wall" is a local
#: detail, not a section the mesh must keep
WALL_FACE_SPAN = 3.0
#: LAD-fea-17: no zone of the sizing field finer than this many envelopes - fTetWild's surface is only
#: known to within its envelope, and its shortest edge is the envelope (its own min_edge_len default)
ZONE_FLOOR_EPS = 4.0
#: LAD-fea-17: A WALL HAS ROOM BEYOND IT. A reading that would set the envelope is tested on the CAD: from
#: up to WALL_SAMPLES points of its face, BEYOND_SHARE walls in, at least one must be outside the solid
#: (past a real wall's far side is space); where every one is still inside, the "wall" is two faces
#: overlapping inside the material (the dev pivot's 0.5 mm), and it sets nothing
BEYOND_SHARE = 1.5
WALL_SAMPLES = 48
#: the probes' surface is tessellated within this share of the thinnest wall tested (each probe sits half
#: a wall past the far face: the surface must be well inside that)
PROBE_SHARE = 0.25
#: at most this many readings are tested, thinnest first (the first real one sets the envelope)
WALL_CHECKS = 40
#: the CAD is tessellated within this share of the envelope
TESSELLATION_SHARE = 0.5
#: the tessellation's angular deflection (radians)
ANGULAR_DEFLECTION = 0.5
#: fTetWild's own optimisation stop (its default): the worst conformal AMIPS energy
STOP_ENERGY = 10.0
#: LAD-fea-18: fTetWild's own round cap (--max-its; its default 80)
MAX_ITS = 80
#: fTetWild's own cap on its sizing-field rounds (fixed in its source)
SIZING_ROUNDS = 20
#: LAD-fea-18: at this share of the rung's time a part still improving its elements is asked to finish the
#: mesh it has (the stop file); the rest is for its last steps (the outside filtered, the mesh written)
SOFT_SHARE = 0.8
#: ... or sooner: while this many of its longest rounds still fit before the hard deadline
ROUNDS_LEFT = 3.0
#: LAD-fea-18: THE RUNG'S TIME (rule id) - BUDGET_FACTOR times its planned time (the plan's element estimate
#: at fTetWild's measured rate on the job's cores), never under BUDGET_FLOOR_S, never past the machine's wall
TIME_RULE = "LAD-fea-18"
#: fTetWild's rate, per element its sizes ask for (estimate_tets), a second on one thread - set so the slowest corpus part ran 2.3 times its planned time (lab, 2 Nov: median 0.45, docs/RULES.md LAD-fea-18) ...
RATE_PER_THREAD = 350.0
#: ... and how it grows with threads: rate x threads ** THREAD_GAIN (lab, 2 Nov: 1.6-1.7 times on 8 threads)
THREAD_GAIN = 0.25
BUDGET_FACTOR = 5.0
BUDGET_FLOOR_S = 1800.0
#: ... and never past 4 hours, whatever the part (the Cloud Run job's own wall): past it the reason is said
BUDGET_CAP_S = 4 * 3600.0
#: the rung pass's own backstop past its budget: gmsh's other parts, the CAD's tessellation, naming the
#: faces and writing the solver files (the budget times fTetWild alone)
MARGIN_SHARE = 0.5
MARGIN_FLOOR_S = 1800.0
#: what a part out of its time says (the driver reads it: no second rung pass after it)
OUT_OF_TIME = "the time this part allows ran out"
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
    never more than a tenth of the thinnest REAL wall (real_walls), never less than the floor (LAD-fea-17)."""
    eps = EPS_DIAG_SHARE * diag_m
    if thinnest_m and thinnest_m > 0:
        eps = min(eps, EPS_WALL_SHARE * thinnest_m)
    floor = EPS_FLOOR_SHARE * diag_m
    if eps >= floor:
        return eps
    # under the floor: the envelope rises to it, but never past WALL_KEEP_SHARE of a real wall (under half of
    # it, the wall's two faces stay apart) - a wall that thin keeps its section and costs its time
    return max(eps, EPS_MIN_SHARE * diag_m, min(floor, WALL_KEEP_SHARE * float(thinnest_m or math.inf)))


def real_walls(faces: list[dict], crossing: set[str],
               binding_m: float = math.inf) -> tuple[list[tuple[float, str]], list[dict]]:
    """LAD-fea-17: the wall readings that may set the envelope ((metres, face), thinnest first), and the ones
    set aside with why. A reading does not count on a face the checks found crossing or overlapping another
    (the "wall" between two overlapping faces is no material), nor on a face narrower than WALL_FACE_SPAN
    walls (its area under (WALL_FACE_SPAN x t)^2: a 0.04 mm2 sliver reading 0.11 mm on a 640 mm part is a
    local detail, and held the whole part's envelope at 13 microns). The runner then tests the ones that
    would bind for room beyond them (walls_with_room)."""
    kept: list[tuple[float, str]] = []
    aside: list[dict] = []
    for f in faces:
        if not f.get("thickness_min_mm"):
            continue
        t = float(f["thickness_min_mm"]) * 1e-3
        name = str(f.get("name"))
        area = f.get("area_mm2")
        if t >= binding_m:
            kept.append((t, name))           # too thick to set the envelope: nothing to decide
        elif name in crossing:
            aside.append({"face": name, "wall_mm": _sig(t * 1e3), "why": "crossing"})
        elif area is not None and float(area) * 1e-6 < (WALL_FACE_SPAN * t) ** 2:
            aside.append({"face": name, "wall_mm": _sig(t * 1e3), "why": "narrow", "area_mm2": _sig(float(area))})
        else:
            kept.append((t, name))
    return sorted(kept), aside


def crossing_faces(record: Mapping, crossing: Mapping | None) -> set[str]:
    """The faces the checks found crossing or overlapping another: LAD-fea-16's (the driver's `crossing`)
    and every self-overlap the geometry check located (record["suspects"])."""
    out = {str(f) for f in (crossing or {}).get("faces") or []}
    for s in record.get("suspects") or []:
        if s.get("kind") == "self_overlap":
            out |= {str(f) for f in s.get("faces") or []}
    return out


def zone_floor_m(eps_m: float) -> float:
    """LAD-fea-17: the finest size the rung's sizing field asks for - ZONE_FLOOR_EPS envelopes."""
    return ZONE_FLOOR_EPS * float(eps_m)


def params(plan: Mapping, record: Mapping, diag_m: float, parts: list[int] | None,
           crossing: Mapping | None = None) -> dict:
    """The rung's settings, every number from the plan and the measured part (plan["mesh"]["ftetwild"]):
    LAD-fea-17's sizes (the envelope from the real walls, the sizing field's floor) and LAD-fea-18's stop."""
    assembly = int(record.get("bodies") or 1) > 1
    every = [int(b["n"]) for b in record.get("part_bases") or []] if assembly else []
    chosen = list(parts) if parts is not None else (every or [None])  # type: ignore[list-item]
    skip = crossing_faces(record, crossing)
    per: dict[str, dict] = {}
    for p in chosen:
        faces = _part_faces(plan, p)
        d = _part_diag_m(record, diag_m, p)
        walls, aside = real_walls(faces, skip, binding_m=EPS_DIAG_SHARE * d / EPS_WALL_SHARE)
        thinnest = walls[0][0] if walls else None
        eps = envelope_m(d, thinnest)
        # the readings that would bind (a tenth of them under the default envelope), for the room test
        binding = [{"face": f, "t_m": _sig(t)} for t, f in walls if EPS_WALL_SHARE * t < EPS_DIAG_SHARE * d]
        per["whole" if p is None else f"Part_{p}"] = {
            "edge_length_m": _sig(rung_edge(plan, p, d)), "epsilon_m": _sig(eps), "diag_m": _sig(d),
            "thinnest_wall_m": _sig(thinnest) if thinnest else None,
            "tessellation_m": _sig(TESSELLATION_SHARE * eps), "zone_floor_m": _sig(zone_floor_m(eps)),
            **({"wall_checks": binding[:WALL_CHECKS]} if binding else {}),
            **({"walls_set_aside": aside[:20], "walls_set_aside_count": len(aside)} if aside else {}),
            **({"edge_rule": EDGE_RULE, "plan_base_m": _sig(_part_base(plan, p))}
               if rung_edge(plan, p, d) < _part_base(plan, p) else {})}
    return {"engine": ENGINE, "version": VERSION, "stop_energy": STOP_ENERGY, "max_its": MAX_ITS,
            "threads": "the job's cores", "parts": None if parts is None or not assembly else sorted(int(p) for p in parts),
            "per_part": per, "rule": "LAD-fea-9", "size_rule": SIZE_RULE,
            "basis": {"eps_diag_share": EPS_DIAG_SHARE, "eps_wall_share": EPS_WALL_SHARE,
                      "eps_floor_share": EPS_FLOOR_SHARE, "tessellation_share": TESSELLATION_SHARE,
                      "wall_face_span": WALL_FACE_SPAN, "zone_floor_eps": ZONE_FLOOR_EPS,
                      "sizing": "the plan's zones (fea_size_field), none finer than the zone floor, as fTetWild's "
                                "background mesh"}}


def rung_rate(cpus: int) -> float:
    """LAD-fea-18: fTetWild's measured rate (elements a second) on `cpus` threads."""
    return RATE_PER_THREAD * max(1, int(cpus)) ** THREAD_GAIN


def rung_budget(elements: float, *, cpus: int, wall_s: float) -> dict:
    """LAD-fea-18: THE RUNG'S TIME - BUDGET_FACTOR times the planned time (the plan's element estimate at
    fTetWild's rate on `cpus` threads), never under BUDGET_FLOOR_S, never past the machine's wall (less the
    pass's margin); the pass's own ceiling is the budget plus that margin. The budget times fTetWild itself:
    at SOFT_SHARE of it a part still improving finishes the mesh it has, at the whole of it one still
    running is stopped and said."""
    rate = rung_rate(cpus)
    planned = float(max(0.0, elements)) / rate if rate > 0 else 0.0

    # the budget and its margin within the machine's wall
    budget = min(max(BUDGET_FLOOR_S, BUDGET_FACTOR * planned), float(wall_s) / (1.0 + MARGIN_SHARE), BUDGET_CAP_S)
    ceiling = min(float(wall_s), budget + max(MARGIN_FLOOR_S, MARGIN_SHARE * budget))
    return {"rule": TIME_RULE, "elements": int(max(0.0, elements)), "cpus": int(cpus), "rate": _sig(rate),
            "planned_s": int(round(planned)), "factor": BUDGET_FACTOR, "floor_s": int(BUDGET_FLOOR_S),
            "cap_s": int(BUDGET_CAP_S),
            "wall_s": int(wall_s), "budget_s": int(round(budget)), "ceiling_s": int(round(ceiling)),
            "soft_share": SOFT_SHARE}


def own_budget(cap_s: float | None, tets: list[int], cpus: int) -> dict:
    """LAD-fea-18 on the runner: the rung's time from fTetWild's OWN sizes - BUDGET_FACTOR times the tets they
    ask for (estimate_tets, every part) at its rate on the job's cores, at least BUDGET_FLOOR_S, never past
    the driver's budget (`cap_s`, from the plan's estimate: the pass's ceiling is sized from it). No cap, no
    budget (an older plan)."""
    n = int(sum(int(t) for t in tets))
    if not cap_s:
        return {"tets": n}
    planned = n / rung_rate(cpus)
    budget = min(float(cap_s), max(BUDGET_FLOOR_S, BUDGET_FACTOR * planned))
    return {"rule": TIME_RULE, "tets": n, "cpus": int(cpus), "planned_s": int(round(planned)),
            "budget_s": int(round(budget)), "cap_s": int(cap_s)}


def budget_words(timed: Mapping) -> str:
    return (f"fTetWild gets {_minutes(float(timed['budget_s']))} for this pass: {timed['factor']:g} times its planned "
            f"{_minutes(float(timed['planned_s']))} (about {int(timed['elements']):,} elements on {timed['cpus']} cores), at "
            f"least {_minutes(float(timed['floor_s']))}. Near the end it finishes the mesh it has; if it is still "
            f"working when the time is up it is stopped and the reason said (LAD-fea-18).")


def size_words(settings: Mapping) -> str:
    """LAD-fea-17's line in the why record: each part's envelope, edge and sizing floor, and any wall reading
    set aside."""
    bits = []
    for key, s in (settings.get("per_part") or {}).items():
        aside = int(s.get("walls_set_aside_count") or 0)
        bits.append(f"{'the part' if key == 'whole' else key}: envelope {_mm(float(s['epsilon_m']) * 1e3)}, edge "
                    f"{_mm(float(s['edge_length_m']) * 1e3)}, no size under {_mm(float(s['zone_floor_m']) * 1e3)}"
                    + (f" ({aside} wall reading(s) on crossing or sliver faces set aside)" if aside else ""))
    return ("fTetWild's own sizes (LAD-fea-17) - " + "; ".join(bits[:6]) + (" ..." if len(bits) > 6 else "") + ".")


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
    overlapping = [w["face"] for p in info.get("per_part") or [] for w in p.get("walls_without_room") or []]
    if overlapping:
        said.append(f"Thin \"walls\" measured between faces that overlap inside the material ({_names(overlapping)}) "
                    "are not walls, so they set none of fTetWild's sizes (LAD-fea-17).")
    refilled = [p["part"] for p in info.get("per_part") or [] if (p.get("refilled") or {}).get("kept")]
    if refilled:
        said.append(f"On {', '.join(str(p) for p in refilled[:8])} fTetWild's first mesh left part of the CAD out (its "
                    "faces cross or face the wrong way), so it was filled again by what the CAD's surface encloses "
                    "(LAD-fea-17).")
    stopped = info.get("stopped") or []
    if stopped:
        rounds = [p.get("rounds") for p in info.get("per_part") or [] if p.get("stopped")]
        said.append(f"Its time ran short on {', '.join(str(s) for s in stopped[:8])}, so fTetWild finished the mesh "
                    f"it had there after {min(r for r in rounds if r is not None) if any(r is not None for r in rounds) else 'its first'} "
                    "round(s) of improving it (LAD-fea-18); the element checks below are measured on that mesh.")
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


def winding(points: np.ndarray, xyz: np.ndarray, tris: np.ndarray, chunk: int = 1_000_000) -> np.ndarray:
    """The generalised winding number of the triangle soup at each point (van Oosterom & Strackee's solid
    angle, summed over 4 pi): 1 inside a closed outward surface, 0 outside, 2 where two pieces overlap - it
    counts the material, where a ray-parity or nearest-face test is fooled by faces lying inside it."""
    a0, b0, c0 = (np.asarray(xyz, dtype=float)[np.asarray(tris, dtype=np.int64)[:, i]] for i in range(3))
    out = np.zeros(len(points))
    step = max(1, int(chunk) // max(1, len(a0)))
    for s in range(0, len(points), step):
        p = np.asarray(points[s:s + step], dtype=float)[:, None, :]
        a, b, c = a0[None] - p, b0[None] - p, c0[None] - p
        la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
        det = np.einsum("pti,pti->pt", a, np.cross(b, c))
        div = (la * lb * lc + np.einsum("pti,pti->pt", a, b) * lc + np.einsum("pti,pti->pt", b, c) * la
               + np.einsum("pti,pti->pt", c, a) * lb)
        out[s:s + step] = (2.0 * np.arctan2(det, div)).sum(axis=1) / (4.0 * math.pi)
    return out


def walls_with_room(checks: list[dict], xyz: np.ndarray, tris: np.ndarray, label: np.ndarray,
                    names: list[str]) -> tuple[float | None, list[dict]]:
    """LAD-fea-17: (the thinnest wall with room beyond it, the readings found with none), testing `checks`
    ({"face", "t_m"}, thinnest first) until one is real. From up to WALL_SAMPLES of the face's triangles
    (`xyz`, `tris` the part's outward surface in metres), the point BEYOND_SHARE walls in is tested for
    material (winding: at least half a layer): a real wall has space past its far side somewhere on its
    face; a reading with every probe still in the material is two faces overlapping, not a wall (the bar's
    top under a pad laid into it). A face with no triangle to probe counts as real (nothing is set aside
    untested)."""
    index = {str(n): k for k, n in enumerate(names)}
    a, b, c = (xyz[tris[:, i]] for i in range(3))
    normal = np.cross(b - a, c - a)
    length = np.linalg.norm(normal, axis=1)
    none: list[dict] = []
    for chk in checks:
        t = float(chk["t_m"])
        k = index.get(str(chk["face"]))
        rows = np.flatnonzero((label == k) & (length > 0)) if k is not None else np.array([], dtype=np.int64)
        if not len(rows):
            return t, none
        # the face's own triangles split until about WALL_SAMPLES of them cover it (a plane is 2 triangles)
        spacing = math.sqrt(float(length[rows].sum()) / 2.0 / WALL_SAMPLES) * 1.5
        soup, _lab = split_long(xyz, tris[rows], np.zeros(len(rows), dtype=np.int64), spacing, cap=16 * WALL_SAMPLES)
        sn = np.cross(soup[:, 1] - soup[:, 0], soup[:, 2] - soup[:, 0])
        sl = np.linalg.norm(sn, axis=1)
        keep = np.flatnonzero(sl > 0)
        keep = keep[np.linspace(0, len(keep) - 1, min(WALL_SAMPLES, len(keep))).round().astype(np.int64)]
        probes = soup[keep].mean(axis=1) - (sn[keep] / sl[keep, None]) * (BEYOND_SHARE * t)
        if (winding(probes, xyz, tris) < 0.5).any():
            return t, none
        none.append({"face": str(chk["face"]), "wall_mm": _sig(t * 1e3), "probes": int(len(probes))})
    return None, none


def without_walls(plan: Mapping, no_room: list[dict]) -> Mapping:
    """The plan with no zone drawn from a "wall" that has no room beyond it (LAD-fea-17): its own wall zones
    and the corners capped at its thickness."""
    if not no_room:
        return plan
    faces = {str(w["face"]) for w in no_room}
    thick = [float(w["wall_mm"]) * 1e-3 for w in no_room]

    def drawn(z: Mapping) -> bool:
        zf = {str(f) for f in z.get("faces") or []}
        if z.get("kind") == "wall" and zf and zf <= faces:
            return True
        t = z.get("thickness_m")
        return z.get("kind") in ("wall", "corner") and t is not None and any(
            abs(float(t) - w) <= 1e-3 * w for w in thick)
    return {**plan, "zones": [z for z in plan.get("zones") or [] if not drawn(z)]}


def sizing_grid(xyz: np.ndarray, tris: np.ndarray, label: np.ndarray, names: list[str], plan: Mapping,
                edge: float, pad: float = 0.0, floor: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """fTetWild's background mesh: the part's box in BG_DIVISIONS cells along EACH side (a thin pane's
    thickness is resolved as finely as its length - fc_glass_frames, 25 Oct: one 57 mm cell across a
    10 mm pane let its 7.5 mm wall zone fall through, and its tets ran 7 mm off the CAD), a cell or
    `pad` beyond it whichever is more, six tetrahedra a cell, the plan's zone size at every node (the
    part's edge length where no zone reaches, never above it), no zone finer than `floor` (LAD-fea-17).
    None when no zone is finer than the edge."""
    from meshpipeline.cad.fea_features import Surface
    from meshpipeline.engines.fea_size_field import SizeField

    zones = [{**z, "size_m": max(float(z["size_m"]), float(floor))} for z in plan.get("zones") or []
             if float(z.get("size_m") or math.inf) < edge and max(float(z["size_m"]), float(floor)) < edge]
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


#: a regular tetrahedron of edge h holds h^3 / (6 sqrt 2): this many of them fill h^3
TETS_PER_CUBE = 6.0 * math.sqrt(2.0)


def estimate_tets(xyz: np.ndarray, tris: np.ndarray, bg: tuple | None, edge: float) -> int:
    """LAD-fea-18: how many tetrahedra fTetWild's sizes ask for - the part's volume (its surface's own,
    by the divergence theorem) filled at the ideal edge, or, with a sizing field, the field's density
    (TETS_PER_CUBE / h^3, the mean of each background tet's corners) over the background mesh, times the
    share of the background's volume the part fills."""
    a, b, c = (np.asarray(xyz, float)[np.asarray(tris, np.int64)[:, i]] for i in range(3))
    vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)
    if bg is None:
        return int(TETS_PER_CUBE * vol / float(edge) ** 3)
    pts, tets, h = bg
    p = [pts[tets[:, i]] for i in range(4)]
    v = np.abs(np.einsum("ij,ij->i", np.cross(p[1] - p[0], p[2] - p[0]), p[3] - p[0])) / 6.0
    density = TETS_PER_CUBE * (np.asarray(h, float)[tets] ** -3.0).mean(axis=1)
    box = float(v.sum())
    return int(float((v * density).sum()) * (vol / box if box > 0 else 1.0))


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


class Progress:
    """LAD-fea-18: what fTetWild's own lines say about where it is - its phase, its optimisation round
    (of --max-its), the worst element energy and the element count it last printed, how much of the CAD's
    surface it had not yet inserted, and whether it stopped on the stop file. Fed one line at a time."""

    _PHASES = (("preprocessing ", "prepared"), ("tetrahedralizing ", "tetrahedralized"),
               ("cutting ", "inserted"), ("mesh optimization ", "optimised"))

    def __init__(self, max_its: int):
        self.max_its = int(max_its)
        self.phase = "preparing"
        self.round = 0
        self.energy: float | None = None
        self.tets: int | None = None
        self.uninserted: tuple[int, int] | None = None
        self.stopped = False
        self.sizing = 0
        self.phases: dict[str, float] = {}
        #: the longest round (optimisation or sizing field) so far, seconds
        self.longest = 0.0
        self._mark: float | None = None

    def feed(self, line: str) -> bool:
        """Read one line; True when it moved what the run's live line shows (phase or round)."""
        s = line.strip()
        before = (self.phase, self.round, self.sizing)
        for head, done in self._PHASES:
            # its info lines carry a time and a logger prefix: "[...] [float-tetwild] [info] cutting 1.5s"
            at = s.find(f"] {head}") + 2 if f"] {head}" in s else (0 if s.startswith(head) else -1)
            if at >= 0 and s.endswith("s"):
                try:
                    self.phases[done] = float(s[at + len(head):-1])
                except ValueError:
                    continue
                self.phase = {"prepared": "tetrahedralizing", "tetrahedralized": "inserting",
                              "inserted": "improving", "optimised": "finishing"}[done]
        if s.startswith("////////////////") and " pass " in s:
            try:
                self.round = int(s.split(" pass ")[1].split()[0]) + 1
                self.phase = "improving"
            except (IndexError, ValueError):
                pass
        elif s.startswith("//////////////// postprocessing"):
            self.phase = "finishing"
        elif s.startswith("/////////") and not s.startswith("//////////") and len(s.split()) == 2:
            # a round of the sizing field (at most SIZING_ROUNDS): "/////////3 1300.45" - its round, its worst energy
            try:
                self.sizing = int(s[9:].split()[0]) + 1
                self.energy = float(s.split()[1])
                self.phase = "sizing"
            except (IndexError, ValueError):
                pass
        elif s.startswith("max_energy = "):
            try:
                self.energy = float(s.split("=")[1])
            except ValueError:
                pass
        elif s.startswith("#t = "):
            try:
                self.tets = int(s.split("=")[1])
            except ValueError:
                pass
        elif "uninserted #f = " in s:
            try:
                a, b = s.split("uninserted #f = ")[1].split()[0].split("/")
                self.uninserted = (int(a), int(b))
            except (IndexError, ValueError):
                pass
        elif s.startswith("stop file:"):
            self.stopped = True
            self.phase = "finishing"
        moved = (self.phase, self.round, self.sizing) != before
        if moved and self.phase in ("improving", "sizing"):
            now = time.monotonic()
            if self._mark is not None:
                self.longest = max(self.longest, now - self._mark)
            self._mark = now
        return moved

    def label(self, name: str) -> str:
        """The mesher's own line for the stage file (never the user's words: contracts/narration says those)."""
        said = {"preparing": "preparing the surface", "tetrahedralizing": "tetrahedralizing",
                "inserting": "inserting the CAD's surface", "finishing": "finishing (filtering the outside, writing)"}
        if self.phase == "sizing":
            return (f"fTetWild {name}: refining to the sizing field, round {self.sizing} of at most {SIZING_ROUNDS}"
                    + (f", worst energy {self.energy:.3g}" if self.energy is not None else ""))
        if self.phase == "improving" and self.round < 1:
            return f"fTetWild {name}: starting to improve the elements"
        if self.phase == "improving":
            bits = [f"round {self.round} of at most {self.max_its}"]
            if self.energy is not None:
                bits.append(f"worst energy {self.energy:.3g}")
            if self.tets is not None:
                bits.append(f"{self.tets:,} tets")
            return f"fTetWild {name}: improving the elements, " + ", ".join(bits)
        return f"fTetWild {name}: {said.get(self.phase, self.phase)}"

    def record(self) -> dict:
        return {"rounds": self.round, "sizing_rounds": self.sizing, "energy": _sig(self.energy) if self.energy is not None else None,
                "uninserted": list(self.uninserted) if self.uninserted else None, "stopped": self.stopped,
                "phases_s": dict(self.phases)}


def _stage_note(ws: Path | None, key: str, label: str, n: int | None = None, of: int | None = None) -> None:
    if ws is None:
        return
    from meshpipeline.engines.gmsh.fea_adapter import note_stage

    note_stage(ws, key, label, n=n, of=of, unit="rounds" if n is not None else "")


def run_part(job: dict) -> dict:
    """One fTetWild run (its own process): job = {"dir", "xyz", "tris", "edge", "epsilon", "bg": (pts, tets,
    values) | None, "name"} and, optionally, "threads" (fTetWild's TBB threads, default 1), "max_its", "ws"
    (the workspace whose stage file shows its progress) and "soft_at" / "hard_at" (time.monotonic() deadlines,
    LAD-fea-18: at the soft one the stop file is written and fTetWild finishes the mesh it has; at the hard
    one it is killed). Returns {"xyz", "tets"} or {"error"}; writes nothing outside job["dir"] (and the stage
    file)."""
    import threading

    exe = binary()
    if exe is None:
        return {"error": "fTetWild is not installed in this mesh image"}
    d = Path(job["dir"])
    d.mkdir(parents=True, exist_ok=True)
    src, out = d / "input.obj", d / "output.msh"
    stop = Path(f"{out}.stop")
    stop.unlink(missing_ok=True)
    _write_obj(src, job["xyz"], job["tris"])
    xyz = job["xyz"]
    threads = max(1, int(job.get("threads") or 1))
    max_its = int(job.get("max_its") or MAX_ITS)
    # fTetWild's envelope is a share of ITS bounding box: the input's own box, as it computes it
    diag = float(np.linalg.norm(xyz.max(axis=0) - xyz.min(axis=0)))
    cmd = [exe, "-i", str(src), "-o", str(out), "--la", repr(float(job["edge"])),
           "-e", repr(float(job["epsilon"]) / diag), "--max-threads", str(threads),
           "--stop-energy", repr(STOP_ENERGY), "--max-its", str(max_its), "--level", str(LOG_LEVEL), "--no-color"]
    if job.get("floodfill"):
        cmd += ["--use-floodfill"]
    if job.get("bg") is not None:
        bg = d / "sizing.msh"
        _write_bg(bg, *job["bg"])
        cmd += ["--bg-mesh", str(bg)]
    ws = Path(job["ws"]) if job.get("ws") else None
    progress = Progress(max_its)
    t0 = time.monotonic()
    tail: list[str] = []
    peak_kb = 0
    hwm_kb = 0
    timers: list[threading.Timer] = []
    killed = threading.Event()
    asked = threading.Event()
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(d)) as proc:
        assert proc.stdout is not None

        def ask_stop() -> None:
            # LAD-fea-18: the soft deadline - fTetWild ends its optimisation where it is and writes that mesh
            asked.set()
            stop.write_text("the rung's time is up\n")
            print(f"{RUNG_LINE} {job['name']}: the time this part allows is nearly spent - fTetWild is asked to "
                  "finish the mesh it has", file=sys.stderr, flush=True)

        def kill() -> None:
            killed.set()
            proc.kill()
        now = time.monotonic()
        if job.get("soft_at") is not None:
            timers.append(threading.Timer(max(0.0, float(job["soft_at"]) - now), ask_stop))
        if job.get("hard_at") is not None:
            # ... and sooner when a round is long: the stop is read only between rounds, so it is asked while
            # ROUNDS_LEFT of the longest round seen still fit before the hard deadline (a round of a big mesh's
            # sizing field took over 6 minutes: lab, 2 Nov)
            hard = float(job["hard_at"])

            def watch() -> None:
                while not asked.is_set() and not killed.is_set() and proc.poll() is None:
                    if time.monotonic() >= hard - ROUNDS_LEFT * progress.longest:
                        ask_stop()
                        return
                    time.sleep(2.0)
            timers.append(threading.Timer(0.0, watch))
        if job.get("hard_at") is not None:
            timers.append(threading.Timer(max(0.0, float(job["hard_at"]) - now), kill))
        for tm in timers:
            tm.daemon = True
            tm.start()
        _stage_note(ws, "ftetwild", progress.label(job["name"]))
        try:
            for line in proc.stdout:
                # fTetWild's own lines are the runner's sign of progress (the FEA watchdog reads output)
                print(f"{RUNG_LINE} {job['name']}: {line.rstrip()}", file=sys.stderr, flush=True)
                tail = (tail + [line.rstrip()])[-20:]
                if progress.feed(line):
                    # LAD-fea-18: the round and the worst energy on the run's live line
                    if progress.phase == "improving" and progress.round >= 1:
                        _stage_note(ws, "ftetwild_improving", progress.label(job["name"]), n=progress.round,
                                    of=max_its)
                    elif progress.phase == "sizing":
                        _stage_note(ws, "ftetwild_sizing", progress.label(job["name"]), n=progress.sizing,
                                    of=SIZING_ROUNDS)
                    else:
                        _stage_note(ws, "ftetwild", progress.label(job["name"]))
                hwm_kb = max(hwm_kb, _hwm_kb(proc.pid))
            hwm_kb = max(hwm_kb, _hwm_kb(proc.pid))
            try:
                _pid, status, usage = os.wait4(proc.pid, 0)
                rc = os.waitstatus_to_exitcode(status)
                proc.returncode = rc
                peak_kb = int(usage.ru_maxrss)
            except ChildProcessError:
                rc = proc.wait()
        finally:
            for tm in timers:
                tm.cancel()
    # ITS OWN PEAK: the kernel's high-water mark of fTetWild's own memory, read while it ran. The reaped
    # rusage's ru_maxrss also holds the forking process's resident memory at the fork (it carries across
    # fork and exec): beside a 1.5 GB parent every small part read 1.5 GB (lab, 26 Oct), so it stands
    # only where the high-water mark cannot be read
    peak_kb = hwm_kb or peak_kb
    seconds = round(time.monotonic() - t0, 1)
    peak_gib = round(peak_kb / 1024.0 ** 2, 2)
    facts = {"seconds": seconds, "peak_gib": peak_gib, "threads": threads, **progress.record()}
    # asked to finish: the stop file ends the optimisation (it says so) or the sizing-field rounds (silently)
    facts["stopped"] = bool(progress.stopped or (asked.is_set() and not killed.is_set()))
    if killed.is_set():
        return {**facts, "rc": rc, "out_of_time": True,
                "error": f"fTetWild was still {_doing(progress)} when the time this part allows ran out "
                         f"({_minutes(seconds)}), so it was stopped (LAD-fea-18)"}
    if rc != 0 or not out.exists():
        return {**facts, "error": f"fTetWild exited {rc}: " + " | ".join(tail[-4:]), "rc": rc}
    try:
        pts, tets = _read_tets(out)
    except Exception as exc:  # noqa: BLE001 - said as this part's failure
        return {**facts, "error": f"fTetWild's mesh could not be read ({exc})"}
    return {**facts, "xyz": pts, "tets": tets, "command": [Path(cmd[0]).name, *cmd[5:]]}


#: LAD-fea-17: the CAD's surface this share of the part from fTetWild's mesh is material left out
LOST_SHARE = 0.02


def cad_gap(cad_xyz: np.ndarray, result: Mapping) -> float:
    """How far the CAD tessellation's furthest vertex is from the surface of fTetWild's mesh (metres)."""
    tets, _v = orient(result["xyz"], result["tets"])
    tris = boundary(tets)
    span = float(np.linalg.norm(cad_xyz.max(axis=0) - cad_xyz.min(axis=0)))
    near = Nearest(result["xyz"], tris, np.zeros(len(tris), dtype=np.int64), max(span / 200.0, 1e-9))
    _l, d = near.query(cad_xyz)
    return float(d.max(initial=0.0))


def _doing(progress: Progress) -> str:
    if progress.phase == "sizing":
        return f"refining its mesh to the sizing field (round {progress.sizing} of {SIZING_ROUNDS})"
    if progress.phase == "improving":
        return f"improving its elements (round {progress.round} of {progress.max_its})"
    return {"preparing": "preparing the CAD's surface", "tetrahedralizing": "building its first tetrahedra",
            "inserting": "inserting the CAD's surface into its tetrahedra",
            "finishing": "finishing its mesh"}.get(progress.phase, progress.phase)


def _minutes(seconds: float) -> str:
    m = int(round(float(seconds) / 60.0))
    return f"{m} minute{'s' if m != 1 else ''}" if m >= 1 else f"{int(seconds)} seconds"


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
    # LAD-fea-18: EVERY CORE - as many parts at once as there are cores (at most), each part's fTetWild
    # given its share of them as TBB threads (one part: every core). The patched build gives the same mesh
    # on any thread count, so the share changes no mesh
    at_once = max(1, min(int(workers), len(jobs)))
    with ThreadPoolExecutor(max_workers=at_once) as pool:
        while pending or running:
            while pending and len(running) < at_once and (not running or memory_room() >= MEMORY_ROOM_SHARE):
                i = pending.pop(0)
                share = max(1, int(workers) // min(at_once, len(running) + len(pending) + 1))
                running[i] = pool.submit(run_part, {"threads": share, **jobs[i]})
            done, _ = wait(list(running.values()), timeout=5.0, return_when=FIRST_COMPLETED)
            for i, fut in list(running.items()):
                if fut in done:
                    r = fut.result()
                    del running[i]
                    if r.get("rc") in (-9, 137) and not r.get("out_of_time"):
                        alone.append(i)
                    else:
                        results[i] = r
    for i in alone:
        if jobs[i].get("hard_at") is not None and time.monotonic() >= float(jobs[i]["hard_at"]):
            results[i] = {"error": "fTetWild was killed for memory beside other parts, and the rung's time ran out "
                                   "before it could run again alone (LAD-fea-18)", "out_of_time": True}
            continue
        print(f"{RUNG_LINE} {jobs[i]['name']}: killed for memory beside other parts - run again alone",
              file=sys.stderr, flush=True)
        results[i] = run_part({**jobs[i], "threads": max(1, int(workers))})
    return [r if r is not None else {"error": "not run"} for r in results]


def mesh_into(gmsh, ws: Path, plan: Mapping, staged: Path, volumes: list[int], *, volume_index: Mapping[int, int],
              surface_tag_of: Mapping[int, int], names: list[str], order: int = 2, workers: int = 1) -> dict:
    """fTetWild's mesh of each of `volumes` (gmsh volume tags of the staged model), written INTO that
    model: its nodes on the volume, its 10-node tetrahedra in the volume, each boundary triangle (6-node)
    on the CAD surface it is nearest to. `volume_index`: gmsh volume tag -> 0-based solid index (MapShapes
    order); `surface_tag_of`: 0-based face index -> gmsh surface tag; `names`: each face index's name
    (Face_k / Part_k_Face_j). Returns the record quality.json keeps (fea.ftetwild); raises when a part
    could not be meshed."""
    from meshpipeline.engines.gmsh.fea_adapter import note_stage

    settings = (plan.get("mesh") or {}).get("ftetwild") or {}
    per = settings.get("per_part") or {}
    shape, fmap, _smap, own = cad_faces(staged)
    scale = frame_scale(gmsh, shape)
    jobs: list[dict] = []
    meta: list[dict] = []
    from meshpipeline.cad.fea_assembly import part_of_face

    for v in volumes:
        faces = own[int(volume_index[int(v)])]
        part = next((part_of_face(names[k]) for k in faces if part_of_face(names[k]) is not None), None)
        key = f"Part_{part}" if part is not None and f"Part_{part}" in per else "whole"
        cfg = per.get(key) or next(iter(per.values()), None)
        if cfg is None:
            raise ValueError("the plan holds no fTetWild settings for this part")
        edge, eps = float(cfg["edge_length_m"]), float(cfg["epsilon_m"])
        no_room: list[dict] = []
        if cfg.get("wall_checks"):
            # LAD-fea-17: A WALL HAS ROOM BEYOND IT - the readings that would set the envelope, tested on the
            # CAD (a tessellation at the default envelope is enough to place the probes), thinnest first
            d = float(cfg["diag_m"])
            note_stage(ws, "ftetwild", "testing which thin walls are real (room beyond them)")
            probe_defl = max(PROBE_SHARE * float(cfg["wall_checks"][0]["t_m"]),
                             TESSELLATION_SHARE * EPS_FLOOR_SHARE * d)
            cx, ct, cl = tessellate(fmap, faces, probe_defl / scale)
            thinnest, no_room = walls_with_room(cfg["wall_checks"], cx * scale, ct, cl, names)
            eps = envelope_m(d, thinnest)
        defl = TESSELLATION_SHARE * eps if no_room else float(cfg.get("tessellation_m") or TESSELLATION_SHARE * eps)
        floor = zone_floor_m(eps) if cfg.get("zone_floor_m") is not None else 0.0
        xyz, tris, label = tessellate(fmap, faces, defl / scale)
        xyz = xyz * scale
        # LAD-fea-17: the plan's zones, none finer than the rung's floor (older plans: none), and none drawn
        # from a "wall" with no room beyond it
        bg = sizing_grid(xyz, tris, label, list(names), without_walls(plan, no_room), edge, pad=4.0 * eps,
                         floor=floor)
        jobs.append({"dir": str(ws / "_ftetwild" / f"volume_{int(v)}"), "xyz": xyz, "tris": tris, "edge": edge,
                     "epsilon": eps, "bg": bg, "name": key if key != "whole" else "part", "ws": str(ws),
                     "max_its": int(settings.get("max_its") or MAX_ITS)})
        meta.append({"volume": int(v), "key": key, "sized": bg is not None, "part": part, "faces": faces, "xyz": xyz, "tris": tris,
                     "label": label, "edge": edge, "eps": eps, "deflection": defl, "no_room": no_room,
                     "zone_floor": floor})
    note_stage(ws, "ftetwild", f"meshing {len(jobs)} part(s) with fTetWild (the last rung)")
    # LAD-fea-18: THE RUNG'S TIME, from its own estimate (settings["budget_s"]): at SOFT_SHARE of it every part
    # still optimising is asked to finish the mesh it has; at the whole of it a part still running is stopped
    timed = own_budget(settings.get("budget_s"), [estimate_tets(j["xyz"], j["tris"], j["bg"], j["edge"]) for j in jobs],
                       workers)
    budget = timed.get("budget_s")
    if budget:
        t0 = time.monotonic()
        for j in jobs:
            j["soft_at"] = t0 + SOFT_SHARE * float(budget)
            j["hard_at"] = t0 + float(budget)
        print(f"{RUNG_LINE} the rung's time: {_minutes(float(budget))} for about {timed['tets']:,} tetrahedra on "
              f"{workers} core(s) ({TIME_RULE})", file=sys.stderr, flush=True)
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
    moved_all: list[str] = []
    lost_all: list[str] = []
    # LAD-fea-17: A PART'S MATERIAL LEFT OUT. fTetWild keeps the tets its winding number calls inside; on CAD
    # whose faces cross or are turned the wrong way (unsewn pieces laid into each other) that can drop a whole
    # piece (the lab pivot: its bar gone, 55 mm of CAD with no mesh). Where the CAD's surface is further than
    # LOST_SHARE of the part from the mesh, the part runs again with fTetWild's flood fill (what the CAD's
    # surface encloses, from the outside in), within the same time, and the closer mesh is kept
    for i, (m, r) in enumerate(zip(meta, results, strict=True)):
        gap = cad_gap(m["xyz"], r)
        lost_at = LOST_SHARE * float(np.linalg.norm(m["xyz"].max(axis=0) - m["xyz"].min(axis=0)))
        m["cad_gap_mm"] = _sig(gap * 1e3)
        if gap <= lost_at or (jobs[i].get("hard_at") is not None and time.monotonic() >= float(jobs[i]["hard_at"])):
            continue
        print(f"{RUNG_LINE} {jobs[i]['name']}: the CAD's surface is {gap * 1e3:.3g} mm from fTetWild's mesh - part of "
              "it was left out; run again with fTetWild's flood fill", file=sys.stderr, flush=True)
        again = run_parts([{**jobs[i], "floodfill": True}], workers)[0]
        if "error" not in again:
            gap2 = cad_gap(m["xyz"], again)
            m["refilled"] = {"gap_mm": _sig(gap * 1e3), "flood_fill_gap_mm": _sig(gap2 * 1e3), "kept": gap2 < gap}
            if gap2 < gap:
                results[i] = again
                m["cad_gap_mm"] = _sig(gap2 * 1e3)
    for m, r in zip(meta, results, strict=True):
        tets, _vol = orient(r["xyz"], r["tets"])
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
                          "command": r.get("command"), "threads": r.get("threads"), "rounds": r.get("rounds"),
                          "energy": r.get("energy"), "uninserted": r.get("uninserted"),
                          "stopped": bool(r.get("stopped")), "phases_s": r.get("phases_s"),
                          "zone_floor_mm": _sig(m["zone_floor"] * 1e3) if m["zone_floor"] else None,
                          "cad_gap_mm": m.get("cad_gap_mm"), **({"refilled": m["refilled"]} if m.get("refilled") else {}),
                          **({"walls_without_room": m["no_room"]} if m["no_room"] else {})})
        worst_dev = max(worst_dev, dev)
        eps_max = max(eps_max, m["eps"] + m["deflection"])
        moved_all += moved
        lost_all += lost
    info = {"engine": ENGINE, "version": VERSION, "rule": "LAD-fea-9",
            "threads": max(int(p.get("threads") or 1) for p in out_parts),
            "parts": [p["part"] for p in out_parts], "per_part": out_parts,
            "epsilon_mm": _sig(eps_max * 1e3), "deviation_mm": _sig(worst_dev * 1e3),
            "moved_faces": moved_all, "lost_faces": lost_all,
            "stopped": [p["part"] for p in out_parts if p.get("stopped")],
            "budget": timed,
            "tied": bool(settings.get("tied"))}
    info["words"] = words(info)
    print(f"{RUNG_LINE} meshed {', '.join(str(p['part']) for p in out_parts)}: "
          + ", ".join(f"{p['part']} {p['elements']} elements in {p['fTetWild_s']} s, within {p['deviation_mm']} mm"
                      for p in out_parts), file=sys.stderr, flush=True)
    return info


__all__ = ["BIN_PATH", "COMMIT", "ENGINE", "MAX_RUNG_PASSES", "Nearest", "VERSION", "binary", "boundary",
           "closest_on_triangles", "envelope_m", "failed_parts", "mesh_into", "orient", "params", "quadratic",
           "rung_parts", "run_part", "sizing_grid", "split_long", "tessellate", "words"]
