# Responsibility: Run OpenFOAM commands for the conjugate mesher with a bounded, secret-scrubbed environment.
# Boundaries: every command is time-bounded, and parse-time code directives are refused before any mesher starts.
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
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
_FATAL = (("negative volume", "negative-volume cells"),
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


def check_mesh(workspace, *, bashrc: str = _DEFAULT_BASHRC, region: str = "",
               timeout_s: float = CHECK_TIMEOUT_S) -> dict:
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
    cmd = f"source {bashrc} >/dev/null 2>&1 && checkMesh -constant{_reg}"
    # A MEASUREMENT THAT DID NOT FINISH IS NOT A BAD MESH. It used to raise out of here, which
    # discarded a mesh the mesher had already built - hours of work thrown away at the last step by
    # the step that was only ever supposed to describe it. Now it says so, and the manifest records
    # a measurement that was not taken rather than a mesh that was not made.
    try:
        proc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                           capture_output=True, text=True, timeout=float(timeout_s))
    except Exception as exc:  # noqa: BLE001 - subprocess timeout types vary by caller
        logger.warning("checkMesh did not finish within %.0f s (%s): the mesh stands and its "
                       "quality is unmeasured", float(timeout_s), type(exc).__name__)
        return {"mesh_ok": False, "fatal": ["checkMesh did not finish"],
                "skew_faces": 0, "skew_fraction": 0.0, "unmeasured": True}
    raw = proc.stdout + proc.stderr
    _keep_checkmesh_output(ws, region, raw)
    return parse_check_mesh(raw)


#: What checkMesh said, kept verbatim beside the mesh it said it about.
CHECKMESH_LOG = "checkMesh{suffix}.log"


def _keep_checkmesh_output(ws: Path, region: str, raw: str) -> None:
    """Write checkMesh's own words next to the mesh, and never fail a run over it.

    THE PARSER IS NOT THE MEASUREMENT. checkMesh walks every cell and every face - 204.9 s on the
    coolant and 584.5 s on the metal of the first customer plate - and until now the only thing
    that survived was whatever `_CM` happened to have a regex for. Four of the six readings that
    customer asked for were in this text on every run since the engine was written, and getting
    them meant measuring the mesh again. Keeping the text makes a missing metric a re-parse.
    """
    try:
        suffix = f".{region}" if region else ""
        (ws / CHECKMESH_LOG.format(suffix=suffix)).write_text(raw, encoding="utf-8")
    except OSError:
        logger.warning("could not keep checkMesh's output beside the mesh", exc_info=True)


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
    cmd = f"source {bashrc} >/dev/null 2>&1 && foamToVTK -constant -noFaceZones{_all}"
    try:
        run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                    capture_output=True, text=True, timeout=timeout)
    except Exception:
        # LOUD, because this is not only a missing picture. The msh that `mesh_paths.surface`
        # names is converted from what this writes, and that artifact is required - so a quiet
        # warning here surfaces downstream as a review that cannot open, on a mesh that is
        # perfectly good and already paid for. Said plainly so the cause is one search away.
        logger.error("foamToVTK did not finish within %.0f s: the mesh stands, but the review "
                     "surface is built from this export and will be missing", float(timeout))
        return None
    # THE NEWEST TIME DIRECTORY, not the first alphabetically. foamToVTK names them
    # "<case>_<n>" and a split case has more than one: measured on a real run, this returned
    # review_0 while the review's own lookup returned review_1, so the two disagreed about which
    # file described the delivered mesh. The index is read numerically so _10 sorts after _9.
    def newest(paths: list) -> str | None:
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
