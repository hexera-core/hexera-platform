# Responsibility: Run OpenFOAM commands for the conjugate mesher with a bounded, secret-scrubbed environment.
# Boundaries: every command is time-bounded, written to a log beside the mesh as it runs, and parse-time code directives are refused before any mesher starts.
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.contracts.native_time import STALL_WINDOW_S
from meshpipeline.render.boundary_quality import CHECKMESH_WRITE_FIELDS
from meshpipeline.sandbox.safe_exec import run_guarded

logger = logging.getLogger(__name__)

_DEFAULT_BASHRC = rtcfg.OPENFOAM_BASHRC   # single source of truth (env-overridable there)

# The pre-execution guard is ONE authoritative implementation (sandbox.foam_case_guard) shared by
# every OpenFOAM-family bundle, so a security check can never drift between engines. It scans BOTH
# system/ and constant/ and refuses every executable/include/library-loading construct (#codeStream,
# #calc, #system, #include*, libs, dynamicCode …). This bundle re-exports it under the name every
# runner + dispatch already call, so there is no second copy of the logic here.
from meshpipeline.sandbox.foam_case_guard import scan_case_dicts  # noqa: F401  (bundle re-export)


def _foam_env() -> dict:
    from meshpipeline.sandbox.safe_exec import scrubbed_subprocess_env
    return scrubbed_subprocess_env()


# #
# checkMesh  (native - quality report + fatal-only verdict)
# #
#: HOW MANY PROCESSES WROTE THIS LOG, and it changes what three of the numbers below MEAN.
#: checkMesh prints its header on every run, serial or not, so this is always answerable.
_NPROCS = re.compile(r"nProcs\s*:\s*(\d+)")

_CM = {
    "cells": re.compile(r"cells:\s+(\d+)"),
    "faces": re.compile(r"faces:\s+(\d+)"),
    "points": re.compile(r"points:\s+(\d+)"),
    "internal_faces": re.compile(r"internal faces:\s+(\d+)"),
    # EVERY CELL TYPE checkMesh counts, not two of them. It prints all seven and this table read
    # hexahedra and polyhedra, so a mesh with prisms in it reported as neither - and "cell types
    # and count per type" is one of the six readings the first customer asked for by name
    # (Corintis, 2026-10-29). The counts cost nothing: they are on lines already in the output of
    # a run that has always been made.
    "hexahedra": re.compile(r"hexahedra:\s+(\d+)"),
    "prisms": re.compile(r"prisms:\s+(\d+)"),
    # The lookbehind matters: checkMesh prints "tet wedges:" two lines below "wedges:", and a
    # bare \bwedges: matches both. It happens to find the right one first today only because of
    # the order they are printed in, which is not a thing to rely on.
    "wedges": re.compile(r"(?<!tet )\bwedges:\s+(\d+)"),
    "pyramids": re.compile(r"pyramids:\s+(\d+)"),
    "tet_wedges": re.compile(r"tet wedges:\s+(\d+)"),
    "tetrahedra": re.compile(r"tetrahedra:\s+(\d+)"),
    "polyhedra": re.compile(r"polyhedra:\s+(\d+)"),
    # THE SIZE RANGE, which is the other reading they asked for. checkMesh prints volumes; a cell
    # SIZE is the cube root of one, and `min_cell_m`/`max_cell_m` below are derived from these
    # rather than guessed from a mean. On a two-scale part the ratio between them IS the story.
    "min_volume": re.compile(r"Min volume = ([0-9.eE+-]*[0-9])"),
    "max_volume": re.compile(r"Max volume = ([0-9.eE+-]*[0-9])"),
    "max_aspect_ratio": re.compile(r"Max aspect ratio = ([0-9.eE+-]*[0-9])"),
    "max_cell_openness": re.compile(r"Max cell openness = ([0-9.eE+-]*[0-9])"),
    # Cells too flat for a solver to integrate over. checkMesh prints this only when it finds
    # some, so an absent pattern means none - the same convention as skew_faces below.
    "small_determinant_cells": re.compile(
        r"small determinant[^\n]*number of cells:\s*(\d+)"),
    # THE OTHER FOUR THE EXTENDED CHECKS PRINT. Patterns taken from the library's own strings
    # rather than from a run, so a mesh that happens not to fail one of them cannot leave the
    # pattern untested against its real wording:
    #     ***Concave cells (using face planes) found, number of cells: 3676061
    #     ***Faces with small interpolation weight (< 0.05) found, number of faces: 6
    #     ***Faces with small volume ratio (< 0.01) found, number of faces: 3
    #     ***Error in face tets: 22 faces with low quality or negative volume decomposition tets.
    # Absent means none, the same convention as the determinant count above. The last one is the
    # only line in either of this plate's two reports that checkMesh calls an Error, and until the
    # flag above went in there was nothing to read it off.
    "concave_cells": re.compile(r"Concave cells[^\n]*number of cells:\s*(\d+)"),
    "low_weight_faces": re.compile(
        r"small interpolation weight[^\n]*number of faces:\s*(\d+)"),
    "low_vol_ratio_faces": re.compile(
        r"small volume ratio[^\n]*number of faces:\s*(\d+)"),
    "low_quality_tet_faces": re.compile(
        r"Error in face tets:\s*(\d+) faces"),
    "max_non_ortho": re.compile(r"non-orthogonality Max:\s+([\d.]+)"),
    "avg_non_ortho": re.compile(r"non-orthogonality Max:\s+[\d.]+\s+average:\s+([\d.eE+-]+)"),
    # The meshed volume, which is how a mesh gets compared with the CAD it came from. A
    # conformal two-region split proves the two sides share a surface and nothing about WHICH
    # cells ended up on which side; the fluid region's volume against the coolant's own is what
    # proves that, and it has to come from the same reader as everything else checkMesh says.
    "total_volume": re.compile(r"Total volume = ([0-9.eE+-]*[0-9])"),
    "max_skewness": re.compile(r"[Mm]ax skewness\s*=\s*([\d.eE+-]+)"),
    # checkMesh prints "N highly skew faces detected" ONLY when some exceed the threshold.
    # The COUNT (not the max) is what tells production-grade from broken: a handful of skewed
    # faces localized at a wing-body junction (out of millions) is normal + solvable; the single
    # worst value is not representative. Absent pattern ⇒ zero skewed faces.
    "skew_faces": re.compile(r"(\d+)\s+highly skew faces"),
}
# "negative cell volume" AND NOT "negative volume", which is what this said. The extended geometry
# checks print "faces with low quality or negative volume decomposition tets" - a poor tet
# decomposition, not a negative cell - and the shorter marker matched it, so turning -allGeometry on
# would have filed a fatal the mesh does not have on the customer plate's metal region. Read off the
# library's own strings in the pinned 2412 image, the three things that mean an actually negative
# cell all carry the longer phrase: "***Zero or negative cell volume detected.", "Zero or negative
# cell volume detected for cell" and ", Number of negative volume cells:".
_FATAL = (("negative cell volume", "negative-volume cells"),
          ("open cell", "open cells"),
          ("incorrectly oriented", "incorrectly oriented faces"),
          # checkMesh: "Total number of faces on empty patches is not divisible by the number of
          # cells in the mesh. Hence this mesh is not 1D or 2D." An `empty` patch on a 3D mesh
          # (a background block's unnamed outside) makes every solver refuse the case.
          ("not 1d or 2d", "empty patches on a 3D mesh"))


#: How long checkMesh may take to MEASURE a mesh, as a multiple of what it took to build it.
#: It is a measurement, not a build, so a fixed ceiling was fine while meshes were small - and at
#: 900 s it is the thing that would throw away a finished mesh that took hours to make, after the
#: mesher had already succeeded. checkMesh walks every cell and every face, so what it costs
#: follows the cell count, not the clock.
CHECK_TIMEOUT_S = 7200.0


#: The sets checkMesh collects a failing check's cells and faces into, and writes with
#: `-writeSets`. Read off the utility's own strings rather than guessed at.
POOR_SETS = (
    "skewFaces", "nonOrthoFaces", "wrongOrientedFaces", "warpedFaces", "concaveFaces",
    "lowWeightFaces", "lowVolRatioFaces", "lowQualityTetFaces", "edgeFaces", "multiZoneFaces",
    "highAspectRatioCells", "underdeterminedCells", "concaveCells", "zipUpCells",
    "twoInternalFacesCells", "multiZoneCells",
)


#: Where the poor-cell surfaces are KEPT, one folder per region. checkMesh writes them wherever
#: `mergeAndWrite` puts them - under postProcessing, by the set's own name and nothing else - so
#: the metal's run writes `skewFaces.vtk` over the coolant's. On a conjugate mesh that is the
#: whole measurement lost: 27,932 of the first customer plate's 28,132 poor cells are in the metal
#: and 200 are in the coolant, and one file called skewFaces cannot be both.
POOR_DIR = "poor_cells"


def collect_poor_sets(workspace, *, region: str = "") -> list[dict]:
    """Move the poor-cell surfaces checkMesh just wrote under `POOR_DIR/<region>/`, and say what
    they are: [{name, path, bytes}], the path relative to the workspace.

    FOUND BY NAME rather than by path, because the path is OpenFOAM's to choose and has moved
    between versions; the names are the utility's own and are what it calls the sets in its own
    output. Moved rather than copied, so a later region's run cannot pick them up again.
    """
    ws = Path(workspace)
    want = {n.lower() for n in POOR_SETS}
    dest = ws / POOR_DIR / (region or "mesh")
    out: list[dict] = []
    for f in sorted([*ws.rglob("*.vtk"), *ws.rglob("*.vtp")]):
        if f.stem.lower() not in want or POOR_DIR in f.parts:
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            # THE RANK IS PART OF THE NAME when there is one. A parallel checkMesh writes
            # `skewFaces.vtk` on EVERY rank that found one, and moving them all to `dest / f.name`
            # keeps whichever happened to be moved last - so seven ranks' worth of poor cells
            # would vanish into a filename collision and the highlight would show one eighth of
            # the problem while reporting the whole count beside it.
            rank = next((p for p in f.parts if p.startswith("processor") and p[9:].isdigit()), "")
            landed = dest / (f"{rank}_{f.name}" if rank else f.name)
            f.replace(landed)
            out.append({"name": f.stem, "path": str(landed.relative_to(ws)),
                        "bytes": int(landed.stat().st_size)})
        except OSError:
            logger.warning("could not keep the poor-cell set %s", f.name, exc_info=True)
    return out


def poor_sets(workspace, *, region: str = "") -> list[dict]:
    """What `collect_poor_sets` has kept for one region, or for every region when none is named."""
    ws = Path(workspace)
    root = ws / POOR_DIR / region if region else ws / POOR_DIR
    if not root.is_dir():
        return []
    out = []
    for f in sorted([*root.rglob("*.vtk"), *root.rglob("*.vtp")]):
        try:
            out.append({"name": f.stem, "path": str(f.relative_to(ws)),
                        "bytes": int(f.stat().st_size)})
        except OSError:
            continue
    return out


#: WHY THE CONJUGATE INTERFACE IS MEASURED AS A PLAIN WALL, on a copy of the boundary file and
#: never on the one the mesh ships with.
#:
#: `splitMeshRegions` couples the two halves with a `mappedWall` pair - the fluid's interface
#: carries `sampleRegion solid` and the solid's carries `sampleRegion fluid` - and that pair IS
#: the conjugate coupling, so it has to stay on the delivered mesh.
#:
#: `checkMesh -region fluid` loads ONE polyMesh. Every geometric check runs and prints, and then
#: the last step asks the mapped patch for its sample mesh - `time().lookupObject<polyMesh>` -
#: which was never registered:
#:
#:     --> FOAM FATAL ERROR: failed lookup of solid (objectRegistry cr_ws_p7bn1361)
#:         available objects of type polyMesh: 1(fluid)
#:
#: It dies AFTER the summary and BEFORE the verdict, which is the worst place it could pick. The
#: log carries every number the parser wants and neither "Mesh OK." nor "Failed N mesh checks",
#: so `mesh_ok` reads False on a mesh that nothing has actually judged, and the three quality
#: fields `-writeFields` was asked for are never written, so `render.boundary_quality` has
#: nothing to colour the viewer with.
#:
#: MEASURED ON JOB b6762d5c, 2026-11-13, the first customer plate to go through the whole product:
#: 326,631,400 cells, the mesher clean at rc=0 and the interface conformal at 21,618,626 faces on
#: both sides, and then the same fatal on BOTH regions - "failed lookup of solid" under
#: -region fluid and "failed lookup of fluid" under -region solid. Both verdicts missing, both
#: sidecars absent (checkMesh wrote none of the three fields), and the two refusals that reached
#: the customer-facing report - "fluid: checkMesh did not report Mesh OK", "solid: checkMesh did
#: not report Mesh OK" - were this, not the mesh.
#:
#: THE RETYPE ITSELF IS MEASURED, not reasoned: on a 33,000-cell two-region reproducer built for
#: this question (2026-11-07), `checkMesh -region` went from rc=1 with no verdict to rc=0 with
#: one, for 2.3% more wall-clock, purely by handing it a boundary file whose interface said
#: `wall`. A control arm on the same reproducer refuted the other candidate explanation at the
#: time - that a region with no cells of its own was what it could not look up.
#:
#: NOTHING GEOMETRIC MOVES. The faces, their order and their count are the file's own; only
#: `type`, `inGroups` and the keys that only a mapped patch may carry are rewritten, so every
#: number checkMesh prints is the number it would have printed. The original text is restored in
#: a `finally` whatever happens, because the coupling is the deliverable.
#:
#: The keys dropped while it is measured. `sampleRegion` is the one that faults; the rest are its
#: companions and mean nothing on a plain wall.
_MAPPED_ONLY_KEYS = ("sampleMode", "sampleRegion", "samplePatch", "coupleGroup",
                     "offsetMode", "offset", "offsets", "distance")


def as_plain_walls(text: str) -> tuple[str, tuple[str, ...]]:
    """`text` with every mapped patch retyped to a plain wall, and the names that were retyped.

    Line-wise and deliberately dumb: the words this touches - `type`, `inGroups` and the keys in
    _MAPPED_ONLY_KEYS - appear nowhere in a polyBoundaryMesh but inside a patch entry, and a
    parser that understood the dictionary would be a second thing that could disagree with
    OpenFOAM about what the file says. Returns the text unchanged, and no names, when there is no
    mapped patch in it.
    """
    out: list[str] = []
    retyped: list[str] = []
    name = ""
    for line in text.splitlines(keepends=True):
        word = line.split()
        head = word[0] if word else ""
        indent = line[:len(line) - len(line.lstrip())]
        if len(word) == 1 and head not in ("{", "}", "(", ")"):
            name = head          # a patch's own name is the only bare word that precedes its "{"
        if head == "type" and len(word) > 1 and word[1].rstrip(";").startswith("mapped"):
            out.append(indent + "type            wall;\n")
            retyped.append(name)
            continue
        if head == "inGroups" and "mappedPatch" in line:
            out.append(indent + "inGroups        1(wall);\n")
            continue
        if head in _MAPPED_ONLY_KEYS:
            continue
        out.append(line)
    return "".join(out), tuple(retyped)


def _boundaries_to_measure(ws: Path, *, region: str, ranks: int) -> list[Path]:
    """Every `boundary` file the measurement about to run will open.

    Serially that is the region's own, and with no region named it is nothing at all: `checkMesh`
    without `-region` reads the pre-split mesh, which has no mapped patch on it to trip over.
    Across ranks it is one per rank and NOT the root - see the call site for why the root is left.
    """
    if not region:
        return []
    if int(ranks) <= 1:
        one = ws / "constant" / region / "polyMesh" / "boundary"
        return [one] if one.is_file() else []
    from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import processor_dirs

    return [p for p in ((d / "constant" / region / "polyMesh" / "boundary")
                        for d in processor_dirs(ws)) if p.is_file()]


def check_mesh(workspace, *, bashrc: str = _DEFAULT_BASHRC, region: str = "",
               timeout_s: float = CHECK_TIMEOUT_S, write_sets: bool = True,
               write_fields: bool = False, ranks: int = 1) -> dict:
    """Measure one region, and across the ranks when that is where the region's cells are.

    `ranks` ABOVE ONE MEANS THERE IS NO SERIAL MESH TO MEASURE. With CONJUGATE_MERGE_REGIONS=0 the
    chain never reconstructs, so `constant/<region>/polyMesh` carries the patch table
    `_publish_decomposed_boundaries` wrote and no cells: a serial `checkMesh -region` there finds
    no mesh at all, and the run would reach the reviewer with no verdict, no `mesh_ok` and none of
    the three fields the viewer's colours are reduced from. The caller passes the rank count only
    for the regions that publisher named, so a merged delivery keeps byte for byte the command it
    was measured with.
    """
    ws = Path(workspace)
    # A measurement taken WHERE THE MESH WAS BUILT wins, because here it may be impossible:
    # on the Cloud Run path the mesh is built in a container that has checkMesh and read back
    # by a worker that does not. native._run_cartesian_mesh_local writes this file next to the
    # polyMesh it measured; it arrives with it (same contract as engines/snappy/foam_exec).
    if not region:
        cached = ws / "mesh_quality.json"
        if cached.is_file():
            try:
                measured = json.loads(cached.read_text())
                if isinstance(measured, dict) and measured:
                    return measured
            except (OSError, ValueError):
                logger.warning("mesh_quality.json unreadable; measuring locally instead")
    reason = scan_case_dicts(ws)
    if reason:
        return {"mesh_ok": False, "fatal": [f"case dicts rejected: {reason}"],
                "skew_faces": 0, "skew_fraction": 0.0}
    _reg = f" -region {region}" if region else ""
    # -writeSets: WHICH CELLS ARE POOR, not how many. Corintis asked for a "highlight of
    # poor-quality cells" (2026-10-29) and the engine could only answer with a count - 28,132 on
    # their plate, 27,932 of them in the metal - which says a customer has a problem without
    # saying where. checkMesh has already collected those cells into sets by the time it prints
    # the count; this is the flag that makes it write them out.
    #
    # IT COSTS NOTHING ON A GOOD MESH. The sets are the cells that FAILED a check, so a mesh that
    # passes writes no files at all (measured on the 8-rank 6,100,416-cell build: "Mesh OK." and
    # nothing written). On a bad one the set is small by definition - 28,132 cells out of 102.6
    # million - so it is drawable at full resolution whatever the mesh's size, which a heatmap
    # over the whole boundary is not.
    sets = " -writeSets vtk" if write_sets else ""
    # -writeFields: THE NUMBER FOR EVERY CELL, not only the worst one. The viewer's per-face
    # colours came from re-deriving these in numpy, which needs the whole mesh in memory - about
    # 25.6 GB for this plate's coolant and 36.3 GB for its metal against a 16 GB worker, which is
    # what render.face_quality's 40 M-face cap is. checkMesh has already walked every cell and
    # every face by the time it prints its summary; this is the flag that makes it write down
    # what it measured, and `render.boundary_quality` reduces that to one value per boundary face.
    #
    # THE NUMBERS ARE THE SAME ONES, which is the point: measured on the delivered coupon
    # 2026-10-30, the fields' own maxima are checkMesh's own summary - nonOrthoAngle 16.88060
    # against "Mesh non-orthogonality Max: 16.8806", skewness 0.69653 against "Max skewness =
    # 0.696529", aspectRatio 1.20951 against "Max aspect ratio = 1.20951".
    #
    # WHAT IT COSTS, measured on that region (5,686,631 cells), two runs each: 42.13 and 42.04 s
    # without, 48.46 and 48.40 s with, so +15.1%. Every verdict line is byte-identical - Mesh OK,
    # all three maxima, the cell count - and the only new output is "Writing ..." lines, so the
    # parser below sees exactly the measurement it saw before. Three fields and not the other
    # twelve: cellVolume and minPyrVolume alone are 68 MB each here and nothing reads them.
    #
    # +15.1% IS AN UPPER BOUND, NOT A CONSTANT, and a small region is where it is worst. On the
    # customer plate's 182 M-cell mesh the same call took 652 s over 75,162,351 cells and 878 s
    # over 107,107,563 - 8.7 and 8.2 us a cell - against the 654.3 s a 66,764,096-cell region of
    # the same part took WITHOUT the fields, 9.8 us a cell. At that size the walk over every cell
    # and face is the cost and writing down what it found is lost in it.
    fields = f" -writeFields '{CHECKMESH_WRITE_FIELDS}'" if write_fields else ""
    # -allGeometry: THE CHECKS THE PARSER ALREADY HAS PATTERNS FOR. Without it checkMesh runs
    # closedness, face area, cell volume, non-orthogonality, pyramids, skewness and coupled point
    # location, and stops; the extended geometry checks - cell determinant, concave cells, face
    # interpolation weights, volume ratio and the face tet decomposition - are behind this flag.
    #
    # MEASURED on the customer plate's coolant region, same mesh and same binary, within the hour:
    # without the flag "Mesh OK." and zero poor-cell sets written; with it, 152,556 cells of small
    # determinant and 944,302 concave cells. On the metal region it adds three more, one of them
    # labelled Error: 22 faces with a low quality or negative volume tet decomposition, 6 faces of
    # small interpolation weight and 3 of small volume ratio. So on this mesh the flag is the
    # difference between every failure and none of them, and the engine was reporting Mesh OK.
    #
    # It is also why the highlight was empty. Five of POOR_SETS' sixteen names - concaveCells,
    # underdeterminedCells, lowWeightFaces, lowVolRatioFaces, lowQualityTetFaces - are filled only
    # by these checks, so `-writeSets vtk` had nothing to write and `_poor_surfaces` nothing to
    # draw on a region where 944,302 cells had failed one.
    #
    # +32% on that region (767 s to 1,011 s). NOT -allTopology, which is the one that does not
    # fit: its regionSplit and cell-shape walk over 241,984,911 cells was OOM-killed at 8,631 s
    # against 251 GiB. Its one answer worth having - the piece count per region - is already read
    # by `conjugate.read_region` from the summary line.
    geom = " -allGeometry"
    tool = f"checkMesh -constant{_reg}{geom}{sets}{fields}"
    if int(ranks) > 1:
        # `-noZero` IS NOT DECORATION AND IT IS MEASURED. `preparePar` and
        # `splitMeshRegions -parallel` both leave a `processorN/0`, and a `-parallel` utility given
        # `-constant` walks `Time = constant` and then `Time = 0` - which is how
        # `topoSet -parallel -constant` hung at rc=124 on a 300 s ceiling, twice, until the flag
        # was added (see `conjugate._port_chain`). checkMesh would reach a time with no mesh in it
        # after a measurement that had already succeeded, which is the worst place to fail.
        #
        # ONLY ON THE PARALLEL FORM. The serial command stays exactly the string every corpus
        # measurement was taken with - "+15.1% with the fields and every verdict line
        # byte-identical" is a claim about THAT string.
        from meshpipeline.engines.cfmesh_conjugate.conjugate import mpi_command

        # IMPORTED HERE AND NOT AT THE TOP, because `conjugate` imports this module: the chain
        # knows how to launch ranks and this file knows how to measure a mesh, and the dependency
        # runs that way round. A second spelling of the mpirun flags in this file is the thing
        # that would drift (MPI_BUFFER_SIZE and OMP_NUM_THREADS have to reach every rank's own
        # environment or the launch dies in cfMesh's load balancing).
        cmd = f"source {bashrc} >/dev/null 2>&1 && {mpi_command(tool + ' -noZero', int(ranks))}"
    else:
        cmd = f"source {bashrc} >/dev/null 2>&1 && {tool}"
    # A MEASUREMENT THAT DID NOT FINISH IS NOT A BAD MESH. It used to raise out of here, which
    # discarded a mesh the mesher had already built - hours of work thrown away at the last step by
    # the step that was only ever supposed to describe it. Now it says so, and the manifest records
    # a measurement that was not taken rather than a mesh that was not made.
    #
    # STRAIGHT TO A LOG BESIDE THE MESH, NOT TO A PIPE, and that is the whole of this engine's
    # progress during a window measured at 2,776 s on the Corintis plate (724 s over the coolant's
    # 83 M cells and 2,052 s over the metal's 250 M, from `native._boundary_quality`'s own table).
    # With `capture_output=True` nothing under the workspace moved while checkMesh walked every
    # cell, so `runtime/mesh_runner._output_mark` - which IS what progress means, a `*.log` whose
    # size or mtime moves - saw a run that had stopped. `_beacon` then stopped moving `progress_at`,
    # `cloud_run_client._relay_progress` stopped calling `progress_clock.touch()`, and
    # `pipeline_budget.progress_extension` returned None after PROGRESS_GRACE_S (1,020 s): the
    # lease is lost and the job accounted TIMED OUT under a measurement that was working.
    #
    # THE SHAPE IS `conjugate._foam`'s, not a new one: a handle on a workspace log as `stdout`,
    # `stderr` folded into it, and `run_guarded(stall_s=STALL_WINDOW_S)` on top - which is also how
    # the stall watch resolves the file it reads (`safe_exec._log_of` takes the path off the
    # handle), so a checkMesh that HANGS is still stopped after 900 s of no sign of life instead of
    # sitting on the rest of the run's budget. That is both halves of `contracts/native_time`.
    #
    # And the text is read back off that same file, so the parser below sees exactly what it saw
    # before. On a run that did not finish the partial output is now KEPT rather than lost with the
    # exception, which is the only evidence of where the measurement got to.
    # UNDERSCORE, NOT A DOT, and it is load-bearing: `engines/mesher_stage.read_stage` takes the
    # tool off `newest.stem.split("_")[0]`, so "checkMesh_fluid.log" is checkMesh and
    # "checkMesh.fluid.log" is a tool it has never heard of. See CHECKMESH_LOG.
    suffix = f"_{region}" if region else ""
    log = ws / CHECKMESH_LOG.format(suffix=suffix)
    # THE INTERFACE IS MEASURED AS A WALL - see _MAPPED_ONLY_KEYS above for the fatal this avoids
    # and for why nothing geometric moves. Only when a region is named: `checkMesh` with no
    # -region reads the pre-split mesh, which has no mapped patch on it to trip over.
    # EVERY BOUNDARY FILE THAT WILL BE OPENED, which is not one file on a parallel run. Each
    # rank loads its OWN `processorN/constant/<region>/polyMesh`, and `splitMeshRegions -parallel`
    # wrote the mapped pair on to every one of them - so retyping only the root leaves every rank
    # carrying `sampleRegion` and every rank taking the same fatal after its own summary. That was
    # tried and it does not work.
    #
    # THE ROOT IS LEFT ALONE ON A PARALLEL RUN, deliberately. No rank reads it, and on a
    # decomposed delivery it is the PUBLISHED patch table - the file `open_index` stamps and
    # cross-checks the sidecar against. One fewer file rewritten is one fewer way to leave the
    # deliverable's coupling broken if this process dies between the write and the restore.
    restore: list[tuple[Path, str]] = []
    retyped: tuple[str, ...] = ()
    for target in _boundaries_to_measure(ws, region=region, ranks=int(ranks)):
        try:
            coupled = target.read_text(encoding="utf-8")
            plain, names = as_plain_walls(coupled)
            if not names:
                continue
            target.write_text(plain, encoding="utf-8")
            restore.append((target, coupled))
            retyped = retyped or names
        except OSError:
            logger.warning("checkMesh: could not read %s, so the mesh is measured as it stands "
                           "and a mapped interface on it will take checkMesh down before its "
                           "verdict", target, exc_info=True)
    if retyped:
        logger.info("checkMesh: %s measured with %s as a plain wall across %d boundary file(s) - "
                    "the mapped coupling goes back as soon as it has finished",
                    region or "the mesh", ", ".join(retyped), len(restore))
    try:
        with log.open("w", encoding="utf-8") as fh:
            run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                        stdout=fh, stderr=subprocess.STDOUT, timeout=float(timeout_s),
                        stall_s=STALL_WINDOW_S)
    except Exception as exc:  # noqa: BLE001 - subprocess timeout types vary by caller
        stuck = (f"it showed no sign of life for {STALL_WINDOW_S:.0f} s" if getattr(exc, "stalled", False)
                 else f"it did not finish within {float(timeout_s):.0f} s")
        logger.warning("checkMesh was stopped - %s (%s): the mesh stands and its quality is "
                       "unmeasured; %s has what it printed", stuck, type(exc).__name__, log.name)
        return {"mesh_ok": False, "fatal": ["checkMesh did not finish"],
                "skew_faces": 0, "skew_fraction": 0.0, "unmeasured": True}
    finally:
        # THE COUPLING IS THE DELIVERABLE, so it goes back whatever happened above - a timeout, a
        # stall, a checkMesh that died. A mesh left behind with its interface retyped to a wall
        # is a mesh no conjugate solver can run, broken by the step that only measures.
        for target, coupled in restore:
            try:
                target.write_text(coupled, encoding="utf-8")
            except OSError:
                # SAID AS LOUDLY AS THERE IS ANYWHERE TO SAY IT. A mesh left with its interface
                # retyped to a wall is a mesh no conjugate solver can run, broken by the step
                # that only measures.
                logger.error("checkMesh: COULD NOT PUT THE MAPPED COUPLING BACK on %s - this "
                             "mesh's interface is a plain wall and it must not be delivered",
                             target, exc_info=True)
    q = parse_check_mesh(_read_checkmesh_output(log))
    if retyped:
        # SAID IN THE REPORT, not only in the log: a reader comparing these numbers against a
        # checkMesh they ran by hand needs to know the interface was a wall while they were taken.
        q["checked_as_walls"] = list(retyped)
    if write_sets:
        # RECORDED, not re-derived. The reading already carries the COUNT of poor cells; this is
        # where they are, which is the half a customer can act on.
        found = collect_poor_sets(ws, region=region)
        if found:
            q["poor_sets"] = found
    return q


#: What checkMesh said, kept verbatim beside the mesh it said it about - and now WRITTEN AS IT IS
#: SAID rather than after the fact, because this file is also the run's only sign of life while
#: checkMesh walks the mesh (see check_mesh above).
#:
#: THE REGION IS SEPARATED BY AN UNDERSCORE because this name is now read by two things, not one.
#: `runtime/mesh_runner._output_mark` only needs it to end in ".log" - that is what the job counts
#: as progress - but `engines/mesher_stage.read_stage` ALSO picks the newest ".log" in the
#: workspace and names the tool from `stem.split("_")[0]`, which is how the customer's live line
#: says what the mesher is doing. "checkMesh.fluid.log" gives it the stem "checkMesh.fluid", a
#: tool it has never heard of, so the line would fall back to a bare clock for the 2,776 s this
#: engine's two checkMesh runs take at the plate's size; "checkMesh_fluid.log" is `_TOOLS`'
#: "checkMesh" and the line says "checking the cells".
#:
#: WHAT IS STILL A BARE CLOCK, said plainly: `foamToVTK.log` and the three phase logs
#: `engines/dispatch.watching_progress` writes (review_surface, boundary_index, return_home) are
#: not names `_TOOLS` holds either, so those windows show a clock. Closing that means adding rows
#: to `engines/mesher_stage._TOOLS`, which is not this package's file. The PROGRESS channel is
#: unaffected - `_output_mark` globs "*.log" and does not care what the tool is called.
CHECKMESH_LOG = "checkMesh{suffix}.log"


def _read_checkmesh_output(log: Path) -> str:
    """What checkMesh wrote to its log, for the parser; "" when it cannot be read.

    THE PARSER IS NOT THE MEASUREMENT. checkMesh walks every cell and every face - 204.9 s on the
    coolant and 584.5 s on the metal of the first customer plate - and until now the only thing
    that survived was whatever `_CM` happened to have a regex for. Four of the six readings that
    customer asked for were in this text on every run since the engine was written, and getting
    them meant measuring the mesh again. Keeping the text makes a missing metric a re-parse.

    `errors="replace"`, the same as `safe_exec._captured_watched` reads its own scratch files with:
    an OpenFOAM build can print a byte that is not UTF-8 and a quality report is not lost over it.
    """
    try:
        return log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("could not read back checkMesh's output from %s", log.name, exc_info=True)
        return ""


def parse_check_mesh(out: str) -> dict:
    """Everything checkMesh said, as numbers. Split out from check_mesh so it can be TESTED.

    Reading checkMesh is the one piece of this that every cfMesh path depends on and none of them
    could exercise without starting a subprocess, so the regexes went untested and a second reader
    grew up beside this one in the conjugate path. One reader, callable with a string.
    """
    q: dict = {"mesh_ok": "Mesh OK" in out}
    for k, pat in _CM.items():
        m = pat.search(out)
        if m:
            v = m.group(1)
            q[k] = float(v) if any(c in v for c in ".eE") else int(v)
    low = out.lower()
    q["fatal"] = [lbl for mk, lbl in _FATAL if mk in low]
    # skewness LOCALIZATION: fraction of faces flagged skewed. A production external-aero mesh has
    # a few skewed faces at the wing-body junction (a known limit of octree-hex+prism meshers) and
    # is fully solvable; this fraction - not the single worst value - is the honest quality signal.
    q.setdefault("skew_faces", 0)
    # A PARALLEL RUN'S faces, points AND internal faces ARE SUMS OVER RANKS, NOT TOTALS, and
    # nothing downstream could tell. Each face on a processor boundary was ONE internal face in
    # the whole mesh and becomes a BOUNDARY face on each of the two ranks that share it, so the
    # summed counts move by exactly the processor-face count P in opposite directions:
    # faces = true + P, internal faces = true - P. Measured on one 504,715,948-cell mesh checked
    # both ways, same binary, same mesh:
    #
    #                    serial         8 ranks        difference
    #     fluid faces    263,003,681    263,310,321    +306,640
    #     internal       237,313,600    237,006,960    -306,640
    #     points          98,268,345     98,598,852    +330,507
    #     cells           82,529,515     82,529,515    IDENTICAL
    #
    # `cells` is the one count with no duplication, which is why it is left alone. The three that
    # move are REMOVED rather than corrected, because recovering the true totals needs P, and P is
    # only in the patch table this does not parse - and `read_region` ships `faces` and `points`
    # straight into the customer's quality report, so a number that is wrong by an unknown amount
    # is worse than a number that is absent. A reader that needs them gets a missing key, loudly,
    # instead of a plausible wrong one. The rank-summed values are kept under their own names so
    # nothing is thrown away.
    #
    # UNREACHABLE TODAY, ON PURPOSE. Every checkMesh this engine runs is one process
    # (`check_mesh` builds a bare command, and conjugate.build deletes the per-rank copies before
    # it), so this costs today's runs nothing and is here so the first parallel check cannot
    # quietly publish eight ranks' arithmetic as a mesh measurement.
    # THE FRACTION IS TAKEN FIRST, AND THE ORDER IS THE WHOLE POINT. Computed after the rename
    # below, `q.get("faces")` is gone and every parallel check reports skew_fraction 0.0 - which
    # is not "unknown", it is a customer-facing quality figure saying the mesh has no skew
    # faces. Against the rank sum the denominator is too large by the processor-face count P,
    # which on the measured plate is 306,640 of 263,310,321: the fraction reads 0.12% low. A
    # figure that is 0.12% optimistic is a measurement; 0.0 on a mesh with skew faces is a false
    # claim of a clean mesh.
    q["skew_fraction"] = (q["skew_faces"] / q["faces"]) if q.get("faces") else 0.0
    _np = _NPROCS.search(out)
    q["nprocs"] = int(_np.group(1)) if _np else 1
    if q["nprocs"] > 1:
        for _rk in ("faces", "points", "internal_faces"):
            if _rk in q:
                q[f"{_rk}_rank_summed"] = q.pop(_rk)
    q.setdefault("small_determinant_cells", 0)
    for _k in ("concave_cells", "low_weight_faces", "low_vol_ratio_faces",
               "low_quality_tet_faces"):
        q.setdefault(_k, 0)

    # THE CELL TYPES AS ONE BLOCK, with the kinds that are absent left out rather than written as
    # zeroes. A reader wants "what is this mesh made of"; seven rows of which five say 0 answers
    # it worse than one row saying hexahedra. The total is carried so a reader can see at a glance
    # whether the types account for every cell - they should, and when they do not, something in
    # the mesh is a kind checkMesh did not name.
    kinds = ("hexahedra", "prisms", "wedges", "pyramids", "tet_wedges", "tetrahedra", "polyhedra")
    present = {k: int(q[k]) for k in kinds if int(q.get(k) or 0) > 0}
    if present:
        q["cell_types"] = present
        q["cell_types_total"] = sum(present.values())

    # CELL SIZE AS A LENGTH, because that is what a mesh is judged by and what the budget is set
    # in. checkMesh reports volumes; the cube root is the edge of the equivalent cube. On a graded
    # mesh min and max are the two ends of the grading, and their RATIO is how many octree levels
    # the mesh actually spans - which is the thing a two-scale part is sized by.
    for src, dst in (("min_volume", "min_cell_m"), ("max_volume", "max_cell_m")):
        v = q.get(src)
        if isinstance(v, (int, float)) and v > 0.0:
            q[dst] = float(v) ** (1.0 / 3.0)
    if q.get("min_cell_m") and q.get("max_cell_m"):
        q["cell_size_ratio"] = q["max_cell_m"] / q["min_cell_m"]
    mr = re.search(r"Number of regions:\s*(\d+)", out)
    if mr:
        # Region count is INFORMATION, not a hard gate. A disconnected region is a defect
        # for single-region flow, but LEGITIMATE for multi-region simulations (conjugate
        # heat transfer = solid + fluid regions by design). Whether 2+ regions is wrong
        # depends on the user's request - that's the reviewer's judgment, not a blind gate.
        # The fatal gate stays reserved for universally-invalid defects.
        q["regions"] = int(mr.group(1))
    # AND HOW BIG EACH ONE IS, which checkMesh prints beside the count for nothing extra and four
    # separate records then threw away. The count alone sent a ten-agent investigation at the cell
    # size: the coolant was in six pieces, and three of those pieces are a SINGLE cell, which says
    # on sight that the question is which cells were tagged rather than how big they are.
    # [[project-corintis-island-diagnosis]]
    #
    # BY REGION INDEX, not by where the line appears. The numbers identify region0 as the passage
    # and region5 as the second shell, and an order taken from the text happens to agree today
    # without being the thing being read. `Writing region information to "constant/cellToRegion"`
    # is printed first in the same shape and is not a piece; it has no "with N cells" and so
    # cannot match. Absent when checkMesh printed no sizes, which is every mesh in one body: a
    # reading never taken is empty, not a one-element list of the whole region.
    pc = re.findall(r"Writing region (\d+) with (\d+) cells to cellSet", out)
    if pc:
        q["region_cells"] = tuple(int(n) for _k, n in sorted(pc, key=lambda kv: int(kv[0])))
    mb = re.search(r"Overall domain bounding box \(([-\d.eE+\s]+?)\)\s*\(([-\d.eE+\s]+?)\)", out)
    if mb:
        try:
            mn = [float(x) for x in mb.group(1).split()]
            mx = [float(x) for x in mb.group(2).split()]
            if len(mn) == 3 and len(mx) == 3:
                q["bounds"] = mn + mx  # [xmin,ymin,zmin,xmax,ymax,zmax] of the ACTUAL mesh
        except Exception:
            pass
    return q


#: How long foamToVTK may take to WRITE a mesh out. It was 300 s, a fixed ceiling from when
#: meshes were small, and it is the same mistake CHECK_TIMEOUT_S above already records: foamToVTK
#: walks every cell and writes every one of them to disk, so its cost follows the cell count and
#: not the clock. Measured on the first real customer mesh (102,630,992 cells, 2026-10-28): the
#: VTK directories were created at 21:05:37 and the run gave up at 21:10:38 - 301 s - leaving
#: `VTK/fluid` and `VTK/solid` empty.
#:
#: THAT IS NOT A LOST PICTURE, IT IS A LOST RUN. review_surface.build_region_msh converts this
#: export into the msh that `mesh_paths.surface` names, and that artifact is required=True
#: (_shared._RENDER_ARTIFACTS). No export, no surface, no review, and a mesh that took 1.75 hours
#: to build cannot reach `succeeded` - because a picture of it timed out.
EXPORT_TIMEOUT_S = 7200.0


#: EXCLUDE THE FIELD TIME. The delivered case has two times: `constant`, which is the mesh, and
#: `0`, which solver_case writes with <<HEXERA_FILL_THIS_IN>> where every physical value belongs.
#: foamToVTK's default time range is every time, so `-constant` alone exported the mesh AND tried
#: to export the blank fields - and died on them. Measured in the mesh image, 2026-10-29, on a
#: 2x2x2 blockMesh carrying one volScalarField at time 0:
#:
#:     -constant            readable field    rc 0    tc_0  tc_0.vtm  tc_1  tc_1.vtm
#:     -constant -noZero    readable field    rc 0    tc_0  tc_0.vtm
#:     -constant            BLANK field       rc 1    tc_0  tc_0.vtm  tc_1        <- no .vtm
#:     -constant -noZero    BLANK field       rc 0    tc_0  tc_0.vtm
#:
#: That third row is what a live run left behind: a half-written time directory whose last .vtp
#: stopped mid-base64, and no .vtm beside it. review_surface read it as the newest time, the fluid
#: came back unparseable, the review ran on the solid alone, and the job terminated "delivered
#: with the review inconclusive" on a mesh that had passed every gate. The mesh is in `constant`
#: and a review wants the mesh, so the field time is not exported at all.
NO_FIELD_TIME = "-noZero"
#: Where foamToVTK's own output is written AS IT RUNS - the export's sign of life for the run's
#: progress channel, and the only record of why one that landed nothing landed nothing.
EXPORT_LOG = "foamToVTK.log"
#: foamToVTK writes "<case>_<n>.vtm" beside "<case>_<n>/" only once that time is fully written, so
#: a time directory WITHOUT its .vtm is an export that died partway. This is the general guard
#: behind NO_FIELD_TIME: whatever kills an export mid-write leaves this same signature, and a
#: reviewer has to be able to tell a finished export from a truncated one.
VTM_SUFFIX = ".vtm"


def finished_export(time_dir) -> bool:
    """True when foamToVTK finished writing this time directory - see VTM_SUFFIX."""
    d = Path(time_dir)
    return d.is_dir() and d.with_suffix(VTM_SUFFIX).is_file()


def prefer_finished(time_dirs: list) -> list:
    """The finished exports, or all of them when none is finished.

    A PREFERENCE AND NOT A PRECONDITION, deliberately. The .vtm's job is to break the tie between
    a complete export and a half-written one, which is the case that was actually delivered. Made
    a hard requirement it does the opposite of its purpose: a readable internal.vtu whose marker
    is missing for any reason at all would leave the review with no surface where today it has
    one. Falling back costs nothing, because the per-file read is already inside a try/except and
    a region that ends up with nothing now says so by name.
    """
    finished = [d for d in time_dirs if finished_export(d)]
    return finished or list(time_dirs)


#: WHERE A DECOMPOSED DELIVERY KEEPS ITS CELLS, and the one shape of case the serial export
#: below cannot read. This engine delivers the regions across N ranks with no
#: `reconstructParMesh` - driver.py says so in the run's own log, "the regions will be delivered
#: decomposed across 8 ranks" - so `constant/<region>/polyMesh` carries a patch table and no
#: points, and foamToVTK run over it exits FATAL before it reads a cell:
#:
#:     Cannot find file "points" in directory "fluid/polyMesh" in times "constant"
#:
#: MEASURED, job 110daca9, 2026-11-24, and it is not one missing picture. The export returned
#: None, so `native._review_aids` had nothing to `pv.read`, so not one cut was drawn, so
#: `manifest._regions_published` fell to [] for want of BOTH a volume and a picture, so the
#: reviewer was asked for one region target and the mesh produced none. A 279,281,786-cell mesh
#: that passed all eight gates shipped `review_inconclusive`. One failed export is the review of
#: every decomposed delivery this engine makes, and the big parts are the decomposed ones.
#:
#: NOT `foamToVTK -parallel`, which native.py rules out by name - "every consumer of its output is
#: written for the serial layout". A processorN directory IS a serial case, though: its
#: `constant/<region>/polyMesh` is a complete mesh. So each rank is exported on its own, serially,
#: and the pieces are merged into the single serial-layout file every consumer already expects.
#: Nothing downstream learns that the delivery was decomposed.
#:
#: NOT `reconstructParMesh` either, and that is the dangerous option rather than the slow one: it
#: writes `constant/<region>/polyMesh`, which on a decomposed delivery is the PUBLISHED patch
#: table - the file `open_index` stamps the boundary-index sidecar against, where a 64-byte
#: difference already dropped a coolant once. Rebuilding the deliverable to take a picture of it
#: is the wrong way round.
#:
#: MEASURED TWICE, both on job 110daca9's own delivered ranks. One rank alone, 10,857,613 of the
#: fluid region's 87,194,075 cells: foamToVTK 163 s for a 900,568,786-byte internal.vtu, read back
#: by the home side's own pyvista in 14.0 s at 1.12 GB of peak RSS. Two ranks through THIS
#: function: 451 s, 21,710,181 real cells merged into 574,340,404 bytes at 4.09 GB of peak RSS,
#: and a plane through the result cut 1,170 cells. Eight ranks is about 30 minutes, a 2.3 GB file
#: and about 16 GB of peak RSS - against the 7,200 s EXPORT_TIMEOUT_S and the mesh VM's 512 GiB.
#:
#: PEAK RSS TRACKS CELLS AND NOT BYTES, which is worth saying here because VOLUME_READ_RSS_RATIO
#: is written as a multiple of the file's size. 9.5x there on an 18.8-byte-a-cell uniform grid and
#: 1.24x here on an 83-byte-a-cell export are the same statement said twice: about 100 to 190
#: bytes of RSS per cell. Sizing this pass off the ratio would have priced it at ten times what it
#: costs and skipped it on a machine that fits it easily.
RANK_VTK_DIRNAME = "VTKRANK"

#: WHAT THE MERGED EXPORT IS CALLED. The trailing `_0` is load-bearing and not decoration:
#: `export_volume_vtk.newest` reads the index off `name.rpartition("_")` to sort `_10` after `_9`,
#: and a name without one sorts to -1. Dot-free, because `finished_export` finds the marker beside
#: it with `Path.with_suffix`.
MERGED_TIME_DIRNAME = "decomposed_0"

#: NO FIELDS AND NO BOUNDARY on the per-rank exports, and both are about what the cuts need. The
#: consumer is `native._review_aids`, which reads this file to SLICE it; the boundary of a
#: decomposed delivery reaches the reviewer by another road entirely
#: (`review_surface.build_region_msh` falls back to the boundary index, which is why mesh.msh
#: exists on a run where this export produced nothing at all). Measured above: 83 bytes a cell
#: without them against the 113.5 the plate's own fielded export carried.
RANK_EXPORT_FLAGS = "-no-boundary -no-fields"


def _merged_vtm(time_dir: Path) -> None:
    """foamToVTK's own finished-export marker, written beside a merged time directory.

    `finished_export` reads this file and nothing else to tell a complete export from one that
    died partway - see VTM_SUFFIX, and the live run whose last .vtp stopped mid-base64. A merged
    export has exactly the same failure mode and earns the marker exactly the same way: written
    only once internal.vtu is closed, so `prefer_finished` goes on meaning what it means.
    """
    time_dir.with_suffix(VTM_SUFFIX).write_text(
        '<?xml version="1.0"?>\n'
        '<VTKFile type="vtkMultiBlockDataSet" version="1.0" byte_order="LittleEndian" '
        'header_type="UInt64">\n'
        '  <vtkMultiBlockDataSet>\n'
        f'    <DataSet index="0" name="internal" file="{time_dir.name}/internal.vtu"/>\n'
        '  </vtkMultiBlockDataSet>\n'
        '</VTKFile>\n', encoding="utf-8")


def _volume_from_ranks(ws: Path, ranks: list, region: str, *, bashrc: str,
                       timeout: float) -> str | None:
    """One region's cells out of a decomposed case, as the one file a serial export would leave.

    None on any failure, like the serial path: a review aid may never cost a mesh that is already
    built. Every reason it can return None is logged at ERROR with the rank that caused it,
    because an export that lands nothing is otherwise indistinguishable from one that was never
    attempted - which is exactly how job 110daca9's review was lost without a line about it.
    """
    log = ws / EXPORT_LOG
    started = time.monotonic()
    pieces: list[Path] = []
    # ONE LOG FOR THE WHOLE PASS, appended rank by rank, for the reason the serial export writes
    # to a file at all: `runtime/mesh_runner._output_mark` counts a *.log whose size moves as the
    # run making progress, and eight silent exports in a row is 22 minutes of a dead-looking job.
    with log.open("w", encoding="utf-8") as fh:
        for rank in ranks:
            left = float(timeout) - (time.monotonic() - started)
            if left <= 0.0:
                logger.error("the volume export ran out of its %.0f s before %s; the mesh stands "
                             "and the review will have no interior to cut", float(timeout),
                             rank.name)
                return None
            fh.write(f"\n----- {rank.name} -----\n")
            fh.flush()
            cmd = (f"source {bashrc} >/dev/null 2>&1 && "
                   f"foamToVTK -case '{rank}' -region {region} -constant {NO_FIELD_TIME} "
                   f"{RANK_EXPORT_FLAGS} -overwrite -name {RANK_VTK_DIRNAME}")
            try:
                run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                            stdout=fh, stderr=subprocess.STDOUT, timeout=left,
                            stall_s=STALL_WINDOW_S)
            except Exception as exc:    # noqa: BLE001 - subprocess timeout types vary by caller
                logger.error("foamToVTK stopped on %s (%s); the mesh stands, but the review will "
                             "have no interior to cut and %s has what it printed",
                             rank.name, exc, log.name)
                return None
            got = sorted((rank / RANK_VTK_DIRNAME / region).glob("*/internal.vtu"))
            keep = set(prefer_finished([p.parent for p in got]))
            got = [p for p in got if p.parent in keep]
            if not got:
                logger.error("%s exported no cells for region %r - %s has what foamToVTK printed",
                             rank.name, region, log.name)
                return None
            pieces.append(max(got, key=lambda p: p.stat().st_size))

    out_dir = ws / "VTK" / region / MERGED_TIME_DIRNAME
    try:
        # THROUGH backend's OWN pyvista and not an `import pyvista` here, for the reason
        # native.py states where it draws the cuts: backend pins VTK to Mesa's software
        # rasteriser before pyvista is first imported, and an import sorter would lift a bare
        # `import pyvista` above that line. Reaching through backend makes the order enforceable.
        from meshpipeline.sandbox.backend import pv

        merged = None
        for piece in pieces:
            part = pv.read(str(piece))
            merged = part if merged is None else merged.merge(part)
        if merged is None or not merged.n_cells:
            logger.error("the %d rank exports of region %r merged to nothing", len(pieces), region)
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        merged.save(str(out_dir / "internal.vtu"))
        cells = int(merged.n_cells)
        del merged
    except Exception:                   # noqa: BLE001 - a review aid may never cost a built mesh
        logger.error("the rank exports of region %r could not be merged; the mesh stands and the "
                     "review will have no interior to cut", region, exc_info=True)
        return None
    # ONLY NOW. The marker says the time directory is complete, so it is written after the file is
    # closed and never before - the whole point of VTM_SUFFIX.
    _merged_vtm(out_dir)
    # AND THE PIECES GO. They are a second copy of every cell in the region - 7.2 GB at this
    # plate's size - and `engines/dispatch` hands whatever is left on the workspace to
    # `return_scope`, which compresses every byte of it to decide whether the archive fits.
    for rank in ranks:
        shutil.rmtree(rank / RANK_VTK_DIRNAME, ignore_errors=True)
    logger.info("the volume export merged %d rank(s) of region %r into %d cells",
                len(pieces), region, cells)
    return str((out_dir / "internal.vtu").relative_to(ws))


def export_volume_vtk(workspace, *, all_regions: bool = False, region: str = "",
                      bashrc: str = _DEFAULT_BASHRC,
                      timeout: float = EXPORT_TIMEOUT_S) -> str | None:
    """The cells as VTK, for the reviewer to slice. None on any failure - never a failed mesh.

    `all_regions` exports every region of a split case, and `region` says which one's file to
    return. Without them this exported the case as ONE body and returned
    sorted(glob("VTK/**/internal.vtu"))[0], which on a conjugate case is the merged
    VTK/case_0/internal.vtu - the whole block as a single mesh. That is precisely the thing a
    conjugate review exists to tell a real split apart from, so handing the reviewer the merged
    file would have shown a correct mesh and a wrong one as the same picture. With both exports on
    disk the glob picks the region's only by alphabetical luck, so the region is named rather than
    guessed, in the shape check_mesh in this same file already uses for -region.
    """
    ws = Path(workspace)
    reason = scan_case_dicts(ws)
    if reason:
        logger.warning("export_volume_vtk: %s", reason)
        return None
    # A DECOMPOSED DELIVERY GOES THE OTHER WAY, and the test is the FATAL's own two halves rather
    # than the presence of processor dirs alone: a case can carry both, and the serial export is
    # right whenever the root region mesh has cells in it. See RANK_VTK_DIRNAME for what this
    # cost when it was missing.
    from meshpipeline.engines.cfmesh_conjugate.pinch import processor_dirs

    ranks = processor_dirs(ws)
    # THE MESH THE SERIAL COMMAND BELOW WOULD READ, which is the region's only when one is named.
    root_mesh = (ws / "constant" / region if region else ws / "constant") / "polyMesh"
    if ranks and not (root_mesh / "points").is_file():
        if not region:
            # WHICH REGION'S CELLS cannot be guessed here. The serial command would read the root
            # mesh, which on this path has none, and spend 90 s reaching the same FATAL - measured
            # on job 110daca9's own workspace. Said once, here, instead.
            logger.error("export_volume_vtk: the case is decomposed across %d rank(s) and no "
                         "region was named, so there is no mesh to export", len(ranks))
            return None
        return _volume_from_ranks(ws, ranks, region, bashrc=bashrc, timeout=timeout)
    _all = " -allRegions" if all_regions else ""
    cmd = (f"source {bashrc} >/dev/null 2>&1 && "
           f"foamToVTK -constant {NO_FIELD_TIME} -noFaceZones{_all}")
    # THE SAME WORKSPACE LOG TREATMENT AS checkMesh ABOVE, AND FOR THE SAME REASON. This is the
    # longest window inside the engine - EXPORT_TIMEOUT_S is 7,200 s and the plate's own export is
    # a second copy of 334 M cells - and with `capture_output=True` not one byte of it reached a
    # file `runtime/mesh_runner._output_mark` looks at, so the job stopped counting the run as
    # making progress 1,020 s (PROGRESS_GRACE_S) into an export that was writing gigabytes a
    # minute. `stall_s` on top means an export that HANGS is stopped after 900 s of no sign of
    # life rather than holding what is left of the budget - the two halves of
    # `contracts/native_time`. What it printed is kept beside the mesh either way, which is the
    # only record of why an export that landed nothing landed nothing: the glob below cannot tell
    # ENOSPC from anything else.
    log = ws / EXPORT_LOG
    try:
        with log.open("w", encoding="utf-8") as fh:
            run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                        stdout=fh, stderr=subprocess.STDOUT, timeout=timeout,
                        stall_s=STALL_WINDOW_S)
    except Exception as exc:  # noqa: BLE001 - subprocess timeout types vary by caller
        # LOUD, because this is not only a missing picture. The msh that `mesh_paths.surface`
        # names is converted from what this writes, and that artifact is required - so a quiet
        # warning here surfaces downstream as a review that cannot open, on a mesh that is
        # perfectly good and already paid for. Said plainly so the cause is one search away.
        stuck = (f"showed no sign of life for {STALL_WINDOW_S:.0f} s" if getattr(exc, "stalled", False)
                 else f"did not finish within {float(timeout):.0f} s")
        logger.error("foamToVTK %s: the mesh stands, but the review surface is built from this "
                     "export and will be missing; %s has what it printed", stuck, log.name)
        return None
    # THE NEWEST TIME DIRECTORY, not the first alphabetically. foamToVTK names them
    # "<case>_<n>" and a split case has more than one: measured on a real run, this returned
    # review_0 while the review's own lookup returned review_1, so the two disagreed about which
    # file described the delivered mesh. The index is read numerically so _10 sorts after _9.
    def newest(paths: list) -> str | None:
        # A truncated export is worse than a missing one: it reads as a mesh and is not one.
        keep = set(prefer_finished([p.parent for p in paths]))
        paths = [p for p in paths if p.parent in keep]
        if not paths:
            return None
        def key(p):
            _, _, tail = p.parent.name.rpartition("_")
            try:
                return (int(tail), p.parent.name)
            except ValueError:
                return (-1, p.parent.name)
        return str(max(paths, key=key).relative_to(ws))

    if region:
        return newest(sorted((ws / "VTK" / region).glob("*/internal.vtu")))
    return newest(sorted(ws.glob("VTK/**/internal.vtu")))
