# Responsibility: Run the snap-grid mesher on a staged workspace, and turn what it wrote into the pipeline's manifest.
# Owns: the workspace layout (source.ecxml, snapgrid_plan.json, log.snapgrid), the fidelity-to-plan table, the native run (the CLI in a guarded, time-bounded child process) and finalize's quality dict.
# Boundaries: the mesher itself is engines/snapgrid/mesher.py + native.py; this judges nothing the gates judge.
# Collaborates with: engines/snapgrid/driver.py (stages and dispatches), engines/dispatch.py (runs it where OpenFOAM is), pipeline/executor.py (finalize).
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: The ECXML the mesher reads, staged into the workspace under one name whatever it was uploaded as.
SOURCE_NAME = "source.ecxml"
#: The grid plan the driver chose (JSON of engines/snapgrid/grid.GridPlan fields).
PLAN_NAME = "snapgrid_plan.json"
#: The mesher's own output (the CLI's stdout and stderr).
LOG_NAME = "log.snapgrid"
#: What the mesher wrote (engines/snapgrid/mesher.REPORT_NAME / SIDECAR_NAME).
REPORT_NAME = "snapgrid_report.json"
SIDECAR_NAME = "thermal_model.json"

#: Mesh fidelity -> the grid plan. Every layer always keeps at least one cell through it; the
#: plan sets the electronics-cooling resolution rules (engines/snapgrid/thermal_rules.py), how
#: round a cylinder is, and the cell budget. The numbers follow the tools' published practice:
#: Flotherm puts 3 cells across a fin gap by default and 5 for pressure drop, 1-2 through a fin
#: and 2-3 through a heat-sink base, and at least 3 cells across a small heat source; Icepak asks
#: 2-3 elements in every gap, 4-5 on every fan, vent and grille, and neighbouring cells within 2x
#: (general CFD practice: within 1.3x, aspect under 20-50 away from walls). Standard's budget
#: keeps every rule on the real packages of the test set (they need 3-4M cells; at 2M most rules
#: had to relax); the job's memory still lowers it before anything is built.
FIDELITY_ORDER: tuple[str, ...] = ("draft", "standard", "max")
FIDELITY_PLANS: dict[str, dict] = {
    "draft": {"max_cells": 500_000, "min_cells_across": 1, "cylinder_cells": 8,
              "hot_cells_across": 2, "gap_cells": 2, "band_cells": 2, "spread_cells": 4,
              "opening_cells": 4, "growth": 1.4, "air_aspect": 20.0},
    "standard": {"max_cells": 4_000_000, "min_cells_across": 2, "cylinder_cells": 12,
                 "hot_cells_across": 3, "gap_cells": 4, "band_cells": 4, "spread_cells": 6,
                 "opening_cells": 6, "growth": 1.3, "air_aspect": 10.0},
    "max": {"max_cells": 8_000_000, "min_cells_across": 3, "cylinder_cells": 16,
            "hot_cells_across": 4, "gap_cells": 6, "band_cells": 6, "spread_cells": 10,
            "opening_cells": 10, "growth": 1.2, "air_aspect": 5.0},
}
#: The plan keys that are grid.GridPlan fields (the rest - "fidelity" - are the run's own).
PLAN_FIELDS: tuple[str, ...] = ("max_cells", "background_cells", "min_cells_across",
                                "cylinder_cells", "growth", "hot_cells_across", "gap_cells",
                                "band_cells", "spread_cells", "opening_cells", "air_aspect")


def grid_plan(plan: dict):
    """The grid.GridPlan a plan dict asks for (unknown keys ignored, missing ones defaulted)."""
    from meshpipeline.engines.snapgrid.grid import GridPlan

    return GridPlan(**{k: plan[k] for k in PLAN_FIELDS if plan.get(k) is not None})


def plan_for(fidelity: str = "", step: int = 0) -> dict | None:
    """The grid plan for a mesh fidelity, `step` levels finer (a review-caused retry asks for the
    next level). None when no finer level is left. The budget never exceeds the hard cell limit."""
    import meshpipeline.settings.policy as polcfg

    level = str(fidelity or "standard").strip().lower()
    base = FIDELITY_ORDER.index(level) if level in FIDELITY_ORDER else 1
    idx = base + int(step)
    if idx >= len(FIDELITY_ORDER):
        return None
    plan = dict(FIDELITY_PLANS[FIDELITY_ORDER[idx]])
    plan["max_cells"] = int(min(plan["max_cells"], polcfg.CELL_HARD_LIMIT))
    plan["fidelity"] = FIDELITY_ORDER[idx]
    return plan


def write_plan(workspace, plan: dict) -> None:
    Path(workspace, PLAN_NAME).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")


def read_plan(workspace) -> dict:
    p = Path(workspace, PLAN_NAME)
    if not p.is_file():
        return plan_for("standard") or {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{PLAN_NAME} is not a JSON object")
    return data


def cli_args(plan: dict) -> list[str]:
    """The CLI options a plan sets (engines/snapgrid/cli.py); unknown keys are ignored."""
    out: list[str] = []
    for key in PLAN_FIELDS:
        flag = "--" + key.replace("_", "-")
        if plan.get(key) is not None:
            out += [flag, str(plan[key])]
    return out


def find_source(source_path: str) -> Path | None:
    """The uploaded ECXML for a run: the canonical path itself when it is the ECXML, else the
    verified upload beside it (application/geometry_materializer writes `source.<suffix>` and its
    canonical form next to each other)."""
    from meshpipeline.cad.ingest import IngestError
    from meshpipeline.cad.ingest.canonical import resolve_format

    if not source_path:
        return None
    p = Path(source_path)
    candidates = [p] + [p.with_suffix(s) for s in (".ecxml", ".xml")]
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            if resolve_format(cand) == "ecxml":
                return cand
        except (IngestError, OSError, ValueError):
            continue
    return None


# native: the CLI in a guarded child process (an out-of-memory kill or a timeout is the child's)

def _pythonpath() -> str:
    """The import path the child needs: where this meshpipeline is, then this process's own
    path - so the child runs exactly the code that dispatched it (the lab overlay, a worktree)."""
    import os

    import meshpipeline

    here = str(Path(meshpipeline.__file__).resolve().parents[1])
    return os.pathsep.join(dict.fromkeys([here] + [p for p in sys.path if p]))


def run_native_build(workspace, *, bashrc: str, timeout: int) -> dict:
    from meshpipeline.sandbox.safe_exec import (
        NativeOutcome,
        describe_native_result,
        run_guarded,
        scrubbed_subprocess_env,
    )

    ws = Path(workspace)
    if not (ws / SOURCE_NAME).is_file():
        return {"rc": -2, "timed_out": False, "code": "snapgrid_source_missing",
                "log_tail": f"REJECTED before meshing: the workspace holds no {SOURCE_NAME} - the "
                            "snap-grid mesher reads the ECXML model itself."}
    try:
        plan = read_plan(ws)
    except (OSError, ValueError) as exc:
        return {"rc": -2, "timed_out": False, "code": "snapgrid_plan_unreadable",
                "log_tail": f"REJECTED before meshing: {PLAN_NAME} cannot be read ({exc})."}
    cmd = [sys.executable, "-m", "meshpipeline.engines.snapgrid.cli", SOURCE_NAME, ".",
           *cli_args(plan), "--bashrc", bashrc]
    env = scrubbed_subprocess_env({"PYTHONPATH": _pythonpath()})
    try:
        p = run_guarded(cmd, cwd=str(ws), env=env, capture_output=True, text=True,
                        timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        def _text(v) -> str:
            return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
        out = f"{_text(exc.stdout)}\n{_text(exc.stderr)}\nTIMEOUT after {timeout} s"
        (ws / LOG_NAME).write_text(out)
        return describe_native_result(returncode=-1, args=cmd, stage="snapgrid", output=out,
                                      outcome=NativeOutcome.timed_out)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    (ws / LOG_NAME).write_text(out)
    result = describe_native_result(returncode=p.returncode, args=cmd, stage="snapgrid",
                                    output=out)
    try:
        report = json.loads((ws / REPORT_NAME).read_text())
        result["cells"] = (report.get("native") or {}).get("cells") or \
            (report.get("polymesh") or {}).get("cells")
        result["regions"] = len(report.get("fluid_regions", [])) + \
            len(report.get("solid_regions", []))
    except (OSError, ValueError):
        pass
    return result


def _run_snapgrid_local(workspace, *, bashrc: str = "", timeout: int = 3600) -> dict:
    from meshpipeline.engines.snapgrid.native import default_bashrc

    return run_native_build(workspace, bashrc=bashrc or default_bashrc(), timeout=timeout)


# finalize: what the mesher wrote -> the manifest

def meshed_regions(ws: Path) -> list[str]:
    """Every region directory holding a complete polyMesh - the regions a solver will read."""
    const = Path(ws) / "constant"
    if not const.is_dir():
        return []
    need = ("owner", "neighbour", "faces", "points", "boundary")
    return sorted(d.name for d in const.iterdir()
                  if d.is_dir() and d.name != "polyMesh"
                  and all((d / "polyMesh" / f).exists() for f in need))


def quality_of(ws: Path, report: dict) -> dict:
    """The manifest's quality dict, from the mesher's report and the checkMesh results in it."""
    native = report.get("native") or {}
    fluids = list(report.get("fluid_regions") or [])
    solids = list(report.get("solid_regions") or [])
    declared = fluids + solids
    present = meshed_regions(ws)
    meshes = report.get("region_meshes") or {}
    checked = native.get("regions") or {}
    whole = native.get("whole") or {}
    fatal = list(whole.get("fatal") or [])
    rows = []
    for name in present:
        q = checked.get(name) or {}
        fatal += [f"{name}:{f}" for f in (q.get("fatal") or [])]
        rows.append({"name": name, "type": "fluid" if name in fluids else "solid",
                     "cells": int((meshes.get(name) or {}).get("cells") or q.get("cells") or 0),
                     "fatal": list(q.get("fatal") or [])})
    interfaces = list(native.get("interfaces") or [])
    mismatch = [f"{r['a']}_to_{r['b']}" for r in interfaces if not r.get("ok")]
    short = [f"{r['part']} ({r['axis']}, {r['thickness_m'] * 1e6:.4g} um: {r['cells_across']} of "
             f"{r['wanted']} cells)" for r in (report.get("thin_layers_short") or [])]
    q_out = {
        "cells": int(native.get("cells") or 0),
        "fatal": fatal,
        "skew_fraction": float(native.get("skew_fraction") or 0.0),
        "skew_faces": int(native.get("skew_faces") or 0),
        "max_non_ortho": native.get("max_non_ortho"),
        "max_skewness": native.get("max_skewness"),
        "regions": rows,
        "regions_missing": [n for n in declared if n not in present],
        "regions_undeclared": [n for n in present if n not in declared],
        "interface_ok": bool(interfaces) and not mismatch,
        "interfaces": interfaces,
        "interface_mismatch": mismatch,
        "thin_layers_short": short,
        "layers_budget": int(report.get("layers_budget") or 0),
        # the thermal rules, measured off the grid lines: every gap, heated surface, heat source
        # and opening short of what the level asks (absent - never a pass - when not measured)
        **_resolution_quality(report),
        "staircased": list(report.get("staircased") or []),
        "file_checked": any(str(line).startswith("Checked:")
                            for line in report.get("build_report") or []),
        "native_problems": list(native.get("problems") or []),
    }
    q_out["mesh_ok"] = bool(native.get("ok")) and not q_out["regions_missing"] \
        and not q_out["regions_undeclared"] and q_out["interface_ok"]
    return q_out


def _resolution_quality(report: dict) -> dict:
    """The resolution audit as the quality dict carries it: what fell short, in words, and the
    budget that would have kept every rule. Nothing when the mesher did not measure it."""
    res = report.get("resolution") or {}
    if not res.get("measured"):
        return {}
    short = [(f"{r.get('what')}: first cell {float(r['first_m']) * 1e3:.2g} mm, asked "
              f"{float(r['first_want_m']) * 1e3:.2g} mm")
             if r.get("first_m") and (r.get("got") or 0) >= (r.get("want") or 0) else
             f"{r.get('what')}: {r.get('got')} of {r.get('want')} cells"
             for r in res.get("short") or [] if isinstance(r, dict)]
    return {"resolution_short": short,
            "resolution_checked": int(sum((res.get("counts") or {}).values())),
            "rules_budget": int(report.get("rules_budget") or 0)}


_OF_TYPE_TO_ROLE = {"wall": "wall", "symmetryplane": "symmetry", "symmetry": "symmetry",
                    "empty": "empty"}
_MESH_EVIDENCED_ROLES = frozenset({"wall", "symmetry", "empty"})


def delivered_boundary_types(sidecar: dict, intake_patches: list) -> dict[str, str]:
    """The outside patches the mesh delivers (the domain sides and the file's openings and wall
    plates), each with the role the mesh evidences. Interfaces are the engine's, never the user's."""
    declared = {(p.get("name") or "").strip(): (p.get("type") or "").strip()
                for p in (intake_patches or []) if isinstance(p, dict) and p.get("name")}
    out: dict[str, str] = {}
    for row in sidecar.get("patches") or []:
        name = row.get("mesh_patch")
        if not name or not row.get("faces"):
            continue
        mapped = _OF_TYPE_TO_ROLE.get(str(row.get("mesh_patch_type") or "").lower())
        role = declared.get(name)
        out[name] = mapped or (role if role and role.lower() not in _MESH_EVIDENCED_ROLES
                               else "patch")
    return out


def finalize(workspace_dir, intake_patches: list, engine: str, domain: str = "",
             internal_flow: bool = False, engine_params: dict | None = None,
             flow_topology: str = "") -> dict:
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    from meshpipeline.engines.manifest import write_manifest

    ws = Path(workspace_dir)
    try:
        report = json.loads((ws / REPORT_NAME).read_text())
    except (OSError, ValueError):
        out = f"[SNAPGRID] no readable {REPORT_NAME} - the mesher did not finish"
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    if not report.get("native"):
        out = "[SNAPGRID] checkMesh never ran on this mesh - it cannot be delivered unchecked"
        return {"success": False, "stdout": "", "stderr": "", "output": out}
    try:
        sidecar = json.loads((ws / SIDECAR_NAME).read_text())
    except (OSError, ValueError):
        sidecar = {}
    q = quality_of(ws, report)
    from meshpipeline.engines.snapgrid.driver import thin_layer_warning
    warning = thin_layer_warning(report)
    if warning:
        q["thin_layers_warning"] = warning
    patch_types = delivered_boundary_types(sidecar, intake_patches)
    box = ((sidecar.get("domain") or {}).get("box_m")) or {"min": [0.0] * 3, "max": [1.0] * 3}
    lo, hi = [float(v) for v in box["min"]], [float(v) for v in box["max"]]
    # REVIEW SURFACE: the placed parts the run staged (input.stl, one named solid per part, in
    # metres) - the same kind of surface the other multi-region engine renders from.
    patch_entities: dict = {}
    try:
        from meshpipeline.cad.stl_io import read_stl_solids
        from meshpipeline.render.review_artifacts import build_review_msh
        stl = ws / "input.stl"
        if stl.exists():
            solids = read_stl_solids(stl)
            if solids:
                patch_entities, _ = build_review_msh(ws, solids)
    except Exception:  # noqa: BLE001 - the review surface is render-only
        logger.exception("snapgrid finalize: review-mesh build failed (non-fatal)")
    from meshpipeline.engines.snapgrid.native import review_volume
    ep = dict(engine_params or {})
    ep["_regions"] = [{"name": r["name"], "type": r["type"]} for r in q["regions"]]
    # THE SLICES a thermal model is looked at through - across the board, upright through the
    # hottest parts: the review inspects them and the console offers them ready-made
    from meshpipeline.engines.snapgrid.results import slice_planes
    try:
        planes = slice_planes(sidecar, report)
    except Exception:  # noqa: BLE001 - the generic slices stand in
        logger.exception("snapgrid finalize: thermal slices unavailable - generic slices used")
        planes = []
    write_manifest(
        ws, patch_types=patch_types,
        patch_entities={**{n: [] for n in patch_types}, **patch_entities},
        bbox=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        quality=q, domain=domain or "conjugate heat transfer (electronics cooling)",
        body_bbox=(tuple(lo), tuple(hi)),
        mesh_bounds=(lo[0], lo[1], lo[2], hi[0], hi[1], hi[2]),
        # the review's VOLUME: the whole mesh as VTK (native.export_volume), which is what the
        # renderer slices for the interior regions the manifest promises; a constant/ directory
        # is no VTU, so the review found no volume and could not run at all (job d6a0ac60)
        volume_path=str((ws / (review_volume(ws) or "constant")).resolve()),
        mesh_units=COMPLETED_MESH_UNIT.value, mesh_mode="snapgrid",
        flow_topology=flow_topology, engine_params=ep, inspection_regions=planes or None)
    out = (f"[SNAPGRID] regions={len(q['regions'])} cells={q['cells']} "
           f"missing={q['regions_missing']} interfaces_ok={q['interface_ok']} fatal={q['fatal']} "
           f"thin_layers_short={len(q['thin_layers_short'])}"
           + (f" WARNING: {warning}" if warning else ""))
    return {"success": q["mesh_ok"], "stdout": out, "stderr": "", "output": out}


__all__ = ["FIDELITY_PLANS", "LOG_NAME", "PLAN_NAME", "REPORT_NAME", "SOURCE_NAME", "cli_args",
           "delivered_boundary_types", "finalize", "find_source", "meshed_regions", "plan_for",
           "quality_of", "read_plan", "run_native_build", "write_plan"]
