# Responsibility: Keep snappyHexMesh's castellated phase mesh on a SERIAL run, turn it into one
#                 decimated boundary surface the viewer can draw, and put the final mesh back where
#                 every downstream consumer expects it.
# Owns: the decision not to pass `-overwrite`, the reading of a numbered time directory, the
#       decimation and packing of a partial payload, and the removal of the intermediates.
# Boundaries: geometry and files only. It publishes nothing, uploads nothing and judges no run;
#             the runner's watcher (runtime/mesh_runner) carries what this produces, and
#             settings/mesh_snapshot owns the switch.
# Collaborates with: engines/snappy/native.py (invocation and promotion), engines/snappy/
#                    polymesh_surface.py (the boundary reader), render/skin_mesh.simplified (the
#                    decimation), render/viewer_pack.polymesh_response (the serialisation).
"""THE MESH-SO-FAR, AND WHY IT IS NOT SIMPLY LYING THERE.

snappyHexMesh writes a COMPLETE polyMesh per phase into numbered time directories - `1/`
castellated, `2/` snapped, `3/` layers - unless it is run with `-overwrite`, which this repo does.
With `-overwrite` there is no intermediate at all: each phase lands in `constant/polyMesh`, over
the last. So the only lever for showing a mesh arriving is to drop that flag.

Dropping it has two consequences, and both are handled here.

MID-RUN `constant/polyMesh` IS A TRAP, NOT A PARTIAL. Without `-overwrite` snappy leaves
`constant/polyMesh` exactly as blockMesh wrote it: the background box. A reader that took it for
"the mesh so far" would hand the user a hex box and call it their mesh, which is worse than showing
nothing. Nothing in this module ever reads `constant/polyMesh`; only a numbered time directory is
ever read, and `partial_payload` refuses a path that is not one.

THE FINAL MESH NO LONGER LANDS IN `constant/polyMesh`. It lands in the highest-numbered time
directory, while `constant/polyMesh` still holds the box - and every consumer downstream of the
mesher (parallel_stages.polymesh_complete, checkMesh, foamToVTK, the boundary deliverable, the
manifest) reads `constant/polyMesh`. `promote_final_mesh` moves the last phase's mesh there and
removes the intermediates, so the case the run finishes with is byte-for-byte the shape
`-overwrite` would have left. It is AUTHORITATIVE: when it cannot do that it removes
`constant/polyMesh` outright, so the run fails closed on a missing mesh rather than shipping the
background box.

The intermediates are deleted as soon as they have been read, and in any case before the workspace
is archived. A workspace already brushes the 512 MiB extraction ceiling (engines/return_scope) and
a 1M-cell polyMesh is about 42 MB; a second and third copy of it would lose the mesh on the way
home on exactly the big jobs this feature is for.

THE FLAG ALONE REACHES NOTHING. `snapshots_enabled` needs a SERIAL run, and no deployed
environment is serial by default: the Cloud Run mesh job is 8 vCPU, so `determinism.snappy_ranks`
returns 8, and the big VM runs the same image with no CPU limit at all. So
`MESH_PROGRESSIVE_SNAPSHOT_ENABLED=true` on its own changes nothing anywhere -
`MESH_SNAPPY_RANKS=1` has to be pinned beside it, at the cost of a serial mesh. A run that is
asked for frames and cannot publish them says so once, at info, rather than leaving the silence to
be read as a broken feature (see `snapshots_enabled`).
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: ONE CLAIM ON THE TIME DIRECTORIES AT A TIME. The snapshot runs on the runner's watcher thread
#: and the promotion runs on the thread the engine runs on, in the same process, over the same
#: directories. Without this they race, and the losing case is not a missing frame: it is the
#: watcher deleting the phase directory the promotion was about to move into `constant/polyMesh`.
#: Once a workspace has been promoted no snapshot may touch it again - the mesh is the deliverable
#: and a viewer frame is a courtesy.
#:
#: HELD ONLY OVER THE CLAIM, never over the work. What must be atomic with the promotion is the
#: MOVE of the phase directory out of the promotion's reach (`_claim_frame`); the boundary read and
#: the decimation that follow take seconds and run with nothing held, because `promote_final_mesh`
#: runs the instant snappy returns and a finished mesh must not wait on a viewer frame.
_GUARD = threading.Lock()
_PROMOTED: set[str] = set()
#: Workspaces whose frame has already been read torn once: the next look tries the same frame
#: again, and the log says it once rather than once per poll.
_TORN: set[str] = set()
#: Said once per process: frames were asked for and this run cannot publish them (see
#: `snapshots_enabled`). A per-poll line about a decision that cannot change mid-run is noise.
_SAID_PARALLEL = False
#: WHEN THIS ATTEMPT'S SNAPPY STARTED, stamped by `snappy_command` - the only call this module gets
#: before the run, and one per attempt (the driver re-plans and calls it again). It is what makes a
#: time directory "this attempt's": see `polymesh_complete`.
_ATTEMPT_AT: float | None = None


def _claimed(ws) -> str:
    return str(Path(ws).resolve())


#: WHERE A CLAIMED FRAME IS READ FROM. Not a numbered time directory on purpose: once the frame has
#: been moved in here `time_dirs` cannot see it, so the promotion can neither promote it nor delete
#: it from under a read in flight - which is what lets the decimation run with no lock held. It is
#: removed by `drop_time_dirs` along with the intermediates, so it can never ride home in the
#: archive either. The frame inside keeps snappy's own directory name, because a partial is read
#: only from a numbered time directory's polyMesh (`partial_payload`).
_STAGING = ".snapshot"

#: The phase whose mesh is published. Phase 1 publishes this one frame and no other: a second
#: (post-snapping) frame is a separate decision, not a free extension of this one.
CASTELLATED = "castellated"
#: snappy's own name for that phase's mesh: time directory `1`. It is `1` and not something else
#: because the case this repo writes starts at time zero (engines/snappy/case_scaffold: "startTime
#: 0"), and snappy writes its phases at the next whole times from there.
CASTELLATED_TIME_DIR = "1"
#: A numbered time directory, and nothing else. `constant` and `system` do not match, which is the
#: point: see the module text.
_TIME_DIR = re.compile(r"\A[0-9]+(?:\.[0-9]+)?\Z")
#: The files a polyMesh must have before it is read as one (the same roster parallel_stages holds).
_REQUIRED = ("points", "faces", "owner", "neighbour", "boundary")
#: THE PARTIAL'S TRIANGLE BUDGET. Well under render/skin_mesh.STAGE_MAX_TRIANGLES: this is a frame
#: of a mesh that is still being built, shown while the browser is also drawing the part, and it
#: travels through the exchange on every snapshot. 100k is a shape, not a surface to measure on.
MAX_TRIANGLES = 100_000
#: THE WORK CEILING. The decimator clusters every corner of every triangle, so its cost and its
#: peak memory are the SOURCE size, not the budget - and this runs in the mesher's container,
#: beside a snappy that is still working and is already memory-bounded (native._rank_count). A
#: frame whose boundary is past this is not drawn at all: a snapshot must never be the reason a
#: mesh runs out of memory. A 2M-cell castellated boundary measures a few hundred thousand faces,
#: well inside it.
MAX_SOURCE_TRIANGLES = 3_000_000


def snapshots_enabled(*, nproc: int) -> bool:
    """Whether THIS run keeps its phase meshes: the deployment's switch, AND a SERIAL run.

    The parallel path is excluded here rather than left to fail later. Each rank writes its own
    `processor*/1/polyMesh`, so a frame of the whole mesh would need a `reconstructParMesh` per
    snapshot - minutes of serial CPU charged against the mesh's own timeout, on the largest jobs.

    WHICH MAKES THE SWITCH ALONE INERT, everywhere this is deployed: the mesh job is 8 vCPU and
    `MESH_SNAPPY_RANKS` is blank, so `determinism.snappy_ranks` answers 8. The flag has to be
    paired with `MESH_SNAPPY_RANKS=1`. A run that was asked for frames and is parallel says so
    ONCE, at info, because the alternative is an operator reading the silence as a broken feature.
    """
    global _SAID_PARALLEL
    import meshpipeline.settings.mesh_snapshot as mscfg
    if not mscfg.MESH_PROGRESSIVE_SNAPSHOT_ENABLED:
        return False
    if int(nproc) >= 2:
        _say_parallel(f"this run has {int(nproc)} snappy ranks")
        return False
    return True


def _say_parallel(because: str) -> None:
    """Why a run that was ASKED for frames publishes none - once per process, at info.

    Once: the rank count cannot change mid-run, and the watcher looks every few seconds. Said at
    all because the alternative is an operator who set the flag, saw nothing, and has no way to
    learn from the logs that the flag is not enough on its own.
    """
    global _SAID_PARALLEL
    if _SAID_PARALLEL:
        return
    _SAID_PARALLEL = True
    logger.info("progressive snapshots: asked for, but %s, so NO frame is published - each rank "
                "writes its own processor*/1/polyMesh and a frame of the whole mesh would need a "
                "reconstructParMesh per look. Pin MESH_SNAPPY_RANKS=1 to reach the feature; the "
                "mesh is then serial and slower.", because)


def snappy_command(*, nproc: int) -> str:
    """The serial snappyHexMesh invocation: today's `-overwrite` one unless this run snapshots.

    Also THE ATTEMPT'S CLOCK (`_ATTEMPT_AT`): this is the last thing asked of this module before
    snappy runs, so a time directory older than this call belongs to an earlier attempt and not to
    the mesh being built now.
    """
    global _ATTEMPT_AT
    on = snapshots_enabled(nproc=nproc)
    if on:
        _ATTEMPT_AT = time.time()
    return "snappyHexMesh" if on else "snappyHexMesh -overwrite"


def polymesh_complete(polymesh: Path, *, since: float | None = None) -> bool:
    """Whether `polymesh` is a whole polyMesh - not one snappy is still writing.

    `since`: the attempt's start, when it is known (`_ATTEMPT_AT`). A file older than it is STALE -
    a previous attempt's mesh carried forward in the same workspace - and a stale directory must
    never be promoted as this attempt's deliverable or published as this attempt's frame. The same
    test `parallel_stages.polymesh_complete` makes of `constant/polyMesh`, to the same tolerance.
    """
    for name in _REQUIRED:
        f = polymesh / name
        if not f.is_file():
            return False
        st = f.stat()
        if st.st_size <= 0 or (since is not None and st.st_mtime + 1e-6 < since):
            return False
    return True


def time_dirs(ws) -> list[Path]:
    """The numbered time directories of `ws`, lowest first. Never `constant`, never `system`."""
    return sorted((p for p in Path(ws).iterdir()
                   if p.is_dir() and _TIME_DIR.match(p.name)),
                  key=lambda p: float(p.name))


def castellated_polymesh(ws, *, since: float | None = None) -> Path | None:
    """`1/polyMesh` once it is complete, else None. The ONLY path a partial is ever read from."""
    pm = Path(ws) / CASTELLATED_TIME_DIR / "polyMesh"
    return pm if pm.is_dir() and polymesh_complete(pm, since=since) else None


def drop_time_dirs(ws, *, keep: Path | None = None) -> list[str]:
    """Remove the intermediate meshes (see the module text). Returns the names removed.

    The staging directory goes with them: a frame claimed but never read must not ride home in the
    archive either, and this runs on every exit path the engine has (engines/snappy/native).
    """
    gone = []
    staging = Path(ws) / _STAGING
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
        if not staging.exists():
            gone.append(_STAGING)
    for d in time_dirs(ws):
        if keep is not None and d == Path(keep):
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            gone.append(d.name)
    return gone


def _claim_frame(ws, *, since: float | None) -> Path | None:
    """Under the claim: take the castellated time directory OUT of the promotion's namespace.

    Returns the staged polyMesh to read (`.snapshot/1/polyMesh`), or None. Only this move is
    atomic with `promote_final_mesh`, and it is all that needs to be: the race the lock exists to
    stop is the watcher deleting a directory the promotion is about to MOVE, and once the frame is
    staged the promotion cannot see it at all (`time_dirs` does not match `.snapshot`). The
    promotion's own cleanup does remove the staging, so a read still in flight when the mesh
    finishes may come apart - which costs a frame of a mesh that is already built, and is handled
    like any torn read. A frame left staged by an earlier torn read is handed back again instead
    of a new one being claimed.
    """
    staging = Path(ws) / _STAGING
    held = staging / CASTELLATED_TIME_DIR / "polyMesh"
    with _GUARD:
        if _claimed(ws) in _PROMOTED:
            return None                       # the run has its final mesh; nothing here is ours
        if polymesh_complete(held):
            return held
        pm = castellated_polymesh(ws, since=since)
        if pm is None:
            return None
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pm.parent), str(staging / CASTELLATED_TIME_DIR))
        return held


def take_castellated(ws, *, units: str, max_triangles: int = MAX_TRIANGLES,
                     since: float | None = None) -> dict | None:
    """The castellated frame, and the intermediate gone: the snapshot side's ONE entry point.

    The claim moves the phase directory aside under the lock and the decimation runs outside it
    (see `_claim_frame`); the upload is left to the caller, with the payload already in memory.
    None when there is nothing to take - the ordinary case on every look before the phase is
    written, on every parallel run, and on every engine but snappy.

    IT NEVER RAISES, and it never deletes a frame it failed to read. `mesh_.write()` writes
    straight to its final paths, so each file appears at size 0 and grows, and the all-five-present
    test cannot prove the last one has finished: a read can come apart. The frame then stays
    staged and the next look reads it again, which costs one more poll; deleting it - as this used
    to - ended snapshotting for the whole run over a file that was a second from complete. A read
    that COMPLETED, frame or no frame, takes the directory with it: that is all it had to give.

    `since` is the attempt's start, defaulting to the attempt's own clock (`snappy_command`): a
    time directory older than that belongs to an earlier attempt and is not published as this
    run's frame.
    """
    pm = _claim_frame(ws, since=since if since is not None else _ATTEMPT_AT)
    if pm is None:
        # THE DEPLOYED CASE, said where it is actually reached. The engine only ever asks
        # `snapshots_enabled` on its serial branch (engines/snappy/native), so a parallel run
        # never consults this module at all - but the runner's watcher polls here on every run the
        # switch is on for, and a workspace with `processor*` in it is that run. Nothing is
        # decided here: a parallel workspace has no `1/polyMesh` to claim either way.
        if not _SAID_PARALLEL and any(Path(ws).glob("processor*")):
            _say_parallel("this run is parallel (its mesh is in processor* directories)")
        return None
    try:
        payload = partial_payload(pm, units=units, stage=CASTELLATED, max_triangles=max_triangles)
    except Exception:  # noqa: BLE001 - a viewer frame is never a reason to lose a mesh
        if _claimed(ws) not in _TORN:
            _TORN.add(_claimed(ws))
            logger.info("progressive snapshots: the castellated frame did not read whole; it is "
                        "kept for the next look", exc_info=True)
        return None
    shutil.rmtree(pm.parent.parent, ignore_errors=True)
    return payload


def promote_final_mesh(ws, *, since: float | None = None) -> bool:
    """Put the last phase's mesh in `constant/polyMesh` and remove every intermediate.

    True when `constant/polyMesh` now holds a complete mesh snappy wrote THIS attempt. False -
    having REMOVED `constant/polyMesh` - when no time directory carried one: the alternative is
    leaving blockMesh's background box in place for the gates to read as the delivered mesh.

    `since` defaults to the attempt's own clock (`snappy_command`), so a stale `1/` or `2/` that
    somehow rode into the workspace cannot be promoted as this attempt's deliverable. A caller
    with its own attempt-start (native's `attempt_started`) may pass it instead.
    """
    ws = Path(ws)
    with _GUARD:
        _PROMOTED.add(_claimed(ws))
        return _promote(ws, since=since if since is not None else _ATTEMPT_AT)


def _promote(ws: Path, *, since: float | None = None) -> bool:
    constant = ws / "constant" / "polyMesh"
    final = next((d for d in reversed(time_dirs(ws))
                  if polymesh_complete(d / "polyMesh", since=since)), None)
    if final is None:
        # Fail closed. Everything downstream reads constant/polyMesh, and what is in it right now
        # is the background box: parallel_stages.polymesh_complete would call it complete and
        # fresh, and the run would deliver a hex box as the customer's mesh.
        shutil.rmtree(constant, ignore_errors=True)
        logger.error("progressive snapshots: no phase mesh to promote; constant/polyMesh removed "
                     "so the run fails on a missing mesh rather than on the background box")
        drop_time_dirs(ws)
        return False
    shutil.rmtree(constant, ignore_errors=True)
    constant.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(final / "polyMesh"), str(constant))
    drop_time_dirs(ws)
    logger.info("progressive snapshots: phase mesh %s promoted to constant/polyMesh", final.name)
    return polymesh_complete(constant)


def _triangles(points, polys):
    """The patch's polygons as triangles: a fan per face, which is what the browser draws anyway.

    `polys` is the interleaved `[n, i0 ... in-1]` block polymesh_surface writes, and the points are
    already patch-local.
    """
    import numpy as np

    tris: list = []
    i, n = 0, len(polys)
    while i < n:
        k = int(polys[i])
        if k < 3 or i + 1 + k > n:
            break
        face = polys[i + 1:i + 1 + k]
        for j in range(1, k - 1):
            tris.append((face[0], face[j], face[j + 1]))
        i += 1 + k
    if not tris:
        return None
    return points[np.asarray(tris, dtype=np.int64)]


def _packed(name: str, type_: str, tris) -> dict:
    """One patch, in the shape render/viewer_pack.polymesh_response consumes.

    Welded first (render/skin_mesh's own weld, so a partial and a delivered skin are welded to the
    same tolerance): a triangle soup carries three copies of every shared corner, which would
    triple the largest block in a payload that travels on every snapshot.
    """
    import numpy as np

    from meshpipeline.render.skin_mesh import _weld

    verts, faces = _weld(tris)
    if not len(faces):
        return {}
    polys = np.empty(len(faces) * 4, dtype=np.uint32)
    polys[0::4] = 3
    polys[1::4], polys[2::4], polys[3::4] = faces[:, 0], faces[:, 1], faces[:, 2]
    return {"name": name, "type": type_, "face_count": int(len(faces)),
            "points": verts.astype(np.float32).tobytes(), "polys": polys.tobytes()}


def partial_payload(polymesh, *, units: str, stage: str = CASTELLATED,
                    max_triangles: int = MAX_TRIANGLES) -> dict | None:
    """One decimated boundary surface of the phase mesh at `polymesh`, or None when there is none.

    Refuses anything but a numbered time directory's polyMesh: `constant/polyMesh` mid-run holds
    blockMesh's background box (see the module text), and the cheapest way to make that unreachable
    is to refuse the path rather than to remember not to pass it.
    """
    import numpy as np

    from meshpipeline.engines.snappy.polymesh_surface import boundary_surface
    from meshpipeline.render.skin_mesh import simplified
    from meshpipeline.render.viewer_pack import polymesh_response

    pm = Path(polymesh)
    if pm.name != "polyMesh" or not _TIME_DIR.match(pm.parent.name):
        raise ValueError(
            f"a partial is read only from a numbered time directory's polyMesh, not {pm}: mid-run "
            "constant/polyMesh is blockMesh's background box, not the mesh so far")
    if not polymesh_complete(pm):
        return None

    raw, _stats = boundary_surface(pm)
    if not raw:
        return None
    # The budget is the WHOLE frame's, shared out by how many faces each patch carries, so one
    # large patch cannot spend it all and a small one is not decimated away to nothing.
    total = sum(int(p["face_count"]) for p in raw) or 1
    # A polygon of k corners fans into k-2 triangles; a hex mesh's boundary is quads, so this is
    # the face count for the usual case and more for snappy's polyhedra. Measured BEFORE anything
    # is triangulated, because the ceiling exists to stop the work, not to report on it.
    if total * 2 > MAX_SOURCE_TRIANGLES:
        logger.info("progressive snapshots: %d boundary faces is past the work ceiling; no frame "
                    "for this phase", total)
        return None
    packed: list[dict] = []
    for p in raw:
        points = np.frombuffer(p["points"], dtype=np.float32).reshape(-1, 3)
        polys = np.frombuffer(p["polys"], dtype=np.uint32)
        tris = _triangles(points, polys)
        if tris is None:
            continue
        share = max(1_000, int(max_triangles * int(p["face_count"]) / total))
        one = _packed(p["name"], p["type"], simplified(tris, share))
        if one:
            packed.append(one)
    resp = polymesh_response(packed, None, {}, units)
    if not resp:
        return None
    # NOT A DELIVERED MESH, and the payload says so in its own words rather than leaving a reader
    # to infer it: no manifest, no quality, no roles, no cell count. `is_mesh` false is what keeps
    # it out of everything the viewer does with a delivered unit.
    resp.update({"is_mesh": False, "partial": True, "stage": str(stage)})
    return resp


__all__ = ["CASTELLATED", "CASTELLATED_TIME_DIR", "MAX_TRIANGLES", "castellated_polymesh",
           "drop_time_dirs", "partial_payload", "polymesh_complete", "promote_final_mesh",
           "snappy_command", "snapshots_enabled", "time_dirs"]
