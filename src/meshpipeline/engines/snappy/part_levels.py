# Responsibility: The flow route's use of the user's words tied to parts (cad/part_words, NOTE-4): on a surface of several named parts, the cell size the user asked for on each part ("2 mm cells on the fins", "refine the inlet duct", "coarser on the housing") becomes that part's own snappyHexMesh surface level against the background cell (PLAN-cfd-4).
# Boundaries: reads the staged STL's named solids and asks cad/part_words once per run; writes nothing but the per-part refinement surfaces it hands the dict writer (engines/snappy/snappy_runner.render_snappy_case). With no words, one part, or no answer: nothing applied, said - never a failed run.
# Collaborates with: engines/snappy/drivers.py (calls it before the dict is written), engines/snappy/snappy_runner.py (writes the levels), cad/part_words.py (the shared reading), application/part_matches.py (the reading the user checked before approval).
"""SIZES PER PART ON THE FLOW ROUTE (PLAN-cfd-4).

A size becomes a level against the background cell: level = round(log2(base / size)), the coarsest level inside the
user's range when one is (cell = base / 2^level). "Finer" is one level over the surface's own, "coarser" one under.
A part that is its own region in the staged surface gets its level there (finer or coarser). A part merged into a
wall with others can only be refined (a distance region on the part's own surface); a coarser ask on it is said and
not applied."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from meshpipeline.cad import part_words as PW
from meshpipeline.contracts.event_stream import StaleExecutionPublish

logger = logging.getLogger(__name__)

RULE = "PLAN-cfd-4"
CONSUMER = "snappy_part_levels"
PURPOSE = "CFD: a snappyHexMesh flow mesh around and through the parts (cells cut from a background grid)"
#: the most levels a part may be refined past the background (snappyHexMesh's own practical ceiling)
MAX_LEVEL = 10
#: a refine-only part's distance band, in its own cells
BAND_CELLS = 3


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name)) or "part"


def surface_of(solids: Mapping[str, list]) -> tuple[SimpleNamespace, dict, dict]:
    """The staged surface's named solids (metres) as the labelled surface part_words reads (millimetres): Part_k is
    the k-th solid in the file's order, named by the solid. Returns (surface, record, {Part_k: solid name})."""
    import numpy as np

    xyz, tris, label, names, by_id, bases = [], [], [], [], {}, []
    base = 0
    for k, (name, tri) in enumerate(((n, t) for n, t in solids.items() if t), start=1):
        pts = np.asarray(tri, dtype=float).reshape(-1, 3) * 1000.0
        xyz.append(pts)
        tris.append(np.arange(base, base + len(pts)).reshape(-1, 3))
        label.append(np.full(len(pts) // 3, k - 1, dtype=np.int64))
        names.append(f"Part_{k}_Face_1")
        by_id[f"Part_{k}"] = str(name)
        bases.append({"id": f"Part_{k}", "name": str(name)})
        base += len(pts)
    if not xyz:
        return SimpleNamespace(xyz=np.zeros((0, 3)), tris=np.zeros((0, 3), dtype=np.int64),
                               label=np.zeros(0, dtype=np.int64), names=[]), {"part_bases": []}, {}
    surface = SimpleNamespace(xyz=np.vstack(xyz), tris=np.vstack(tris), label=np.concatenate(label), names=names)
    return surface, {"part_bases": bases}, by_id


def level_for(min_m: float, max_m: float, base_m: float) -> int:
    """The level whose cell (base / 2^level) lies inside [min, max] - the coarsest such - else the nearest to the
    middle of the range (round(log2(base / size)))."""
    lo = math.ceil(math.log2(base_m / max_m) - 1e-9)
    hi = math.floor(math.log2(base_m / min_m) + 1e-9)
    level = lo if lo <= hi else round(math.log2(base_m / math.sqrt(min_m * max_m)))
    return int(min(MAX_LEVEL, max(0, level)))


def _mm(v: float) -> str:
    return f"{v * 1000:.3g} mm"


def plan(ps: Mapping | None, by_id: Mapping[str, str], *, base_m: float, smin: int, smax: int,
         regions: list[str]) -> dict:
    """{"regions": {region: (lo, hi)}, "refine": {solid name: level}, "words": [...]} from a checked reading. Pure."""
    out: dict = {"regions": {}, "refine": {}, "words": []}
    if not ps or ps.get("status") != "used" or base_m <= 0:
        return out
    staged = {safe(r): r for r in regions}
    done: set[str] = set()
    for e in ps.get("entries") or []:
        ask = str(e.get("ask") or "size")
        if ask == "name":
            continue
        for pid in [*e.get("parts", []), *e.get("rest_parts", [])]:
            name = by_id.get(pid)
            if not name or name in done:
                continue
            done.add(name)
            if ask == "size":
                lv = level_for(float(e["min_mm"]) * 1e-3, float(e["max_mm"]) * 1e-3, base_m)
                lo, hi = lv, lv
                said = f"cells of about {_mm(base_m / 2 ** lv)}"
            elif ask == "refine":
                lo, hi = min(MAX_LEVEL, smin + 1), min(MAX_LEVEL, smax + 1)
                said = f"finer: cells of about {_mm(base_m / 2 ** hi)}"
            else:
                lo, hi = max(0, smin - 1), max(0, smax - 1)
                said = f"coarser: cells of about {_mm(base_m / 2 ** hi)}"
            region = staged.get(safe(name))
            if region is not None:
                out["regions"][region] = (lo, hi)
                out["words"].append(f"{name}: {said}, as your notes asked (\"{e.get('quote')}\").")
            elif hi > smax:
                out["refine"][name] = hi
                out["words"].append(f"{name}: {said} near its surface, as your notes asked (\"{e.get('quote')}\").")
            else:
                out["words"].append(f"{name} was not made coarser: it is meshed as one wall with other parts, so it "
                                    "keeps their cells.")
    return out


async def decide(ws: Path, solids: Mapping[str, list], state: Mapping, *, job_id: str = "", run=None,
                 wait_s: float = PW.TIMEOUT_S) -> dict:
    """The reading of the notes on the staged surface's parts (cad/part_words; the reading the user checked before
    approval first): {"reading", "by_id"}, for the dict writer to turn into levels on the background cell it really
    uses (plan). Nothing for one part or no words. Never raises (but a fenced worker's stale publish)."""
    if len([t for t in solids.values() if t]) < 2 or not PW.wanted(state):
        return {"reading": PW.section("none"), "by_id": {}}
    surface, record, by_id = surface_of(solids)
    try:
        ps = await PW.decide(Path(ws), record, surface, state, consumer="snappy_part_levels", purpose=PURPOSE, job_id=job_id,
                             run=run, wait_s=wait_s)
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - the parts keep the surface's levels
        logger.warning("snappy part levels: the notes were not read (%s)", type(exc).__name__)
        ps = PW.section("failed", note=PW.UNREAD_WORDS)
    return {"reading": ps, "by_id": by_id}


def words(got: Mapping | None, levels: Mapping | None = None) -> list[str]:
    """What became of the notes about parts, in plain words: each part's cells, and what was not applied."""
    if not got:
        return []
    ps = got.get("reading") or {}
    if ps.get("status") in (None, "none"):
        return []
    lv = levels or {}
    return [*(lv.get("words") or []), *[w for w in PW.words(ps) if w.startswith("Not applied")]] or PW.words(ps)[:1]


def decisions(got: Mapping | None, levels: Mapping | None = None) -> list[dict]:
    if not got:
        return []
    out = PW.decisions(got.get("reading"), stage="plan")
    lv = levels or {}
    if lv.get("regions") or lv.get("refine"):
        out.append({"stage": "plan", "decided": "levels per part", "rule": RULE,
                    "value": len(lv.get("regions") or {}) + len(lv.get("refine") or {}),
                    "inputs": {"regions": {k: list(v) for k, v in (lv.get("regions") or {}).items()},
                               "refine": dict(lv.get("refine") or {})},
                    "words": " ".join(lv.get("words") or [])[:600]})
    return out


__all__ = ["CONSUMER", "PURPOSE", "RULE", "decide", "decisions", "level_for", "plan", "safe", "surface_of", "words"]
