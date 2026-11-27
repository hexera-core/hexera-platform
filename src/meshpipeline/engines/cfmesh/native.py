# Responsibility: Run cartesianMesh (and the native cartesian2DMesh for 2D) and report the result.
# Boundaries: the native seam: it executes the real mesher and returns the shared result contract. It judges nothing.
from __future__ import annotations

import json
import logging
import math
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from meshpipeline.cad.fluid_passages import PASSAGES_FILE
from meshpipeline.engines.cfmesh.foam_exec import (
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    export_volume_vtk,
    scan_case_dicts,
)
from meshpipeline.sandbox.safe_exec import NativeOutcome, describe_native_result, run_guarded

logger = logging.getLogger(__name__)

#: The two cfMesh binaries, chosen by the marker `_configure_external_2d` writes.
CARTESIAN_MESH = "cartesianMesh"
CARTESIAN_2D_MESH = "cartesian2DMesh"

#: Written by `_configure_external_2d`; its presence is what makes a case 2D at run time.
TWO_D_MARKER = ".cartesian2d"

#: The front/back merge is a dictionary rewrite over an existing mesh, not a meshing run.
CREATE_PATCH_TIMEOUT_S = 300

#: A case whose dicts were rejected never reaches an executor. Not a native failure.
RC_DICTS_REJECTED = -2

#: How much of the native log travels back with the result.
LOG_TAIL_LINES = 25

#: The measurement taken beside the mesh; foam_exec.check_mesh reads it before shelling out.
QUALITY_FILE = "mesh_quality.json"

#: Wall seconds per native stage (mesher, checkMesh, passage measure, foamToVTK, total),
#: written beside the mesh and carried in the result as `timing`.
TIMING_FILE = "mesh_timing.json"

#: PASSAGES_FILE (cad/fluid_passages) is written by cfmesh_runner._stage_passages when the
#: confirmed openings lie on several separate passages: every passage after the first is a case
#: directory of its own (its surface only), meshed here with the same meshDict and merged in.
_SURFACE_FILE = re.compile(r'surfaceFile\s+"[^"]*"\s*;')

#: INS-29: how many times a further passage is meshed again, its cells halved each time, when one of its
#: openings came back with no faces - cartesianMesh keeps one closed region, and a passage whose neck is
#: under a cell across meshes as several (abc 63401's passage 2: 3 regions; abc 99768's: 9, 4,350 cells)
PASSAGE_REFINES = 3
#: INS-29: the cells a further passage may reach while it is refined for its openings (8 times per halving)
PASSAGE_REFINE_CELLS = 2_000_000
#: INS-29: the words a run whose opening landed in a dropped region fails with: the same plan run again
#: drops it again, so the ladder does not repeat it (pipeline/engine_fallback)
PORT_REGION_DROPPED = "an opening landed in a piece of its passage the mesher dropped"
#: what the user can change when it happens (product bar: no refusal without a way out)
PORT_REGION_WAY_OUT = ("That opening most likely reaches the rest of its passage only through a gap narrower than "
                       "the cells there: widen or close that gap in the CAD, or ask for a smaller cell size near "
                       "that opening in the notes.")
PASSAGE_PORTS_RULE = "INS-29"
#: INS-35: a passage that lost an opening still brings the openings it kept into the mesh, and the refusal names
#: the passages the run never meshed
PASSAGE_KEPT_RULE = "INS-35"


def _not_meshed(port_lists: list[list[str]], first: int) -> str:
    """INS-35: the passages from number `first` on, which the run stopped before meshing, with their openings -
    so a gate that finds those openings with no faces is read for what it is ('' when there are none)."""
    rest = [(k, ports) for k, ports in enumerate(port_lists, start=1) if k >= first and ports]
    if not rest:
        return ""
    return " Not meshed, as the run stopped here: " + "; ".join(
        f"passage {k} ({', '.join(ports)})" for k, ports in rest) + "."


_CELL_SIZES = re.compile(r"\b(maxCellSize|cellSize|boundaryCellSize)(\s+)([0-9.eE+-]+)(\s*;)")
_N_CELLS = re.compile(rb"nCells:\s*(\d+)")
_N_LISTED = re.compile(rb"^\s*(\d+)\s*\(", re.M)


def ports_without_faces(polymesh: Path, ports: list[str]) -> list[str] | None:
    """INS-29: the `ports` a mesh holds no face of (missing or nFaces 0), or None when its boundary cannot be
    read (nothing judged)."""
    from meshpipeline.engines.layer_census import NotMeasurable, _boundary
    try:
        patches = _boundary(polymesh / "boundary")
    except (OSError, NotMeasurable):
        return None
    return [p for p in ports if int(patches.get(p, (0, 0))[1]) <= 0]


def cells_of(polymesh: Path) -> int | None:
    """The cell count an OpenFOAM owner file's header notes - else about a third of the faces it lists (a hex
    mesh has about three faces per cell; cfMesh writes no note: abc 63401's passage failed unrefined) - or None."""
    try:
        with (polymesh / "owner").open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    m = _N_CELLS.search(head)
    if m:
        return int(m.group(1))
    n = _N_LISTED.search(head.split(b"}", 1)[-1])
    return max(1, int(n.group(1)) // 3) if n else None


#: INS-39: the record of which connected cell region of the merged mesh holds which opening, written beside the
#: mesh of a case meshed as several passages (evidence; the run's words come from it)
PASSAGE_REGIONS_FILE = "passage_regions.json"
PASSAGE_REGIONS_RULE = "INS-39"


def opening_regions(polymesh: Path, ports: list[str]) -> dict | None:
    """INS-39: the connected cell regions of a polyMesh (cells joined through internal faces) and the region each
    of `ports` has its faces in, largest region first: {"cells": [per region], "openings": {port: [regions]}}.
    None when the mesh cannot be read (binary, missing files): nothing is judged then."""
    import numpy as np
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    from meshpipeline.engines.layer_census import NotMeasurable, _boundary
    from meshpipeline.render.face_quality import UnreadableMesh, _read_labels
    try:
        owner = _read_labels(polymesh / "owner")
        neighbour = _read_labels(polymesh / "neighbour")
        patches = _boundary(polymesh / "boundary")
    except (OSError, ValueError, UnreadableMesh, NotMeasurable):
        return None
    if not len(owner):
        return None
    n = int(max(int(owner.max()), int(neighbour.max()) if len(neighbour) else -1)) + 1
    k = len(neighbour)
    graph = coo_matrix((np.ones(k, dtype=np.int8), (owner[:k], neighbour)), shape=(n, n))
    _count, label = connected_components(graph, directed=False)
    sizes = np.bincount(label)
    rank = np.empty(len(sizes), dtype=np.int64)
    rank[np.argsort(-sizes, kind="stable")] = np.arange(len(sizes))
    out: dict = {"cells": sorted((int(s) for s in sizes), reverse=True), "openings": {}}
    for p in ports:
        start, nf = patches.get(p, (0, 0))
        if nf <= 0:
            out["openings"][p] = []
            continue
        regs = np.unique(rank[label[owner[start:start + nf]]])
        out["openings"][p] = [int(r) + 1 for r in regs]
    return out


def regions_judged(read: dict, port_lists: list[list[str]]) -> tuple[str, str]:
    """INS-39: (refusal, account) for a merged multi-passage mesh, from opening_regions. A passage whose openings
    sit in more than one cell region is cut in two inside the mesh, so the flow cannot go from one of its openings
    to another: that is a refusal in plain words. Otherwise the account says which region holds which openings,
    and that the passages lie side by side, not joined (they do not connect in the CAD); a region holding no
    opening is named (cells the flow never reaches)."""
    openings = read.get("openings") or {}
    cells = list(read.get("cells") or [])
    cut = []
    for k, ports in enumerate(port_lists, start=1):
        regs = sorted({r for p in ports for r in openings.get(p, [])})
        if len(regs) > 1:
            where = "; ".join(f"region {r}: " + ", ".join(p for p in ports if r in openings.get(p, []))
                              for r in regs)
            cut.append(f"passage {k} came out as {len(regs)} separate pieces ({where})")
    held = {r for regs in openings.values() for r in regs}
    rows = []
    for r in range(1, len(cells) + 1):
        names = [p for p, regs in openings.items() if r in regs]
        rows.append(f"region {r} ({cells[r - 1]:,} cells): " + (", ".join(names) if names else "no opening"))
    account = (f"the mesh holds {len(cells)} separate fluid volume(s), side by side and not joined - the passages "
               "do not connect in the CAD: " + "; ".join(rows) + ".")
    stray = [r for r in range(1, len(cells) + 1) if r not in held]
    if stray:
        account += (f" {len(stray)} region(s) hold no opening: cells the flow never reaches.")
    if cut:
        return (f"[CFMESH] the merged mesh does not keep each passage in one piece: {'; '.join(cut)}. The flow "
                "cannot pass between those openings in this mesh. " + PORT_REGION_WAY_OUT, account)
    return "", account


def finer_mesh_dict(text: str, factor: float = 0.5) -> str:
    """INS-29: a meshDict with every cell size it sets (background, wall band, local boxes) times `factor`."""
    return _CELL_SIZES.sub(lambda m: f"{m.group(1)}{m.group(2)}{float(m.group(3)) * factor:.6g}{m.group(4)}", text)

_BAD_FACES = re.compile(r"Number of bad faces is\s+(\d+)")
_WRONG_WAY = re.compile(r"(\d+)\s+faces pointing the wrong way")
_UNTANGLED = ("Finished untangling the mesh", "Finished renaming boundary patches")


def untangle_stall(log_tail: str) -> list[str]:
    """The broken cells a cartesianMesh run that ran out of time was still repairing, read from its
    log tail: its last untangling pass still counted bad faces, and it never reported the
    untangling finished. [] for a run that was merely slow (the soccer ball had finished untangling
    and was renaming its patches when its time ran out)."""
    text = str(log_tail or "")
    counts = list(_BAD_FACES.finditer(text))
    if not counts or int(counts[-1].group(1)) <= 0:
        return []
    if any(text.rfind(done) > counts[-1].start() for done in _UNTANGLED):
        return []
    wrong = [int(m.group(1)) for m in _WRONG_WAY.finditer(text)]
    bad = int(counts[-1].group(1))
    return [f"{bad:,} bad faces cfMesh could not untangle"
            + (f", {wrong[-1]:,} of them pointing the wrong way" if wrong else "")]


#: TIME-20: THE MESHER IS STOPPED WHEN IT IS NOT UNTANGLING, read from its own counts, never the clock.
#: cartesianMesh untangles in passes of at most UNTANGLE_PASS_ITERATIONS iterations, printing its bad
#: faces at each. static_mixer_004 (c2_res5t) spent 4,531 s of 5,514 s in 108 such passes; its last
#: rose to 64,438 bad faces and ended at 2,107, and the mesh was refused anyway. A pass is judged once
#: UNTANGLE_FAILED_PASSES earlier passes of the run have ended with bad faces left: at the geometric rate
#: of its last UNTANGLE_RATE_WINDOW iterations, a pass still at UNTANGLE_WATCH_MIN_FACES or more that
#: would need more iterations than it has left is not converging, and the run moves to the next engine.
UNTANGLE_RULE = "TIME-20"
UNTANGLE_PASS_ITERATIONS = 50
UNTANGLE_RATE_WINDOW = 10
UNTANGLE_FAILED_PASSES = 2
UNTANGLE_WATCH_MIN_FACES = 1000
_UNTANGLE_START = "Starting untangling the mesh"
_UNTANGLE_END = "Finished untangling the mesh"
_ITERATION_BAD = re.compile(r"Iteration (\d+)\. Number of bad faces is (\d+)")


def untangle_not_converging(log_text: str) -> str:
    """TIME-20: why the cartesianMesh run whose log reads `log_text` is not untangling ('' while it
    may be). The same log gives the same answer on any machine."""
    failed = 0
    cur: list[int] | None = None
    last_it = -1
    for line in str(log_text or "").splitlines():
        if _UNTANGLE_START in line:
            cur, last_it = [], -1
        elif _UNTANGLE_END in line:
            failed += 1 if cur and cur[-1] > 0 else 0
            cur = None
        elif cur is not None:
            m = _ITERATION_BAD.search(line)
            if m:
                # One untangling holds several loops, each counting its iterations from 0 or 1 again:
                # the rate is read inside the loop running now, never across a loop's restart
                # (static_mixer_004: 109 at one loop's start against 39,402 three iterations into the next).
                it = int(m.group(1))
                if it <= last_it:
                    cur = []
                last_it = it
                cur.append(int(m.group(2)))
    if cur is None or len(cur) <= UNTANGLE_RATE_WINDOW or failed < UNTANGLE_FAILED_PASSES:
        return ""
    now, then = cur[-1], cur[-1 - UNTANGLE_RATE_WINDOW]
    left = UNTANGLE_PASS_ITERATIONS - len(cur)
    if now < UNTANGLE_WATCH_MIN_FACES:
        return ""
    rate = now / then if then > 0 else float("inf")
    need = (UNTANGLE_RATE_WINDOW * math.log(now) / -math.log(rate)) if rate < 1.0 else float("inf")
    if need <= left:
        return ""
    return (f"cfMesh was not untangling the mesh: {failed} untangling passes had already ended with bad faces "
            f"left, and this one was at {now:,} bad faces after {len(cur)} iterations ({then:,} "
            f"{UNTANGLE_RATE_WINDOW} iterations before) - "
            + ("they were not falling" if need == float("inf") else
               f"at that rate it needed about {need:.0f} more iterations, and had {max(left, 0)} left"))


class UntangleWatch:
    """TIME-20 as a run watch: reads the growing log at each look and says why to stop, once."""

    def __init__(self, log: Path):
        self.log = Path(log)
        self.tripped = ""

    def __call__(self) -> str | None:
        if not self.tripped:
            try:
                self.tripped = untangle_not_converging(self.log.read_text(errors="replace"))
            except OSError:
                return None
        return self.tripped or None


def _watches(*watches):
    """One run watch from several: the first that says stop."""
    live = [w for w in watches if w is not None]

    def watch() -> str | None:
        for w in live:
            why = w()
            if why:
                return why
        return None
    return watch


#: What the run keeps back from its cap for the steps after the mesher (passage measure, layer
#: census, foamToVTK), beyond twice the first checkMesh's time: a rescue never eats into them.
POST_MESH_RESERVE_S = 120.0


def as_built_fidelity(ws: Path, *, bashrc: str, flow: str) -> dict | None:
    """TIME-15: the CAD check's own measure (engines/cad_fidelity.measure) of the mesh as it stands in `ws`, its boundary
    written by foamToVTK first (removed again when this wrote it). None when it cannot be measured."""
    from meshpipeline.engines.cad_fidelity import measure
    from meshpipeline.engines.cfmesh.foam_exec import export_volume_vtk
    ws = Path(ws)
    had = (ws / "VTK").exists()
    try:
        export_volume_vtk(ws, bashrc=bashrc)
        if not any(ws.glob("VTK/**/boundary/*.vtp")):
            return None
        return measure(ws, intake_patches=[], flow=flow)
    finally:
        if not had:
            shutil.rmtree(ws / "VTK", ignore_errors=True)


def _fidelity_of(bashrc: str, flow: str):
    """The TIME-15 check the rescue asks for: only an internal flow is held to all of its staged boundary."""
    if flow != "internal":
        return None
    return lambda w: as_built_fidelity(w, bashrc=bashrc, flow=flow)


def _rescued(ws: Path, q: dict, *, bashrc: str, timing: dict, t0: float, timeout: int,
             is_2d: bool) -> dict:
    """The quality record after a near-edge rescue (engines/cfmesh/rescue.py) - `q` unchanged
    when the mesh is not near its validity edge. The rescue runs inside the run's own cap, less
    what the steps after it need, so the run still returns inside the time it was given."""
    from meshpipeline.engines.cfd_requirements import non_ortho_limit
    from meshpipeline.engines.cfmesh.rescue import near_edge, rescue
    limit = non_ortho_limit(ws)          # CFDREQ-5: a tighter non-orthogonality the user asked for, else None
    if not near_edge(q, limit):
        return q
    t = time.monotonic()
    deadline = t0 + float(timeout) - (2.0 * float(timing.get("checkMesh_s") or 0.0)
                                      + POST_MESH_RESERVE_S)
    mesher_s = float(timing.get(f"{CARTESIAN_2D_MESH if is_2d else CARTESIAN_MESH}_s") or 0.0)
    try:
        from meshpipeline.engines.workspace_facts import read_flow_topology
        flow = read_flow_topology(ws)
    except Exception:  # noqa: BLE001 - an unread flow asks for no CAD check first
        flow = ""
    try:
        out = rescue(ws, q, bashrc=bashrc, deadline=deadline, mesher_seconds=mesher_s,
                     check_seconds=float(timing.get("checkMesh_s") or 0.0), is_2d=is_2d, limit=limit,
                     fidelity=_fidelity_of(bashrc, flow))
    except Exception:  # noqa: BLE001 - a second chance must never lose the first mesh
        logger.warning("cfMesh rescue errored; the mesh as built stands", exc_info=True)
        return q
    timing["rescue_s"] = round(time.monotonic() - t, 1)
    return out


def run_cartesian_mesh(workspace, *, context=None, bashrc: str = _DEFAULT_BASHRC,
                       timeout: int = 1800) -> dict:
    reason = scan_case_dicts(workspace)
    if reason:
        return {"rc": RC_DICTS_REJECTED, "timed_out": False, "log_tail": f"REJECTED: {reason}"}
    from meshpipeline.contracts.mesh_execution import run_mesh
    return run_mesh(workspace, engine="cfmesh", timeout=timeout)


#: INS-31: each further passage's own mesh is checked, and rescued at its validity edge, before it is merged
PASSAGE_RESCUE_RULE = "INS-31"


def rescue_passage(sub: Path, *, bashrc: str, deadline: float) -> dict | None:
    """A further passage's own mesh in `sub`, measured with checkMesh and, when it is at its validity edge, given the
    cfMesh rescue (engines/cfmesh/rescue: the best measured candidate is left in `sub`, every opening kept, INS-30)
    before mergeMeshes takes it. Returns the quality record kept (None when it could not be measured: the passage is
    merged as built, as before). `deadline`: the monotonic end of the passages' own guard."""
    from meshpipeline.engines.cfmesh.foam_exec import check_mesh
    from meshpipeline.engines.cfmesh.rescue import near_edge, rescue
    try:
        q = check_mesh(sub, bashrc=bashrc) or None
    except Exception:  # noqa: BLE001 - an unmeasured passage is merged as built
        logger.warning("cfMesh: %s - the passage mesh in %s could not be measured", PASSAGE_RESCUE_RULE, sub,
                       exc_info=True)
        return None
    if not q or not near_edge(q):
        return q
    logger.warning("cfMesh: %s - the passage mesh in %s is at its validity edge (%s): rescued before the merge",
                   PASSAGE_RESCUE_RULE, sub.name, near_edge(q))
    try:
        # TIME-14: a guard trip in here stops this passage's rescue only - the passage is merged as built and the
        # attempt goes on to finish (and have judged) its own mesh
        # TIME-15: a passage (always an internal flow) that misses most of its own surface is merged as built
        return rescue(sub, q, bashrc=bashrc, deadline=deadline, mesher_seconds=0.0, step_only=True,
                      fidelity=_fidelity_of(bashrc, "internal"))
    except Exception:  # noqa: BLE001 - the rescue keeps the best mesh so far in place; the passage is merged with it
        logger.warning("cfMesh: %s - the passage rescue in %s failed", PASSAGE_RESCUE_RULE, sub.name, exc_info=True)
        return q


#: TIME-14: the passages whose own rescue a guard stopped, each with the words the user is told (beside the case)
PASSAGE_TRIPS_FILE = "passage_rescue_trips.json"


def _note_passage_trip(ws: Path, k: int, kept: dict | None) -> None:
    """Record, beside the case, that passage `k`'s own rescue was stopped by its guard (TIME-14) - in words."""
    rec = (kept or {}).get("rescue") if isinstance(kept, dict) else None
    if not isinstance(rec, dict) or not rec.get("guard_step_only"):
        return
    path = ws / PASSAGE_TRIPS_FILE
    try:
        have = json.loads(path.read_text()) if path.exists() else {}
        have = have if isinstance(have, dict) else {}
        have[str(k)] = f"Passage {k}, before it was merged: {rec.get('note') or ''}".strip()
        path.write_text(json.dumps(have))
    except (OSError, ValueError):
        logger.warning("cfMesh: could not record passage %d's stopped rescue", k, exc_info=True)


def passage_trip_words(ws: Path) -> str:
    """The plain words for every passage whose own rescue its guard stopped (TIME-14); '' when none."""
    try:
        have = json.loads((Path(ws) / PASSAGE_TRIPS_FILE).read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(have, dict):
        return ""
    return " ".join(str(have[k]) for k in sorted(have, key=lambda s: int(s) if str(s).isdigit() else 0))


def _with_passage_trips(ws: Path, q: dict) -> dict:
    """`q` with every stopped passage rescue told in the run's rescue note (the note the user hears, TIME-14)."""
    words = passage_trip_words(ws)
    if not words:
        return q
    rec = dict(q.get("rescue") or {}) if isinstance(q.get("rescue"), dict) else {}
    rec["note"] = f"{rec.get('note') or ''} {words}".strip()
    rec["passage_trips"] = True
    return {**q, "rescue": rec}


#: how mesh_passages says a passage was STUCK (no sign of life for the stall window), not slow
PASSAGE_STALLED = "made no progress for the stall window"


def mesh_passages(ws: Path, *, bashrc: str = _DEFAULT_BASHRC, timeout: float = 1800.0) -> tuple[int, bool, str]:
    """THE OTHER PASSAGES of a case whose confirmed openings lie on several that do not connect
    (PASSAGES_FILE). cartesianMesh keeps the largest closed region it finds, so each passage is
    meshed in a case of its own - its surface, the case's own meshDict and controls - and merged
    into the case's mesh (mergeMeshes; patches of one name are one patch). Returns (rc, timed out,
    what went wrong in words, or ""): a passage that does not mesh fails the run, since its
    openings would otherwise come back with no faces."""
    try:
        spec = json.loads((ws / PASSAGES_FILE).read_text())
    except (OSError, ValueError) as exc:
        return 1, False, f"[CFMESH] the passages record could not be read: {exc}"
    deadline = time.monotonic() + float(timeout)
    angle = float(spec.get("feature_angle", 30.0))
    port_lists = [list(p or []) for p in (spec.get("ports") or [])]
    # INS-29: the first passage's openings are on the case's own mesh before anything is merged in: one that
    # landed in a dropped region fails the run here, before the further passages, the rescue and the exports
    lost = ports_without_faces(ws / "constant" / "polyMesh", port_lists[0]) if port_lists else None
    if lost:
        return 1, False, (f"[CFMESH] passage 1: {PORT_REGION_DROPPED} ({', '.join(lost)} has no faces) - the "
                          "same plan drops it again." + _not_meshed(port_lists, 2) + " " + PORT_REGION_WAY_OUT)
    for k, rel in enumerate(spec.get("runs") or [], start=2):
        sub = (ws / str(rel)).resolve()
        if ws.resolve() not in sub.parents or not (sub / "geom.stl").exists():
            return 1, False, f"[CFMESH] passage {k}: its surface is missing ({rel})"
        shutil.rmtree(sub / "system", ignore_errors=True)
        shutil.rmtree(sub / "constant", ignore_errors=True)
        shutil.copytree(ws / "system", sub / "system")
        (sub / "constant").mkdir(exist_ok=True)

        def _left() -> float:
            return max(1.0, deadline - time.monotonic())

        def _run(cmd: str, cwd: Path, log_name: str) -> int:
            from meshpipeline.contracts.native_time import STALL_WINDOW_S
            with (cwd / log_name).open("w") as fh:
                return run_guarded(["bash", "-lc", f"source {bashrc} >/dev/null 2>&1 && {cmd}"], cwd=str(cwd),
                                   env=_foam_env(), stdout=fh, stderr=subprocess.STDOUT, timeout=_left(),
                                   stall_s=STALL_WINDOW_S).returncode

        try:
            from meshpipeline.cad.surface_wrap import WRAP_REQUEST_FILE
            if (ws / WRAP_REQUEST_FILE).exists():
                # LAD-cfd-10: a wrapped skin is meshed with no feature edges but its patch borders
                surface = "geom.stl"
            else:
                surface = "geom.fms" if _run(f"surfaceFeatureEdges -angle {angle:g} geom.stl geom.fms", sub,
                                             "surfaceFeatureEdges.log") == 0 and (sub / "geom.fms").exists() else "geom.stl"
            md = sub / "system" / "meshDict"
            md.write_text(_SURFACE_FILE.sub(f'surfaceFile "{surface}";', md.read_text(), count=1))
            mine = port_lists[k - 1] if len(port_lists) >= k else []
            for refined in range(PASSAGE_REFINES + 1):
                if _run(CARTESIAN_MESH, sub, "cartesianMesh.log") != 0 or not (sub / "constant" / "polyMesh" / "owner").exists():
                    tail = "\n".join((sub / "cartesianMesh.log").read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
                    return 1, False, f"[CFMESH] passage {k} ({', '.join(spec.get('ports', [[]] * k)[k - 1])}) did not mesh:\n{tail}"
                # INS-29: EVERY OPENING OF THE PASSAGE HAS FACES, or the passage is meshed again finer - its own
                # resolution, never the case's cell carried over: cartesianMesh kept one region and the one
                # holding the opening was dropped (a neck under a cell across)
                lost = ports_without_faces(sub / "constant" / "polyMesh", mine)
                if not lost:
                    break
                cells = cells_of(sub / "constant" / "polyMesh")
                if refined == PASSAGE_REFINES or cells is None or 8 * cells > PASSAGE_REFINE_CELLS:
                    # INS-35: the passage's openings that DID keep their faces go into the mesh the gate judges -
                    # never read as zero because another opening of the passage was lost (abc 63401: outlet_2
                    # had 6,392 faces in its passage's mesh, yet read zero beside the lost outlet_3)
                    kept = [p for p in mine if p not in lost]
                    held = ""
                    if kept and cells and _run(f"mergeMeshes -overwrite . {shlex.quote(str(rel))}", ws,
                                               f"mergeMeshes_p{k}.log") == 0:
                        held = f" In the mesh with its faces: {', '.join(kept)}."
                    return 1, False, (f"[CFMESH] passage {k}: {PORT_REGION_DROPPED} ({', '.join(lost)} has no "
                                      f"faces after {refined} finer re-mesh(es), {cells or 0:,} cells) - the same "
                                      "plan drops it again." + held + _not_meshed(port_lists, k + 1) + " "
                                      + PORT_REGION_WAY_OUT)
                logger.info("cfMesh: INS-29 - passage %d: %s has no faces (%s cells): meshed again at half the "
                            "cell size", k, ", ".join(lost), cells)
                md.write_text(finer_mesh_dict(md.read_text()))
                shutil.rmtree(sub / "constant" / "polyMesh", ignore_errors=True)
            # INS-31: the passage's own mesh is measured and, at its validity edge, given the engine's own second
            # chances BEFORE it is merged - merged, its bad cells are the whole mesh's (abc 99768: passage 2 carried
            # the 171-degree, negative-volume cells the merged mesh's rescue then could not clear)
            _note_passage_trip(ws, k, rescue_passage(sub, bashrc=bashrc, deadline=deadline))
            if _run(f"mergeMeshes -overwrite . {shlex.quote(str(rel))}", ws, f"mergeMeshes_p{k}.log") != 0:
                tail = "\n".join((ws / f"mergeMeshes_p{k}.log").read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
                return 1, False, f"[CFMESH] passage {k} could not be merged into the mesh:\n{tail}"
            # INS-29: the merged mesh holds the passage's openings too (abc 99768: merged, yet outlet_3 was no patch
            # of the mesh the rescue then spent 22 minutes on)
            gone = ports_without_faces(ws / "constant" / "polyMesh", mine)
            if gone:
                return 1, False, (f"[CFMESH] passage {k} could not be merged into the mesh: {', '.join(gone)} has "
                                  "no faces in the merged mesh")
        except subprocess.TimeoutExpired as exc:
            if getattr(exc, "stalled", False):
                return -1, True, f"[CFMESH] passage {k} {PASSAGE_STALLED}"
            return -1, True, f"[CFMESH] passage {k} ran out of time"
        # the merged mesh is the case's; the passage's own copy only fills the returned archive
        shutil.rmtree(sub / "constant" / "polyMesh", ignore_errors=True)
    # INS-39: mergeMeshes only puts the passages' meshes side by side - it joins nothing. The merged mesh's cell
    # regions are read, with the openings each holds, so the run says what it delivers (abc 63401: inlet + outlet_1
    # in one volume, outlet_2 + outlet_3 in another 100 mm away - two solids of an assembly) and refuses a mesh
    # that cut one passage in two
    read = opening_regions(ws / "constant" / "polyMesh", [p for ports in port_lists for p in ports])
    if read is not None:
        refusal, account = regions_judged(read, port_lists)
        try:
            (ws / PASSAGE_REGIONS_FILE).write_text(json.dumps(
                {"rule": PASSAGE_REGIONS_RULE, "passages": port_lists, "account": account, "refusal": refusal,
                 **read}, indent=1))
        except OSError:
            pass
        if refusal:
            return 1, False, refusal
        logger.info("cfMesh: %d further passage(s) meshed and placed beside the first (%s): %s",
                    len(spec.get("runs") or []), PASSAGE_REGIONS_RULE, account)
    else:
        logger.info("cfMesh: %d further passage(s) meshed and placed beside the first, not joined (their cell "
                    "regions could not be read)", len(spec.get("runs") or []))
    return 0, False, ""


def _cfmesh_brake(ws: Path, log: Path):
    """The size brake for this case: its run plan's estimated cells as the budget (cfmesh_runner
    RUN_PLAN_FILE); None when the case carries no plan (nothing to measure against)."""
    from meshpipeline.engines.cfmesh.cfmesh_runner import RUN_PLAN_FILE
    from meshpipeline.engines.size_brake import CFMESH_COUNTS, SizeBrake
    try:
        est = int((json.loads((ws / RUN_PLAN_FILE).read_text()) or {}).get("estimated_cells") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return SizeBrake(log, est, counts=CFMESH_COUNTS) if est > 0 else None


def _run_cartesian_mesh_local(workspace, *, bashrc: str = _DEFAULT_BASHRC,
                              timeout: int = 1800) -> dict:
    ws = Path(workspace)
    is_2d = (ws / TWO_D_MARKER).exists()
    binary = CARTESIAN_2D_MESH if is_2d else CARTESIAN_MESH
    log = ws / "cartesianMesh.log"
    cmd = f"source {bashrc} >/dev/null 2>&1 && {binary}"
    # WALL TIME PER STAGE, in the result and beside the mesh. The mesher's own budget bounds
    # only the mesher; what runs after it (checkMesh, the passage measure, foamToVTK) has to
    # be visible in the run's record, or a 13-second cartesianMesh inside an 18-minute
    # execution is invisible (Cloud Run execution f5r99, 2026-09-12).
    timing: dict[str, float] = {}
    t0 = time.monotonic()
    (ws / PASSAGE_TRIPS_FILE).unlink(missing_ok=True)     # this run's stopped passage rescues only (TIME-14)
    # NO GUESSED TIME LIMIT (contracts/native_time): `timeout` is the machine's WALL, and the mesher
    # is WATCHED - stopped only when its log has not grown for the stall window
    from meshpipeline.contracts.native_time import STALL_WINDOW_S
    from meshpipeline.sandbox.safe_exec import MemoryExpired, StalledExpired, StoppedByWatch
    stalled = False
    oom_gib: float | None = None
    # THE SIZE BRAKE (engines/size_brake): cartesianMesh prints the cells it built from its
    # octree before it maps the surface, adds layers and optimises; a count far past the case's own
    # run plan stops it there - deterministic, no clock - and the builder is told by how much
    brake = _cfmesh_brake(ws, log)
    untangle = UntangleWatch(log)       # TIME-20: an untangling that is not converging stops the run
    try:
        with log.open("w") as fh:
            proc = run_guarded(["bash", "-lc", cmd], cwd=str(ws), env=_foam_env(),
                               stdout=fh, stderr=subprocess.STDOUT, timeout=timeout,
                               stall_s=STALL_WINDOW_S, watch=_watches(brake, untangle))
        rc, timed_out = proc.returncode, False
        if rc == 0 and is_2d and (ws / "system" / "createPatchDict").exists():
            with (ws / "createPatch.log").open("w") as fh:
                proc2 = run_guarded(["bash", "-lc",
                                     f"source {bashrc} >/dev/null 2>&1 && createPatch -overwrite"],
                                    cwd=str(ws), env=_foam_env(),
                                    stdout=fh, stderr=subprocess.STDOUT,
                                    timeout=CREATE_PATCH_TIMEOUT_S)
            rc = proc2.returncode
    except StoppedByWatch:
        rc, timed_out = -1, False
    except MemoryExpired as exc:
        rc, timed_out, oom_gib = -1, False, float(exc.used_gib)
    except StalledExpired:
        rc, timed_out, stalled = -1, True, True
    except subprocess.TimeoutExpired:
        rc, timed_out = -1, True
    timing[f"{binary}_s"] = round(time.monotonic() - t0, 1)
    tail = ""
    if log.exists():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:])
    if rc == 0 and not timed_out and not is_2d and (ws / PASSAGES_FILE).exists():
        t_p = time.monotonic()
        rc, timed_out, why = mesh_passages(ws, bashrc=bashrc,
                                           timeout=max(60.0, timeout - (time.monotonic() - t0)))
        timing["passages_s"] = round(time.monotonic() - t_p, 1)
        stalled = stalled or (timed_out and PASSAGE_STALLED in why)
        if why:
            tail = (tail + "\n" + why).strip()
    # The SHARED reading of a native result - signal vs ordinary failure vs timeout - so this
    # bundle does not carry its own interpretation of a negative return code.
    out = describe_native_result(
        returncode=rc, args=["bash", "-lc", cmd], stage=binary, output=tail,
        outcome=NativeOutcome.timed_out if timed_out else None)
    if stalled:
        out["stalled"] = True
    if oom_gib is not None:
        # OUT OF MEMORY, stopped cleanly (sandbox/safe_exec.MemoryExpired): one machine up
        out.update(out_of_memory=True, timed_out=False, used_gib=oom_gib)
    if brake is not None and brake.tripped:
        out["oversize"] = dict(brake.tripped)
    elif untangle.tripped:
        out["untangle_not_converging"] = {"words": untangle.tripped, "rule": UNTANGLE_RULE}
        tail = (tail + "\n[CFMESH] " + untangle.tripped).strip()
        out["log_tail"] = tail
    # QUALITY IS MEASURED HERE, beside the mesh, and travels home with it. finalize and the
    # solvability gate call check_mesh on the worker, which carries no OpenFOAM: on the Cloud
    # Run path every figure came back empty and the reviewer refused a finished, paid-for mesh
    # for "required deterministic evidence missing ('metric:max_non_ortho',)" (HEX-6 pilot,
    # job 90f39982). Same remedy as engines/snappy/native.py: measure where the binary and the
    # bytes both are, write the figures next to the polyMesh, and read them back on the far
    # side (foam_exec.check_mesh prefers the file). The volume VTK the reviewer slices is
    # written by foamToVTK - another binary that exists only here - for the same reason.
    if rc == 0 and not timed_out and (ws / "constant" / "polyMesh" / "owner").exists():
        try:
            t1 = time.monotonic()
            q = check_mesh(ws, bashrc=bashrc)
            timing["checkMesh_s"] = round(time.monotonic() - t1, 1)
            if q:
                q = _with_passage_trips(ws, _rescued(ws, q, bashrc=bashrc, timing=timing, t0=t0,
                                                     timeout=timeout, is_2d=is_2d))
                # the passage measure beside the mesh too: cells across the local passage at
                # every boundary point (flow_gates.resolution_floor holds 12 at the narrowest).
                # It reads the boundary files only and gives up past its own budget.
                try:
                    from meshpipeline.engines.passage import passage_of_polymesh
                    from meshpipeline.engines.workspace_facts import read_flow_topology
                    if read_flow_topology(ws) == "internal":
                        t2 = time.monotonic()
                        q.update(passage_of_polymesh(ws))
                        timing["passage_measure_s"] = round(time.monotonic() - t2, 1)
                except Exception:  # noqa: BLE001 - evidence, not a verdict
                    logger.warning("passage measure after meshing failed; omitted", exc_info=True)
                # the LAYERS cfMesh does not report, counted on the mesh where it was built: the
                # builder's run_mesh result then shows "2.0 of 5 requested", and the finalize step
                # reads the same figures back from the quality file
                try:
                    from meshpipeline.engines.cfmesh.finalize import census_quality
                    from meshpipeline.engines.workspace_facts import contract_patches
                    t4 = time.monotonic()
                    q.update(census_quality(ws, contract_patches(ws)))
                    timing["layer_census_s"] = round(time.monotonic() - t4, 1)
                except Exception:  # noqa: BLE001 - evidence, not a verdict
                    logger.warning("layer census after meshing failed; omitted", exc_info=True)
                out["quality"] = q
                _first = (q.get("rescue") or {}).get("fidelity_first") if isinstance(q.get("rescue"), dict) else None
                if _first:
                    # TIME-15: the mesh as built misses more of the CAD than a rescue can bring back - the builder
                    # ends the attempt with the CAD check's own words (agents/builder/tools/meshing)
                    out["fidelity_first"] = dict(_first)
                (ws / QUALITY_FILE).write_text(json.dumps(q, default=str))
        except Exception:  # noqa: BLE001 - a measurement must never lose a finished mesh
            logger.warning("checkMesh after meshing failed; quality omitted", exc_info=True)
        try:
            t3 = time.monotonic()
            if export_volume_vtk(ws, bashrc=bashrc):
                logger.info("volume VTK exported beside the mesh for review")
            timing["foamToVTK_s"] = round(time.monotonic() - t3, 1)
        except Exception:  # noqa: BLE001 - an export must never lose a finished mesh
            logger.warning("foamToVTK after meshing failed; the reviewer will have no volume "
                           "to slice", exc_info=True)
    timing["total_s"] = round(time.monotonic() - t0, 1)
    out["timing"] = timing
    try:
        (ws / TIMING_FILE).write_text(json.dumps(timing))
    except OSError:
        pass
    logger.info("cfMesh native stages (s): %s", timing)
    return out


__all__ = ["CARTESIAN_2D_MESH", "CARTESIAN_MESH", "CREATE_PATCH_TIMEOUT_S", "LOG_TAIL_LINES",
           "PASSAGES_FILE", "QUALITY_FILE", "mesh_passages", "RC_DICTS_REJECTED", "TWO_D_MARKER", "run_cartesian_mesh",
           "untangle_stall"]
