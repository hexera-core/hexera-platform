# Responsibility: The FEA path's fixed lever ladder: which approved change follows which measured failure, by how much, at most once each - and which pass of an attempt is delivered.
# Boundaries: pure over the measured facts of each pass. Nothing here is chosen by a model: AI levers are off (Areen's decision 4, 20 Oct); when they come (FEA build step 9) a proposal can only name a lever from this list, inside its limits.
# Collaborates with: engines/fea_driver.py (applies the levers between passes), engines/fea_plan.py (tightened zones, the base factor, the growth), engines/gmsh/fea_adapter.py (the mesh settings), docs/RULES.md (LAD-fea-n).
"""THE LADDER (docs/RULES.md LAD-fea-n). One attempt runs at most MAX_PASSES passes. After a pass,
the FIRST trigger that holds pulls its lever, if that lever was not pulled already:

  LAD-fea-2  no mesh came back (the fill failed)     -> the other 3D algorithm (Delaunay -> HXT)
  LAD-fea-1  elements under the quality floor        -> gentler growth (1.3 -> 1.2) and three
                                                         optimisation passes instead of one
  LAD-fea-4  more elements than the level holds      -> a coarser base size (the rules kept)
  LAD-fea-3  a plan rule fell short on the mesh       -> the zones that fell short, 20% finer
  LAD-fea-5  the best pass is delivered: a valid mesh first, then every rule met, then within
             the level, then the better worst element, then the earlier pass.
"""
from __future__ import annotations

from collections.abc import Mapping

from meshpipeline.engines import fea_plan as P

MAX_PASSES = 3
#: AI levers (design 6 guard rails) are off until FEA build step 9 / D9 shows they help.
AI_LEVERS = False
#: The FEA quality floor (engines/gmsh/driver.SICN_FLOOR): the element floor for FE assembly.
SICN_FLOOR = 0.1
GENTLE_GROWTH = 1.2
OPTIMISE_PASSES = 3
TIGHTEN = 0.8


def growth(levers: list[dict]) -> float:
    return GENTLE_GROWTH if any(lv["lever"] == "LAD-fea-1" for lv in levers) else P.GROWTH


def base_factor(levers: list[dict]) -> float:
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
    return {"algorithm3d": "hxt" if "LAD-fea-2" in names else "delaunay",
            "optimise_passes": OPTIMISE_PASSES if "LAD-fea-1" in names else 1}


def pass_facts(n: int, rc, q: Mapping, plan: Mapping) -> dict:
    """What one pass measured, in the terms the ladder and the delivery read."""
    built = bool(q) and rc in (0, 4)
    rules = ((q.get("fea") or {}).get("rules") or {}) if built else {}
    sicn = q.get("min_sicn") if built else None
    cells = int(q.get("cells") or 0) if built else 0
    ceiling = int((plan.get("detail") or {}).get("ceiling") or 0)
    valid = built and not q.get("fatal") and sicn is not None and float(sicn) >= SICN_FLOOR
    return {"pass": n, "rc": rc, "built": built, "valid": bool(valid), "min_sicn": sicn, "cells": cells,
            "estimate": (plan.get("estimate") or {}).get("elements"), "ceiling": ceiling,
            "over": bool(built and ceiling and cells > ceiling), "short": list(rules.get("short") or []),
            "through_wall_min": (rules.get("through_wall") or {}).get("min"),
            "per_quarter_min": (rules.get("hot_spots") or {}).get("min_per_quarter_turn")}


def next_lever(facts: Mapping, levers: list[dict], plan: Mapping) -> dict | None:
    """The lever this pass's failure pulls, or None (nothing failed, or its lever was pulled)."""
    pulled = {lv["lever"] for lv in levers}
    if len(levers) + 1 >= MAX_PASSES:
        return None
    if not facts["built"]:
        if "LAD-fea-2" in pulled:
            return None
        return {"lever": "LAD-fea-2", "inputs": {"rc": facts["rc"]},
                "words": "No mesh came back from the fill, so the next pass uses gmsh's other 3D algorithm (HXT)."}
    if not facts["valid"]:
        if "LAD-fea-1" in pulled:
            return None
        return {"lever": "LAD-fea-1", "inputs": {"min_sicn": facts["min_sicn"], "floor": SICN_FLOOR},
                "words": (f"Worst element {facts['min_sicn']} under the {SICN_FLOOR} floor, so the next pass grows "
                          f"sizes more gently ({GENTLE_GROWTH}) and optimises {OPTIMISE_PASSES} times.")}
    if facts["over"]:
        if "LAD-fea-4" in pulled:
            return None
        factor = round((P.AIM * facts["ceiling"] / max(facts["cells"], 1)) ** (1.0 / 3.0), 4)
        return {"lever": "LAD-fea-4", "factor": factor,
                "inputs": {"cells": facts["cells"], "ceiling": facts["ceiling"]},
                "words": (f"{facts['cells']:,} elements, over the level's {facts['ceiling']:,}, so the next pass "
                          f"coarsens the base size by {1 / factor:.3g} times; every rule is kept.")}
    if facts["short"]:
        if "LAD-fea-3" in pulled:
            return None
        zones = sorted(facts["short"])
        return {"lever": "LAD-fea-3", "zones": zones, "inputs": {"short": zones},
                "words": (f"{len(zones)} zone(s) fell short of their rule on the mesh ({', '.join(zones[:6])}), "
                          f"so the next pass makes them {int(round((1 - TIGHTEN) * 100))}% finer.")}
    return None


def _rank(f: Mapping) -> tuple:
    return (not f["valid"], bool(f["short"]), f["over"], -float(f["min_sicn"] or 0.0), f["pass"])


_DECIDING = ("a valid mesh", "every rule met", "within the level", "the better worst element", "the earlier pass")


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


__all__ = ["AI_LEVERS", "MAX_PASSES", "best_pass", "base_factor", "growth", "mesh_settings", "next_lever",
           "pass_facts", "tightened"]
