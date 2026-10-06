# Responsibility: The one-command entry point of the placed-parts (snap-grid) ECXML mesher: an ECXML file in, a multi-region OpenFOAM case and its report out.
# Boundaries: argument parsing and a printed summary; the work is engines/snapgrid/mesher.py and native.py.
"""Mesh an ECXML thermal model on a snap grid (Option B: no gluing).

    python -m meshpipeline.engines.snapgrid.cli MODEL.ecxml OUT_DIR [--max-cells N]
        [--background-cells N] [--min-cells-across N] [--cylinder-cells N] [--growth G]
        [--no-check] [--check-each-region auto|yes|no] [-j N] [--split-with-openfoam]
        [--keep-merged] [--ascii] [--dry-run] [--global-grid] [--bashrc PATH]

OUT_DIR becomes an OpenFOAM case: constant/<region>/polyMesh for every region (written directly,
each with its mappedWall interfaces), constant/regionProperties, snapgrid_report.json (grid,
regions, thin layers, curved parts, interfaces, checks, timings) and thermal_model.json (the
physics, in the fused path's sidecar shape). checkMesh then reads the whole mesh once (and each
region, in parallel, when there are few). Exit code 0 when the mesh is written and checked clean;
2 when the file cannot be meshed (the reason is printed); 3 when the check found a problem.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from meshpipeline.cad.ingest.ecxml import EcxmlError
    from meshpipeline.engines.snapgrid.grid import GridPlan, OverBudget
    from meshpipeline.engines.snapgrid.mesher import mesh_ecxml

    ap = argparse.ArgumentParser(prog="snapgrid", description=__doc__.split("\n\n")[0])
    ap.add_argument("ecxml", type=Path)
    ap.add_argument("out", type=Path)
    d = GridPlan()
    ap.add_argument("--max-cells", type=int, default=d.max_cells)
    ap.add_argument("--background-cells", type=int, default=d.background_cells)
    ap.add_argument("--min-cells-across", type=int, default=d.min_cells_across)
    ap.add_argument("--cylinder-cells", type=int, default=d.cylinder_cells)
    ap.add_argument("--growth", type=float, default=d.growth)
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
    a = ap.parse_args(argv)
    plan = GridPlan(max_cells=a.max_cells, background_cells=a.background_cells,
                    min_cells_across=a.min_cells_across, cylinder_cells=a.cylinder_cells,
                    growth=a.growth)
    t0 = time.perf_counter()
    from meshpipeline.engines.snapgrid.native import dialect_of

    try:
        result = mesh_ecxml(a.ecxml, a.out, plan, binary=False if a.ascii else None,
                            dry_run=a.dry_run, local=not a.global_grid,
                            dialect=dialect_of(a.bashrc))
    except OverBudget as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
    except EcxmlError as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
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
