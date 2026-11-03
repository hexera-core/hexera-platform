# Responsibility: Run a Netgen plan where the mesh is built (the mesh image), walk its ladder, and deliver an OpenFOAM case measured the way every polyMesh engine is measured; and give the pipeline the engine's runtime names.
# Owns: the native run (netgen_mesh.py under the netgen interpreter, gmshToFoam, the wall retype, checkMesh, the passage measure, the layer census, the volume export), its time and its record.
# Boundaries: no plan is chosen here (engines/netgen/plan.py), nothing is staged here (engines/netgen/staging.py); the OpenFOAM-family finalize, quality reading and solvability are cfMesh's, reused by name.
# Collaborates with: engines/dispatch.py (_run_netgen_local), engines/netgen/adapter.py (forwards here), engines/cfmesh/finalize.py (calls back by name: check_mesh, write_manifest, read_stl_solids, inspect_stl, export_volume_vtk, build_review_msh, review_geometry_stls).
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from meshpipeline.cad.cad_tessellate import tessellate_to_stl as _cad_tessellate_to_stl

# THE ENGINE'S DECLARED NAMESPACE (as in engines/cfmesh/cfmesh_runner.py): cfMesh's finalize resolves
# these by name off the engine it is handed, so each import below is used at run time.
from meshpipeline.cad.stl_io import inspect_stl, read_stl_solids  # noqa: F401
from meshpipeline.engines.cfmesh.finalize import finalize  # noqa: F401  (engine-adapter seam)
from meshpipeline.engines.cfmesh.foam_exec import (  # noqa: F401
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    export_volume_vtk,
)
from meshpipeline.engines.domain_extent_gate import (  # noqa: F401  (engine-adapter seam)
    extent_gate_for_request as check_domain_extents,
)
from meshpipeline.engines.manifest import write_manifest  # noqa: F401
from meshpipeline.engines.netgen.solvability import check_solvability  # noqa: F401  (engine-adapter seam)
from meshpipeline.engines.netgen.staging import FLUID_BOUNDARY, stage_declared  # noqa: F401
from meshpipeline.render.review_artifacts import build_review_msh  # noqa: F401
from meshpipeline.sandbox.safe_exec import run_guarded

logger = logging.getLogger(__name__)

name = "netgen"

#: the volume file netgen_mesh.py writes and gmshToFoam reads
VOLUME_MSH = "mesh_volume.msh"
#: beside the mesh: the native run's record (the ladder, the counts, the times)
REPORT_FILE = "netgen_report.json"
QUALITY_FILE = "mesh_quality.json"
TIMING_FILE = "mesh_timing.json"
LOG_FILE = "log.netgen"
#: the interpreter that has netgen (the mesh image's isolated env, Dockerfile); overridable
NETGEN_PYTHON = os.environ.get("NETGEN_PYTHON", "/opt/netgen-env/bin/python")
SCRIPT = Path(__file__).with_name("netgen_mesh.py")
#: the passage measure's budget: a tetrahedral wall carries more faces than a hex mesher's, and the
#: default budget abandoned the carotid's measure at 31 s
PASSAGE_BUDGET_S = 600.0
#: what an attempt needs for its last rung to still be worth starting
RUNG_MIN_SECONDS = 60
_POLYMESH = ("points", "faces", "owner", "neighbour", "boundary")


def tessellate_to_stl(geom_path, out_stl, *, context=None, prepared=None):
    from meshpipeline.contracts.intake_formats import is_cad
    geom_path, out_stl = Path(geom_path), Path(out_stl)
    if is_cad(geom_path):
        shutil.copy2(geom_path, out_stl.parent / "geometry.step")
    return _cad_tessellate_to_stl(geom_path, out_stl, prepared=prepared)


def review_geometry_stls(workspace, internal_flow: bool = False) -> list:
    """The surface the reviewer navigates by: the staged fluid boundary, every patch a solid."""
    return [Path(workspace) / FLUID_BOUNDARY]


def netgen_python() -> str:
    if Path(NETGEN_PYTHON).exists():
        return NETGEN_PYTHON
    found = shutil.which("netgen-python")
    return found or NETGEN_PYTHON


def available() -> bool:
    """Whether this machine can run Netgen (the mesh image after the Dockerfile change)."""
    return Path(netgen_python()).exists()


def retype_walls(polymesh: Path, walls: list[str]) -> list[str]:
    """gmshToFoam writes every patch as `type patch`; the walls the plan names become `type wall`
    (the boundary_types gate holds them to it). Returns the patches changed."""
    b = polymesh / "boundary"
    text = b.read_text()
    changed = []
    for w in walls:
        pat = re.compile(r"(^\s*" + re.escape(w) + r"\s*\n\s*\{[^}]*?)\btype\s+\w+\s*;", re.M)
        new, n = pat.subn(r"\1type            wall;", text)
        if n:
            text, changed = new, [*changed, w]
    text = re.sub(r"^\s*physicalType\s+\w+\s*;\s*\n", "", text, flags=re.M)
    b.write_text(text)
    return changed


def _drop_empty_default(polymesh: Path) -> None:
    """gmshToFoam adds a `defaultFaces` patch for faces no physical named; with none it is empty,
    and an empty patch nobody declared is noise in the contract - removed when it holds no faces."""
    b = polymesh / "boundary"
    text = b.read_text()
    m = re.search(r"^\s*defaultFaces\s*\n\s*\{([^}]*)\}\s*\n", text, re.M)
    if not m or not re.search(r"nFaces\s+0\s*;", m.group(1)):
        return
    text = text[:m.start()] + text[m.end():]
    count = re.search(r"^(\d+)\s*\n\(", text, re.M)
    if count:
        text = text[:count.start(1)] + str(int(count.group(1)) - 1) + text[count.end(1):]
    b.write_text(text)


def _convert(ws: Path, *, bashrc: str, timeout: float) -> tuple[bool, str]:
    shutil.rmtree(ws / "constant" / "polyMesh", ignore_errors=True)
    (ws / "constant").mkdir(exist_ok=True)
    cmd = f"source {bashrc} >/dev/null 2>&1 && gmshToFoam {VOLUME_MSH}"
    with (ws / "log.gmshToFoam").open("w") as fh:
        proc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(), stdout=fh,
                           stderr=subprocess.STDOUT, timeout=max(30, int(timeout)))
    pm = ws / "constant" / "polyMesh"
    missing = [f for f in _POLYMESH if not (pm / f).is_file() or (pm / f).stat().st_size == 0]
    if proc.returncode != 0 or missing:
        return False, f"gmshToFoam rc {proc.returncode}, missing {missing}"
    return True, ""


def _measure(ws: Path, *, bashrc: str, walls: list[str], layers: int, timing: dict) -> dict:
    """checkMesh, the passage measure and the layer census, beside the mesh (as cfMesh's run does:
    the worker that finalizes has no OpenFOAM)."""
    (ws / QUALITY_FILE).unlink(missing_ok=True)
    t = time.monotonic()
    q = check_mesh(ws, bashrc=bashrc)
    timing["checkMesh_s"] = round(time.monotonic() - t, 1)
    if q.get("fatal"):
        return q
    try:
        from meshpipeline.engines.passage import passage_of_polymesh
        t = time.monotonic()
        q.update(passage_of_polymesh(ws, budget_s=PASSAGE_BUDGET_S))
        timing["passage_measure_s"] = round(time.monotonic() - t, 1)
    except Exception:  # noqa: BLE001 - evidence, not a verdict
        logger.warning("netgen: passage measure failed; omitted", exc_info=True)
    try:
        from meshpipeline.engines.cfmesh.finalize import census_quality
        from meshpipeline.engines.workspace_facts import contract_patches
        t = time.monotonic()
        cq = census_quality(ws, contract_patches(ws))
        if not cq and layers > 0:
            # the request stated no count: measured against the plan's own stack
            cq = _census_against(ws, walls, layers)
        q.update(cq)
        timing["layer_census_s"] = round(time.monotonic() - t, 1)
    except Exception:  # noqa: BLE001
        logger.warning("netgen: layer census failed; omitted", exc_info=True)
    return q


def _census_against(ws: Path, walls: list[str], layers: int) -> dict:
    from meshpipeline.engines.layer_census import census
    per, why = census(ws / "constant" / "polyMesh", layers, walls)
    if not per:
        return {"layers_requested": layers, "layer_coverage_source": "unmeasured",
                "layer_census_note": why}
    faces = sum(v["faces"] for v in per.values()) or 1
    area = sum(v["area_m2"] for v in per.values()) or 1.0
    return {"layers_requested": layers, "layer_coverage_source": "census",
            "layer_coverage_pct": round(sum(v["coverage_pct"] * v["faces"] for v in per.values()) / faces, 2),
            "layers_mean": round(sum(v["mean_layers"] * v["area_m2"] for v in per.values()) / area, 3),
            "layer_area_full_pct": round(100.0 * sum(v["area_with_full_layers"] * v["area_m2"]
                                                     for v in per.values()) / area, 2),
            "layer_area_any_pct": round(100.0 * sum(v["area_with_any_layer"] * v["area_m2"]
                                                    for v in per.values()) / area, 2)}


def run_cartesian_mesh(workspace, *, timeout: int, context=None) -> dict:
    """The uniform run seam: to the mesh executor (the mesh image runs _run_netgen_local, where
    Netgen and OpenFOAM are)."""
    from meshpipeline.contracts.mesh_execution import run_mesh
    return run_mesh(workspace, engine="netgen", timeout=timeout)


def _run_netgen_local(workspace, *, timeout: int = 3600, bashrc: str = _DEFAULT_BASHRC,
                      **_ignored) -> dict:
    from meshpipeline.contracts.native_time import STALL_WINDOW_S
    from meshpipeline.sandbox.safe_exec import (
        MemoryExpired,
        NativeOutcome,
        StalledExpired,
        describe_native_result,
    )
    ws = Path(workspace)
    t0 = time.monotonic()
    deadline = t0 + float(timeout)
    timing: dict = {}
    spec_path = ws / "netgen_spec.json"
    if not spec_path.exists() or not (ws / FLUID_BOUNDARY).exists():
        return {"rc": 1, "timed_out": False,
                "log_tail": "netgen: no plan (netgen_spec.json) or no staged fluid boundary - "
                            "netgen was not started"}
    if not available():
        return {"rc": 127, "timed_out": False,
                "log_tail": f"netgen: {netgen_python()} is not in this mesh image - rebuild the mesh "
                            "image (Dockerfile: the netgen env)"}
    spec = json.loads(spec_path.read_text())
    walls = list(spec.get("walls") or ["wall"])
    rungs = list(spec.get("rungs") or [])
    record: dict = {"plan": {k: spec.get(k) for k in ("cells_across", "estimated_cells", "maxh_m",
                                                      "h_narrow_m", "layers", "first_layer_m",
                                                      "layer_stack_m", "notes")},
                    "rungs": []}
    log_lines: list[str] = []
    rc, timed_out, stalled, oom = 1, False, False, None
    q: dict = {}
    built = None
    for k, rung in enumerate(rungs, start=1):
        left = deadline - time.monotonic()
        if k > 1 and left < RUNG_MIN_SECONDS:
            log_lines.append(f"[netgen] rung {k} ({rung.get('name')}) not started: {int(left)} s left")
            break
        t = time.monotonic()
        entry: dict = {"rung": k, "name": rung.get("name"), "layers": rung.get("layers")}
        try:
            with (ws / LOG_FILE).open("a") as fh:
                proc = run_guarded([netgen_python(), str(SCRIPT), str(ws), str(k)], cwd=str(ws),
                                   env={**os.environ, "OMP_NUM_THREADS": "1"}, stdout=fh,
                                   stderr=subprocess.STDOUT, timeout=max(30, int(left)),
                                   stall_s=STALL_WINDOW_S)
            rc = proc.returncode
        except StalledExpired:
            rc, stalled, timed_out = -1, True, True
        except MemoryExpired as exc:
            rc, oom = -1, float(exc.used_gib)
        except subprocess.TimeoutExpired:
            rc, timed_out = -1, True
        entry["mesh_s"] = round(time.monotonic() - t, 1)
        try:
            entry.update({x: v for x, v in json.loads((ws / f"netgen_rung{k}.json").read_text()).items()
                          if x in ("ok", "why", "cells", "by_type", "patch_faces", "naming", "reoriented")})
        except (OSError, ValueError):
            pass
        if rc == 0 and (ws / VOLUME_MSH).exists():
            ok, why = _convert(ws, bashrc=bashrc, timeout=deadline - time.monotonic())
            if ok:
                entry["retyped"] = retype_walls(ws / "constant" / "polyMesh", walls)
                _drop_empty_default(ws / "constant" / "polyMesh")
                q = _measure(ws, bashrc=bashrc, walls=walls, layers=int(rung.get("layers") or 0),
                             timing=timing)
                entry["fatal"] = list(q.get("fatal") or [])
                entry["mesh_ok"] = bool(q.get("mesh_ok"))
                if not q.get("fatal"):
                    built = entry
                    record["rungs"].append(entry)
                    break
                why = f"checkMesh: {q.get('fatal')}"
            entry["why"] = why
            rc = 1
        record["rungs"].append(entry)
        log_lines.append(f"[netgen] rung {k} ({rung.get('name')}): {entry.get('why') or 'failed'}")
        if timed_out or oom is not None:
            break
        shutil.rmtree(ws / "constant" / "polyMesh", ignore_errors=True)
    timing["netgen_s"] = round(sum(r.get("mesh_s", 0) for r in record["rungs"]), 1)
    if built is not None:
        rc = 0
        record["built"] = {"rung": built["rung"], "name": built["name"], "layers": built.get("layers"),
                           "cells": built.get("cells"), "by_type": built.get("by_type")}
        if built["rung"] > 1:
            q["netgen_ladder_note"] = ("Netgen built the mesh on its rung " + str(built["rung"]) +
                                       f" ({built['name']}): " + "; ".join(
                                           f"rung {r['rung']} {r.get('why') or 'failed'}"
                                           for r in record["rungs"][:-1]))
        q["netgen"] = record["built"]
        try:
            t = time.monotonic()
            export_volume_vtk(ws, bashrc=bashrc)
            timing["foamToVTK_s"] = round(time.monotonic() - t, 1)
        except Exception:  # noqa: BLE001 - an export never loses a mesh
            logger.warning("netgen: foamToVTK failed", exc_info=True)
        (ws / QUALITY_FILE).write_text(json.dumps(q, default=str))
    timing["total_s"] = round(time.monotonic() - t0, 1)
    record["timing"] = timing
    (ws / REPORT_FILE).write_text(json.dumps(record, indent=1, default=str))
    (ws / TIMING_FILE).write_text(json.dumps(timing))
    tail = ""
    if (ws / LOG_FILE).exists():
        tail = "\n".join((ws / LOG_FILE).read_text(errors="replace").splitlines()[-60:])
    tail = "\n".join([*log_lines, tail]).strip()
    out = describe_native_result(returncode=rc, args=[netgen_python(), str(SCRIPT)], stage="netgen",
                                 output=tail, outcome=NativeOutcome.timed_out if timed_out else None)
    if stalled:
        out["stalled"] = True
    if oom is not None:
        out.update(out_of_memory=True, timed_out=False, used_gib=oom)
    if q:
        out["quality"] = q
    out["timing"] = timing
    out["netgen"] = record
    return out


__all__ = ["FLUID_BOUNDARY", "REPORT_FILE", "VOLUME_MSH", "available", "retype_walls",
           "review_geometry_stls", "run_cartesian_mesh", "stage_declared"]
