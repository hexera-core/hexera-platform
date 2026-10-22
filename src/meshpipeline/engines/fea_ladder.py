# Responsibility: The FEA path's fixed lever ladder: which approved change follows which measured failure, by how much, at most once each - and which pass of an attempt is delivered.
# Boundaries: pure over the measured facts of each pass. Nothing here is chosen by a model: AI levers are off (Areen's decision 4, 20 Oct); when they come (FEA build step 9) a proposal can only name a lever from this list, inside its limits.
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
  LAD-fea-6  NO IDENTICAL RETRY: the surfaces a failed pass went wrong on (the fill named them, or
             an invalid element touches them) are meshed the other way (gmsh's MeshAdapt) on the
             next pass - with LAD-fea-1 or LAD-fea-2, or on its own once those are pulled. A surface
             the default 2D algorithm leaves empty is meshed again with MeshAdapt in the same pass.

A VALID MESH is the only thing that fails an FEA run (Areen, 21 Oct): no degenerate or inverted
element. Every other figure - the quality note floor included (SICN 0.1 straight, 0.01 curved) - is a note.
"""
from __future__ import annotations

from collections.abc import Mapping

from meshpipeline.engines import fea_plan as P

MAX_PASSES = 3
#: AI levers (design 6 guard rails) are off until FEA build step 9 / D9 shows they help.
AI_LEVERS = False
#: The FEA quality floor (engines/gmsh/driver.SICN_FLOOR): a note on the delivery, never a failure.
SICN_FLOOR = 0.1
GENTLE_GROWTH = 1.2
OPTIMISE_PASSES = 3
TIGHTEN = 0.8


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
    remesh = sorted(_remeshed(levers))
    return {"algorithm3d": "hxt" if "LAD-fea-2" in names else "delaunay",
            "optimise_passes": OPTIMISE_PASSES if "LAD-fea-1" in names else 1,
            **({"remesh_surfaces": remesh} if remesh else {})}


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


def _remeshed(levers: list[dict]) -> set[int]:
    return {int(s) for lv in levers for s in lv.get("surfaces") or []}


def pass_facts(n: int, rc, q: Mapping, plan: Mapping, log: str = "") -> dict:
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
            "bad_surfaces": [] if valid else failed_surfaces(q if built else {}, log)}


def next_lever(facts: Mapping, levers: list[dict], plan: Mapping) -> dict | None:
    """The lever this pass's failure pulls, or None (nothing failed, or its lever was pulled)."""
    pulled = {lv["lever"] for lv in levers}
    if len(levers) + 1 >= MAX_PASSES:
        return None
    # LAD-fea-6: the surfaces this pass went wrong on that no lever has re-meshed yet
    fresh = sorted(set(facts.get("bad_surfaces") or []) - _remeshed(levers))
    other = (f" The surface(s) it went wrong on ({_listed(fresh)}) are meshed with gmsh's MeshAdapt "
             "(LAD-fea-6).") if fresh else ""
    if not facts["built"]:
        if "LAD-fea-2" in pulled:
            return _other_way(fresh, facts)
        return {"lever": "LAD-fea-2", "inputs": {"rc": facts["rc"], "surfaces": fresh}, "surfaces": fresh,
                "words": "No mesh came back from the fill, so the next pass uses gmsh's other 3D algorithm (HXT)." + other}
    if not facts["valid"]:
        if "LAD-fea-1" in pulled:
            return _other_way(fresh, facts)
        return {"lever": "LAD-fea-1", "inputs": {"min_sicn": facts["min_sicn"], "surfaces": fresh}, "surfaces": fresh,
                "words": (f"A degenerate or inverted element (worst {facts['min_sicn']}), so the next pass grows "
                          f"sizes more gently ({GENTLE_GROWTH}) and optimises {OPTIMISE_PASSES} times." + other)}
    if facts["over"]:
        if "LAD-fea-4" in pulled:
            return None
        factor = round((P.LIMIT_AIM * facts["limit"] / max(facts["cells"], 1)) ** (1.0 / 3.0), 4)
        return {"lever": "LAD-fea-4", "factor": factor, "inputs": {"cells": facts["cells"], "limit": facts["limit"]},
                "words": (f"{facts['cells']:,} elements, over the {facts['limit']:,} limit, so the next pass makes the "
                          f"finest zones {1 / factor:.3g} times coarser - never past what keeps elements valid.")}
    if facts["short"]:
        if "LAD-fea-3" in pulled or plan.get("coarsened"):
            # a plan coarsened to fit is short where it says, by choice: tightening those zones only
            # puts it over the limit again (PLAN-fea-8)
            return None
        zones = sorted(facts["short"])
        return {"lever": "LAD-fea-3", "zones": zones, "inputs": {"short": zones},
                "words": (f"{len(zones)} zone(s) fell short of their rule on the mesh ({', '.join(zones[:6])}), "
                          f"so the next pass makes them {int(round((1 - TIGHTEN) * 100))}% finer.")}
    return None


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


__all__ = ["AI_LEVERS", "MAX_PASSES", "best_pass", "coarse_factor", "failed_surfaces", "growth", "mesh_settings",
           "next_lever", "pass_facts", "tightened"]
