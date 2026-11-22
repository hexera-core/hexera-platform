# Responsibility: THE MESH CONTROLS ON THE MESHERS (docs/RULES.md NOTE-13): the appliers contracts/mesh_controls names, the one loop every plan seam calls (each asked control applied, or "not on this mesher" with the registry's reason), and the one row every route's report writes for a control.
# Boundaries: pure over plain data: an applier changes only the seam's own `target` dict and says what it built; it never raises past the loop (an error of ours is "not applied", the plan as it was).
# Collaborates with: contracts/mesh_controls.py (the table), engines/mesh_requirements.py (the asked set), engines/fea_driver.py, engines/snappy/drivers.py (plan seams), engines/fea_requirements.py, engines/shell_requirements.py, engines/cfd_requirements.py (reports).
from __future__ import annotations

import importlib
import logging
import math
from collections.abc import Callable, Mapping

from meshpipeline.contracts import mesh_controls as MC

logger = logging.getLogger(__name__)

RULE = MC.RULE
#: where a plan seam that holds no requirement set reads them (the FEA measurement record carries them into make_plan)
RECORD_KEY = "user_controls"


def asked(req: Mapping | None) -> dict[str, dict]:
    """Every control the set asks for: key -> {"id", "value", "choice"} - the reader's `controls`, and an existing
    field the registry names (sizes.growth; a list of the set's own, `local`: its entries are the value)."""
    r = req or {}
    out = {k: dict(v) for k, v in (r.get("controls") or {}).items() if k in MC.CONTROLS}
    rows = {str(x.get("field")): x for x in r.get("rows") or []}
    for c in MC.CONTROLS.values():
        if c.field and c.key not in out:
            value = (r.get("sizes") or {}).get(c.field)
            if value is None and isinstance(r.get(c.field), list) and r.get(c.field):
                value = [dict(e) for e in r[c.field]]
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
        if entry.get("place") and status in ("applied", "nearest"):
            # asked for ONE place (mesh_requirements: contracts/mesh_controls.place_of): set on the whole mesh, the
            # nearest there is, and said - never dropped to our default
            status, built = "nearest", (f"{built} - on the whole mesh, not only {entry['place']}: a setting for one "
                                        "place is not offered yet")
        out[key] = {"id": entry.get("id"), "status": status, "built": built,
                    **({"value": entry["value"]} if isinstance(entry.get("value"), (int, float)) else {})}
    return out


def asked_count(plan: Mapping | None, key: str = "wall_elements") -> int | None:
    """The count the notes asked for a control the plan applied (its "controls", from `apply`), else None: what the
    report holds the mesh to in place of our rule's number (elements through the walls: PLAN-fea-3's 2)."""
    got = ((plan or {}).get("controls") or {}).get(key) or {}
    v = got.get("value")
    if got.get("status") not in ("applied", "nearest") or not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return int(v)


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


def _fea_marks_again(z: dict) -> None:
    """A zone a size range held coarser (fea_plan.user_sized) and the count then made finer: the marks the range left
    on it (fewer through the wall than the rule, below the hot spot's accuracy) are worked out again for its new
    size, so the words and the mesh check read what is planned now."""
    rule = z.get("rule_size_m")
    if rule is None:
        return
    from meshpipeline.engines import fea_plan as P
    for k in ("crossed", "below_accuracy"):
        z.pop(k, None)
    if float(z["size_m"]) <= float(rule) and z.get("kind") in ("hole", "fillet", "notch"):
        # as fine as its rule's size again: the count per quarter-turn the range lowered is the rule's own again
        z.pop("per_quarter", None)
    P._accuracy({**z, "size_m": float(rule)}, z, float(z["size_m"]))


def fea_wall_elements(target: dict, value) -> tuple[str, str]:
    """FEA solid: N elements through every wall (PLAN-fea-3's 2; a hot spot's wall keeps its own 3 when that is more).
    Each wall zone is sized thickness / N - also where a size range the user set (their notes or their saved
    meshing standard: fea_plan.user_sized, applied before this) would hold it coarser: the count is the more exact
    ask and wins there, and the words say how many walls that made finer than the shortest size asked."""
    n = int(value)
    walls = 0
    under_min: list[float] = []          # the shortest size asked, on each wall the count made finer than it
    for z in target.get("zones") or []:
        t = z.get("thickness_m")
        if not t:
            continue
        before = float(z["size_m"])
        if z.get("kind") in ("wall", "wall_spot"):
            z["size_m"] = float(t) / n
            z["wall_elements"] = n
            z["why"] = f"{z.get('why', '').split(':')[0]}: {n} elements through it, as your notes ask"
            walls += 1
        elif n > float(z.get("wall_elements") or 0):
            z["size_m"] = min(float(z["size_m"]), float(t) / n)
            z["wall_elements"] = n
        else:
            continue
        if float(z["size_m"]) < before:
            z.pop("valid_held", None)    # finer for the count now, not held at the coarsest valid size
        _fea_marks_again(z)
        lo = z.get("user_min_m")
        if lo is not None and float(z["size_m"]) < float(lo) * (1 - 1e-6):
            z["count_wins"] = n          # fea_plan.user_words says why it is finer than the range
            if z.get("kind") in ("wall", "wall_spot"):
                under_min.append(float(lo))
    if not walls:
        return "applied", "no wall of this part is thin enough to set the element size; its own size gives more"
    built = f"{n} elements through each of {walls} wall{'s' if walls != 1 else ''}"
    if under_min:
        k = len(under_min)
        built += (f" - {'all' if k == walls else k} of them finer than the shortest element asked "
                  f"({max(under_min) * 1e3:.3g} mm): the count wins there")
    return "applied", built


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


def vmtk_cells_across(target: dict, value) -> tuple[str, str]:
    """VMTK (the vessel mesher): cells across every passage - its edge is `edge_length_factor` times the local radius,
    so 2 / factor cells span a diameter (engines/vmtk/authoring: 0.15 puts about 13 across). The budget fit may
    coarsen it after (engines/vmtk/vmtk_runner says so on the row)."""
    n = int(value)
    if n < 2:
        target["edge_length_factor"] = 1.0
        return "nearest", "2 cells across each passage: an edge no longer than the local radius is the coarsest"
    target["edge_length_factor"] = round(2.0 / n, 5)
    return "applied", f"{n} cells across every passage (each edge {2.0 / n:.3g} times the local radius)"


def cfmesh_cells_across(target: dict, value) -> tuple[str, str]:
    """cfMesh, a flow through a passage: cells across it - the wall band's cell is the passage's width / N (its
    typical width, `passage_d`; engines/cfmesh/cfmesh_runner then puts it on the octree and says what it built)."""
    d = float(target.get("passage_d") or 0.0)
    if not target.get("internal"):
        return "not_applied", "on a flow around a body the gaps get our own cells-across rule; a number of yours is " \
                              "not wired there yet"
    if d <= 0.0:
        return "not_applied", "the passage's width was not measured on this case, so the cells across it cannot be set"
    n = max(1, int(value))
    target["wall_cell"] = d / n
    return "applied", f"{n} cells across the passage (cells of {d / n * 1e3:.3g} mm at its walls)"


def snappy_surface_level(target: dict, value) -> tuple[str, str]:
    """snappyHexMesh, a flow through a passage: the wall's refinement level against the background cell. The wall
    cell stays bore / cells across; the level sets how much coarser the background is (2 to the level)."""
    if not target.get("internal"):
        return "not_applied", "on a flow around a body the wall's level follows the sizes; give a size at the walls"
    lv = int(value)
    target["surface_level"] = lv
    target["feature_level"] = max(int(target.get("feature_level") or 0), lv + 1)
    return "applied", f"level {lv} at the walls: the background cell is {2 ** lv} times the wall cell"


def shell_place_sizes(target: dict, value) -> tuple[str, str]:
    """Shell (SHMESH-52): a size in one named place - the `local` asks (`value`) - on the sewn faces the parts reading
    ties them to (engines/shell_places.plan). `target`: the shell plan seam's {"rows", "owners", "record", "places",
    "areas_mm2", "curved", "planned_max"}; the rows are sized in place and what was done is left in
    target["applied"] (the report's rows read it)."""
    from meshpipeline.engines import shell_places as SP

    got = SP.plan(target["rows"], target["owners"], target["record"], target.get("places"), list(value or []),
                  areas_mm2=target["areas_mm2"], curved=target.get("curved"),
                  planned_max=float(target.get("planned_max") or math.inf))
    target["applied"] = got
    if not got or not got.get("applied"):
        return "not_applied", "no place your notes name could be found on the shell"
    return ("nearest" if got.get("held") else "applied"), SP.summary_words(got)


__all__ = ["RECORD_KEY", "RULE", "apply", "asked", "asked_count", "cfmesh_cells_across", "vmtk_cells_across", "fea_growth", "fea_wall_elements", "report_row",
           "shell_place_sizes", "snappy_cells_across", "snappy_surface_level"]
