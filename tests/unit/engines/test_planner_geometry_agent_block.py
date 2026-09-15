# Responsibility: Verify the measurement package's typed block reaches the planner intact, after the truncation, and never by accident.
from __future__ import annotations

import asyncio
import json
import types

import pytest

import meshpipeline.engines.snappy.planner as planner
from meshpipeline.engines.snappy.planner import GEOMETRY_AGENT_BLOCK_SCHEMA, plan_with_accounting

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
    "places": [{"id": "o2", "kind": "tilted_port", "what": "26.6 degrees off the grid",
                "where_m": [1.99, -0.63, 0.0], "measurement": {"tilt_deg": 26.6}}],
    "source_sha256": "a" * 64,
    "agent_git_sha": "deadbeef",
    "units": "m",
}


def _captured_user_message(monkeypatch, tmp_path, *, geometry_agent):
    """Drive one plan and return the user message the planner actually sent."""
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
        workspace=tmp_path, job_id="j", request_txt="mesh it",
        geometry_agent=geometry_agent,
        surface=prepared_surface(tmp_path / "input.stl")))
    return seen


def _metrics_of(user: str) -> dict:
    body = user.split("MEASURED GEOMETRY (metres):\n", 1)[1]
    depth, end = 0, 0
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0 and ch == "}":
            end = i + 1
            break
    return json.loads(body[:end])


def test_with_no_block_the_planner_sends_exactly_what_it_sends_today(monkeypatch, tmp_path):
    """The fail-open, asserted as an absence rather than as a default."""
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=None)
    metrics = _metrics_of(seen["user"])
    assert "geometry_agent" not in metrics
    assert set(metrics) == {"diag_m", "extent_m", "surface_area_m2", "thinnest_feature_m",
                            "recommended_surface_level", "recommended_feature_level",
                            "affordable_level_at_4M_cells", "max_cells_HARD_CEILING"}
    assert "geometry_agent" not in seen["user"]


def test_the_block_rides_in_the_metrics_dict_which_is_after_the_truncation(monkeypatch, tmp_path):
    """The structural finding this whole channel rests on.

    `planner.py` cuts `request_txt[:2000]` and then serialises `metrics` after the cut, so a brief
    far longer than the cut cannot push the block out.
    """
    long_brief = "x" * 9000
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
        seen["user"] = messages[1]["content"]
        return ModelRoundResult(assistant_text="{}")

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _fake_call)
    from tests._geometry_support import prepared_surface
    asyncio.run(plan_with_accounting(
        workspace=tmp_path, job_id="j", request_txt=long_brief, geometry_agent=BLOCK,
        surface=prepared_surface(tmp_path / "input.stl")))
    # the brief WAS cut, and the block survived it
    assert seen["user"].count("x") <= 2100
    assert _metrics_of(seen["user"])["geometry_agent"]["inlet_bore_m"] == 0.35694


def test_every_value_the_planner_is_shown_is_in_metres(monkeypatch, tmp_path):
    """The dict is labelled metres because `require_metre_surface` guarantees the rest of it is."""
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=BLOCK)
    block = _metrics_of(seen["user"])["geometry_agent"]
    assert block["units"] == "m"
    assert block["inlet_bore_m"] < 1.0 and block["smallest_port_min_dim_m"] < 1.0
    assert not any(k.endswith(("_mm", "_file")) for k in block)


def test_no_seed_point_reaches_the_planner_even_if_a_block_carries_one(monkeypatch, tmp_path):
    """dev_plan_v3 W12: the seed check has never run, and neither planner prompt has a seed field."""
    seen = _captured_user_message(
        monkeypatch, tmp_path,
        geometry_agent={**BLOCK, "seed_point_m": [0.3, 0.0, 0.0],
                        "cell_estimate_refined": 982_404_891})
    block = _metrics_of(seen["user"])["geometry_agent"]
    assert "seed_point_m" not in block and "cell_estimate_refined" not in block
    assert "seed_point" not in seen["user"]
    assert set(planner.GEOMETRY_AGENT_BLOCK_REFUSED) == {"seed_point_m", "cell_estimate_refined"}


def test_a_block_that_does_not_name_itself_is_refused(monkeypatch, tmp_path):
    seen = _captured_user_message(monkeypatch, tmp_path,
                                  geometry_agent={**BLOCK, "schema": "something.else.v9"})
    assert "geometry_agent" not in _metrics_of(seen["user"])


def test_a_block_with_no_status_is_refused(monkeypatch, tmp_path):
    """A partial block silently missing two numbers is read by a model as a part that has neither."""
    without = {k: v for k, v in BLOCK.items() if k != "status"}
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=without)
    assert "geometry_agent" not in _metrics_of(seen["user"])


def test_a_degraded_block_travels_and_says_what_is_missing(monkeypatch, tmp_path):
    degraded = {**BLOCK, "status": "degraded", "inlet_bore_m": None,
                "missing": ["inlet_bore_m"], "missing_because": "the unit was never confirmed"}
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=degraded)
    block = _metrics_of(seen["user"])["geometry_agent"]
    assert block["status"] == "degraded" and block["missing"] == ["inlet_bore_m"]
    assert "unit" in block["missing_because"]


def test_the_forecast_never_travels_without_its_measured_error(monkeypatch, tmp_path):
    """0.52x median before a plan exists. The note has to tell the model not to shave max_cells."""
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=BLOCK)
    block = _metrics_of(seen["user"])["geometry_agent"]
    assert block["forecast_calibration"]["median_ratio"] == 0.52
    assert "0.52" in seen["user"] and "do NOT shave max_cells" in seen["user"]


def test_the_note_is_sent_only_with_a_block(monkeypatch, tmp_path):
    """A prompt that describes a key the model will not find is the failure this phase removes."""
    assert "geometry_agent" not in _captured_user_message(
        monkeypatch, tmp_path, geometry_agent=None)["user"]
    assert "geometry_agent" in _captured_user_message(
        monkeypatch, tmp_path, geometry_agent=BLOCK)["user"]


def test_the_static_system_prompt_is_untouched_by_this_phase(monkeypatch, tmp_path):
    """Both planner prompts are production text. The block changes the user message and nothing else."""
    for block in (None, BLOCK):
        seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=block)
        assert seen["system"] == planner.PLANNER_SYSTEM
        assert "geometry_agent" not in seen["system"]


@pytest.mark.parametrize("junk", [{}, [], "block", 0, {"schema": GEOMETRY_AGENT_BLOCK_SCHEMA}])
def test_junk_never_reaches_a_prompt(monkeypatch, tmp_path, junk):
    seen = _captured_user_message(monkeypatch, tmp_path, geometry_agent=junk)
    assert "geometry_agent" not in _metrics_of(seen["user"])
