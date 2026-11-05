# Responsibility: Verify a snappy planner answer is held to usable, in-range values before any driver reads it.
from __future__ import annotations

import asyncio
import math

import pytest
from tests.unit.engines.test_snappy_timeout_replan import (  # noqa: F401 - the driver fixture
    _same_plan_every_time,
    internal,
)

import meshpipeline.engines.snappy.planner as planner
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.snappy.planner import plan_float, plan_int, validate_plan

GOOD_EXTERNAL = {
    "approach": "body-fitted hex with prism layers",
    "domain_margin": {"up": 10.0, "down": 20.0, "side": 10.0, "vert": 10.0},
    "reference_length_m": 1.0,
    "n_layers": 4,
    "first_layer_rel": 0.35,
    "quality": "balanced",
    "max_cells": 3_000_000,
    "wake": {"enabled": True, "length": 4.0, "levels_below_surface": 1, "tiers": 3},
    "focus": "leading edge",
    "risks": "layer collapse at the trailing edge",
}
GOOD_INTERNAL = {
    "approach": "bore-relative", "cells_across_diameter": 24, "surface_level": 2,
    "feature_level": 3, "n_layers": 5, "first_layer_rel": 0.3, "quality": "strict",
    "max_cells": 2_000_000, "focus": "the bend", "risks": "skew at the bend",
}


# a good plan passes unchanged
def test_a_good_external_plan_passes_unchanged():
    out, notes = validate_plan(dict(GOOD_EXTERNAL), flow_regime="external",
                               state={"reference_length_m": 1.0})
    assert out == GOOD_EXTERNAL and notes == []


def test_a_good_internal_plan_passes_unchanged():
    out, notes = validate_plan(dict(GOOD_INTERNAL), flow_regime="internal")
    assert out == GOOD_INTERNAL and notes == []


# garbage types fall back to the rules default (the key is absent)
@pytest.mark.parametrize("bad", [None, "30-40", "lots", float("nan"), float("inf"), [], [None],
                                 {"a": 1}, True])
def test_a_knob_that_is_not_a_number_is_dropped(bad):
    out, notes = validate_plan({"cells_across_diameter": bad, "n_layers": bad,
                                "first_layer_rel": bad, "surface_level": bad,
                                "feature_level": bad, "max_cells": bad},
                               flow_regime="internal")
    assert out == {}
    if bad is not None:
        assert notes


def test_clean_numeric_strings_and_whole_floats_are_read():
    out, notes = validate_plan({"n_layers": "4", "cells_across_diameter": 24.0,
                                "first_layer_rel": "0.3", "surface_level": [1, 2]},
                               flow_regime="internal")
    assert out == {"n_layers": 4, "cells_across_diameter": 24, "first_layer_rel": 0.3,
                   "surface_level": 2}
    assert isinstance(out["n_layers"], int) and isinstance(out["cells_across_diameter"], int)


# out-of-range values are clamped to the brief's own stated ranges
def test_internal_knobs_are_held_to_the_briefs_ranges():
    out, notes = validate_plan({"cells_across_diameter": 400, "surface_level": 9,
                                "feature_level": 12, "n_layers": 40, "first_layer_rel": 3.0},
                               flow_regime="internal")
    assert out == {"cells_across_diameter": 40, "surface_level": 3, "feature_level": 4,
                   "n_layers": 6, "first_layer_rel": 0.4}
    assert len(notes) == 5
    low, _ = validate_plan({"cells_across_diameter": 2, "surface_level": -3, "n_layers": 0,
                            "first_layer_rel": 0.001}, flow_regime="internal")
    assert low == {"cells_across_diameter": 16, "surface_level": 1, "n_layers": 3,
                   "first_layer_rel": 0.2}


def test_external_knobs_are_held_to_the_briefs_ranges():
    out, _ = validate_plan({"n_layers": 12, "first_layer_rel": 0.01}, flow_regime="external")
    assert out == {"n_layers": 5, "first_layer_rel": 0.25}


def test_feature_level_never_below_the_surface_level():
    out, _ = validate_plan({"surface_level": 3, "feature_level": 2}, flow_regime="internal")
    assert out["feature_level"] == 3


def test_the_cell_budget_is_held_to_the_hard_limit():
    out, notes = validate_plan({"max_cells": 10**12}, flow_regime="external")
    assert out["max_cells"] == polcfg.CELL_HARD_LIMIT and notes
    out, _ = validate_plan({"max_cells": 10}, flow_regime="external")
    assert out["max_cells"] == min(200_000, polcfg.CELL_HARD_LIMIT)
    out, _ = validate_plan({"max_cells": -5}, flow_regime="external")
    assert "max_cells" not in out


def test_quality_outside_balanced_or_strict_is_dropped():
    out, notes = validate_plan({"quality": "production"}, flow_regime="external")
    assert out == {} and notes
    out, _ = validate_plan({"quality": " Strict "}, flow_regime="external")
    assert out == {"quality": "strict"}


# keys outside the regime's schema, and nested junk
def test_unknown_and_wrong_regime_keys_are_dropped():
    out, notes = validate_plan({"n_layers": 3, "surface_level": 2, "cells_across_diameter": 30,
                                "evil": {"nested": [1, 2]}, "local_boxes": [[0, 0, 0]]},
                               flow_regime="external")
    assert out == {"n_layers": 3}
    assert any("evil" in n for n in notes)
    out, _ = validate_plan({"domain_margin": {"up": 5}, "wake": False,
                            "reference_length_m": 1.0}, flow_regime="internal")
    assert out == {}


def test_margins_are_held_to_positive_ranges_and_junk_is_dropped():
    out, notes = validate_plan({"domain_margin": {"up": -3, "down": "far", "side": 0,
                                                  "vert": 0, "diag": 9, "x": None}},
                               flow_regime="external")
    assert out == {"domain_margin": {"up": 1.0, "side": 0.0, "vert": 2.0}}
    assert len(notes) >= 4
    out, _ = validate_plan({"domain_margin": {"up": 1e9, "down": float("nan")}},
                           flow_regime="external")
    assert out == {"domain_margin": {"up": 100.0}}
    out, _ = validate_plan({"domain_margin": [1, 2, 3]}, flow_regime="external")
    assert out == {}


def test_wake_junk_knobs_keep_their_defaults():
    out, notes = validate_plan({"wake": {"length": 99, "tiers": 3.0, "levels_below_surface": "x",
                                         "spin": 1}}, flow_regime="external")
    assert out == {"wake": {"tiers": 3}} and len(notes) == 3
    assert validate_plan({"wake": False}, flow_regime="external")[0] == {"wake": False}
    assert validate_plan({"wake": "yes"}, flow_regime="external")[0] == {}


# the reference length is the ruler, so it must agree with what is known
def test_a_reference_length_that_disagrees_with_the_stated_one_is_dropped():
    out, notes = validate_plan({"reference_length_m": 2.5}, flow_regime="external",
                               state={"reference_length_m": 1.0})
    assert out == {} and notes
    out, _ = validate_plan({"reference_length_m": 1.05}, flow_regime="external",
                           state={"reference_length_m": 1.0})
    assert out == {"reference_length_m": 1.05}


def test_a_reference_length_is_checked_against_the_geometry_when_none_was_stated():
    st = {"reference_length_m": None, "extent_m": [0.3, 2.0, 0.1], "diag_m": 2.03}
    assert validate_plan({"reference_length_m": 0.31}, flow_regime="external",
                         state=st)[0] == {"reference_length_m": 0.31}
    assert validate_plan({"reference_length_m": 300.0}, flow_regime="external",
                         state=st)[0] == {}
    # nothing to agree with - dropped
    assert validate_plan({"reference_length_m": 1.0}, flow_regime="external")[0] == {}
    # null is the brief's own "no reference length", not a mistake
    assert validate_plan({"reference_length_m": None}, flow_regime="external") == ({}, [])
    for bad in ("1 m", -1, 0, float("nan"), True):
        assert validate_plan({"reference_length_m": bad}, flow_regime="external",
                             state=st)[0] == {}


def test_a_plan_that_is_not_an_object_is_the_rules_plan():
    for bad in ("plan", 3, [1, 2]):
        out, notes = validate_plan(bad, flow_regime="external")
        assert out == {} and notes
    assert validate_plan(None, flow_regime="external") == ({}, [])


# the tolerant readers the drivers use never raise
@pytest.mark.parametrize("bad", [None, "30-40", float("nan"), [], {"a": 1}, object()])
def test_plan_readers_fall_back_to_the_default(bad):
    assert plan_int({"k": bad}, "k", 7) == 7
    assert plan_float({"k": bad}, "k", 0.3) == 0.3
    assert plan_int(None, "k", 2) == 2


def test_plan_readers_read_usable_values():
    assert plan_int({"k": [1, 3]}, "k", 0) == 3
    assert plan_int({"k": "5"}, "k", 0) == 5
    assert math.isclose(plan_float({"k": "0.25"}, "k", 0.0), 0.25)


# end to end: the planner's own output is the guarded plan
def test_plan_with_accounting_returns_the_guarded_plan(monkeypatch, tmp_path):
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda _s: {"diag": 1.7, "extent": [1.0, 1.0, 1.0],
                                    "surface_area": 6.0, "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda _a, **_k: {"surface_level": 2, "feature_level": 3,
                                          "afford_level": 2})
    monkeypatch.setattr(planner, "_log_plan_event", lambda *a, **k: None)

    async def _fake_call(messages, tools=None, tool_choice="none", job_id="", **_kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult
        return ModelRoundResult(
            assistant_text='{"cells_across_diameter": "30-40", "n_layers": null, '
                           '"surface_level": 7, "first_layer_rel": 0.3, "bogus": 1}',
            finish_reason="stop", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _fake_call)
    from tests._geometry_support import prepared_surface
    out = asyncio.run(planner.plan_with_accounting(
        workspace=tmp_path, job_id="j", request_txt="mesh it", flow_regime="internal",
        surface=prepared_surface(tmp_path / "input.stl")))
    assert out.plan == {"surface_level": 3, "first_layer_rel": 0.3}
    assert out.guard_notes and not out.failure_marker


# drive-level: a bad knob that still reaches the internal driver keeps its default, never raises


def test_internal_driver_survives_unusable_knobs(internal, monkeypatch):  # noqa: F811
    internal.timeouts = 0                                    # the first pass meshes
    seen: list = []
    _same_plan_every_time(monkeypatch, {"cells_across_diameter": "30-40", "n_layers": None,
                                        "surface_level": None, "first_layer_rel": "thin",
                                        "feature_level": [], "quality": 7,
                                        "max_cells": "lots"}, seen)
    _ws, (ok, _value, _outcome), pub = internal.go()
    assert ok is True and internal.runs == [1]
    filling = [n for n in pub.notes if n.startswith("Filling the cavity")]
    assert filling and "about 24 cells across the bore" in filling[0], filling
