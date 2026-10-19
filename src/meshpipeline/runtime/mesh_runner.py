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


def _beacon(ws: str, output_uri: str, stop) -> None:
    """THE MESHER'S STAGE, said while it runs: every few seconds the newest log is read
    (engines/mesher_stage) and, when the stage has moved on, written beside the result for the
    waiting worker to relay to the run's live line. Best effort: a beacon that cannot write
    stops trying after a few failures, and never touches the mesh."""
    from meshpipeline.adapters.mesh_execution.gcs_exchange import upload_progress
    from meshpipeline.engines.mesher_stage import read_stage

    said, failures = None, 0
    while not stop.wait(BEACON_EVERY_S) and failures < 3:
        stage = read_stage(ws)
        if not stage or stage.get("key") == said:
            continue
        try:
            upload_progress(output_uri, {**stage, "at": time.time()})
            said = stage.get("key")
        except Exception:  # noqa: BLE001 - the live line is a courtesy
            failures += 1


def run(input_uri: str, output_uri: str, engine: str, timeout: int) -> dict:
    from meshpipeline.adapters.mesh_execution.gcs_exchange import download_workspace, upload_result
    from meshpipeline.engines.dispatch import run_engine_local

    ws = download_workspace(input_uri)
    stop = threading.Event()
    beacon = threading.Thread(target=_beacon, args=(ws, output_uri, stop), daemon=True)
    beacon.start()
    try:
        result = run_engine_local(ws, engine=engine, timeout=int(timeout))
    finally:
        stop.set()
    result.pop("workspace_tar", None)
    upload_result(output_uri, ws, result)
    return result


if __name__ == "__main__":
    from meshpipeline.runtime.mesh_invocation import from_environment
    _inv = from_environment()
    _r = run(_inv.input_uri, _inv.output_uri, _inv.engine, _inv.timeout_seconds)
    print("mesh complete:", json.dumps({k: v for k, v in _r.items() if k != "log_tail"}))
