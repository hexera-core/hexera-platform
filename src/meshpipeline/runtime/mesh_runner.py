# Responsibility: Serve the mesh image's entry point: run one mesh and report health.
# Boundaries: the only process that needs the native toolchains.
from __future__ import annotations

import json
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


def _beacon(ws: str, output_uri: str, stop, *, every_s: float | None = None,
            pulse_s: float | None = None, returning=None) -> None:
    """THE MESHER'S STAGE AND ITS PROGRESS, said while the runner lives: every few seconds the newest
    log is read (engines/mesher_stage) and written beside the result for the waiting worker - when the
    stage has moved on, and at least every `pulse_s` regardless (the pulse). `progress_at` is when the
    mesher's output last moved; once `returning` is set (the mesh is being sent back) the stage is
    RETURNING and that counts as moving. Best effort: a write that fails is tried again at the next
    pulse, and the beacon never touches the mesh."""
    from meshpipeline.adapters.mesh_execution.gcs_exchange import upload_progress
    from meshpipeline.engines.mesher_stage import read_stage

    every_s = BEACON_EVERY_S if every_s is None else every_s
    pulse_s = BEACON_PULSE_S if pulse_s is None else pulse_s
    said = None
    mark, moved_at, sent_at = _output_mark(ws), time.time(), 0.0
    while not stop.wait(every_s):
        now = time.time()
        handing_back = returning is not None and returning.is_set()
        cur = _output_mark(ws)
        if cur != mark or handing_back:
            mark, moved_at = cur, now
        stage = dict(RETURNING) if handing_back else read_stage(ws)
        new_stage = stage is not None and bool(stage) and stage.get("key") != said
        if not (new_stage or now - sent_at >= pulse_s):
            continue
        try:
            upload_progress(output_uri, {**(stage or {}), "at": now, "progress_at": moved_at})
            said = (stage or {}).get("key", said)
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
    beacon = threading.Thread(target=_beacon, args=(ws, output_uri, stop), kwargs={"returning": returning},
                              daemon=True)
    beacon.start()
    try:
        try:
            result = run_engine_local(ws, engine=engine, timeout=int(timeout))
        finally:
            # the mesher is done; the beacon goes on pulsing while the result is sent back, so the
            # waiting worker never takes a long upload for a remote that is gone
            returning.set()
        result.pop("workspace_tar", None)
        upload_result(output_uri, ws, result)
    finally:
        stop.set()
    return result


if __name__ == "__main__":
    from meshpipeline.runtime.mesh_invocation import from_environment
    _inv = from_environment()
    _r = run(_inv.input_uri, _inv.output_uri, _inv.engine, _inv.timeout_seconds, _inv.contract)
    print("mesh complete:", json.dumps({k: v for k, v in _r.items() if k != "log_tail"}))
