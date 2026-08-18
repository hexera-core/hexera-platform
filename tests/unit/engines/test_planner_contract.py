# Responsibility: Verify the planner clamps its budget, returns nothing on any failure, and reads its own settings.
from __future__ import annotations

import asyncio
import importlib
import types
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.planner as planner
from meshpipeline.engines.snappy.planner import clamp_cell_budget, make_mesh_plan


# Max_cells budget clamp (non-finite / non-positive rejected; excessive clamped)
def test_excessive_budget_is_clamped_to_the_ceiling():
    assert clamp_cell_budget(10**12, ceiling=8_000_000) == 8_000_000


@pytest.mark.parametrize("bad", [-5, 0, -1, float("nan"), float("inf"), float("-inf"),
                                 "abc", None, [1], {"a": 1}, True])
def test_invalid_budget_falls_back_to_the_default_not_the_bad_value(bad):
    out = clamp_cell_budget(bad, ceiling=8_000_000, default=4_000_000)
    assert out == 4_000_000


def test_a_clean_numeric_string_budget_is_accepted():
    assert clamp_cell_budget("2000000", ceiling=8_000_000) == 2_000_000


def test_default_is_itself_capped_by_the_ceiling():
    assert clamp_cell_budget(None, ceiling=1_000_000, default=4_000_000) == 1_000_000


# Make_mesh_plan failure/parse semantics (optional; None on any failure)
def _drive_plan(monkeypatch, tmp_path, *, response, api_failure):
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")   # exists() gate only
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _stl: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 6.0,
                                      "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3, "afford_level": 2})
    monkeypatch.setattr(planner, "TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None),
                        raising=False)

    async def _fake_call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult
        if api_failure:
            return ModelRoundResult(failure_marker=api_failure)
        return response

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _fake_call)
    from tests._geometry_support import prepared_surface
    return asyncio.run(make_mesh_plan(
        workspace=tmp_path, job_id="j", request_txt="mesh it",
        surface=prepared_surface(tmp_path / "input.stl")))


def _resp(content: str):
    from meshpipeline.contracts.model_inference import ModelRoundResult
    return ModelRoundResult(assistant_text=content, finish_reason="stop",
                            input_tokens=1, output_tokens=1)


def test_missing_input_stl_returns_none(monkeypatch, tmp_path):
    assert asyncio.run(make_mesh_plan(workspace=tmp_path, job_id="j", request_txt="x")) is None


def test_provider_failure_returns_none(monkeypatch, tmp_path):
    assert _drive_plan(monkeypatch, tmp_path, response=None, api_failure="rate_limit") is None


def test_empty_response_returns_none(monkeypatch, tmp_path):
    assert _drive_plan(monkeypatch, tmp_path, response=_resp(""), api_failure="") is None


def test_malformed_json_returns_none(monkeypatch, tmp_path):
    assert _drive_plan(monkeypatch, tmp_path, response=_resp("no json here"), api_failure="") is None


def test_two_json_objects_are_not_ambiguously_accepted(monkeypatch, tmp_path):
    # a greedy '{...}' over two objects is invalid JSON → None, never the first object silently
    out = _drive_plan(monkeypatch, tmp_path, response=_resp('{"a": 1} {"b": 2}'), api_failure="")
    assert out is None


def test_valid_json_returns_the_plan(monkeypatch, tmp_path):
    out = _drive_plan(monkeypatch, tmp_path,
                      response=_resp('{"approach": "x", "max_cells": 500000, "n_layers": 3}'),
                      api_failure="")
    assert isinstance(out, dict) and out["approach"] == "x"


def test_planner_never_raises_it_returns_none_on_internal_error(monkeypatch, tmp_path):
    (tmp_path / "input.stl").write_text("solid x")
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
    out = asyncio.run(make_mesh_plan(workspace=tmp_path, job_id="j", request_txt="x"))
    assert out is None


# Config independence from the Builder
def test_planner_call_kwargs_read_planner_settings_not_builder():
    import meshpipeline.engines.snappy.settings as pcfg
    from meshpipeline.adapters.model_inference.deepinfra import BUILDER_CALL_KWARGS, _planner_call_kwargs
    pk = _planner_call_kwargs()
    assert pk is not BUILDER_CALL_KWARGS
    assert set(pk) >= {"model", "temperature", "max_tokens", "top_p", "extra_body"}
    # the VALUES are the planner's own, sourced from PLANNER_* (a builder-temp mutation would break this)
    assert pk["temperature"] == pcfg.PLANNER_TEMPERATURE
    assert pk["max_tokens"] == pcfg.PLANNER_MAX_TOKENS
    assert pk["top_p"] == pcfg.PLANNER_TOP_P
    assert pk["extra_body"]["min_p"] == pcfg.PLANNER_MIN_P
    assert pk["model"] == pcfg.PLANNER_MODEL


def test_planner_kwargs_follow_a_distinct_planner_temperature(monkeypatch):
    import meshpipeline.engines.snappy.settings as pcfg
    from meshpipeline.adapters.model_inference.deepinfra import _planner_call_kwargs
    monkeypatch.setenv("PLANNER_TEMPERATURE", "0.71")
    importlib.reload(pcfg)
    try:
        assert _planner_call_kwargs()["temperature"] == pytest.approx(0.71)
    finally:
        monkeypatch.delenv("PLANNER_TEMPERATURE", raising=False)
        importlib.reload(pcfg)


def test_changing_builder_temperature_does_not_change_the_planner(monkeypatch):
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.engines.snappy.settings as pcfg
    monkeypatch.setenv("BUILDER_TEMPERATURE", "1.7")
    _b = importlib.reload(bcfg)
    _p = importlib.reload(pcfg)
    try:
        assert _b.BUILDER_TEMPERATURE == pytest.approx(1.7)
        assert _p.PLANNER_TEMPERATURE == pytest.approx(0.3)   # unmoved
    finally:
        monkeypatch.delenv("BUILDER_TEMPERATURE", raising=False)
        importlib.reload(bcfg)
        importlib.reload(pcfg)


def test_changing_planner_temperature_does_not_change_the_builder(monkeypatch):
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.engines.snappy.settings as pcfg
    monkeypatch.setenv("PLANNER_TEMPERATURE", "0.9")
    _p = importlib.reload(pcfg)
    try:
        assert _p.PLANNER_TEMPERATURE == pytest.approx(0.9)
        assert bcfg.BUILDER_TEMPERATURE == pytest.approx(0.3)   # unmoved (not reloaded, not coupled)
    finally:
        monkeypatch.delenv("PLANNER_TEMPERATURE", raising=False)
        importlib.reload(pcfg)


# Explicit planner deadline contract
def test_planner_total_timeout_is_finite_positive_and_its_own():
    import meshpipeline.engines.snappy.settings as pcfg
    assert isinstance(pcfg.PLANNER_TOTAL_TIMEOUT_SECONDS, int)
    assert pcfg.PLANNER_TOTAL_TIMEOUT_SECONDS > 0


def test_out_of_range_planner_temperature_raises_at_import(monkeypatch):
    import meshpipeline.engines.snappy.settings as pcfg
    from meshpipeline.settings.env import ConfigurationError
    monkeypatch.setenv("PLANNER_TEMPERATURE", "3.0")
    with pytest.raises(ConfigurationError):
        importlib.reload(pcfg)
    monkeypatch.delenv("PLANNER_TEMPERATURE", raising=False)
    importlib.reload(pcfg)


# Structural - the planner writes no PipelineState and has no state authority
def test_planner_module_does_not_import_pipeline_state_or_write_authority():
    src = Path(planner.__file__).read_text()
    for banned in ("PipelineState", "executor_success", "final_result", "reviewer_verdict",
                   "run_python", "subprocess"):
        assert banned not in src, f"planner references {banned!r} - it must stay advisory"
