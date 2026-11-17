# Responsibility: THE MESH CONTROLS ON THE MESHERS (docs/RULES.md NOTE-13): the appliers contracts/mesh_controls names, the one loop every plan seam calls (each asked control applied, or "not on this mesher" with the registry's reason), and the one row every route's report writes for a control.
# Boundaries: pure over plain data: an applier changes only the seam's own `target` dict and says what it built; it never raises past the loop (an error of ours is "not applied", the plan as it was).
# Collaborates with: contracts/mesh_controls.py (the table), engines/mesh_requirements.py (the asked set), engines/fea_driver.py, engines/snappy/drivers.py (plan seams), engines/fea_requirements.py, engines/shell_requirements.py, engines/cfd_requirements.py (reports).
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Mapping

from meshpipeline.contracts import mesh_controls as MC

logger = logging.getLogger(__name__)

RULE = MC.RULE
#: where a plan seam that holds no requirement set reads them (the FEA measurement record carries them into make_plan)
RECORD_KEY = "user_controls"


def asked(req: Mapping | None) -> dict[str, dict]:
    """Every control the set asks for: key -> {"id", "value", "choice"} - the reader's `controls`, and an existing
    field the registry names (sizes.growth)."""
    r = req or {}
    out = {k: dict(v) for k, v in (r.get("controls") or {}).items() if k in MC.CONTROLS}
    rows = {str(x.get("field")): x for x in r.get("rows") or []}
    for c in MC.CONTROLS.values():
        if c.field and c.key not in out:
            value = (r.get("sizes") or {}).get(c.field)
            if value is not None:
                out[c.key] = {"id": (rows.get(c.field) or {}).get("id"), "value": value, "choice": ""}
    return out


def _resolve(dotted: str) -> Callable:
    mod, _dot, name = dotted.rpartition(".")
    return getattr(importlib.import_module(mod), name)


def apply(lane: str, req: Mapping | None, target: dict) -> dict[str, dict]:
    """THE ONE LOOP a plan seam calls: each control the set asks for, applied on `target` by its applier for `lane`,
    or "not applied" with the registry's reason. A control the lane's own older code applies (Legacy) is left to it.
    key -> {"id", "status": "applied" | "nearest" | "not_applied", "built"}."""
    out: dict[str, dict] = {}
    for key, entry in asked(req).items():
        how = MC.lane_of(lane, key)
        if how is None or isinstance(how, MC.Legacy):
            continue
        if isinstance(how, MC.NotHere):
            out[key] = {"id": entry.get("id"), "status": "not_applied", "built": how.why}
            continue
        try:
            status, built = _resolve(how)(target, entry.get("choice") or entry.get("value"))
        except Exception:  # noqa: BLE001 - a control never fails a mesh
            logger.warning("mesh controls: %s on %s failed - the plan's own value stands", key, lane, exc_info=True)
            status, built = "not_applied", "it could not be applied here (an error of ours); our own setting is used"
        out[key] = {"id": entry.get("id"), "status": status, "built": built}
    return out


def report_row(lane: str, key: str, applied: Mapping | None = None) -> dict:
    """THE ROW a route's report writes for a control (the shape of mesh_requirements.limit_row's callers): what the
    seam did (`applied`, from `apply`), else what the registry says this lane does. "setting": applied as a setting,
    never told as met."""
    c = MC.CONTROLS[key]
    got = (applied or {}).get(key)
    if got is not None and got.get("status") == "not_applied":
        return {"applied": False, "met": None, "delivered": f"not applied - {got['built']}"}
    if got is not None:
        lead = "applied as a setting" if got["status"] == "applied" else "not possible as asked"
        return {"applied": True, "met": None, "delivered": f"{lead} - {got['built']}"}
    how = c.lanes.get(lane)
    if isinstance(how, MC.NotHere):
        return {"applied": False, "met": None, "delivered": f"not applied - {how.why}"}
    if c.verify != "setting":
        return dict(_resolve(c.verify)(lane, key))
    return {"applied": True, "met": None, "delivered": "applied as a setting"}


# #
# the appliers (named in contracts/mesh_controls.CONTROLS)
# #

def fea_growth(target: dict, value) -> tuple[str, str]:
    """FEA solid: the plan's growth rate (PLAN-fea-6's 1.3). The ladder's gentler grading (LAD-fea-1), when a pass
    needed it to build valid elements, is kept over a faster one asked."""
    g = float(value)
    ours = float(target.get("growth") or 1.3)
    if target.get("gentle") and g > ours:
        return "nearest", f"sizes grow by at most {ours:g}: a gentler grading was needed to build valid elements"
    target["growth"] = g
    return "applied", f"sizes grow by at most {g:g} from one element to the next"


def fea_wall_elements(target: dict, value) -> tuple[str, str]:
    """FEA solid: N elements through every wall (PLAN-fea-3's 2; a hot spot's wall keeps its own 3 when that is more).
    Each wall zone is sized thickness / N."""
    n = int(value)
    walls = 0
    for z in target.get("zones") or []:
        t = z.get("thickness_m")
        if not t:
            continue
        if z.get("kind") in ("wall", "wall_spot"):
            z["size_m"] = float(t) / n
            z["wall_elements"] = n
            z["why"] = f"{z.get('why', '').split(':')[0]}: {n} elements through it, as your notes ask"
            walls += 1
        elif n > float(z.get("wall_elements") or 0):
            z["size_m"] = min(float(z["size_m"]), float(t) / n)
            z["wall_elements"] = n
    if not walls:
        return "applied", "no wall of this part is thin enough to set the element size; its own size gives more"
    return "applied", f"{n} elements through each of {walls} wall{'s' if walls != 1 else ''}"


def snappy_cells_across(target: dict, value) -> tuple[str, str]:
    """snappyHexMesh, a flow through a passage: cells across the bore (the planner's 16-40); at least 8, which keeps
    the passage open (CFDREQ-2's validity floor)."""
    if not target.get("internal"):
        return "not_applied", "on a flow around a body the gaps get our own cells-across rule; a number of yours is " \
                              "not wired there yet"
    n = int(value)
    target["cells_across"] = max(8, n)
    if n < 8:
        return "nearest", "8 cells across the bore: fewer would not keep the passage open"
    return "applied", f"{n} cells across the bore"


def snappy_surface_level(target: dict, value) -> tuple[str, str]:
    """snappyHexMesh, a flow through a passage: the wall's refinement level against the background cell. The wall
    cell stays bore / cells across; the level sets how much coarser the background is (2 to the level)."""
    if not target.get("internal"):
        return "not_applied", "on a flow around a body the wall's level follows the sizes; give a size at the walls"
    lv = int(value)
    target["surface_level"] = lv
    target["feature_level"] = max(int(target.get("feature_level") or 0), lv + 1)
    return "applied", f"level {lv} at the walls: the background cell is {2 ** lv} times the wall cell"


__all__ = ["RECORD_KEY", "RULE", "apply", "asked", "fea_growth", "fea_wall_elements", "report_row",
           "snappy_cells_across", "snappy_surface_level"]
