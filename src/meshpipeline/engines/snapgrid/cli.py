# Responsibility: The one-command entry point of the placed-parts (snap-grid) ECXML mesher: an ECXML file in, a multi-region OpenFOAM case and its report out.
# Boundaries: argument parsing and a printed summary; the work is engines/snapgrid/mesher.py and native.py.
"""Mesh an ECXML thermal model on a snap grid (Option B: no gluing).

    python -m meshpipeline.engines.snapgrid.cli MODEL.ecxml OUT_DIR [--level draft|standard|max]
        [--max-cells N] [--background-cells N] [--min-cells-across N] [--cylinder-cells N]
        [--growth G] [--hot-cells-across N] [--gap-cells N] [--band-cells N] [--spread-cells N]
        [--opening-cells N] [--air-aspect A]
        [--no-check] [--check-each-region auto|yes|no] [-j N] [--split-with-openfoam]
        [--keep-merged] [--ascii] [--dry-run] [--global-grid] [--bashrc PATH] [--memory-gb G]

OUT_DIR becomes an OpenFOAM case: constant/<region>/polyMesh for every region (written directly,
each with its mappedWall interfaces), constant/regionProperties, snapgrid_report.json (grid,
regions, thin layers, curved parts, interfaces, checks, timings) and thermal_model.json (the
physics, in the fused path's sidecar shape). checkMesh then reads the whole mesh once (and each
region, in parallel, when there are few). Exit code 0 when the mesh is written and checked clean;
2 when the file cannot be meshed (the reason is printed); 3 when the check found a problem;
4 when the mesh would not match the file (a mesher bug, never written).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from meshpipeline.cad.ingest.ecxml import EcxmlError
    from meshpipeline.cad.ingest.ecxml_build import FidelityError
    from meshpipeline.engines.snapgrid.grid import GridPlan, OverBudget
    from meshpipeline.engines.snapgrid.mesher import mesh_ecxml

    ap = argparse.ArgumentParser(prog="snapgrid", description=__doc__.split("\n\n")[0])
    ap.add_argument("ecxml", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--level", choices=("draft", "standard", "max"), default=None,
                    help="start from a detail level's plan (runner.FIDELITY_PLANS); the options "
                         "below override it (default: a bare grid plan, thermal rules off)")
    ap.add_argument("--max-cells", type=int, default=None)
    ap.add_argument("--background-cells", type=int, default=None)
    ap.add_argument("--min-cells-across", type=int, default=None)
    ap.add_argument("--cylinder-cells", type=int, default=None)
    ap.add_argument("--growth", type=float, default=None)
    ap.add_argument("--hot-cells-across", type=int, default=None,
                    help="cells through each heated part (0: --min-cells-across)")
    ap.add_argument("--gap-cells", type=int, default=None,
                    help="cells across each air gap between two solids (0: off)")
    ap.add_argument("--band-cells", type=int, default=None,
                    help="cells inside the boundary layer of each heated surface (0: off)")
    ap.add_argument("--spread-cells", type=int, default=None,
                    help="cells across each heat-producing part's width (0: off)")
    ap.add_argument("--opening-cells", type=int, default=None,
                    help="cells across each fan, vent and grille (0: off)")
    ap.add_argument("--air-aspect", type=float, default=None,
                    help="longest/shortest cell side in gaps and heated bands (0: no limit)")
    ap.add_argument("--no-check", "--no-split", dest="no_check", action="store_true",
                    help="write the mesh, run no OpenFOAM tool")
    ap.add_argument("--check-each-region", choices=("auto", "yes", "no"), default="auto",
                    help="checkMesh every region too (auto: when there are at most 64)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="checkMesh runs at once")
    ap.add_argument("--split-with-openfoam", action="store_true",
                    help="cut the regions with splitMeshRegions instead (slow at scale)")
    ap.add_argument("--keep-merged", action="store_true",
                    help="keep constant/polyMesh (the whole mesh) after the check")
    ap.add_argument("--ascii", action="store_true", help="ASCII files (small meshes)")
    ap.add_argument("--dry-run", action="store_true", help="size the grid, write nothing")
    ap.add_argument("--global-grid", action="store_true",
                    help="one tensor grid over the whole domain (no local refinement)")
    ap.add_argument("--bashrc", default=None, help="OpenFOAM bashrc to source")
    ap.add_argument("--mesh-every-layer", action="store_true",
                    help="mesh every thin layer with cells through it, however flat (default: a "
                         "layer too thin to mesh soundly is a thin-layer thermal resistance)")
    ap.add_argument("--memory-gb", type=float, default=None,
                    help="memory the job may use (default: read from the machine and its container)")
    a = ap.parse_args(argv)
    from meshpipeline.engines.snapgrid.runner import PLAN_FIELDS, grid_plan, plan_for

    asked = dict(plan_for(a.level) or {}) if a.level else {}
    asked.update({k: getattr(a, k) for k in PLAN_FIELDS if getattr(a, k) is not None})
    plan = grid_plan(asked) if asked else GridPlan()
    t0 = time.perf_counter()
    from meshpipeline.engines.snapgrid.native import dialect_of

    try:
        result = mesh_ecxml(a.ecxml, a.out, plan, binary=False if a.ascii else None,
                            dry_run=a.dry_run, local=not a.global_grid,
                            dialect=dialect_of(a.bashrc),
                            memory_bytes=a.memory_gb * 1e9 if a.memory_gb else None,
                            thin_layers=not a.mesh_every_layer)
    except OverBudget as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
    except EcxmlError as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
    except FidelityError as exc:
        print(f"NOT MESHED (the mesh would not be the file's model - a mesher bug): {exc}",
              file=sys.stderr)
        return 4
    rep = result.report
    g = rep["grid"]
    print(f"grid {g['cells']:,} cells in {g['blocks']} block(s), max neighbour ratio "
          f"{g['max_neighbour_ratio']:.3g}; written in {time.perf_counter() - t0:.1f} s")
    if a.dry_run:
        print(json.dumps(rep, indent=1, default=float))
        return 0
    for line in rep["build_report"]:
        print(" ", line)
    code = 0
    if not a.no_check:
        from meshpipeline.engines.snapgrid import native as N

        t1 = time.perf_counter()
        if a.split_with_openfoam:
            res = N.split_and_check(a.out, bashrc=a.bashrc, keep_merged=a.keep_merged)
            label = "split + checkMesh"
        else:
            each = {"auto": None, "yes": True, "no": False}[a.check_each_region]
            res = N.check(a.out, bashrc=a.bashrc, jobs=a.jobs, each_region=each,
                          keep_merged=a.keep_merged)
            label = "checkMesh"
        print(f"{label}: {'OK' if res['ok'] else 'PROBLEMS'}; {len(res['regions'])} regions, "
              f"{res.get('cells', 0):,} cells, max non-ortho {res.get('max_non_ortho')}, "
              f"max skewness {res.get('max_skewness')} ({time.perf_counter() - t1:.1f} s)")
        for p in res["problems"]:
            print("  PROBLEM:", p)
        code = 0 if res["ok"] else 3
    print(f"done in {time.perf_counter() - t0:.1f} s -> {a.out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
