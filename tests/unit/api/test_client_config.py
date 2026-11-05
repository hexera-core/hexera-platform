# Responsibility: Verify the served client configuration matches the values the browser falls back to.
from __future__ import annotations

from pathlib import Path

import meshpipeline.settings.policy as polcfg


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meshpipeline.api.v1 import client_config

    app = FastAPI()
    app.include_router(client_config.router, prefix="/api/v1/client-config")
    return TestClient(app)


def test_ui_config_serves_the_config_values(monkeypatch):
    monkeypatch.setattr(polcfg, "VIEWER_GRID_PX", 3.25)
    monkeypatch.setattr(polcfg, "DISPUTE_MAX_FLAGS", 7)
    d = _client().get("/api/v1/client-config").json()
    v = d["viewer"]
    assert v["grid_px"] == 3.25            # env-tunable, no JS edit needed
    assert v["max_flags"] == 7
    assert set(v) == {"grid_px", "fine_fill", "frame_frac",
                      "flag_span_factor", "max_flags"}


def test_client_defaults_match_served_defaults():
    import json
    import re as _re
    src = (Path(__file__).parent.parent.parent.parent / "ui" / "js" / "viewer"
           / "config.js").read_text()
    body = _re.search(r"Object\.freeze\((\{[^}]*\})\)", src)
    assert body, "the viewer no longer declares a frozen fallback object"
    fallback = json.loads(_re.sub(r"(\w+):", r'"\1":', body.group(1)).replace(",\n}", "\n}"))
    assert fallback == {
        "grid_px": polcfg.VIEWER_GRID_PX,
        "fine_fill": polcfg.VIEWER_FINE_FILL,
        "frame_frac": polcfg.VIEWER_FRAME_FRAC,
        "flag_span_factor": polcfg.VIEWER_FLAG_SPAN_FACTOR,
        "max_flags": polcfg.DISPUTE_MAX_FLAGS,
    }


def test_dispute_endpoint_reads_flag_limit_from_config():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "api" / "v1" / "simulation.py").read_text()
    assert "polcfg.DISPUTE_MAX_FLAGS" in src
    assert "> 20:" not in src              # the old hardcoded literal is gone
