# Responsibility: One entry point for semiconductor conjugate meshing: list, size, mesh, solve.
# Boundaries: an operator tool. It drives the engine and the solver harness and judges nothing
# itself; every verdict printed here is one of theirs.
"""SEMICONDUCTOR CONJUGATE MESHING, FROM ONE COMMAND.

    semi list                          what parts there are and what each would cost
    semi ports  <part>                 which faces the coolant reaches, and the flow plan
    semi mesh   <part>                 build the two-region mesh
    semi solve  <run-dir>              run the external solve on a built mesh and judge it
    semi check  <part>                 mesh, then solve, then say whether the mesh held up
    semi sweep  <part> --across 2 5 13 the same part at several resolutions, side by side
    semi import --from <dir>           bring new CAD in, scaling millimetres to metres

OpenFOAM has to be on PATH, so every command runs inside the mesh image. `devtools/semiconductor/
run.sh` does that:

    devtools/semiconductor/run.sh check unit_cell_100um --across 5

NOTHING IS TYPED TWICE. The coolant volume, the passage count and the narrowest feature come from
`cad/semiconductor/parts.json`, which `index_parts.py` measures off the STLs. The cell size is
derived from the narrowest feature and the cells-across asked for. The inlet and outlet are derived
from which block faces the coolant actually reaches. Each of those was a command-line argument
before, and each one of them is a number that was going to be wrong eventually.

EVERY RUN IS COSTED BEFORE IT STARTS. A conjugate mesh of a chip-scale part at the platform's own
resolution runs into the billions of cells, and a run that will not fit is refused with the figure
rather than discovered when the machine starts swapping an hour in.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import cht
import convergence as V
import parts as P

from meshpipeline.engines.cfmesh import conjugate as C
from meshpipeline.engines.cfmesh.case_scaffold import write_case_skeleton
from meshpipeline.engines.staircase_error import annotate, single_passage_error_pct

#: Bytes of resident memory per cell in the FINISHED mesh, measured 2026-10-21.
BYTES_PER_CELL = 909
#: Bytes of resident memory per cell at cfMesh's PEAK while building it, measured 2026-10-22.
#:
#: NOT THE SAME NUMBER, and the difference is a run that gets killed. A 5.74 M cell build was
#: predicted at 4.9 GB from the finished-mesh figure, passed the check, and was killed by the
#: kernel at 8.0 GB resident with 14.3 GB of virtual address space. The octree, its candidate
#: cells and the smoothing passes all exist at once before anything is written out, so the mesher's
#: high-water mark is several times what the mesh it produces occupies. ConjugateReport
#: .peak_rss_bytes measures it on every run, so this stops being a guess the moment a run finishes.
#:
#: Fitted on two measured builds of the same part, 2026-10-22: 109,200 cells peaked at 0.23 GB and
#: 1,185,484 cells at 1.33 GB, which is 1010 bytes per cell plus 128 MB that is there whatever the
#: mesh size.
PEAK_BYTES_PER_CELL = 1010
#: What OpenFOAM costs before it has meshed anything, from the same two builds.
FIXED_OVERHEAD_BYTES = 128 * 1024 ** 2
#: The share of the machine's memory a run may be predicted to need before it is refused. The
#: mesher is not the only thing resident and a prediction is not a guarantee.
MEMORY_HEADROOM = 0.80
#: Where runs land, and the default for --workspace.
RUNS = Path("runs")
CAD = Path("cad") / "semiconductor"


def memory_bytes() -> int:
    """The machine's total memory, so a refusal can quote the real number."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def estimate_cells(part: P.Part, background_m: float, levels: int,
                   thickness_m: float | None = None) -> tuple[float, float]:
    """(cells, peak bytes) for a conjugate build, in three parts.

    THE METAL AROUND THE COOLANT IS REFINED TOO, and leaving that out is how an estimate comes in
    2.4 times short. `surfaceMeshRefinement` refines within `refinementThickness` of the coolant
    surface on BOTH sides, so a shell of metal that thick is filled at the interface cell, not at
    the background. On a microchannel part that shell is most of the metal there is.

        fluid       its own volume, at the interface cell
        shell       wetted area x refinement thickness of metal, at the interface cell,
                    capped at the metal there is
        the rest    at the background cell

    Measured against three real builds of unit_cell_100um: 11% under at 5 cells across, 38% under
    at 2. Under, both times, because the octree grades between levels rather than stepping, and
    the graded cells are not in this model. The previous model put every cell of metal at the
    background size and predicted 5.74 M cells for a build that was killed by the kernel; this one
    predicts 13.6 M for it and refuses it.
    """
    iface = C.interface_cell_m(background_m, levels)
    thickness = background_m if thickness_m is None else thickness_m
    metal_m3 = max(part.block_volume_m3 - part.fluid_volume_m3, 0.0)
    shell_m3 = min(part.wetted_area_m2 * thickness, metal_m3)
    coarse_m3 = metal_m3 - shell_m3
    cells = ((part.fluid_volume_m3 + shell_m3) / iface ** 3
             + coarse_m3 / background_m ** 3)
    # The PEAK figure plus the fixed cost of starting OpenFOAM at all. What has to fit is the
    # biggest moment, not the mesh that comes out of it.
    return cells, FIXED_OVERHEAD_BYTES + cells * PEAK_BYTES_PER_CELL


#: How much of the expected staircase error a mesh may exceed before the tagging is not believed.
#:
#: The gate is asking ONE question - did the right cells end up on which side - and the staircase
#: is the noise it has to see through. A split that put half the metal in the coolant region is
#: wrong by hundreds of per cent; a 2-cells-across mesh that came out 27% short of the CAD is a
#: mesh doing exactly what 2 cells across does. One fixed number cannot tell those apart at both
#: ends, and the fixed 5% called the second one a failure, so `check` and `sweep` never reached
#: their solve on anything coarse.
VOLUME_TOL_MARGIN = 1.5


def volume_tolerance(cells_across: int, override: float | None = None) -> tuple[float, str]:
    """(tolerance, why) for the fluid-volume gate at this resolution.

    Derived from engines/staircase_error, which owns what a resolution costs, so the gate and the
    evidence cannot disagree. The WORST curve is used rather than the average: a part with one
    passage is the one most likely to be refused for being coarse, and the gate is not there to
    judge accuracy. It never goes below the platform's own allowance, so at the resolution the rule
    asks for nothing is loosened.
    """
    expected = single_passage_error_pct(float(cells_across)) / 100.0
    if override is not None:
        return override, (
            f"{override:.0%}, named on the command line. The measurements put the staircase at "
            f"{expected:.2%} for {cells_across} cells across, so this is a deliberate choice and "
            "not the derived one")
    derived = VOLUME_TOL_MARGIN * expected
    if derived <= C.DEFAULT_VOLUME_TOL:
        return C.DEFAULT_VOLUME_TOL, (
            f"{C.DEFAULT_VOLUME_TOL:.0%}, the standing allowance; {cells_across} cells across is "
            f"fine enough that the measured staircase ({expected:.2%}) is well inside it")
    return derived, (
        f"{derived:.0%}, which is {VOLUME_TOL_MARGIN:g}x the {expected:.1%} staircase error "
        f"measured at {cells_across} cells across on the part that staircases worst. A flat "
        f"{C.DEFAULT_VOLUME_TOL:.0%} would refuse this mesh for being exactly as coarse as asked")


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def registry(cad: Path) -> dict[str, P.Part]:
    try:
        return P.load(cad)
    except P.PartError as exc:
        raise SystemExit(str(exc))


def pick(cad: Path, name: str) -> P.Part:
    reg = registry(cad)
    if name in reg:
        return reg[name]
    close = [k for k in reg if name.lower() in k.lower()]
    if len(close) == 1:
        return reg[close[0]]
    raise SystemExit(
        f"no part called {name!r}."
        + (f" Did you mean one of {close}?" if close else f" There are {sorted(reg)}."))


# #
# sizing and ports, which every command needs
# #
def plan(part: P.Part, cells_across: int, levels: int, lid_mm: float,
         inlet: str | None, outlet: str | None, no_ports: bool = False):
    """Everything derived before anything is run: block, cell size, ports, cost.

    `lid_mm` is in MILLIMETRES, like every other length a person types at this command line, and is
    converted here because everything below this line is in metres. It was passed straight through
    once: `--lid 0.1` meant to add 100 um of metal and added 100 mm, turning a 1.3 mm plate into a
    101.3 mm one. Nothing failed. The mesh built, every check passed, and the block was eighty
    times the part.
    """
    grow = {"zmax": lid_mm * 1e-3} if lid_mm else None
    lo, hi = C.block_bounds(part.solid_lo, part.solid_hi, grow)
    background = part.cell_for(cells_across, levels)
    open_ = C.open_faces(part.fluid_lo, part.fluid_hi, lo, hi)
    # The PASSAGE extent, not the whole coolant's bounding box: on a wide, short channel array the
    # two point at different axes and only one of them is the flow direction.
    #
    # `no_ports` builds the mesh and skips the port plan and the port gate. A sealed part is still
    # worth meshing - murray_branching_4gen is the hardest branching topology in the set and was
    # the article the whole recipe was worked out on - and refusing to mesh it because it cannot be
    # SOLVED would be refusing the wrong thing. It has to be asked for, so an accidentally sealed
    # part is still caught.
    ports = None if no_ports else C.plan_ports(open_, part.passage_extent,
                                               inlet=inlet, outlet=outlet)
    cells, nbytes = estimate_cells(part, background, levels)
    return lo, hi, background, open_, ports, cells, nbytes


def describe(part: P.Part, cells_across: int, levels: int, lo, hi, background: float,
             open_: tuple, ports: C.PortPlan | None, cells: float, nbytes: float,
             override: float | None = None) -> None:
    iface = C.interface_cell_m(background, levels)
    print(f"part          {part.name}")
    if part.note:
        print(f"              {part.note[:110]}")
    print(f"block         {(hi[0] - lo[0]) * 1e3:.3f} x {(hi[1] - lo[1]) * 1e3:.3f} x "
          f"{(hi[2] - lo[2]) * 1e3:.3f} mm")
    print(f"coolant       {part.fluid_volume_m3 * 1e9:.5f} mm3 in {part.passages} passage(s), "
          f"narrowest {part.narrowest_m * 1e6:.0f} um")
    print(f"resolution    {cells_across} cells across  ->  interface {iface * 1e6:.3f} um, "
          f"background {background * 1e6:.3f} um ({levels} levels)")
    ev = annotate(float(cells_across))
    print(f"              expect {ev['expected_volume_error_pct']}% of the coolant volume lost to "
          f"staircasing on a part with many passages, {ev['worst_measured_volume_error_pct']}% on "
          f"one with a single passage ({ev['note']})")
    tol, why = volume_tolerance(cells_across, override)
    print(f"volume gate   {why}")
    print(f"estimate      {cells / 1e6:.2f} M cells, {_gb(nbytes)} at the mesher's peak "
          f"({_gb(cells * BYTES_PER_CELL)} once written); this machine has {_gb(memory_bytes())}")
    if (why := C.background_complaint(lo, hi, background)):
        print(f"SIZING       {why}")
    print(f"open faces    {', '.join(open_) if open_ else 'NONE: the coolant is sealed'}")
    if ports is None:
        print("flow          none: --no-ports was asked for, so this mesh is not for solving")
        return
    print(f"flow          in at {ports.inlet}, out at {ports.outlet}  ({ports.reason})")
    if ports.walls:
        print(f"sealed        {', '.join(ports.walls)}")


class TooBig(Exception):
    """This run will not fit in memory. Raised rather than exiting, so a sweep keeps its rows.

    It used to be a SystemExit, which is correct for one `mesh` and wrong inside `sweep`: the
    refusal unwound straight out of the loop and threw away every resolution already meshed, which
    is the expensive half of the work.
    """


def affordable(nbytes: float, force: bool) -> None:
    total = memory_bytes()
    if not total or force:
        return
    if nbytes > MEMORY_HEADROOM * total:
        raise TooBig(
            f"this would need about {_gb(nbytes)} and the machine has {_gb(total)}. "
            f"Coarsen it with --across, or run it on a bigger box, or pass --force to try anyway. "
            f"Forcing it does not make it fit: the mesher starts swapping and never finishes.")


# #
# commands
# #
def cmd_list(a) -> int:
    reg = registry(a.cad)
    total = memory_bytes()
    print(f"{'part':<34}{'narrow':>8}{'coolant':>11}{'pass':>6}"
          f"{'cells@' + str(a.across):>12}{'RAM':>9}   fits here")
    for name in sorted(reg):
        p = reg[name]
        cells, nbytes = estimate_cells(p, p.cell_for(a.across, a.levels), a.levels)
        fits = "yes" if total and nbytes <= MEMORY_HEADROOM * total else "no"
        print(f"  {name:<32}{p.narrowest_m * 1e6:>6.0f}um{p.fluid_volume_m3 * 1e9:>10.3f} "
              f"{p.passages:>6}{cells / 1e6:>11.1f}M{_gb(nbytes):>9}   {fits}")
    print(f"{chr(10)}  cells and RAM are for {a.across} cells across the narrowest passage at "
          f"{a.levels} octree levels")
    print(f"  this machine has {_gb(total)}, and a run is refused above "
          f"{MEMORY_HEADROOM:.0%} of it")
    return 0


def cmd_ports(a) -> int:
    part = pick(a.cad, a.part)
    try:
        describe(part, a.across, a.levels, *plan(part, a.across, a.levels, a.lid,
                                                 a.inlet, a.outlet, a.no_ports),
                 override=a.volume_tol)
    except C.ConjugateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"{chr(10)}nothing was run")
    return 0


def _workspace(a, part: P.Part) -> Path:
    if a.workspace:
        return Path(a.workspace)
    return RUNS / f"{part.name}-{a.across}across"


def cmd_mesh(a) -> int:
    part = pick(a.cad, a.part)
    try:
        lo, hi, background, open_, ports, cells, nbytes = plan(
            part, a.across, a.levels, a.lid, a.inlet, a.outlet, a.no_ports)
    except C.ConjugateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    describe(part, a.across, a.levels, lo, hi, background, open_, ports, cells, nbytes,
             override=a.volume_tol)
    try:
        affordable(nbytes, a.force)
    except TooBig as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    ws = _workspace(a, part)
    if ws.exists() and a.fresh:
        shutil.rmtree(ws)
    # A stale report from an earlier resolution in this directory would be read as this build's if
    # the build fails before writing its own, and a sweep would show the wrong row as this one's.
    (ws / "mesh-report.json").unlink(missing_ok=True)
    (ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    write_case_skeleton(ws)
    (ws / "constant" / "triSurface" / "block.stl").write_text(C.block_surface_stl(lo, hi))
    shutil.copyfile(part.fluid_stl, ws / "constant" / "triSurface" / "fluid.stl")
    print(f"{chr(10)}workspace     {ws}")

    if a.dry_run:
        (ws / "system" / "meshDict").write_text(C.render_meshdict(
            block_file="constant/triSurface/block.stl",
            fluid_file="constant/triSurface/fluid.stl",
            background_cell_m=background, levels=a.levels))
        (ws / "system" / "topoSetDict").write_text(
            C.render_toposetdict(fluid_file="constant/triSurface/fluid.stl"))
        print("dictionaries written; nothing was run")
        return 0

    print()
    try:
        rep = C.build(ws, background_cell_m=background, levels=a.levels,
                      expect_fluid_volume_m3=part.fluid_volume_m3,
                      expect_fluid_pieces=part.passages,
                      volume_tol=volume_tolerance(a.across, a.volume_tol)[0],
                      ports=ports, narrowest_m=part.narrowest_m)
    except C.ConjugateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print_mesh_report(rep)
    (ws / "mesh-report.json").write_text(json.dumps(mesh_json(part, a, rep, background), indent=2))
    return 1 if rep.problems else 0


def mesh_json(part: P.Part, a, rep: C.ConjugateReport, background: float) -> dict:
    return {
        "part": part.name,
        "cells_across_asked": a.across,
        "levels": a.levels,
        "background_cell_um": background * 1e6,
        "interface_cell_um": C.interface_cell_m(background, a.levels) * 1e6,
        "lid_mm": a.lid,
        "volume_tol": volume_tolerance(a.across, a.volume_tol)[0],
        "background_complaint": C.background_complaint(
            *C.block_bounds(part.solid_lo, part.solid_hi,
                            {"zmax": a.lid * 1e-3} if a.lid else None), background),
        # Explicit, because inlet_faces 0 means two different things: a port the coolant never
        # reached, which is a failure, and a run that was never asked to check one.
        "ports_checked": rep.ports is not None,
        "inlet": rep.ports.inlet if rep.ports else None,
        "outlet": rep.ports.outlet if rep.ports else None,
        "sealed_faces": list(rep.ports.walls) if rep.ports else [],
        "fluid_cells": rep.fluid.cells,
        "solid_cells": rep.solid.cells,
        "fluid_volume_mm3": rep.fluid.volume_m3 * 1e9,
        "solid_volume_mm3": rep.solid.volume_m3 * 1e9,
        # The mean cell each region actually came out at, against the background that was asked
        # for. On a channel array the refinement band covers the metal too, so the solid lands
        # near the interface size and the background is notional; recorded rather than assumed.
        "fluid_mean_cell_um": rep.fluid.mean_cell_m * 1e6,
        "solid_mean_cell_um": rep.solid.mean_cell_m * 1e6,
        "cad_fluid_volume_mm3": part.fluid_volume_m3 * 1e9,
        "fluid_volume_error_pct": (
            (rep.fluid.volume_m3 - part.fluid_volume_m3) / part.fluid_volume_m3 * 100),
        "fluid_passages": rep.fluid.pieces,
        "cad_passages": part.passages,
        "interface_faces": rep.interface_faces_fluid,
        "conformal": rep.conformal,
        "inlet_faces": rep.inlet_faces,
        "outlet_faces": rep.outlet_faces,
        "cells_across_achieved": rep.fluid.cells_across(part.narrowest_m),
        "fluid_patches": rep.fluid.patches,
        "solid_patches": rep.solid.patches,
        "seconds": rep.seconds,
        "post_share": rep.post_share,
        "peak_rss_bytes": rep.peak_rss_bytes,
        "peak_bytes_per_cell": (rep.peak_rss_bytes / (rep.fluid.cells + rep.solid.cells)
                                if (rep.fluid.cells + rep.solid.cells) else None),
        "problems": rep.problems,
        "notes": rep.notes,
    }


def print_mesh_report(rep: C.ConjugateReport) -> None:
    print("stage timings")
    for k, v in rep.seconds.items():
        print(f"  {k:<24} {v:>9.1f}s")
    print(f"  {'post-processing share':<24} {rep.post_share * 100:>8.0f}%")
    if rep.peak_rss_bytes:
        cells = rep.fluid.cells + rep.solid.cells
        per = rep.peak_rss_bytes / cells if cells else 0
        print(f"  {'peak memory':<24} {_gb(rep.peak_rss_bytes):>9}   "
              f"{per:.0f} B per cell at the peak, against {PEAK_BYTES_PER_CELL} predicted")
    print(f"{chr(10)}regions")
    for r in (rep.fluid, rep.solid):
        print(f"  {r.name:<6} {r.cells:>12,} cells  {r.volume_m3 * 1e9:>12.5f} mm3  "
              f"{r.pieces:>4} piece(s)  {'Mesh OK' if r.mesh_ok else 'NOT OK'}"
              f"   non-ortho {r.max_non_ortho}  skew {r.max_skewness}")
        print(f"         patches: "
              + ", ".join(f"{k} {v:,}" for k, v in sorted(r.patches.items())))
    print(f"{chr(10)}interface     {rep.interface_faces_fluid:,} faces fluid side, "
          f"{rep.interface_faces_solid:,} solid side  -> "
          f"{'conformal' if rep.conformal else 'NOT CONFORMAL'}")
    if rep.ports:
        print(f"ports         {rep.ports.inlet} {rep.inlet_faces:,} faces, "
              f"{rep.ports.outlet} {rep.outlet_faces:,} faces  -> "
              f"{'the coolant can flow' if rep.flowable else 'SEALED'}")
    if rep.notes:
        print(f"{chr(10)}worth knowing")
        for n in rep.notes:
            print(f"  - {n}")
    if rep.problems:
        print(f"{chr(10)}PROBLEMS")
        for p in rep.problems:
            print(f"  - {p}")
    else:
        print(f"{chr(10)}every mesh check passed")


def _run_foam(ws: Path, command: str, log: Path, timeout: int) -> tuple[int, float]:
    from meshpipeline.engines.cfmesh.foam_exec import _DEFAULT_BASHRC, _foam_env
    script = chr(10).join((f"source {_DEFAULT_BASHRC} >/dev/null 2>&1 || true", command, ""))
    t0 = time.monotonic()
    with log.open("w") as fh:
        proc = subprocess.run(["bash", "-lc", script], cwd=str(ws), env=_foam_env(),
                              stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return proc.returncode, time.monotonic() - t0


def solve_workspace(ws: Path, a) -> int:
    """Write the solve case onto a built mesh, run it, and judge the mesh by what came out."""
    report_path = ws / "mesh-report.json"
    if not report_path.is_file():
        print(f"no mesh-report.json in {ws}; build the mesh first with `semi mesh`",
              file=sys.stderr)
        return 2
    meta = json.loads(report_path.read_text())
    fluid_patches = sorted(meta["fluid_patches"])
    solid_patches = sorted(meta["solid_patches"])
    cores = a.cores if a.cores > 0 else min(8, os.cpu_count() or 1)
    spec = cht.SolveSpec(
        inlet=a.inlet or meta["inlet"], outlet=a.outlet or meta["outlet"],
        heated_face=a.heated_face, inlet_velocity=a.velocity, heat_flux=a.flux,
        iterations=a.iterations, cores=cores)

    print(f"{chr(10)}solve         chtMultiRegionSimpleFoam on {spec.cores} core(s), at most "
          f"{spec.iterations} iterations in chunks of {a.chunk}")
    print(f"coolant       {spec.coolant.name}: rho {spec.coolant.rho}, Cp {spec.coolant.cp}, "
          f"mu {spec.coolant.mu}, k {spec.coolant.kappa} (Pr {spec.coolant.prandtl:.1f})")
    print(f"metal         {spec.metal.name}: k {spec.metal.kappa} W/m/K")
    print(f"driving       {spec.inlet_velocity} m/s in at {spec.inlet}, "
          f"{spec.heat_flux / 1e4:.0f} W/cm2 into {spec.heated_face}, "
          f"coolant in at {spec.inlet_temperature} K")
    # NOT on --reuse. Re-judging a finished run must not touch the run: write_case rewrites the
    # controlDict and every field in 0/, which is needless mutation of the thing being read and
    # makes "nothing was run" a half-truth.
    if not a.reuse:
        try:
            # The face counts too, so a named patch with nothing behind it is refused before the
            # run rather than producing a verdict with nothing behind it either.
            cht.write_case(ws, spec, fluid_patches, solid_patches,
                           faces={**meta.get("fluid_patches", {}),
                                  **meta.get("solid_patches", {})})
        except cht.SolveError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1

    log = ws / "solve.log"
    stage_seconds: dict[str, float] = {}
    rc, text = 0, ""
    if a.reuse:
        # Re-read a run that already happened instead of running it again. Worth having because
        # the readings are parsed after the fact: a reader that picked the wrong column once
        # reported a mesh carrying 6.5 W as carrying none, and re-judging that run took seconds
        # where re-solving it took twenty-three minutes.
        if not log.is_file():
            print(f"--reuse needs a solve.log in {ws}, and there is none", file=sys.stderr)
            return 2
        text = log.read_text(errors="replace")
        took = 0.0
        print("  reusing the solve already in this workspace; nothing was run")
    if not a.reuse:
        # The previous run's readings AND its state go first. Both are indistinguishable from
        # this run's once they are on disk: the readings because the latest Time wins, and the
        # state because each chunk restarts with `startFrom latestTime`.
        readings, times = cht.clear_previous_run(ws)
        if readings or times:
            print(f"  cleared an earlier solve from this workspace: {readings} reading(s) and "
                  f"{times} time directory(ies)")
        if spec.cores > 1:
            rc, took = _run_foam(ws, cht.decompose_command(), ws / "decomposePar.log", a.timeout)
            stage_seconds["decomposePar"] = round(took, 1)
            if rc != 0:
                print(f"REFUSED: decomposePar exited {rc}; see {ws}/decomposePar.log",
                      file=sys.stderr)
                for line in (ws / "decomposePar.log").read_text(errors="replace").splitlines()[-12:]:
                    print(f"    {line}")
                return 1

        # CHUNKED, and checked between chunks against the same residuals the verdict uses. Neither
        # of the solver's own stopping mechanisms works (see cht._WHAT_DOES_NOT_WORK), so the run
        # is stopped from out here: iterate a chunk, read the log, ask for another only if some
        # equation is still above target. The iteration count then means something - it is how long
        # this mesh took to converge - and the iterations after convergence are not paid for.
        log.write_text("")
        pieces = cht.chunks(spec.iterations, a.chunk)
        for n, (start, end) in enumerate(pieces, 1):
            (ws / "system" / "controlDict").write_text(
                cht.control_dict(spec, start=start, end=end))
            chunk_log = ws / f"solve-{end}.log"
            rc, took = _run_foam(ws, cht.solver_command(spec.cores), chunk_log, a.timeout)
            stage_seconds[f"solve:{end}"] = round(took, 1)
            text += chunk_log.read_text(errors="replace")
            log.write_text(text)
            chunk_log.unlink(missing_ok=True)
            done, converged = cht.progress(text)
            print(f"  chunk {n}/{len(pieces)}: iterations {start}-{end} in {took:.0f}s, "
                  f"reached {done}, {'converged' if converged else 'still running'}")
            if rc != 0:
                print(f"  the solver exited {rc} in this chunk; stopping", file=sys.stderr)
                break
            if converged:
                break
        took = sum(stage_seconds.values())
    rep = cht.collect(ws, spec, text)
    print(f"{chr(10)}ran {rep.iterations_run} iteration(s) in {took:.0f}s, exit {rc}, "
          f"{'converged' if rep.converged else 'NOT converged'}")

    print(f"{chr(10)}residuals (initial, first -> last, against target)")
    for k, (first, last) in sorted(rep.residuals.items()):
        decades = 0.0 if last <= 0 or first <= 0 else -math.log10(last / first)
        target = cht.residual_target(k.split(":")[-1])
        print(f"  {k:<16} {first:>11.3e} -> {last:>11.3e}   {decades:>5.1f} decades"
              f"   target {target:.0e}  {'met' if last <= target else 'NOT MET'}")
    print(f"{chr(10)}temperature   {rep.t_min} K to {rep.t_max} K "
          f"(coolant enters at {spec.inlet_temperature} K)")
    if rep.bulk_t_in is not None and rep.bulk_t_out is not None:
        print(f"coolant rise  {rep.bulk_t_out - rep.bulk_t_in:.2f} K bulk, "
              f"{rep.bulk_t_in:.2f} -> {rep.bulk_t_out:.2f} K")
    print(f"mass          {rep.mass_in} kg/s in, {rep.mass_out} out  -> "
          f"{'balances' if rep.mass_closes else 'DOES NOT BALANCE'}")
    print(f"port area     inlet {rep.inlet_area} m2, outlet {rep.outlet_area} m2")
    print(f"{chr(10)}heat, four readings of the same watts")
    for label, value in (("in at the heated face", rep.q_in),
                         ("out through the solid interface", rep.q_solid),
                         ("in through the fluid interface", rep.q_fluid),
                         ("carried away by the coolant", rep.q_coolant)):
        print(f"  {label:<34} {'n/a' if value is None else format(value, '12.4f')} W")
    if rep.notes:
        print(f"{chr(10)}worth knowing")
        for n in rep.notes:
            print(f"  - {n}")
    if rep.problems:
        print(f"{chr(10)}THE MESH DID NOT HOLD UP")
        for p in rep.problems:
            print(f"  - {p}")
        print(f"{chr(10)}  the tail of {log}:")
        for line in text.splitlines()[-12:]:
            print(f"    {line}")
    else:
        print(f"{chr(10)}the mesh held up: it solves, it conserves mass, and the heat closes")
        print("  that is self-consistency and not accuracy. A mesh of this part 27.5% short of "
              "its CAD closed")
        print("  all four readings to 0.00% too. What moves with resolution is the PORT AREA and "
              "the coolant")
        print("  rise above, so compare those across two resolutions before trusting either.")
    (ws / "solve-report.json").write_text(json.dumps({
        "iterations_run": rep.iterations_run, "converged": rep.converged,
        "exit": rc, "seconds": round(took, 1), "cores": spec.cores,
        "stage_seconds": stage_seconds,
        "inlet": spec.inlet, "outlet": spec.outlet, "heated_face": spec.heated_face,
        "velocity": spec.inlet_velocity, "heat_flux": spec.heat_flux,
        "residuals": {k: list(v) for k, v in rep.residuals.items()},
        "t_min": rep.t_min, "t_max": rep.t_max,
        "bulk_t_in": rep.bulk_t_in, "bulk_t_out": rep.bulk_t_out,
        "mass_in": rep.mass_in, "mass_out": rep.mass_out,
        "inlet_area_m2": rep.inlet_area, "outlet_area_m2": rep.outlet_area,
        "q_in_W": rep.q_in, "q_solid_W": rep.q_solid, "q_fluid_W": rep.q_fluid,
        "q_coolant_W": rep.q_coolant,
        "unmet_residuals": [list(u) for u in rep.unmet],
        "mass_closes": rep.mass_closes,
        "problems": rep.problems, "notes": rep.notes,
    }, indent=2))
    return 1 if (rep.problems or rc != 0) else 0


def cmd_solve(a) -> int:
    return solve_workspace(Path(a.run_dir), a)


def cmd_check(a) -> int:
    part = pick(a.cad, a.part)
    if (rc := cmd_mesh(a)) != 0:
        print(f"{chr(10)}not solving: the mesh did not pass its own checks", file=sys.stderr)
        return rc
    return solve_workspace(_workspace(a, part), a)


def cmd_sweep(a) -> int:
    """The same part at several resolutions, so the cost of coarseness is measured, not asserted.

    This is the experiment the staircase table came from, run end to end: at each resolution, how
    much coolant volume the mesh loses, and then whether the solve on it still closes its energy
    balance. The second half is the one nobody usually does, and it is the one that matters, because
    heat crosses AREA and the staircase inflates area while it eats volume.
    """
    part = pick(a.cad, a.part)
    rows = []
    for across in a.across_list:
        print("=" * 100)
        print(f"{part.name} at {across} cells across")
        print("=" * 100)
        a.across = across
        ws = _workspace(a, part)
        mesh_rc = cmd_mesh(a)
        row = {"across": across, "mesh_rc": mesh_rc}
        if (p := ws / "mesh-report.json").is_file():
            row.update(json.loads(p.read_text()))
        if mesh_rc == 0 and not a.mesh_only:
            row["solve_rc"] = solve_workspace(ws, a)
            if (p := ws / "solve-report.json").is_file():
                row["solve"] = json.loads(p.read_text())
        rows.append(row)

    print("=" * 100)
    print(f"{part.name}: what resolution buys")
    print("=" * 100)
    print(f"{'across':>7}{'iface um':>10}{'got':>7}{'cells':>11}{'vol err %':>11}"
          f"{'iface faces':>13}{'mesh':>6}{'inlet m2':>12}{'coolant dT':>13}"
          f"{'heat gap %':>12}{'solve':>7}{'iters':>7}")
    for r in rows:
        s = r.get("solve") or {}
        dt = (s.get("bulk_t_out") - s.get("bulk_t_in")
              if s.get("bulk_t_out") is not None and s.get("bulk_t_in") is not None else None)
        gap = None
        if s.get("q_in_W") and s.get("q_coolant_W") is not None and s["q_in_W"]:
            gap = abs(abs(s["q_coolant_W"]) - abs(s["q_in_W"])) / abs(s["q_in_W"]) * 100
        achieved = r.get("cells_across_achieved")
        print(f"{r['across']:>7}{r.get('interface_cell_um', 0):>10.3f}"
              f"{(achieved or 0):>7.1f}"
              f"{(r.get('fluid_cells') or 0) / 1e6:>10.2f}M"
              f"{r.get('fluid_volume_error_pct', float('nan')):>+11.3f}"
              f"{r.get('interface_faces', 0):>13,}"
              f"{('ok' if r['mesh_rc'] == 0 else 'FAIL'):>6}"
              f"{('n/a' if s.get('inlet_area_m2') is None else format(s['inlet_area_m2'], '.3e')):>12}"
              f"{('n/a' if dt is None else format(dt, '.2f') + ' K'):>13}"
              f"{('n/a' if gap is None else format(gap, '.2f')):>12}"
              f"{('ok' if r.get('solve_rc') == 0 else 'FAIL' if 'solve_rc' in r else '-'):>7}"
              f"{(s.get('iterations_run') or '-'):>7}")
    out = RUNS / f"{part.name}-sweep.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"{chr(10)}  written to {out}")
    return 0 if all(r["mesh_rc"] == 0 for r in rows) else 1


#: The quantities worth a convergence band, and where each one comes from.
#:
#: THE PEAK METAL TEMPERATURE IS THE HEADLINE. A chip-cooling answer is a junction temperature, so
#: that is the number a band belongs on. The coolant rise is the second, and the port areas are
#: here because they are the geometric cause: a staircased port carries less coolant, which is how
#: a resolution error turns into a temperature error.
#:
#: The heat carried away is included as a CONTROL. It is set by the boundary condition and not by
#: the mesh, so it ought to come out mesh-independent; a band on it that is not near zero means
#: something other than discretisation is moving between the runs.
QUANTITIES = (
    ("peak temperature", "K", "t_max"),
    ("coolant volume", "mm3", "@fluid_volume_mm3"),
    ("coolant rise", "K", "_coolant_dt"),
    ("inlet area", "m2", "inlet_area_m2"),
    ("outlet area", "m2", "outlet_area_m2"),
    ("heat to coolant", "W", "q_coolant_W"),
)


def _quantity(solve, key, mesh=None):
    # A leading @ means the MESH report rather than the solve report. The coolant's own volume is
    # studied because it is the geometry every other answer rests on, and because how it behaves
    # across resolutions turned out not to be what a smooth staircase curve implies.
    if key.startswith("@"):
        return (mesh or {}).get(key[1:])
    if key == "_coolant_dt":
        a, b = solve.get("bulk_t_in"), solve.get("bulk_t_out")
        return (b - a) if (a is not None and b is not None) else None
    return solve.get(key)


def _cell_size_m(mesh):
    """The fluid region's representative cell size, from what the mesh came out at.

    The FLUID's, because every quantity studied here is a fluid quantity; taking it over the whole
    block would average in metal cells of a different size and the refinement ratios would be a
    mixture of two sequences.
    """
    cells = mesh.get("fluid_cells") or 0
    volume_m3 = (mesh.get("fluid_volume_mm3") or 0.0) * 1e-9
    if cells <= 0 or volume_m3 <= 0:
        return 0.0
    return (volume_m3 / cells) ** (1.0 / 3.0)


def _grids_for(runs, key):
    """One Grid per run that actually has this quantity."""
    out = []
    for across, mesh, solve in runs:
        value = _quantity(solve, key, mesh)
        h = _cell_size_m(mesh)
        if value is None or h <= 0:
            continue
        out.append(V.Grid(label=str(across) + " across", cells=mesh.get("fluid_cells") or 0,
                          h=h, value=float(value), converged=bool(solve.get("converged"))))
    return out


def _runs_for(part):
    """Every (cells-across, mesh report, solve report) this part has on disk, coarse to fine."""
    found = []
    for d in sorted(RUNS.glob(part.name + "-*across")):
        mp, sp = d / "mesh-report.json", d / "solve-report.json"
        if not (mp.is_file() and sp.is_file()):
            continue
        mesh = json.loads(mp.read_text())
        solve = json.loads(sp.read_text())
        if mesh.get("part") != part.name or not mesh.get("fluid_cells"):
            continue
        found.append((int(mesh["cells_across_asked"]), mesh, solve))
    return sorted(found, key=lambda t: t[0])


def cmd_converge(a) -> int:
    """What the finest mesh's answer is worth, from the coarser ones.

    Reads runs that already exist rather than making them, so it costs nothing and can be re-run
    after a reader is fixed. `sweep` is what produces the runs.
    """
    part = pick(a.cad, a.part)
    runs = _runs_for(part)
    print("part          " + part.name)
    if not runs:
        print("no solved runs under " + str(RUNS) + "/" + part.name + "-*across. Make some with:",
              file=sys.stderr)
        print("  devtools/semiconductor/run.sh sweep " + part.name + " --across-list 2 4 8",
              file=sys.stderr)
        return 2
    print("runs          " + ", ".join(str(n) + " across" for n, _m, _s in runs))
    header = f"{'resolution':<14}{'fluid cells':>13}{'h um':>9}{'converged':>11}   "
    print(header + "".join(f"{label:>18}" for label, _u, _k in QUANTITIES))
    for across, mesh, solve in runs:
        h = _cell_size_m(mesh) * 1e6
        row = ""
        for _label, _u, k in QUANTITIES:
            v = _quantity(solve, k, mesh)
            row += f"{('n/a' if v is None else format(v, '.6g')):>18}"
        tag = str(across) + " across"
        print(f"  {tag:<12}{mesh.get('fluid_cells') or 0:>13,}{h:>9.2f}"
              f"{('yes' if solve.get('converged') else 'NO'):>11}   {row}")

    print(chr(10) + "grid convergence, Richardson plus GCI on the three finest")
    results = {}
    said = 0
    for label, unit, key in QUANTITIES:
        result = V.analyse(label, _grids_for(runs, key))
        results[key] = result
        print(chr(10) + "  " + label + " [" + unit + "]")
        for g in result.grids:
            print(f"    {g.label:<12} h {g.h * 1e6:>8.2f} um   {g.value:>16.6g}")
        if result.problems:
            for prob in result.problems:
                print("    CANNOT SAY: " + prob)
        else:
            said += 1
            line = "    behaviour " + result.behaviour
            if result.order is not None:
                line += f", observed order {result.order:.3f}"
            print(line)
            if result.extrapolated is not None:
                print(f"    extrapolated to zero cell size   {result.extrapolated:>16.6g}")
            band = result.band
            if result.gci is not None and band is not None:
                print(f"    band on the finest mesh          {result.gci:>15.2%}  "
                      f"[{band[0]:.6g}, {band[1]:.6g}]")
            if result.relative_change is not None:
                print(f"    raw change, two finest           {result.relative_change:>15.2%}"
                      "  (what gets quoted when this is not done)")
        for note in result.notes:
            print("    note: " + note)

    out = RUNS / (part.name + "-convergence.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"part": part.name, "runs": [n for n, _m, _s in runs],
               "safety_factor": V.SAFETY_FACTOR, "quantities": {}}
    for k, r in results.items():
        payload["quantities"][k] = {
            "label": r.quantity, "behaviour": r.behaviour, "order": r.order,
            "extrapolated": r.extrapolated, "gci": r.gci,
            "relative_change": r.relative_change,
            "finest": r.grids[0].value if r.grids else None,
            "band": list(r.band) if r.band else None,
            "grids": [{"label": g.label, "cells": g.cells, "h_um": g.h * 1e6, "value": g.value}
                      for g in r.grids],
            "notes": r.notes, "problems": r.problems,
        }
    out.write_text(json.dumps(payload, indent=2))
    print(chr(10) + "  written to " + str(out))
    return 0 if said else 1


def cmd_import(a) -> int:
    src = Path(a.source)
    if not src.is_dir():
        print(f"no such directory: {src}", file=sys.stderr)
        return 2
    a.cad.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(src.glob("*.stl")):
        dst = a.cad / f.name
        tris = P.scale_stl(f, dst, a.scale)
        lo, hi = P.bounds(P.read_stl(dst))
        why = P.unit_complaint(lo, hi)
        print(f"  {f.name:<46}{tris:>9,} tri  "
              f"{(hi[0] - lo[0]) * 1e3:.3f} x {(hi[1] - lo[1]) * 1e3:.3f} x "
              f"{(hi[2] - lo[2]) * 1e3:.3f} mm" + ("   WRONG UNIT" if why else ""))
        if why:
            # In full, on its own line. It was being cut at the first full stop, which falls inside
            # "1e-3" and inside every decimal in the message, so the one warning whose entire point
            # is to say what to do about it printed as a fragment with the advice removed.
            print(f"      {why}")
        n += 1
    for f in sorted(src.glob("manifest*.json")):
        shutil.copyfile(f, a.cad / f.name)
        print(f"  {f.name}")
    print(f"{chr(10)}  {n} STL(s) scaled by {a.scale} into {a.cad}")
    print("  now run: python devtools/semiconductor/index_parts.py")
    return 0


# #
# the command line
# #
def _add_mesh_args(sp) -> None:
    sp.add_argument("part")
    sp.add_argument("--across", type=int, default=13,
                    help="cells across the narrowest passage (the platform's rule is 13)")
    sp.add_argument("--levels", type=int, default=C.DEFAULT_REFINEMENT_LEVELS,
                    help="octree levels spent at the coolant surface")
    sp.add_argument("--lid", type=float, default=0.0, metavar="MM",
                    help="grow the block's zmax face by this many MILLIMETRES of solid, which "
                         "roofs over channels that are open at the top")
    sp.add_argument("--inlet", default=None, help="block face the coolant enters by")
    sp.add_argument("--outlet", default=None, help="block face it leaves by")
    sp.add_argument("--workspace", default=None)
    sp.add_argument("--fresh", action="store_true", help="delete the workspace first")
    sp.add_argument("--force", action="store_true", help="run even if it will not fit in memory")
    sp.add_argument("--volume-tol", type=float, default=None, metavar="FRACTION",
                    help="how far the meshed coolant may be from the CAD, as a fraction. The "
                         "default is derived from the staircase measurements for the resolution "
                         "asked for; pass this to widen or tighten it deliberately")
    sp.add_argument("--no-ports", action="store_true",
                    help="mesh a part whose coolant is sealed, and skip the port check. The mesh "
                         "is then not solvable and is still worth having as a mesher test")
    sp.add_argument("--dry-run", action="store_true", help="write the dictionaries and stop")


def _add_solve_args(sp) -> None:
    sp.add_argument("--iterations", type=int, default=1500,
                    help="a CAP, not a target: the run stops when its residuals are met, and "
                         "reaching this number instead means it never converged")
    sp.add_argument("--velocity", type=float, default=1.0, metavar="M_S")
    sp.add_argument("--flux", type=float, default=1.0e6, metavar="W_M2",
                    help="heat into the heated face; 1e6 W/m2 is 100 W/cm2")
    sp.add_argument("--heated-face", default="zmin",
                    help="the chip side of the block, where the watts go in")
    sp.add_argument("--chunk", type=int, default=250, metavar="N",
                    help="iterations per chunk; convergence is checked between chunks, so this is "
                         "how often the run gets the chance to stop")
    sp.add_argument("--cores", type=int, default=0, metavar="N",
                    help="MPI ranks for the solve (0 picks min(8, this machine's cores)). The "
                         "solver scales; cfMesh does not, and this does not change the mesher.")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--reuse", action="store_true",
                    help="judge the solve already in the workspace instead of running it again")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="semi", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cad", type=Path, default=CAD)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="every part and what it would cost")
    sp.add_argument("--across", type=int, default=13)
    sp.add_argument("--levels", type=int, default=C.DEFAULT_REFINEMENT_LEVELS)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("ports", help="which faces the coolant reaches, and the flow plan")
    _add_mesh_args(sp)
    sp.set_defaults(fn=cmd_ports)

    sp = sub.add_parser("mesh", help="build the two-region conjugate mesh")
    _add_mesh_args(sp)
    sp.set_defaults(fn=cmd_mesh)

    sp = sub.add_parser("solve", help="run the external solve on a built mesh and judge it")
    sp.add_argument("run_dir")
    sp.add_argument("--inlet", default=None)
    sp.add_argument("--outlet", default=None)
    _add_solve_args(sp)
    sp.set_defaults(fn=cmd_solve)

    sp = sub.add_parser("check", help="mesh, solve, and say whether the mesh held up")
    _add_mesh_args(sp)
    _add_solve_args(sp)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("sweep", help="the same part at several resolutions")
    _add_mesh_args(sp)
    _add_solve_args(sp)
    sp.add_argument("--across-list", type=int, nargs="+", default=[2, 5, 13],
                    dest="across_list", metavar="N")
    sp.add_argument("--mesh-only", action="store_true", help="skip the solves")
    sp.set_defaults(fn=cmd_sweep)

    sp = sub.add_parser("converge",
                        help="what the finest mesh's answer is worth, from the coarser ones")
    sp.add_argument("part")
    sp.set_defaults(fn=cmd_converge)

    sp = sub.add_parser("import", help="bring new CAD in, scaling it to metres")
    sp.add_argument("--from", dest="source", required=True)
    sp.add_argument("--scale", type=float, default=1e-3,
                    help="multiply every coordinate by this; 1e-3 turns mm into m")
    sp.set_defaults(fn=cmd_import)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
