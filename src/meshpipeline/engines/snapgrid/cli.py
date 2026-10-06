# Responsibility: The one-command entry point of the placed-parts (snap-grid) ECXML mesher: an ECXML file in, a multi-region OpenFOAM case and its report out.
# Boundaries: argument parsing and a printed summary; the work is engines/snapgrid/mesher.py and native.py.
"""Mesh an ECXML thermal model on a snap grid (Option B: no gluing).

    python -m meshpipeline.engines.snapgrid.cli MODEL.ecxml OUT_DIR [--max-cells N]
        [--background-cells N] [--min-cells-across N] [--cylinder-cells N] [--growth G]
        [--no-split] [--ascii] [--dry-run] [--bashrc PATH]

OUT_DIR becomes an OpenFOAM case: constant/<region>/polyMesh for every region (after the split),
constant/regionProperties, snapgrid_report.json (grid, regions, thin layers, interfaces, checks,
timings) and thermal_model.json (the physics, in the fused path's sidecar shape). Exit code 0 when
the mesh is written (and, unless --no-split, split and checked clean); 2 when the file cannot be
meshed (the reason is printed); 3 when the native split or check found a problem.
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
    ap.add_argument("--no-split", action="store_true", help="write the merged mesh only")
    ap.add_argument("--keep-merged", action="store_true",
                    help="keep constant/polyMesh after the split")
    ap.add_argument("--ascii", action="store_true", help="ASCII files (small meshes)")
    ap.add_argument("--dry-run", action="store_true", help="size the grid, write nothing")
    ap.add_argument("--bashrc", default=None, help="OpenFOAM bashrc to source")
    a = ap.parse_args(argv)
    plan = GridPlan(max_cells=a.max_cells, background_cells=a.background_cells,
                    min_cells_across=a.min_cells_across, cylinder_cells=a.cylinder_cells,
                    growth=a.growth)
    t0 = time.perf_counter()
    try:
        result = mesh_ecxml(a.ecxml, a.out, plan, binary=False if a.ascii else None,
                            dry_run=a.dry_run)
    except OverBudget as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
    except EcxmlError as exc:
        print(f"NOT MESHED: {exc}", file=sys.stderr)
        return 2
    rep = result.report
    g = rep["grid"]
    print(f"grid {g['x_cells']} x {g['y_cells']} x {g['z_cells']} = {g['cells']:,} cells, "
          f"max neighbour ratio {g['max_neighbour_ratio']:.3g}")
    if a.dry_run:
        print(json.dumps(rep, indent=1, default=float))
        return 0
    for line in rep["build_report"]:
        print(" ", line)
    code = 0
    if not a.no_split:
        from meshpipeline.engines.snapgrid.native import split_and_check

        native = split_and_check(a.out, bashrc=a.bashrc, keep_merged=a.keep_merged)
        print(f"split + checkMesh: {'OK' if native['ok'] else 'PROBLEMS'}; "
              f"{len(native['regions'])} regions, {native.get('cells', 0):,} cells, "
              f"max non-ortho {native.get('max_non_ortho')}, "
              f"max skewness {native.get('max_skewness')}")
        for p in native["problems"]:
            print("  PROBLEM:", p)
        code = 0 if native["ok"] else 3
    print(f"done in {time.perf_counter() - t0:.1f} s -> {a.out}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
