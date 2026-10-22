# Responsibility: Build a conjugate solid/fluid mesh for one part, from the command line.
# Boundaries: an operator tool; it drives engines/cfmesh/conjugate and judges nothing of its own.
"""BUILD A CONJUGATE MESH FOR ONE PART.

    python devtools/semiconductor/conjugate_part.py <solid.stl> <fluid.stl> \\
        --cell 80e-6 [--levels 3] [--expect-fluid-mm3 6.311812] [--expect-pieces 1] \\
        [--workspace /tmp/case] [--dry-run]

The solid STL supplies the part's outer extent, from which the block to mesh is derived; the fluid
STL is the coolant. Both must be in METRES, which is the case convention - a surface still in
millimetres makes every cell size a thousand times wrong and the run never finishes.

WHAT IT PRINTS. The stage timings, the two regions, the interface face counts on each side, and
every check that failed. `--dry-run` writes the dictionaries and stops, which is the fast way to see
what would be run without spending an hour on it.

WHY THE EXPECTATIONS ARE WORTH PASSING. `--expect-fluid-mm3` is what proves the right cells were
tagged: a split that put half the metal in the coolant region would still have a perfectly conformal
interface. `--expect-pieces` is the number of separate passages the part has - 1 for a connected
network, N for N channels that never touch - and catches a mesh too coarse to hold a passage
together, which checkMesh calls "Mesh OK" and no solver will run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meshpipeline.engines.cfmesh import conjugate as C
from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
from meshpipeline.engines.staircase_error import annotate


def _bounds_of_binary_stl(path: Path) -> tuple[tuple, tuple]:
    import struct
    b = path.read_bytes()
    if b[:5] == b"solid" and b"facet" in b[:2048]:
        raise SystemExit(f"{path.name}: ASCII STL; this tool reads binary STL")
    n = struct.unpack_from("<I", b, 80)[0]
    if len(b) != 84 + n * 50:
        raise SystemExit(f"{path.name}: not a binary STL of {n} triangles")
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for i in range(n):
        o = 84 + i * 50 + 12
        for v in range(3):
            for k, c in enumerate(struct.unpack_from("<3f", b, o + v * 12)):
                lo[k] = min(lo[k], c)
                hi[k] = max(hi[k], c)
    return tuple(lo), tuple(hi)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a conjugate solid/fluid mesh for one part.")
    ap.add_argument("solid", type=Path, help="binary STL of the part, in metres")
    ap.add_argument("fluid", type=Path, help="binary STL of the coolant, in metres")
    ap.add_argument("--cell", type=float, required=True,
                    help="background cell size in metres (the interface gets cell/2**levels)")
    ap.add_argument("--levels", type=int, default=C.DEFAULT_REFINEMENT_LEVELS)
    ap.add_argument("--expect-fluid-mm3", type=float, default=None,
                    help="the CAD's own fluid volume; proves the right cells were tagged")
    ap.add_argument("--expect-pieces", type=int, default=None,
                    help="how many separate passages the part has (1 for a connected network)")
    ap.add_argument("--workspace", type=Path, default=Path("conjugate-case"))
    ap.add_argument("--dry-run", action="store_true",
                    help="write the dictionaries and stop")
    a = ap.parse_args(argv)

    for p in (a.solid, a.fluid):
        if not p.is_file():
            print(f"missing: {p}", file=sys.stderr)
            return 2

    ws = a.workspace
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    write_case_skeleton(ws)

    lo, hi = _bounds_of_binary_stl(a.solid)
    extent_mm = [(hi[i] - lo[i]) * 1e3 for i in range(3)]
    (ws / "constant" / "triSurface" / "block.stl").write_bytes(C.block_surface_stl(lo, hi))
    (ws / "constant" / "triSurface" / "fluid.stl").write_bytes(a.fluid.read_bytes())

    iface = C.interface_cell_m(a.cell, a.levels)
    print(f"part        {a.solid.name}")
    print(f"block       {extent_mm[0]:.3f} x {extent_mm[1]:.3f} x {extent_mm[2]:.3f} mm")
    print(f"background  {a.cell * 1e6:.2f} um,  {a.levels} levels  ->  "
          f"interface {iface * 1e6:.2f} um")
    if a.expect_fluid_mm3:
        # The staircase evidence, stated before the run rather than discovered after it.
        # engines/gap_sizing owns the sizing rule; this only says what this resolution is worth.
        print(f"            {annotate(1.0)['basis']}")
    print()

    if a.dry_run:
        (ws / "system" / "meshDict").write_text(C.render_meshdict(
            block_file="constant/triSurface/block.stl",
            fluid_file="constant/triSurface/fluid.stl",
            background_cell_m=a.cell, levels=a.levels))
        (ws / "system" / "topoSetDict").write_text(
            C.render_toposetdict(fluid_file="constant/triSurface/fluid.stl"))
        print(f"dictionaries written to {ws}/system; nothing was run")
        return 0

    try:
        rep = C.build(
            ws, background_cell_m=a.cell, levels=a.levels,
            expect_fluid_volume_m3=(a.expect_fluid_mm3 * 1e-9) if a.expect_fluid_mm3 else None,
            expect_fluid_pieces=a.expect_pieces)
    except C.ConjugateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    print("stage timings")
    for k, v in rep.seconds.items():
        print(f"  {k:<22} {v:>8.1f}s")
    print(f"  {'post-processing share':<22} {rep.post_share * 100:>7.0f}%")
    print()
    print("regions")
    for r in (rep.fluid, rep.solid):
        print(f"  {r.name:<6} {r.cells:>12,} cells  {r.volume_m3 * 1e9:>12.5f} mm^3  "
              f"{r.pieces} piece(s)  {'Mesh OK' if r.mesh_ok else 'NOT OK'}")
    print()
    print(f"interface   {rep.interface_faces_fluid:,} faces fluid side, "
          f"{rep.interface_faces_solid:,} solid side  -> "
          f"{'conformal' if rep.conformal else 'NOT CONFORMAL'}")
    if rep.problems:
        print()
        print("PROBLEMS")
        for p in rep.problems:
            print(f"  - {p}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
