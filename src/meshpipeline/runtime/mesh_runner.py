# Responsibility: Serve the mesh image's entry point: run one mesh and report health.
# Boundaries: the only process that needs the native toolchains.
from __future__ import annotations

import json
import logging
import threading
import time

from fastapi import FastAPI

app = FastAPI(title="mesh-runner")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


#: How often the beacon looks at the mesher's logs and says which stage it is at.
BEACON_EVERY_S = 15.0
#: THE PULSE: the beacon is written again at least this often for as long as this runner lives -
#: while it meshes and while it hands the result back - so the waiting worker can tell a remote that
#: is still there from one that is gone (a VM deleted under it) without a clock. `progress_at` in it
#: says when the mesher's output last MOVED: that, not the pulse, is the job's progress
#: (contracts/progress_clock) - a mesher that is alive but stuck moves nothing, and its own
#: watchdog stops it.
BEACON_PULSE_S = 60.0
#: The stage said while the finished mesh is sent back (an upload of gigabytes takes minutes).
RETURNING = {"key": "returning", "label": "sending the mesh back"}


def _output_mark(ws: str) -> tuple:
    """What moves while the mesher works: the size and time of every log, and of the FEA step file."""
    from pathlib import Path
    root = Path(ws)
    out = []
    for p in sorted([*root.glob("*.log"), *root.glob("*/*.log"), *root.glob("fea_stage.json")]):
        try:
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return tuple(out)


def _life_watch():
    """A watch on this runner's whole process tree (the mesher is its child), or None."""
    try:
        import os

        from meshpipeline.sandbox.liveness import LivenessWatch
        return LivenessWatch(os.getpid(), ())
    except Exception:  # noqa: BLE001 - without it the beacon says output alone, as before
        return None


#: How often the snapshot watcher looks for a phase mesh to have appeared. The phase it waits for
#: takes minutes to write; a look is two stat calls.
SNAPSHOT_EVERY_S = 10.0

#: HOW LONG A FINISHED MESH MAY WAIT FOR A FRAME TO SETTLE, before the frame is abandoned and the
#: result is published anyway.
#:
#: WHY ANY WAIT AT ALL. The worker that collects the result deletes the partial object as part of
#: collecting it (cloud_run_client: one tolerated delete per snappy run). An upload still in flight
#: when the result is published can therefore land AFTER that delete and recreate the object after
#: its own cleanup - and on a supplied exchange bucket, which gets no lifecycle rule, that orphan is
#: permanent. So the snapshot side is settled BEFORE `upload_result`, not left to race it.
#:
#: WHY IT IS SHORT. A frame is never worth delaying a delivered mesh for. The payload is a few
#: megabytes on the same client the beacon uses, so an upload that is going to succeed is done in
#: well under a second; five seconds is an order of magnitude of slack on that, and it is noise
#: beside the result archive's own upload, which takes minutes (gcs_exchange: 300 s of deadline for
#: hundreds of MB). Past it the frame is ABANDONED rather than waited on: the worker's delete may
#: then lose the race, which costs one orphaned object of a few MB, and a mesh waits for nothing.
SNAPSHOT_SETTLE_S = 5.0


def _settle_snapshots(stop, thread, *, timeout_s: float | None = None) -> bool:
    """Stop the snapshot watcher and wait, briefly, for anything in flight to finish.

    True when the snapshot side is quiet (or there never was one); False when a frame was still in
    flight at the bound and has been ABANDONED - said in the log, and the caller publishes anyway.
    Never raises: this sits between a finished mesh and its delivery.
    """
    timeout_s = SNAPSHOT_SETTLE_S if timeout_s is None else timeout_s
    try:
        stop.set()
        if thread is None:
            return True
        thread.join(timeout_s)
        if thread.is_alive():
            logging.getLogger(__name__).warning(
                "progressive snapshots: a frame was still being uploaded %.0fs after the mesh "
                "finished; it is abandoned and the result is published now - the frame's object "
                "may outlive the run's cleanup", timeout_s)
            return False
        return True
    except Exception:  # noqa: BLE001 - the result is the deliverable; nothing here may stop it
        logging.getLogger(__name__).warning(
            "progressive snapshots: the snapshot watcher could not be settled; the result is "
            "published regardless", exc_info=True)
        return False


def _snapshot_watch(ws: str, output_uri: str, stop, partial: dict, *,
                    every_s: float | None = None) -> None:
    """THE MESH ARRIVING. While the mesher works, wait for snappy's castellated phase mesh to be
    written whole, publish ONE decimated boundary surface of it beside the result, put its key in
    `partial` for the beacon to carry, and delete the intermediate again.

    SELF-GATING, and deliberately so. The only way a numbered time directory appears in a workspace
    is a serial snappyHexMesh invoked without `-overwrite`, which happens only when the deployment's
    switch is on (engines/snappy/progressive.snapshots_enabled). A parallel run writes
    `processor*/1/polyMesh` and never `1/polyMesh`, so it never snapshots; a cfMesh, gmsh or vmtk
    run writes no time directory at all. There is no engine name and no rank count to read here.

    Best effort throughout: a frame that cannot be read, decimated or uploaded is dropped with a
    log line. Nothing about a mesh depends on it.
    """
    from meshpipeline.adapters.mesh_execution.gcs_exchange import upload_partial
    from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
    from meshpipeline.engines.snappy import progressive as prog

    every_s = SNAPSHOT_EVERY_S if every_s is None else every_s
    while not stop.wait(every_s):
        try:
            # READ ONCE, THEN GONE: `take_castellated` removes the intermediate as it reads it. A
            # second copy of the mesh in the workspace is what loses a big job on the way home
            # (engines/return_scope), and the frame has nothing more to give.
            payload = prog.take_castellated(ws, units=COMPLETED_MESH_UNIT.value)
        except Exception:  # noqa: BLE001 - a viewer frame is never a reason to lose a mesh
            logging.getLogger(__name__).warning(
                "progressive snapshots: no frame for the castellated phase", exc_info=True)
            return
        if payload is None:
            continue
        try:
            # THE LAST LOOK BEFORE THE WIRE. Reading and decimating a frame takes seconds, and the
            # mesh can finish inside them; an upload started after the result is published can land
            # after the worker has deleted the partial key and recreate the object after its own
            # cleanup (see SNAPSHOT_SETTLE_S). A frame whose moment has passed is simply dropped -
            # the mesh itself is about to be delivered, which is the better picture anyway.
            if stop.is_set():
                logging.getLogger(__name__).info(
                    "progressive snapshots: the mesh finished while the castellated frame was "
                    "being read; the frame is dropped rather than published after the result")
                return
            key = upload_partial(output_uri, prog.CASTELLATED, payload)
            # Set only once the object exists: a pointer the API cannot follow is worse than no
            # pointer. The beacon reads this and carries the KEY, never the geometry.
            partial["partial_key"] = key
            partial["partial_stage"] = prog.CASTELLATED
        except Exception:  # noqa: BLE001 - the frame is a courtesy, like the beacon itself
            logging.getLogger(__name__).warning(
                "progressive snapshots: the castellated frame could not be uploaded", exc_info=True)
        return


def _beacon(ws: str, output_uri: str, stop, *, every_s: float | None = None,
            pulse_s: float | None = None, returning=None, partial=None) -> None:
    """THE MESHER'S STAGE AND ITS PROGRESS, said while the runner lives: every few seconds the newest
    log is read (engines/mesher_stage) and written beside the result for the waiting worker - when the
    stage has moved on, and at least every `pulse_s` regardless (the pulse). `progress_at` is when the
    mesher's output last moved; once `returning` is set (the mesh is being sent back) the stage is
    RETURNING and that counts as moving. Best effort: a write that fails is tried again at the next
    pulse, and the beacon never touches the mesh.

    `partial`: what the snapshot watcher has published, carried as a POINTER - the key of a sibling
    object and the phase it is a frame of, nothing more. The geometry never travels on the beacon:
    this is a small blob on a 30-second timeout, written as a courtesy, and a few megabytes through
    it would make a mesh wait on a viewer frame. A new key is itself a reason to write now."""
    from meshpipeline.adapters.mesh_execution.gcs_exchange import upload_progress
    from meshpipeline.engines.mesher_stage import read_stage

    every_s = BEACON_EVERY_S if every_s is None else every_s
    pulse_s = BEACON_PULSE_S if pulse_s is None else pulse_s
    said = None
    said_partial: str | None = None
    mark, moved_at, sent_at = _output_mark(ws), time.time(), 0.0
    # WORKING, NOT ONLY WRITING (RUN-4, 28 Oct): a mesher can work for an hour without a line of log -
    # gmsh's 3D fill of the 34 M-element cyclone did - and the waiting worker read that as no
    # progress, let the job's deadline pass, lost its lease and threw away 7 hours of meshing. So the
    # beacon also says when this runner's process tree last showed life (CPU, memory, output):
    # `alive_at`. A tree with none is stuck, and both stand still.
    watch = _life_watch()
    alive_at = time.time()
    while not stop.wait(every_s):
        now = time.time()
        handing_back = returning is not None and returning.is_set()
        cur = _output_mark(ws)
        if cur != mark or handing_back:
            mark, moved_at = cur, now
        stage = dict(RETURNING) if handing_back else read_stage(ws)
        new_stage = stage is not None and bool(stage) and stage.get("key") != said
        pointer = dict(partial or {})
        new_partial = bool(pointer.get("partial_key")) and pointer["partial_key"] != said_partial
        if not (new_stage or new_partial or now - sent_at >= pulse_s):
            continue
        try:
            # looked at once per beacon written, not at every glance at the logs: the process tree is
            # read from /proc, and a pulse a minute is all `alive_at` needs
            if handing_back or (watch is not None and watch.poll() is not None):
                alive_at = now
        except Exception:  # noqa: BLE001 - a look that fails says nothing either way
            pass
        try:
            upload_progress(output_uri, {**(stage or {}), **pointer, "at": now,
                                         "progress_at": moved_at, "alive_at": alive_at})
            said = (stage or {}).get("key", said)
            said_partial = pointer.get("partial_key", said_partial)
            sent_at = now
        except Exception:  # noqa: BLE001 - the live line is a courtesy; the next pulse tries again
            sent_at = now


def contract_refusal(contract: str) -> dict | None:
    """The result for a run sent by a worker speaking another exchange contract; None when it may
    run (the same contract, or a worker from before the contract was sent)."""
    from meshpipeline.contracts.mesh_execution import (
        MESH_EXCHANGE_VERSION,
        PLATFORM_UPDATING_KEY,
        PLATFORM_UPDATING_TAG,
        RC_INFRASTRUCTURE,
    )
    asked = str(contract or "").strip()
    if not asked or asked == str(MESH_EXCHANGE_VERSION):
        return None
    return {"rc": RC_INFRASTRUCTURE, "timed_out": False,
            PLATFORM_UPDATING_KEY: {"worker": asked, "mesh_job": str(MESH_EXCHANGE_VERSION)},
            "log_tail": (f"{PLATFORM_UPDATING_TAG} the worker speaks mesh exchange contract {asked} and "
                         f"this mesh job speaks {MESH_EXCHANGE_VERSION}: the platform is mid-update, "
                         "so nothing was meshed")}


def run(input_uri: str, output_uri: str, engine: str, timeout: int, contract: str = "") -> dict:
    from meshpipeline.adapters.mesh_execution.gcs_exchange import download_workspace, upload_result
    from meshpipeline.engines.dispatch import run_engine_local

    refused = contract_refusal(contract)
    if refused is not None:
        # NOT MESHED: the worker that sent this speaks another exchange contract (the platform is
        # mid-update). Said in a result document like any run, so the worker waits and submits
        # again instead of reading a workspace this runner might misread.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="mesh_refused_") as empty:
            upload_result(output_uri, empty, refused)
        return refused
    ws = download_workspace(input_uri)
    stop, returning = threading.Event(), threading.Event()
    # THE SNAPSHOT SIDE STOPS BEFORE THE BEACON DOES, and so has an event of its own: the beacon
    # must go on pulsing through the result's upload (a remote that is still there), while the
    # frame must be settled BEFORE that upload publishes the result (see SNAPSHOT_SETTLE_S).
    snap_stop = threading.Event()
    # What the snapshot watcher has published, read by the beacon. One dict, two threads, two
    # string values: no lock, because the beacon only ever reads a key the watcher has finished
    # writing (it sets them only after the object is up) and a missed look is caught by the next.
    partial: dict = {}
    beacon = threading.Thread(target=_beacon, args=(ws, output_uri, stop),
                              kwargs={"returning": returning, "partial": partial}, daemon=True)
    beacon.start()
    # THE MESH ARRIVING, when the deployment asked for it. With the switch off there is no second
    # thread at all: off is today's run, exactly. With it on the watcher is still self-gating (see
    # _snapshot_watch) - no time directory is written on any path but snappy's serial one.
    import meshpipeline.settings.mesh_snapshot as mscfg
    snapshots = None
    if mscfg.MESH_PROGRESSIVE_SNAPSHOT_ENABLED:
        snapshots = threading.Thread(target=_snapshot_watch,
                                     args=(ws, output_uri, snap_stop, partial), daemon=True)
        snapshots.start()
    try:
        try:
            result = run_engine_local(ws, engine=engine, timeout=int(timeout))
        finally:
            # the mesher is done; the beacon goes on pulsing while the result is sent back, so the
            # waiting worker never takes a long upload for a remote that is gone
            returning.set()
        result.pop("workspace_tar", None)
        # BEFORE THE RESULT IS PUBLISHED, never after: publishing it is what starts the worker's
        # collection, and the collection deletes the frame's object. Bounded at SNAPSHOT_SETTLE_S
        # and abandoned past it - with the flag off there is no thread and this is two statements
        # on an event nobody reads.
        _settle_snapshots(snap_stop, snapshots)
        upload_result(output_uri, ws, result)
    finally:
        snap_stop.set()
        stop.set()
    return result


if __name__ == "__main__":
    from meshpipeline.runtime.mesh_invocation import from_environment
    _inv = from_environment()
    _r = run(_inv.input_uri, _inv.output_uri, _inv.engine, _inv.timeout_seconds, _inv.contract)
    print("mesh complete:", json.dumps({k: v for k, v in _r.items() if k != "log_tail"}))
