# Responsibility: Serve the mesh image's entry point: run one mesh and report health.
# Boundaries: the only process that needs the native toolchains.
from __future__ import annotations

import json

from fastapi import FastAPI

app = FastAPI(title="mesh-runner")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def run(input_uri: str, output_uri: str, engine: str, timeout: int) -> dict:
    from meshpipeline.adapters.mesh_execution.gcs_exchange import download_workspace, upload_result
    from meshpipeline.engines.dispatch import run_engine_local

    ws = download_workspace(input_uri)
    result = run_engine_local(ws, engine=engine, timeout=int(timeout))
    result.pop("workspace_tar", None)
    upload_result(output_uri, ws, result)
    return result


if __name__ == "__main__":
    from meshpipeline.runtime.mesh_invocation import from_environment
    _inv = from_environment()
    _r = run(_inv.input_uri, _inv.output_uri, _inv.engine, _inv.timeout_seconds)
    print("mesh complete:", json.dumps({k: v for k, v in _r.items() if k != "log_tail"}))
