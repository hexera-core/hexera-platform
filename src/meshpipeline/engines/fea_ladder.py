# Responsibility: The FEA path's fixed lever ladder: which approved change follows which measured failure, by how much, at most once each - and which pass of an attempt is delivered.
# Boundaries: pure over the measured facts of each pass. Nothing here is chosen by a model: the AI (engines/fea_ai.py, AI-fea-8) may only pick one of lever_options, the levers this list allows for the failure, inside their limits.
# Collaborates with: engines/fea_driver.py (applies the levers between passes), engines/fea_plan.py (tightened zones, the base factor, the growth), engines/gmsh/fea_adapter.py (the mesh settings), docs/RULES.md (LAD-fea-n).
"""THE LADDER (docs/RULES.md LAD-fea-n). One attempt runs at most MAX_PASSES passes. After a pass,
the FIRST trigger that holds pulls its lever, if that lever was not pulled already:

  LAD-fea-2  no mesh came back (the fill failed)     -> the other 3D algorithm (Delaunay -> HXT)
  LAD-fea-1  an invalid mesh (a degenerate or inverted -> gentler growth (1.3 -> 1.2) and three
             element)                                     optimisation passes instead of one
  LAD-fea-4  more elements than the limit after meshing -> the finest zones coarser, aimed at 85% of
             the limit (never past what keeps elements valid)
  LAD-fea-3  a plan rule fell short on the mesh       -> the zones that fell short, 20% finer
             (never on a plan coarsened to fit: it is short there by choice)
  LAD-fea-5  the best pass is delivered: a valid mesh first, then every rule met, then the better
             worst element, then the earlier pass.
  LAD-fea-7  OUT OF MEMORY: a pass the mesh job's memory limit killed pulls no lever - the other
             3D algorithm and gentler growth need as much memory or more - and the attempt ends
             saying so (two lab parts spent an hour each on a second pass killed the same way).
  LAD-fea-8  A SURFACE STAGE THAT STALLS (the watchdog stopped gmsh in 2D) -> gmsh's first mesh of
             every face is kept (no switch to MeshAdapt), its repeated triangles dropped (MESH-5)
  LAD-fea-14 A 3D REFINEMENT THAT SPINS (the watchdog read gmsh rejecting nearly every point it
             inserts, engines/gmsh/gmsh_runner.RefineSpin) -> a fill that failed: LAD-fea-2 and on
  LAD-fea-6  NO IDENTICAL RETRY: the surfaces a failed pass went wrong on (the fill named them, or
             an invalid element touches them) are meshed the other way (gmsh's MeshAdapt) on the
             next pass - with LAD-fea-1 or LAD-fea-2, or on its own once those are pulled. A surface
             the default 2D algorithm leaves empty is meshed again with MeshAdapt in the same pass.

  LAD-fea-9  THE LAST RUNG: every gmsh option above is spent (for an assembly, the tie too) and no
             pass is valid -> fTetWild meshes the part(s) still without a valid mesh (an assembly's
             parts the failed passes named, tied to the others' gmsh mesh; then, if that fails too,
             every part), its settings from the plan and the measured part (engines/fea_tetwild.py).
             Never before gmsh's options, never instead of them, never for our infrastructure (a guard
             trip, a worker too small) or a plan out of memory on the largest machine (LAD-fea-7).

A VALID MESH is the only thing that fails an FEA run (Areen, 21 Oct): no degenerate or inverted
element. Every other figure - the quality note floor included (SICN 0.1 straight, 0.01 curved) - is a note.
"""
from __future__ import annotations

from collections.abc import Mapping

from meshpipeline.engines import fea_plan as P

MAX_PASSES = 3
#: AI levers (design 15): the AI may pick among lever_options (engines/fea_ai.py, AI-fea-8).
AI_LEVERS = True
#: The FEA quality floor (engines/gmsh/driver.SICN_FLOOR): a note on the delivery, never a failure.
SICN_FLOOR = 0.1
GENTLE_GROWTH = 1.2
OPTIMISE_PASSES = 3
TIGHTEN = 0.8
#: LAD-fea-6: a failure naming more than this share of the part's surfaces is not localised to them
LOCAL_SHARE = 0.25


def growth(levers: list[dict]) -> float:
    return GENTLE_GROWTH if any(lv["lever"] == "LAD-fea-1" for lv in levers) else P.GROWTH


def coarse_factor(levers: list[dict]) -> float:
    """LAD-fea-4: how much finer than planned the zones may not be (under 1: coarser)."""
    f = 1.0
    for lv in levers:
        if lv["lever"] == "LAD-fea-4":
            f *= float(lv["factor"])
    return f


def tightened(levers: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for lv in levers:
        if lv["lever"] == "LAD-fea-3":
            for z in lv["zones"]:
                out[z] = out.get(z, 1.0) * TIGHTEN
    return out


def mesh_settings(levers: list[dict]) -> dict:
    names = {lv["lever"] for lv in levers}
    # LAD-fea-8: a surface the surface stage stalled (or ran out of time) on is never put on MeshAdapt again
    stuck = {int(lv["inputs"]["surface"]) for lv in levers
             if lv["lever"] == SURFACE_STALL and (lv.get("inputs") or {}).get("surface") is not None}
    remesh = sorted(_remeshed(levers) - stuck)
    return {"algorithm3d": "hxt" if "LAD-fea-2" in names else "delaunay",
            "optimise_passes": OPTIMISE_PASSES if "LAD-fea-1" in names else 1,
            **({"remesh_surfaces": remesh} if remesh else {}),
            # LAD-fea-8: the surface stage keeps gmsh's own first mesh of every face (no switch to MeshAdapt)
            **({"surface_switch": False} if SURFACE_STALL in names else {})}


#: LAD-fea-8: the lever a surface stage that stalled pulls
SURFACE_STALL = "LAD-fea-8"


def stalled_surface(log: str) -> int | None:
    """The surface gmsh was meshing when its output stopped, when that was in the 2D stage: the last
    "Meshing surface N" after the last "Meshing 2D..." with no "Done meshing 2D" after it; else None."""
    import re

    text = log or ""
    start = text.rfind("Meshing 2D...")
    if start < 0 or "Done meshing 2D" in text[start:]:
        return None
    found = re.findall(r"Meshing surface (\d+)", text[start:])
    return int(found[-1]) if found else None


def surface_stall_lever(surface: int, levers: list[dict], *, n: int) -> dict | None:
    """LAD-fea-8: A SURFACE STAGE THAT STALLS (the watchdog stopped the mesher in 2D, on `surface`) is not
    run again the same way. gmsh, when its Frontal-Delaunay mesh of a face comes out with two triangles
    the same, throws it away and meshes the face with MeshAdapt, which on a face with a pole at fine size
    can run longer than the watchdog's window with no output (nakuja_rocket's nose cone, Part_2 alone: 15
    minutes silent on one face on a laptop-class worker; about 10 on a 16-core VM). The next pass keeps
    gmsh's first mesh of every face - its repeats dropped by MESH-5 - instead. None when pulled already or
    no pass is left."""
    if any(lv["lever"] == SURFACE_STALL for lv in levers) or len(levers) + 1 >= MAX_PASSES:
        return None
    return {"lever": SURFACE_STALL, "inputs": {"pass": n, "surface": int(surface)}, "surfaces": [],
            "words": (f"The surface stage stopped making progress on surface {int(surface)} (gmsh's switch to "
                      "MeshAdapt there ran past the watchdog), so the next pass keeps gmsh's first mesh of every face "
                      "and drops its repeated triangles (MESH-5).")}


def failed_surfaces(q: Mapping, log: str = "") -> list[int]:
    """The surfaces a pass went wrong on: the invalid elements' (quality.json), else the ones the
    failed fill named (the adapter's BAD SURFACES line in its output)."""
    if q.get("fea_bad_surfaces"):
        return sorted(int(s) for s in q["fea_bad_surfaces"])
    from meshpipeline.engines.gmsh.fea_adapter import BAD_SURFACES

    for line in reversed((log or "").splitlines()):
        if line.startswith(BAD_SURFACES):
            return sorted(int(s) for s in line[len(BAD_SURFACES):].replace(",", " ").split() if s.isdigit())
    return []


def failed_faces(q: Mapping, log: str = "") -> list[str]:
    """The CAD faces a pass went wrong on, by name (the adapter's BAD FACES, or quality.json's)."""
    if q.get("fea_bad_faces"):
        return sorted(str(s) for s in q["fea_bad_faces"])
    from meshpipeline.engines.gmsh.fea_adapter import BAD_FACES

    for line in reversed((log or "").splitlines()):
        if line.startswith(BAD_FACES):
            return sorted(x.strip() for x in line[len(BAD_FACES):].split(",") if x.strip())
    return []


def _remeshed(levers: list[dict]) -> set[int]:
    return {int(s) for lv in levers for s in lv.get("surfaces") or []}


def pass_facts(n: int, rc, q: Mapping, plan: Mapping, log: str = "", killed: bool = False) -> dict:
    """What one pass measured, in the terms the ladder and the delivery read."""
    built = bool(q) and rc in (0, 4)
    rules = ((q.get("fea") or {}).get("rules") or {}) if built else {}
    sicn = q.get("min_sicn") if built else None
    cells = int(q.get("cells") or 0) if built else 0
    limit = int((plan.get("limit") or {}).get("elements") or 0)
    # VALID: built, nothing fatal (no degenerate or inverted element) - the held worst element above 0
    valid = built and not q.get("fatal") and sicn is not None and float(sicn) > 0.0
    return {"pass": n, "rc": rc, "built": built, "valid": bool(valid), "min_sicn": sicn, "cells": cells,
            "estimate": (plan.get("estimate") or {}).get("elements"), "limit": limit,
            "under_floor": int(q.get("elements_under_floor") or 0) if built else None,
            "over": bool(built and limit and cells > limit),
            "short": list(rules.get("short") or []),
            "through_wall_min": (rules.get("through_wall") or {}).get("min"),
            "per_quarter_min": (rules.get("hot_spots") or {}).get("min_per_quarter_turn"),
            "bad_surfaces": [] if valid else failed_surfaces(q if built else {}, log),
            "bad_faces": [] if valid else failed_faces(q if built else {}, log),
            "surfaces_total": int((plan.get("measure") or {}).get("faces") or 0),
            # LAD-fea-7: the mesher was killed by the job's memory limit
            "out_of_memory": bool(killed and not built)}


def next_lever(facts: Mapping, levers: list[dict], plan: Mapping) -> dict | None:
    """The lever this pass's failure pulls, or None (nothing failed, or its lever was pulled)."""
    options = lever_options(facts, levers, plan)
    return options[0] if options else None


def lever_options(facts: Mapping, levers: list[dict], plan: Mapping) -> list[dict]:
    """Every lever this pass's failure ALLOWS, the fixed ladder's own first (next_lever). After a
    fill that failed or an invalid mesh the other unpulled fill levers are allowed too - the AI may
    pick one of them (AI-fea-8, engines/fea_ai.py); after a mesh over the limit or short of a rule
    there is one lever only."""
    pulled = {lv["lever"] for lv in levers}
    if len(levers) + 1 >= MAX_PASSES:
        return []
    if facts.get("out_of_memory"):
        # LAD-fea-7: no lever builds this plan in less memory
        return []
    # LAD-fea-6: the surfaces this pass went wrong on that no lever has re-meshed yet - ONLY THE ONES THAT
    # FAILED: a failure that names more than LOCAL_SHARE of the part's surfaces (a fill that names a whole
    # volume's boundary) is not on those surfaces, and switching them all to MeshAdapt only makes every face
    # slow (1 Nov: all 441 faces of a 640 mm part, MeshAdapt grinding on its largest face for hours)
    bad = set(facts.get("bad_surfaces") or [])
    total = int(facts.get("surfaces_total") or 0)
    if total and len(bad) > LOCAL_SHARE * total:
        bad = set()
    fresh = sorted(bad - _remeshed(levers))
    other = (f" The surface(s) it went wrong on ({_listed(fresh)}) are meshed with gmsh's MeshAdapt "
             "(LAD-fea-6).") if fresh else ""
    hxt = {"lever": "LAD-fea-2", "inputs": {"rc": facts["rc"], "surfaces": fresh}, "surfaces": fresh,
           "words": "No mesh came back from the fill, so the next pass uses gmsh's other 3D algorithm (HXT)." + other}
    gentle = {"lever": "LAD-fea-1", "inputs": {"min_sicn": facts["min_sicn"], "surfaces": fresh}, "surfaces": fresh,
              "words": (f"A degenerate or inverted element (worst {facts['min_sicn']}), so the next pass grows "
                        f"sizes more gently ({GENTLE_GROWTH}) and optimises {OPTIMISE_PASSES} times." + other)}
    if not facts["built"] or not facts["valid"]:
        if not facts["built"]:
            gentle = {**gentle, "words": (f"No mesh came back from the fill, so the next pass grows sizes more gently "
                                          f"({GENTLE_GROWTH}) and optimises {OPTIMISE_PASSES} times." + other)}
            order = [hxt, gentle]
        else:
            hxt = {**hxt, "words": (f"A degenerate or inverted element (worst {facts['min_sicn']}), so the next pass "
                                    "uses gmsh's other 3D algorithm (HXT)." + other)}
            order = [gentle, hxt]
        rest = [o for o in order if o["lever"] not in pulled]
        way = _other_way(fresh, facts)
        if order[0]["lever"] in pulled:
            # the fixed ladder's own choice was pulled already: the other way leads (LAD-fea-6), and
            # with no surface left to mesh the other way the ladder stops here, as it always has
            return [way, *rest] if way else []
        return rest + ([way] if way else [])
    if facts["over"]:
        if "LAD-fea-4" in pulled:
            return []
        factor = round((P.LIMIT_AIM * facts["limit"] / max(facts["cells"], 1)) ** (1.0 / 3.0), 4)
        return [{"lever": "LAD-fea-4", "factor": factor, "inputs": {"cells": facts["cells"], "limit": facts["limit"]},
                 "words": (f"{facts['cells']:,} elements, over the {facts['limit']:,} limit, so the next pass makes the "
                           f"finest zones {1 / factor:.3g} times coarser - never past what keeps elements valid.")}]
    if facts["short"]:
        if "LAD-fea-3" in pulled or plan.get("coarsened"):
            # a plan coarsened to fit is short where it says, by choice: tightening those zones only
            # puts it over the limit again (PLAN-fea-8)
            return []
        zones = sorted(facts["short"])
        return [{"lever": "LAD-fea-3", "zones": zones, "inputs": {"short": zones},
                 "words": (f"{len(zones)} zone(s) fell short of their rule on the mesh ({', '.join(zones[:6])}), "
                           f"so the next pass makes them {int(round((1 - TIGHTEN) * 100))}% finer.")}]
    return []

def _listed(tags: list[int]) -> str:
    return ", ".join(str(s) for s in tags[:8]) + (", ..." if len(tags) > 8 else "")


def _other_way(fresh: list[int], facts: Mapping) -> dict | None:
    """LAD-fea-6 on its own: the lever this failure pulls was pulled already, so the next pass is
    not the same pass again - the surfaces it went wrong on are meshed the other way. None when
    there are none left to change (every one was re-meshed already, or none was named)."""
    if not fresh:
        return None
    return {"lever": "LAD-fea-6", "inputs": {"surfaces": fresh, "rc": facts.get("rc"), "min_sicn": facts.get("min_sicn")},
            "surfaces": fresh,
            "words": (f"The pass went wrong again, on surface(s) {_listed(fresh)}, so the next pass meshes them "
                      "with gmsh's MeshAdapt instead of the default.")}


def rung_words(parts: list[int] | None, rung: int) -> str:
    """LAD-fea-9's line in the plan's words: which parts fTetWild meshes, and why."""
    who = ("the part" if parts is None else
           ("part " if len(parts) == 1 else "parts ") + ", ".join(f"Part_{p}" for p in parts[:8])
           + (", ..." if len(parts) > 8 else ""))
    if parts is None and rung > 1:
        who = "every part"
    return (f"No gmsh option built a valid mesh (Delaunay, HXT, MeshAdapt on the surfaces that failed"
            f"{', the tie' if parts is not None or rung > 1 else ''}), so fTetWild meshes {who} - the last "
            "rung (LAD-fea-9). Its surface follows the CAD within its envelope; the faces keep their names.")


def _rank(f: Mapping) -> tuple:
    return (not f["valid"], bool(f["short"]), f["over"], -float(f["min_sicn"] or 0.0), f["pass"])


_DECIDING = ("a valid mesh", "every rule met", "within the limit", "the better worst element", "the earlier pass")


def best_pass(passes: list[dict]) -> tuple[int | None, str]:
    """(the pass delivered, what decided it) - None when no pass built a valid mesh."""
    valid = [f for f in passes if f["valid"]]
    if not valid:
        return None, "no valid mesh"
    ranked = sorted(valid, key=_rank)
    if len(ranked) == 1:
        return ranked[0]["pass"], "the only valid mesh"
    a, b = _rank(ranked[0]), _rank(ranked[1])
    i = next((k for k in range(len(a)) if a[k] != b[k]), len(a) - 1)
    return ranked[0]["pass"], _DECIDING[i]


__all__ = ["AI_LEVERS", "MAX_PASSES", "SURFACE_STALL", "stalled_surface", "surface_stall_lever", "best_pass", "coarse_factor", "failed_surfaces", "growth", "mesh_settings",
           "lever_options", "next_lever", "pass_facts", "rung_words", "tightened"]
