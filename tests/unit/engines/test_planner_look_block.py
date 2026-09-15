# Responsibility: Verify the look reaches the planner through the untruncated channel, and only when there is one.
# Boundaries: the planner's own allowlist and note; what the look is allowed to contain is the measurement package's.
from __future__ import annotations

import asyncio
import json
import types

import meshpipeline.engines.snappy.planner as planner
from meshpipeline.engines.snappy.planner import GEOMETRY_AGENT_BLOCK_SCHEMA, plan_with_accounting

LOOK = {
    "is_measurement": False,
    "read_as": "words from rendered views, not a measurement: every digit was removed before you saw it.",
    "relied_on": {"attachments": ["a bolt flange at each end"], "confidence": "high",
                  "openings_seen": ["o1", "o2"], "inside_is_plain": True},
    "candidates": {"looks_like": "a branched distribution manifold",
                   "sharp_edges": ["a sharp mouth rim at o2"]},
    "withheld": {"defects": "3 to 5 false alarms on clean files"},
    "identity_confidence_max": 0.6,
    "identity_confidence_max_why": "right on 215 of 322 at best",
    "model": "a-vision-model", "provider": "a-provider",
}

BLOCK = {
    "schema": GEOMETRY_AGENT_BLOCK_SCHEMA,
    "status": "ok",
    "representation": "wall_shell",
    "agent_forecast_cells": 904_514,
    "agent_forecast_low": 904_514,
    "agent_forecast_basis": ["the builder emulator over the measurement alone"],
    "forecast_calibration": {"arm": "facts_only", "median_ratio": 0.52, "p90_ratio": 1.29, "n": 263},
    "customer_cell_cap": None,
    "inlet_bore_m": 0.35694,
    "inlet_opening_id": "o1",
    "smallest_port_min_dim_m": 0.30113,
    "places": [],
    "source_sha256": "a" * 64,
    "agent_git_sha": "deadbeef",
    "units": "m",
}


def _captured(monkeypatch, tmp_path, *, geometry_agent, request_txt="mesh it"):
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                      "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2})
    monkeypatch.setattr(planner, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None),
                        raising=False)
    seen: dict = {}

    async def _fake_call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text=json.dumps({"approach": "x", "max_cells": 1000}))

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _fake_call)
    from tests._geometry_support import prepared_surface
    asyncio.run(plan_with_accounting(
        workspace=tmp_path, job_id="j", request_txt=request_txt,
        geometry_agent=geometry_agent,
        surface=prepared_surface(tmp_path / "input.stl")))
    return seen


def test_a_block_with_no_look_sends_exactly_what_it_sent_before_the_look_existed(monkeypatch, tmp_path):
    """The new key changes nothing until it is there. This is the whole of the vision flag's fail-open."""
    seen = _captured(monkeypatch, tmp_path, geometry_agent=dict(BLOCK))
    assert "look" not in json.loads(_metrics(seen["user"]))["geometry_agent"]
    assert 'ABOUT "look"' not in seen["user"]


def test_the_look_rides_in_the_dict_which_is_after_the_truncation(monkeypatch, tmp_path):
    """The structural point. A brief far longer than the 2,000 character cut cannot push it out."""
    seen = _captured(monkeypatch, tmp_path, geometry_agent={**BLOCK, "look": LOOK},
                     request_txt="x" * 9000)
    metrics = json.loads(_metrics(seen["user"]))
    assert metrics["geometry_agent"]["look"]["relied_on"]["attachments"] == LOOK["relied_on"]["attachments"]
    assert "a branched distribution manifold" in seen["user"]


def test_the_note_rides_with_the_look_and_only_with_it(monkeypatch, tmp_path):
    seen = _captured(monkeypatch, tmp_path, geometry_agent={**BLOCK, "look": LOOK})
    assert 'ABOUT "look" INSIDE "geometry_agent"' in seen["user"]
    assert "It is NOT a measurement" in seen["user"]
    assert "the measurement is right" in seen["user"]


def test_the_look_is_an_allowlisted_key_and_a_stray_one_beside_it_is_dropped(monkeypatch, tmp_path):
    """The block is authored in a separately pinned distribution, so the planner chooses what it shows."""
    seen = _captured(monkeypatch, tmp_path,
                     geometry_agent={**BLOCK, "look": LOOK, "defects_from_the_picture": ["SENTINEL"]})
    assert "look" in planner.GEOMETRY_AGENT_BLOCK_KEYS
    assert "SENTINEL" not in seen["user"]


def test_a_block_that_does_not_name_itself_takes_its_look_down_with_it(monkeypatch, tmp_path):
    seen = _captured(monkeypatch, tmp_path,
                     geometry_agent={**BLOCK, "schema": "something.else.v1", "look": LOOK})
    assert "geometry_agent" not in seen["user"]
    assert 'ABOUT "look"' not in seen["user"]


def _metrics(user: str) -> str:
    body = user.split("MEASURED GEOMETRY (metres):\n", 1)[1]
    depth = 0
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0 and ch == "}":
            return body[:i + 1]
    raise AssertionError("no metrics dict in the user message")
