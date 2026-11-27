# Responsibility: Say where a snappyHexMesh mesh's cells went - by octree level, by the refinement that asked for each level, and by the rule behind it.
# Boundaries: reads the plan the case was authored with and the mesher's own log; it sizes nothing and judges nothing.
# Collaborates with: snappy_runner.render_internal_case (writes the plan), snappy/finalize (reads the record into the quality record).
"""WHERE THE CELLS WENT (rulebook 75).

A mesh that meets every rule can still spend its cells where no rule needs them, and the customer
pays for those cells in run time and solver time. So every internal snappyHexMesh mesh carries a
record of its cells:

* THE PLAN, written when the case is authored (`write_plan`): every refinement the dictionary asks
  for - the background, the wall's level, the near-wall band, each opening, the feature edges, each
  narrow-passage level, the thin-plate boxes, the seed - with its octree level, its cell size and the
  rule that asked for it.
* THE MESH, read from snappyHexMesh's own log (`levels_from_log`): the cells at each octree level once
  the castellated mesh is cut to the fluid ("Refined mesh", then its "Cells per refinement level"
  table), and the wall-layer cells (the final count less the snapped one).

`record` puts the two together: each level's cells go to the refinements that asked for that level
(a level asked for by several is shared, and says so - the log cannot split it); a level nobody
asked for is the grading octree adds between the asked ones (nCellsBetweenLevels); the layers are
their own line. Measured where the log says it, attributed by the plan - stated in the record.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: the plan beside the case, and the record's key in the delivered mesh's quality record
PLAN_FILE = "cell_plan.json"
QUALITY_KEY = "cell_record"

#: the purposes a refinement is spent for, in the words the record uses
CORE = "core"
GRADING = "grading"
LAYERS = "wall layers"

_TABLE = re.compile(r"Cells per refinement level:\s*\n((?:[ \t]*\d+[ \t]+\d+[ \t]*\n)+)")
_REFINED = re.compile(r"Refined mesh\s*:\s*cells:(\d+)")
_SNAPPED = re.compile(r"Snapped mesh\s*:\s*cells:(\d+)")
_LAYERED = re.compile(r"Layer mesh\s*:\s*cells:(\d+)")


def entry(purpose: str, level: int, rule: str, *, cell_m: float | None = None,
          planned_cells: float | None = None) -> dict:
    """One refinement of the plan: what it is for, its octree level, the rule that asked for it."""
    e: dict = {"purpose": str(purpose), "level": int(level), "rule": str(rule)}
    if cell_m is not None:
        e["cell_m"] = float(f"{float(cell_m):.6g}")
    if planned_cells is not None:
        e["planned_cells"] = int(planned_cells)
    return e


def write_plan(workspace, entries: Iterable[Mapping], *, base_cell_m: float,
               n_layers: int, max_cells: int) -> None:
    """The plan beside the case (PLAN_FILE). Never raises: a record never costs a mesh."""
    try:
        plan = {"version": 1, "base_cell_m": float(f"{float(base_cell_m):.6g}"),
                "n_layers": int(n_layers), "max_cells": int(max_cells),
                "entries": [dict(e) for e in entries]}
        (Path(workspace) / PLAN_FILE).write_text(json.dumps(plan), encoding="utf-8")
    except Exception:  # noqa: BLE001
        logger.warning("cell record: the plan was not written", exc_info=True)


def read_plan(workspace) -> dict | None:
    try:
        d = json.loads((Path(workspace) / PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _table(block: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for line in block.strip().splitlines():
        a, b = line.split()[:2]
        out[int(a)] = int(b)
    return out


def _first(pattern: re.Pattern, text: str, pos: int = 0) -> re.Match | None:
    return next(pattern.finditer(text, pos), None)


def levels_from_log(text: str) -> dict | None:
    """{castellated: {level: cells}, refined, snapped, layered, layer_cells} from a snappyHexMesh log:
    the level table printed after "Refined mesh" (the castellated mesh cut to the fluid) and the cell
    counts of the three phases. None when the castellated mesh is not in the log."""
    text = text or ""
    m = _first(_REFINED, text)
    if not m:
        return None
    t = _first(_TABLE, text, m.end())
    if not t:
        return None
    out: dict = {"castellated": _table(t.group(1)), "refined": int(m.group(1))}
    s = _first(_SNAPPED, text, t.end())
    if s:
        out["snapped"] = int(s.group(1))
    lay = _first(_LAYERED, text, t.end())
    if lay:
        out["layered"] = int(lay.group(1))
        out["layer_cells"] = max(0, out["layered"] - out.get("snapped", out["refined"]))
    return out


def record(plan: Mapping | None, levels: Mapping | None) -> dict | None:
    """Where the cells went: one row per purpose (cells, share of the mesh, levels, rules), the
    measured totals, and how the rows were attributed. None without both halves."""
    if not plan or not levels or not levels.get("castellated"):
        return None
    entries = [e for e in plan.get("entries") or [] if isinstance(e, Mapping)]
    asked: dict[int, list[Mapping]] = {}
    for e in entries:
        asked.setdefault(int(e["level"]), []).append(e)
    lowest = min(asked) if asked else 0
    base = float(plan.get("base_cell_m") or 0.0)
    rows: dict[str, dict] = {}

    def add(purpose: str, cells: int, level: int, rules: Iterable[str], shared: list[str]) -> None:
        r = rows.setdefault(purpose, {"purpose": purpose, "cells": 0, "levels": [], "rules": [],
                                      "shared_with": []})
        r["cells"] += int(cells)
        if level not in r["levels"]:
            r["levels"].append(level)
        for x in rules:
            if x and x not in r["rules"]:
                r["rules"].append(x)
        for x in shared:
            if x != purpose and x not in r["shared_with"]:
                r["shared_with"].append(x)

    for level, n in sorted((int(k), int(v)) for k, v in levels["castellated"].items()):
        if n <= 0:
            continue
        who = asked.get(level)
        if who:
            names = list(dict.fromkeys(str(e["purpose"]) for e in who))
            label = names[0] if len(names) == 1 else " + ".join(names)
            add(label, n, level, (str(e.get("rule") or "") for e in who), [])
        elif level < lowest:
            add(CORE, n, level, ["background"], [])
        else:
            add(GRADING, n, level, ["nCellsBetweenLevels 3 (octree 2:1)"], [])
    total = sum(r["cells"] for r in rows.values())
    layer_cells = int(levels.get("layer_cells") or 0)
    if layer_cells:
        add(LAYERS, layer_cells, -1, ["wall treatment (CFDREQ-13) / plan's layer count"], [])
        rows[LAYERS]["levels"] = []
    total += layer_cells
    out_rows = []
    for r in sorted(rows.values(), key=lambda r: -r["cells"]):
        r["share"] = round(r["cells"] / total, 4) if total else 0.0
        r["levels"] = sorted(r["levels"])
        if base > 0 and r["levels"]:
            r["cell_m"] = [float(f"{base / 2 ** lv:.4g}") for lv in r["levels"]]
        if not r["shared_with"]:
            r.pop("shared_with")
        planned = [int(e["planned_cells"]) for e in entries
                   if e.get("purpose") in r["purpose"].split(" + ") and e.get("planned_cells") is not None]
        if planned:
            r["planned_cells"] = int(sum(planned))
        out_rows.append(r)
    return {"version": 1, "cells": total, "rows": out_rows,
            "castellated": {str(k): int(v) for k, v in sorted(levels["castellated"].items())},
            "measured_from": "snappyHexMesh log: cells per refinement level after castellation, "
                             "wall-layer cells = final less snapped",
            "attributed_by": "the octree level each refinement asked for (a level asked for by several "
                             "refinements is one shared row)"}


def of_workspace(workspace) -> dict:
    """{QUALITY_KEY: record} for the delivered mesh's quality record, or {} when either half is
    missing. Never raises."""
    try:
        ws = Path(workspace)
        log = ws / "snappyHexMesh.log"
        if not log.exists():
            return {}
        rec = record(read_plan(ws), levels_from_log(log.read_text(errors="replace")))
        return {QUALITY_KEY: rec} if rec else {}
    except Exception:  # noqa: BLE001 - a record never costs a mesh
        logger.warning("cell record: not read", exc_info=True)
        return {}


def words(rec: Mapping | None, *, top: int = 4) -> str:
    """The record in one plain line: "3.3 M cells: wall layers 31%, near-wall band + feature edges 28%,
    ..."."""
    if not rec or not rec.get("rows"):
        return ""
    cells = float(rec.get("cells") or 0)
    parts = [f"{r['purpose']} {100 * float(r.get('share') or 0):.0f}%" for r in list(rec["rows"])[:top]]
    return f"{cells / 1e6:.2g} M cells: " + ", ".join(parts)


__all__ = ["CORE", "GRADING", "LAYERS", "PLAN_FILE", "QUALITY_KEY", "entry", "levels_from_log",
           "of_workspace", "read_plan", "record", "words", "write_plan"]
