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


#: NOTE-17: the file stem of the n-th picked face's own refinement surface (constant/triSurface/picked_<n>.stl)
PICKED = "picked_"
#: a picked face's triangles must lie within this share of the staged surface's diagonal of its box (same frame)
FRAME_SHARE = 0.02


def picked_surfaces(ws: Path, local, face_map: tuple, *, box) -> dict[str, dict]:
    """NOTE-17: for each size typed with faces picked on the model (a `local` entry with `faces` and `sigs`), its
    faces re-checked against the face map the check kept (cad/face_map.same_face, through the contract seam) and
    their triangles written as one surface of their own, constant/triSurface/picked_<n>.stl (metres), for a distance
    refinement on them alone. Returns {entry id: {"file", "where"}} or {entry id: {"gone": the face(s) as the user
    saw them}} - a face no longer on the model, no face map, or a map in another frame than the staged surface `box`
    ([min, max] metres): told, never put on another face. Never raises."""
    import numpy as np

    out: dict[str, dict] = {}
    fm, scale = face_map if isinstance(face_map, tuple) and len(face_map) == 2 else (None, 1.0)
    n = 0
    for e in local or ():
        if not isinstance(e, Mapping) or not e.get("faces"):
            continue
        ids = [str(f) for f in e["faces"]]
        labels = list(e.get("labels") or [])
        sigs = list(e.get("sigs") or [])
        try:
            from meshpipeline.cad import face_map as FM
            if fm is None:
                raise LookupError("no face map")
            kept = [fm.names.index(f) for i, f in enumerate(ids) if i < len(sigs) and isinstance(sigs[i], Mapping)
                    and FM.same_face(fm, f, dict(sigs[i]), scale=float(scale))]
            tris = [FM.highlight(fm, k, scale=float(scale)) for k in kept]
            pts = np.vstack([p for p, _t in tris]) if tris else np.zeros((0, 3))
            lo, hi = np.asarray(box[0], dtype=float), np.asarray(box[1], dtype=float)
            tol = FRAME_SHARE * float(np.linalg.norm(hi - lo))
            framed = bool(len(pts)) and bool(np.all(pts >= lo - tol) and np.all(pts <= hi + tol))
        except Exception as exc:  # noqa: BLE001 - told as not re-checked, never a failed run
            logger.warning("snappy picked faces: not re-checked (%s)", type(exc).__name__)
            kept, framed, tris = [], False, []
        if not kept or not framed:
            shown = [labels[i] if i < len(labels) and labels[i] else f for i, f in enumerate(ids)]
            out[str(e.get("id"))] = {"gone": f"the face you picked ({'; '.join(shown[:3])})" if len(ids) == 1 else
                                     f"the {len(ids)} faces you picked ({'; '.join(shown[:3])})"}
            continue
        n += 1
        name = f"{PICKED}{n}"
        try:
            from meshpipeline.cad.stl_io import _write_solid
            tri_dir = Path(ws) / "constant" / "triSurface"
            tri_dir.mkdir(parents=True, exist_ok=True)
            corners = [tuple(map(tuple, p[t])) for p, ts in tris for t in ts.astype(np.int64)]
            with (tri_dir / f"{name}.stl").open("w") as fh:
                _write_solid(fh, name, corners)
        except Exception as exc:  # noqa: BLE001 - told as not applied
            logger.warning("snappy picked faces: surface not written (%s)", type(exc).__name__)
            out[str(e.get("id"))] = {"gone": f"the face you picked ({labels[0] if labels else ids[0]})"}
            continue
        names = {str(fm.names[k]) for k in kept} if fm is not None else set()
        missing = [labels[i] if i < len(labels) and labels[i] else f for i, f in enumerate(ids) if f not in names]
        out[str(e.get("id"))] = {"file": name, "where": str(e.get("where") or ""),
                                 **({"gone": "; ".join(missing[:3])} if missing else {})}
    return out


def picked_refinement(levels: Mapping[str, int], *, base_m: float, have) -> tuple[str, str]:
    """NOTE-17: the snappyHexMesh geometry and refinementRegions entries for the picked faces' own surfaces
    ({file stem: level}): a distance band BAND_CELLS of its own cells deep. `have(stem)`: whether its file was
    written (an entry with no file is left out)."""
    geom = refine = ""
    for stem, lv in sorted(levels.items()):
        if not have(stem):
            continue
        band = BAND_CELLS * float(base_m) / 2 ** int(lv)
        geom += f" {stem}.stl {{ type triSurfaceMesh; name {stem}; }}"
        refine += f" {stem} {{ mode distance; levels (({band:.6g} {int(lv)})); }}"
    return geom, refine


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


__all__ = ["CONSUMER", "FRAME_SHARE", "PICKED", "PURPOSE", "RULE", "decide", "decisions", "level_for", "picked_refinement",
           "picked_surfaces", "plan", "safe", "surface_of", "words"]
