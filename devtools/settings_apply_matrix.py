"""THE APPLYING MATRIX for the team's meshing standard (Settings > Workspace > Meshing, STD-1..5): for readings fixed
by hand - one per field, the values from devtools/settings_bank.json, so no model is needed - each part is run on the
real route with the standard as a run carries it (the kept reading, engines/meshing_standard.snapshot), beside the
same part with no standard, and one row is kept per field x part: the engine input before and after, the built
mesh's measure where the route builds one here, the asked-against-delivered row the user sees, and a verdict:

    applied        the engine input changed (or a limit was judged on the mesh), the row says applied / met
    nearest_told   applied as near as the route allows, and the row says so (not met / nearest, with the figure)
    told_not       not applied, and told why
    SILENT         nothing changed and nothing was said (a defect)
    TOLD_WRONG     the user is told it is applied (or why it is not) and that is not what happened (a defect)

Routes: solid (engines/fea_driver.drive, gmsh), shell (the same drive with fea_mesh_as=shell: engines/shell_run in its
own process), cfd (engines/cfd_requirements.read_for_attempt, then each CFD engine's plan seams: what each engine would
be given - no CFD mesh is built here).

Run from the worktree (gmsh and OCP needed for solid and shell):
    PYTHONPATH=src:. python devtools/settings_apply_matrix.py --fixtures                     # the repo's own parts
    PYTHONPATH=src:. python devtools/settings_apply_matrix.py --fixtures --routes solid --fields global,count
    PYTHONPATH=src python devtools/settings_apply_matrix.py --parts parts.json --tree /data/corpus --out m.json
parts.json: [{"name": "bracket_12", "file": "brackets/b12.step", "routes": ["solid", "shell"]}, ...] - STEP in mm.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# #
# the readings, fixed by hand (the bank's values)
# #


def _answer(**fields) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": [], "strictness": [],
            "applies_to": {"whole": ["any"], "lines": []}}
    base.update(fields)
    return json.dumps(base)


def _size(what, value, quote, unit="mm"):
    return {"sizes": [{"what": what, "value": value, "unit": unit, "quote": quote}]}


def _lim(measure, direction, target, quote, shape="any"):
    return {"limits": [{"measure": measure, "shape": shape, "direction": direction, "target": target,
                        "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": quote}]}


def _kind(what, quote, value=0):
    return {"element_kind": [{"what": what, "value": value, "quote": quote}]}


def _count(what, value, quote):
    return {"counts": [{"what": what, "value": value, "quote": quote}]}


def _layer(what, value, quote, unit="none"):
    return {"layers": [{"what": what, "value": value, "unit": unit, "quote": quote}]}


def _control(key, quote, value=0, choice=""):
    return {"controls": [{"key": key, "value": value, "choice": choice, "unit": "none", "where": "", "strict": False,
                          "quote": quote}]}


def _other(quote, kind="not_mesh"):
    return {"other": [{"quote": quote, "kind": kind}]}


FEA, FLOW = ("solid", "shell"), ("cfd",)
#: key -> (routes, the standard's words, the reading's fields, what the route can do with it: "set" a setting that
#: changes the engine input, "judge" a limit judged on the mesh, "told" no setting here - told why)
FIELDS: dict[str, tuple[tuple[str, ...], str, dict, dict[str, str]]] = {
    "global": ((*FEA, *FLOW), "Global element size 5 mm", _size("global", 5, "Global element size 5 mm"), {}),
    "global_in": (FEA, "All dimensions in inches.\nGlobal size 0.2",
                  {"stated_unit": "in", **_size("global", 0.2, "Global size 0.2", "none")}, {}),
    "min_edge": ((*FEA, *FLOW), "min element size 3 mm", _size("min_edge", 3, "min element size 3 mm"), {}),
    "max_edge": ((*FEA, *FLOW), "max element size 4 mm", _size("max_edge", 4, "max element size 4 mm"), {}),
    "growth": ((*FEA, *FLOW), "size growth rate max 1.2", _size("growth", 1.2, "size growth rate max 1.2", "none"), {}),
    "near_wall": (FLOW, "wall cell size 1 mm", _size("near_wall", 1, "wall cell size 1 mm"), {}),
    "aspect": ((*FEA, *FLOW), "Aspect ratio < 5", _lim("aspect_ratio", "max", 5, "Aspect ratio < 5"), {"*": "judge"}),
    "aspect_hard": (FEA, "Aspect ratio < 5 - this is a hard limit",
                    {**_lim("aspect_ratio", "max", 5, "Aspect ratio < 5 - this is a hard limit"),
                     "strictness": [{"target": "aspect_ratio", "strictness": "hard",
                                     "quote": "Aspect ratio < 5 - this is a hard limit"}]}, {"*": "judge"}),
    "jacobian": (FEA, "Jacobian > 0.6", _lim("jacobian", "min", 0.6, "Jacobian > 0.6"), {"*": "judge"}),
    "min_angle": (FEA, "Tria min angle > 20", _lim("min_angle", "min", 20, "Tria min angle > 20", "tria"),
                  {"*": "judge"}),
    "max_angle": (FEA, "Tria max angle < 120", _lim("max_angle", "max", 120, "Tria max angle < 120", "tria"),
                  {"*": "judge"}),
    "skew": (FEA, "skew angle max 45 deg", _lim("skew", "max", 45, "skew angle max 45 deg"), {"*": "judge"}),
    "warpage": (("shell",), "Warpage < 15", _lim("warpage", "max", 15, "Warpage < 15"), {"*": "judge"}),
    "taper": (("shell",), "Taper < 0.5", _lim("taper", "max", 0.5, "Taper < 0.5"), {"*": "judge"}),
    "non_ortho": (FLOW, "maxNonOrtho 65", _lim("non_orthogonality", "max", 65, "maxNonOrtho 65"), {"*": "judge"}),
    "skewness": (FLOW, "max skewness 4", _lim("skewness", "max", 4, "max skewness 4"), {"*": "judge"}),
    "determinant": (FLOW, "min determinant 0.001", _lim("determinant", "min", 0.001, "min determinant 0.001"),
                    {"*": "judge"}),
    "quads_only": (("shell",), "all quads, no trias", _kind("quads_only", "all quads, no trias"), {}),
    "triangles_only": (("shell",), "tria mesh only", _kind("triangles_only", "tria mesh only"), {}),
    "tria_share": (("shell",), "trias max 5%", _kind("max_triangle_percent", "trias max 5%", 5), {}),
    "linear": (FEA, "linear elements only, first order", _kind("linear", "linear elements only, first order"), {}),
    "quadratic": (("solid",), "second order tets (tet10)", _kind("quadratic", "second order tets (tet10)"), {}),
    "no_washers": (("shell",), "no washers please", _kind("no_washers", "no washers please"), {}),
    "structured": (("shell",), "washers round all bolt holes", _kind("structured", "washers round all bolt holes"),
                   {}),
    "count": ((*FEA, *FLOW), "max 20k elements", _count("ceiling", 20000, "max 20k elements"), {}),
    "count_fea": (FEA, "about 5,000 elements", _count("target", 5000, "about 5,000 elements"), {}),
    "count_target": (FLOW, "around 3M cells", _count("target", 3000000, "around 3M cells"), {}),
    "place": ((*FEA, *FLOW), "2 mm at the hole",
              {"local": [{"where": "the hole", "what": "size", "value": 2, "unit": "mm", "quote": "2 mm at the hole"}]},
              {}),
    "wall_elements": (("solid",), "at least 3 elements through the thickness",
                      _control("wall_elements", "at least 3 elements through the thickness", 3), {}),
    "file_units": ((*FEA, *FLOW), "export in mm", _control("file_units", "export in mm", choice="mm"), {"*": "told"}),
    "layers": ((*FEA, *FLOW), "5 prism layers", _layer("count", 5, "5 prism layers"), {"solid": "told",
                                                                                     "shell": "told"}),
    "first_layer": (FLOW, "first layer height 0.02mm", _layer("first_height", 0.02, "first layer height 0.02mm", "mm"),
                    {}),
    "layer_growth": (FLOW, "inflation growth rate 1.15", _layer("growth", 1.15, "inflation growth rate 1.15"), {}),
    "y_plus": (FLOW, "y+ around 1", _layer("y_plus", 1, "y+ around 1"), {}),
    "cells_across": (FLOW, "at least 20 cells across every pipe",
                     _control("cells_across", "at least 20 cells across every pipe", 20), {}),
    "surface_level": (FLOW, "surface refinement level 3", _control("surface_level", "surface refinement level 3", 3),
                      {}),
    "solver_file": (FEA, "Nastran BDF output", _other("Nastran BDF output"), {"*": "told"}),
    "design_intent": ((*FEA, *FLOW), "we care about fatigue at the fillets",
                      _other("we care about fatigue at the fillets", "mesh"), {"*": "told"}),
    "design_intent_nm": ((*FEA, *FLOW), "this goes into a crash model", _other("this goes into a crash model"),
                         {"*": "told"}),
}


def standard(key: str) -> dict:
    """The standard as a run carries it, with the reading kept at save time (no model asked)."""
    from meshpipeline.engines import meshing_standard as MS
    _routes, text, fields, _can = FIELDS[key]
    return MS.snapshot(text, {"key": MS.key_of(text), "arguments": _answer(**fields)}) or {}


def can(key: str, route: str) -> str:
    got = FIELDS[key][3]
    return got.get(route) or got.get("*") or "set"


# #
# the parts
# #

def fixture_parts() -> list[dict]:
    """The repo's own public test parts (tests/fea_fixtures, tests/shell_fixtures, tests/cad_fixtures): built here."""
    from tests import fea_fixtures as fx
    from tests import shell_fixtures as sf
    return [
        {"name": "plate_with_hole (block with holes)", "shape": fx.plate_with_hole, "routes": ["solid", "cfd"]},
        {"name": "bracket (fillets)", "shape": fx.bracket, "routes": ["solid", "cfd"]},
        {"name": "hollow_box (thin walls)", "shape": fx.hollow_box, "routes": ["solid", "cfd"]},
        {"name": "stepped_shaft", "shape": fx.stepped_shaft, "routes": ["solid", "cfd"]},
        {"name": "v_notched_bar", "shape": fx.v_notched_bar, "routes": ["solid"]},
        {"name": "pipe (tube 25x4)", "shape": lambda: sf.tube(25.0, 4.0, 120.0), "routes": ["solid", "cfd"]},
        {"name": "flat_plate", "shape": sf.flat_plate, "routes": ["shell"]},
        {"name": "plate_with_hole (shell)", "shape": sf.plate_with_hole, "routes": ["shell"]},
        {"name": "l_angle", "shape": sf.l_angle, "routes": ["shell"]},
        {"name": "tube (shell)", "shape": sf.tube, "routes": ["shell"]},
        {"name": "bent_bracket", "shape": sf.bent_bracket, "routes": ["shell"]},
    ]


def stage(part: dict, ws: Path) -> Path:
    """The part staged as the builder stages it for gmsh: read in mm, written in metres (geometry.step)."""
    import gmsh
    ws.mkdir(parents=True, exist_ok=True)
    if part.get("shape") is not None:
        from tests import fea_fixtures as fx
        src = fx.write_step(part["shape"](), ws / "upload.step")
    else:
        src = Path(part["path"])
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
    finally:
        gmsh.finalize()
    (ws / "bbox.json").write_text(json.dumps([xmin, ymin, zmin, xmax, ymax, zmax]))
    return ws


# #
# the FEA routes: the real driver, every other AI step finds the model down
# #

class _Down:
    async def call_builder_model(self, *a, **k):
        raise TimeoutError()


class _Publish:
    def __init__(self):
        self.said: list[str] = []

    async def anarrate(self, kind, line, n=None, of=None, unit="", op_id=""):
        self.said.append(str(line))


class _Run:
    deadline_s = None

    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _install_fea() -> None:
    from meshpipeline.contracts import decision_cache
    from meshpipeline.contracts import model_inference as llm
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import fea_adapter
    # the test doubles a unit test sets with monkeypatch, set once for this process (it only runs this harness)
    setattr(decision_cache, "recorded_answer", lambda *a, **k: None)  # noqa: B010
    setattr(llm, "_router", _Down())  # noqa: B010
    setattr(runtime, "get_engine", lambda _n: SimpleNamespace(  # noqa: B010
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    setattr(FD, "_cite", lambda job_id, entries: None)  # noqa: B010


def _json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def fea_run(ws: Path, route: str, std: dict | None) -> dict:
    """One FEA drive; what the engine was given, what was built, what the user is told."""
    from meshpipeline.contracts.user_notes import read_remarks
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import meshing_standard as MS
    from meshpipeline.engines.fea_results import results
    state: dict = {"purpose": "structural", "user_id": "matrix"}
    if route == "shell":
        state["fea_mesh_as"] = "shell"
    if std:
        state[MS.STATE_KEY] = std
    pub = _Publish()
    t0 = time.monotonic()
    driven: Any = FD.drive
    ok, _marker, _o = asyncio.run(driven(ws, state, job_id=f"matrix-{ws.name}", publish=pub, run=_Run()))
    out: dict = {"ok": bool(ok), "seconds": round(time.monotonic() - t0, 1)}
    if route == "solid":
        plan, q = _json(ws / "fea_plan.json"), _json(ws / "quality.json")
        out["input"] = {"base_size_m": plan.get("base_size_m"), "elements_planned": (plan.get("estimate") or {})
                        .get("elements"), "growth": plan.get("growth"), "element": plan.get("element"),
                        "levers": plan.get("levers"), "part_sizes": plan.get("part_sizes")}
        out["built"] = {"cells": q.get("cells"), "aspect": q.get("aspect_max") or q.get("aspect"),
                        "order": (plan.get("element") or {}).get("order") if isinstance(plan.get("element"), dict)
                        else plan.get("element")}
        rows = (q.get("requirements") or {}).get("rows") or []
    else:
        from meshpipeline.engines import shell_run
        rec: dict = shell_run.read(ws) or {}
        sq = _json(ws / "shell_quality.json")
        out["input"] = {"sizing": rec.get("sizing"), "settings": sq.get("settings"),
                        "structure": rec.get("structure"), "elements_kind": rec.get("elements_kind")}
        out["built"] = {"elements": rec.get("elements"), "quads": rec.get("quads"), "trias": rec.get("trias"),
                        "made": rec.get("made")}
        rows = (rec.get("requirements") or {}).get("rows") or []
    try:
        res: dict = results(ws) or {}
        asked = [r for r in res["check"]["rows"] if str(r.get("key", "")).startswith("asked_")]
    except Exception:  # noqa: BLE001 - a result that cannot be read is said as no rows
        asked = []
    out["rows"] = [{k: r.get(k) for k in ("field", "asked", "applied", "met", "measured", "delivered", "from",
                                          "status")} for r in rows]
    out["asked_rows"] = [{"label": r.get("label"), "status": r.get("status"), "detail": r.get("detail")
                          or r.get("value")} for r in asked]
    out["said"] = [s for s in pub.said if s.startswith(("From your settings", "Not applied", "Not a mesh", "Your saved"))]
    # what the result keeps (the plan's words): a line the live guard would not say (contracts/narration.leaks: a
    # quote with "model" in it) is still on the result
    out["result_words"] = [str(w) for w in _json(ws / "fea_plan.json").get("words") or []
                           if str(w).startswith(("From your settings", "Not applied", "Not a mesh", "Your saved"))]
    out["remarks"] = [str(r.get("text")) for r in read_remarks(ws, raw=True)]
    return out


# #
# the flow route: the reading step, then each engine's plan seams (no CFD mesh here)
# #

SNAPPY_TEXT = """castellatedMeshControls { nCellsBetweenLevels 3; maxGlobalCells 2000000; }
addLayersControls { relativeSizes true; expansionRatio 1.2; finalLayerThickness 0.3; minThickness 0.1;
  layers { wall { nSurfaceLayers 3; } } }
meshQualityControls { maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; minDeterminant 0.001;
  minVol 1e-13; }
"""
ENGINES = ("snappy", "cfmesh", "gmsh_flow", "netgen", "vmtk", "snappy_multiregion")


def cfd_run(ws: Path, std: dict | None) -> dict:
    from meshpipeline.engines import cfd_requirements as CR
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines import meshing_standard as MS
    state: dict = {"purpose": "external aerodynamics", "user_id": "matrix"}
    if std:
        state[MS.STATE_KEY] = std
    pub = _Publish()
    sec = asyncio.run(CR.read_for_attempt(ws, state, job_id="matrix", publish=pub))
    box = json.loads((ws / "bbox.json").read_text())
    diag = math.dist(box[:3], box[3:])
    h = diag / 40.0
    volume = max(1e-12, (box[3] - box[0]) * (box[4] - box[1]) * (box[5] - box[2])) * 27.0
    out: dict = {"said": [s for s in pub.said if s.startswith(("From your settings", "Not applied", "Not a mesh",
                                                               "Your saved"))],
                 "status": sec.get("status"), "engines": {}}
    for engine in ENGINES:
        ask = CR.Ask.of(ws, engine)
        if ask is None:
            out["engines"][engine] = {"input": "the engine's own plan (nothing asked)"}
            continue
        target: dict = {}
        ask.controls(target)
        got: dict = {"controls": target}
        try:
            if engine == "gmsh_flow":
                got["sizes"] = CR.gmsh_sizes(ask, h, external=True)
            elif engine == "cfmesh":
                got["max_cell"] = CR.cfmesh_max_cell(ask, h * 4, volume)
                got["wall"] = CR.cfmesh_wall(ask, h, h * 4, h / 16)[:2]
            elif engine == "snappy":
                got["dict"] = CR.snappy_dict(ask, SNAPPY_TEXT, wall_cell_m=h / 8) != SNAPPY_TEXT
                got["budget"] = ask.budget(2_000_000)
        except Exception as exc:  # noqa: BLE001 - a seam that needs a full case is said, not hidden
            got["seam_error"] = type(exc).__name__
        got["rows"] = ask.rows
        got["fields"] = {"global_m": ask.global_m, "min_m": ask.min_m, "max_m": ask.max_m, "wall_m": ask.wall_m,
                         "growth": ask.growth, "ceiling": ask.ceiling, "target": ask.target, "layers": ask.n_layers,
                         "first_m": ask.first_m, "y_plus": ask.y_plus, "limits": [x["measure"] for x in ask.limits],
                         "local": len(ask.local)}
        out["engines"][engine] = got
    out["told"] = MR.told_words(MR.read(ws))
    return out


# #
# the verdict
# #

def _engine_words(name: str, e: dict) -> str:
    rows = list((e.get("rows") or {}).values())
    statuses = ",".join(sorted({str(r.get("status")) for r in rows}))
    return f"{name}: {statuses} " + "/".join(str(r.get("built"))[:60] for r in rows)


def verdict(key: str, route: str, base: dict, got: dict) -> tuple[str, str]:
    """(the verdict, why) for one field x part, from the run without and with the standard."""
    mode = can(key, route)
    said = " | ".join([*(got.get("said") or []), *(got.get("result_words") or [])])
    if route == "cfd":
        told = " | ".join(got.get("told") or [])
        changed = any(e.get("rows") or e.get("controls") for e in got["engines"].values() if isinstance(e, dict))
        statuses = {r.get("status") for e in got["engines"].values() if isinstance(e, dict)
                    for r in (e.get("rows") or {}).values()}
        per = "; ".join(_engine_words(name, e) for name, e in got["engines"].items()
                        if isinstance(e, dict) and e.get("rows"))
        if statuses and mode != "told":
            return ("applied" if "applied" in statuses else "nearest_told" if "nearest" in statuses
                    else "told_not"), per[:300]
        if mode == "told":
            if "not_applied" in statuses:
                return "told_not", per[:300]
            return ("told_not" if ("Not applied" in said or "Not a mesh" in said or told) else "SILENT",
                    said or told or "nothing said")
        if not said and not told:
            return "SILENT", "no line before meshing"
        if changed and statuses <= {"applied", "nearest", "not_applied"} and statuses:
            return ("applied" if "applied" in statuses else "nearest_told" if "nearest" in statuses
                    else "told_not"), f"seam rows: {sorted(s for s in statuses if s)}"
        if mode == "judge":
            return "applied", "a limit the engines hold or judge (snappy dict / report)"
        # the layer count, the budget and the sizes in a place are set in each engine's driver, past the seams run
        # here: not verified on this laptop (a CFD mesh on a cloud machine is)
        return "unverified", f"told '{said[:90]}'; set in the engine's driver, not at a seam run here"
    # the engine input only: the built count moves a little run to run (the 3D fill is threaded)
    changed = base.get("input") != got.get("input") or (route == "shell" and base.get("built") != got.get("built"))
    rows = got.get("rows") or []
    settings_rows = [r for r in rows if r.get("from") == "settings"]
    if mode == "told":
        if any(r.get("applied") is False for r in settings_rows) or "Not applied" in said or "Not a mesh" in said \
                or "delivers them" in said:
            return "told_not", said[:160] or "row: not applied"
        return "SILENT", "nothing said, nothing changed"
    if not settings_rows and not said:
        return ("SILENT" if not changed else "TOLD_WRONG"), "no row and no line"
    applied = [r for r in settings_rows if r.get("applied")]
    refused = [r for r in settings_rows if r.get("applied") is False]
    if refused:
        why = "; ".join(str(r.get("delivered")) for r in refused)
        return ("told_not" if mode != "set" else "TOLD_WRONG" if "notes about parts were not read" in why
                else "told_not"), why[:200]
    if mode == "judge":
        met = [r.get("met") for r in applied]
        return ("applied" if all(m is not False for m in met) else "nearest_told"), \
            "; ".join(f"{r.get('asked')}: {r.get('delivered')}" for r in applied)[:200]
    if not applied and ("Not applied" in said or "Not a mesh" in said):
        return "told_not", said[:200]
    if not changed and applied and all(r.get("met") is True for r in applied):
        return "applied", ("the mesh already holds it, nothing needed to change: "
                           + "; ".join(f"{r.get('asked')}: {r.get('delivered')}" for r in applied))[:200]
    if not changed and applied and all(r.get("delivered") for r in applied):
        # nothing changed and the row says what was built (and why): honest, but only reading the code says whether
        # the route tried (the shell's element kind is not in the record this harness compares)
        return "unchanged_told", ("nothing changed; the row says: "
                                  + "; ".join(f"{r.get('asked')}: {r.get('delivered')}" for r in applied))[:240]
    if not changed:
        return "TOLD_WRONG", ("told as applied, the engine input and the mesh did not change: "
                              + "; ".join(f"{r.get('asked')}: {r.get('delivered')}" for r in applied))[:240]
    met = [r.get("met") for r in applied]
    return ("applied" if all(m is not False for m in met) else "nearest_told"), \
        "; ".join(f"{r.get('asked')}: {r.get('delivered')}" for r in applied)[:200]


def matrix(parts: list[dict], *, routes: tuple[str, ...], fields: tuple[str, ...], root: Path, out=print) -> list[dict]:
    rows: list[dict] = []
    _install_fea()
    for part in parts:
        for route in [r for r in part.get("routes") or [] if r in routes]:
            keys = [k for k in fields if route in FIELDS[k][0]]
            if not keys:
                continue
            ws0 = stage(part, root / f"{_slug(part['name'])}-{route}-base")
            try:
                base = cfd_run(ws0, None) if route == "cfd" else fea_run(ws0, route, None)
            except Exception as exc:  # noqa: BLE001 - one part that cannot run is said, the rest go on
                out(f"!! {part['name']} {route}: the run with no standard failed ({type(exc).__name__}: {exc})")
                continue
            for key in keys:
                ws = root / f"{_slug(part['name'])}-{route}-{key}"
                ws.mkdir(parents=True, exist_ok=True)
                shutil.copy(ws0 / "geometry.step", ws / "geometry.step")
                shutil.copy(ws0 / "bbox.json", ws / "bbox.json")
                try:
                    got = cfd_run(ws, standard(key)) if route == "cfd" else fea_run(ws, route, standard(key))
                except Exception as exc:  # noqa: BLE001
                    got = {"error": f"{type(exc).__name__}: {exc}"}
                v, why = ("ERROR", got["error"]) if "error" in got else verdict(key, route, base, got)
                row = {"part": part["name"], "route": route, "field": key, "verdict": v, "why": why,
                       "before": base.get("input") if route != "cfd" else None,
                       "after": got.get("input") if route != "cfd" else got.get("engines"),
                       "built_before": base.get("built"), "built_after": got.get("built"),
                       "rows": got.get("rows"), "asked_rows": got.get("asked_rows"), "said": got.get("said"),
                       "result_words": got.get("result_words"), "told": got.get("told")}
                rows.append(row)
                out(f"{v:12} {route:5} {key:16} {part['name'][:34]:34} {why[:150]}")
                if v not in ("ERROR",):
                    shutil.rmtree(ws, ignore_errors=True)
            shutil.rmtree(ws0, ignore_errors=True)
    return rows


def table(rows: list[dict], out=print) -> dict:
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        cell = agg[(r["field"], r["route"])]
        cell["parts"] += 1
        cell[r["verdict"]] += 1
    cols = ("applied", "nearest_told", "unchanged_told", "told_not", "unverified", "SILENT", "TOLD_WRONG", "ERROR")
    out("| field | route | parts | " + " | ".join(cols) + " |")
    out("|---|---|---|" + "---|" * len(cols))
    for (field, route), c in sorted(agg.items()):
        out(f"| {field} | {route} | {c['parts']} | " + " | ".join(str(c.get(k, 0)) for k in cols) + " |")
    return {f"{k[0]}|{k[1]}": dict(v) for k, v in agg.items()}


def rejudge(rows: list[dict]) -> list[dict]:
    """The verdicts again from a kept matrix (after a change to `verdict`), no part run again."""
    out = []
    for r in rows:
        if r["verdict"] == "ERROR":
            out.append(r)
            continue
        if r["route"] == "cfd":
            # one row per CFD engine: what that engine's seams did with the field (a row not set at a seam run here
            # is "unverified": the engine's driver sets it, or nothing does - a CFD mesh on a cloud machine says)
            for engine, e in (r.get("after") or {}).items():
                if not isinstance(e, dict):
                    continue
                st = {str(x.get("status")) for x in (e.get("rows") or {}).values()}
                v = ("applied" if "applied" in st else "nearest_told" if "nearest" in st
                     else "told_not" if "not_applied" in st else "unverified")
                if v == "unverified" and can(r["field"], "cfd") == "told" and not r.get("told"):
                    v = "SILENT"
                out.append({**r, "route": f"cfd:{engine}", "verdict": v,
                            "why": _engine_words(engine, e) if e.get("rows") else "no seam row here"})
            continue
        else:
            got = {"input": r.get("after"), "rows": r.get("rows"), "said": r.get("said"),
                   "result_words": r.get("result_words"), "built": r.get("built_after")}
            base = {"input": r.get("before"), "built": r.get("built_before")}
        v, why = verdict(r["field"], r["route"], base, got)
        out.append({**r, "verdict": v, "why": why})
    return out


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name)[:40]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixtures", action="store_true", help="the repo's own test parts (run with . on PYTHONPATH)")
    ap.add_argument("--parts", type=Path, help="a JSON list of {name, file, routes}")
    ap.add_argument("--tree", type=Path, help="where the parts list's files are")
    ap.add_argument("--routes", default="solid,shell,cfd")
    ap.add_argument("--fields", default=",".join(FIELDS))
    ap.add_argument("--max-parts", type=int, default=0, help="at most this many parts per route (0: all)")
    ap.add_argument("--work", type=Path, default=None, help="where the workspaces go (default: a temp folder)")
    ap.add_argument("--out", type=Path, default=None, help="write every row here (JSON)")
    ap.add_argument("--rejudge", type=Path, default=None, help="a kept matrix (--out): its verdicts again, no runs")
    args = ap.parse_args(argv)
    if args.rejudge:
        rows = rejudge(json.loads(args.rejudge.read_text())["rows"])
        summary = table(rows)
        if args.out:
            args.out.write_text(json.dumps({"rows": rows, "table": summary}, indent=1, default=str))
        return 0
    if args.fixtures:
        parts = fixture_parts()
    elif args.parts and args.tree:
        parts = [{"name": p["name"], "path": str(args.tree / p["file"]), "routes": p.get("routes") or ["solid"]}
                 for p in json.loads(args.parts.read_text())]
    else:
        ap.error("give --fixtures, or --parts with --tree")
    routes = tuple(r for r in args.routes.split(",") if r)
    if args.max_parts:
        kept: list[dict] = []
        seen: dict[str, int] = defaultdict(int)
        for p in parts:
            rs = [r for r in p["routes"] if r in routes and seen[r] < args.max_parts]
            for r in rs:
                seen[r] += 1
            if rs:
                kept.append({**p, "routes": rs})
        parts = kept
    fields = tuple(f for f in args.fields.split(",") if f in FIELDS)
    root = args.work or Path(tempfile.mkdtemp(prefix="settings-matrix-"))
    rows = matrix(parts, routes=routes, fields=fields, root=root)
    summary = table(rows)
    if args.out:
        args.out.write_text(json.dumps({"rows": rows, "table": summary}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
