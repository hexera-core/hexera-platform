# Responsibility: Run OpenFOAM commands for the conjugate mesher with a bounded, secret-scrubbed environment.
# Boundaries: every command is time-bounded, written to a log beside the mesh as it runs, and parse-time code directives are refused before any mesher starts.
from __future__ import annotations

import json
import logging
import re
import subprocess
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
            landed = dest / f.name
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


def check_mesh(workspace, *, bashrc: str = _DEFAULT_BASHRC, region: str = "",
               timeout_s: float = CHECK_TIMEOUT_S, write_sets: bool = True,
               write_fields: bool = False) -> dict:
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
    cmd = (f"source {bashrc} >/dev/null 2>&1 && "
           f"checkMesh -constant{_reg}{geom}{sets}{fields}")
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
    boundary = ws / "constant" / region / "polyMesh" / "boundary" if region else None
    coupled = ""
    retyped: tuple[str, ...] = ()
    if boundary is not None and boundary.is_file():
        try:
            coupled = boundary.read_text(encoding="utf-8")
            plain, retyped = as_plain_walls(coupled)
            if retyped:
                boundary.write_text(plain, encoding="utf-8")
                logger.info("checkMesh: %s measured with %s as a plain wall - the mapped "
                            "coupling goes back as soon as it has finished", region,
                            ", ".join(retyped))
            else:
                coupled = ""
        except OSError:
            logger.warning("checkMesh: could not read %s, so the mesh is measured as it stands "
                           "and a mapped interface on it will take checkMesh down before its "
                           "verdict", boundary, exc_info=True)
            coupled = ""
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
        if coupled:
            boundary.write_text(coupled, encoding="utf-8")
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
    q["skew_fraction"] = (q["skew_faces"] / q["faces"]) if q.get("faces") else 0.0
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
