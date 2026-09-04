# Responsibility: Verify the escalation ladder - layer-fatal failures escalate the measured
# policy deterministically (same plan, no model round), everything else still re-plans.
# Boundaries: the driver's decision and the ladder's state machine; the mesher is stubbed.
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.drivers as drv
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.engines.snappy import layer_policy as LP

# -- the fatal-signature reader ---------------------------------------------------------------


def test_is_layer_fatal_reads_exactly_the_layer_inversion_classes():
    assert LP.is_layer_fatal({"fatal": ["negative-volume cells"]})
    assert LP.is_layer_fatal({"fatal": ["incorrectly oriented faces"]})
    assert LP.is_layer_fatal({"fatal": ["open cells", "negative-volume cells"]})
    assert not LP.is_layer_fatal({"fatal": ["open cells"]})
    assert not LP.is_layer_fatal({"fatal": []})
    assert not LP.is_layer_fatal({})
    assert not LP.is_layer_fatal({"fatal": ["case dicts rejected: bad"]})


# -- durable stage ----------------------------------------------------------------------------


def test_escalation_stage_round_trips(tmp_path):
    assert LP.read_escalation(tmp_path) == 0
    LP.write_escalation(tmp_path, 2)
    assert LP.read_escalation(tmp_path) == 2
    assert not list(tmp_path.glob("*.tmp")), "the write is atomic - no temp file left behind"


def test_a_fresh_retry_workspace_continues_the_sibling_attempts_ladder(tmp_path):
    # attempt_2 meshes the same geometry attempt_1 already measured a failure on; restarting
    # the ladder would replay that failure
    a1 = tmp_path / "attempt_1"
    a2 = tmp_path / "attempt_2"
    a1.mkdir()
    a2.mkdir()
    LP.write_escalation(a1, 1)
    assert LP.read_escalation(a2) == 1
    LP.write_escalation(a2, 2)  # its own fact wins once it has one
    assert LP.read_escalation(a2) == 2


def test_a_corrupt_stage_fact_reads_as_stage_zero(tmp_path):
    (tmp_path / LP.ESCALATION_FACT).write_text("not json")
    assert LP.read_escalation(tmp_path) == 0


# -- the driver's decision, end to end over stubs ---------------------------------------------


class _Publish:
    def __init__(self):
        self.notes: list[str] = []

    async def anote(self, msg, *_a, **_k):
        self.notes.append(str(msg))

    async def aerror(self, *_a, **_k): ...
    async def ameshing(self, *_a, **_k): ...
    async def ameshed(self, *_a, **_k): ...
    async def areasoning(self, *_a, **_k): ...
    async def atool_call(self, *_a, **_k): ...
    async def atool_result(self, *_a, **_k): ...


def _policy_for(stage: int) -> LP.LayerPolicy:
    counts = LP.class_layer_counts(5, stage)
    return LP.LayerPolicy(policy={
        "version": 1, "source": "thin_feature_classifier", "mode": "global",
        "requested_layers": 5, "escalation_stage": stage,
        "classes": {c: {"n_layers": counts[c], "area_frac": 0.33} for c in counts},
        "thresholds_m": {"thin_below": 0.05, "razor_below": 0.01},
        "surface_cell_m": 0.01, "stack_m": 0.013,
        "min_thickness_rel": 0.02, "max_thickness_to_medial": None,
        "sharp_area_frac": 0.1, "region_patches": {"body": "razor"},
    })


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """The accountability harness, trimmed: stub the native/CAD seam, count planner rounds,
    record every render's layer_counts."""
    rec = {"planner_calls": 0, "render_counts": [], "fatal": ["negative-volume cells"],
           "policy_stages": []}
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")

    import meshpipeline.engines.snappy.snappy_runner as _sr

    def _prep(*_a, **_k):
        return {"surface_name": "body", "feature_file": "body.eMesh"}

    def _render(*_a, **k):
        rec["render_counts"].append(k.get("layer_counts"))
        return {"surface_level": 2, "n_layers": 5}

    monkeypatch.setattr(_sr, "detect_symmetry_plane", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_sr, "domain_from_strategy", lambda *a, **k: ([0] * 3, [1] * 3))
    monkeypatch.setattr(_sr, "prepare_surface", _prep)
    monkeypatch.setattr(_sr, "render_snappy_case", _render)
    monkeypatch.setattr(_sr, "run_snappy", lambda *a, **k: {"rc": 0})
    monkeypatch.setattr(_sr, "check_mesh", lambda *a, **k: {
        "cells": 1000, "fatal": list(rec["fatal"]), "skew_fraction": 0.0, "skew_faces": 0})
    monkeypatch.setattr(_sr, "_patch_face_counts", lambda *a, **k: {"body": 500, "farfield": 9})

    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *a, **k: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 1.0,
                                         "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda *a, **k: {"surface_level": 2, "feature_level": 3,
                                         "afford_level": 2})
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *a, **k: "body")
    monkeypatch.setattr(drv, "read_purpose", lambda *a, **k: "external")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda r: {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded", lambda **kw: {})

    # the policy seam: a measured field and a stage-respecting policy
    monkeypatch.setattr(LP, "measure_field", lambda *_a, **_k: object())

    def _plan_policy(field_, *, rec=None, strategy=None, wall_name="", stage=0, **_k):
        rec_ = _policy_for(stage)
        harness_rec["policy_stages"].append(stage)
        return rec_

    harness_rec = rec
    monkeypatch.setattr(LP, "plan_layer_policy", _plan_policy)

    import meshpipeline.engines.snappy.planner as pl

    async def _plan(**_kw):
        rec["planner_calls"] += 1
        from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo
        rr = ModelRoundResult(tool_calls=(), assistant_text='{"approach":"a"}',
                              finish_reason="stop", provider=ProviderAttemptInfo(1, "p", "m"),
                              input_tokens=1, output_tokens=1, failure_marker="")
        return pl.PlanOutcome({"approach": "a", "n_layers": 5, "max_cells": 100000}, rr)

    monkeypatch.setattr(drv, "plan_with_accounting", _plan, raising=False)
    monkeypatch.setattr(pl, "plan_with_accounting", _plan)
    monkeypatch.setattr("meshpipeline.engines.snappy.planner.clamp_cell_budget",
                        lambda v, ceiling=None: v or 100000)
    return rec


def _drive(tmp_path, monkeypatch):
    from tests._geometry_support import geometry_state

    from meshpipeline.contracts.geometry_units import LengthUnit

    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial", deadline_s=60.0)

    async def _fence(*_a, **_k): ...
    monkeypatch.setattr(run, "fence", _fence)
    state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
             "intake_patches": [], "dimensionality": "3D", "flow_topology": "external",
             "geometry": geometry_state(Path(tmp_path) / "_src", unit=LengthUnit.metre,
                                        filename="body.stl")}
    return asyncio.run(drv.drive(Path(tmp_path), state, job_id="j",
                                 publish=_Publish(), run=run))


def test_layer_fatal_passes_escalate_without_consulting_the_planner(harness, tmp_path,
                                                                    monkeypatch):
    ok, _value, _outcome = _drive(tmp_path, monkeypatch)
    assert ok is False, "every pass stayed fatal - no deliverable"
    # ONE planner round (the initial plan); the layer-fatal retries kept the plan and walked
    # the ladder instead of burning model rounds on a failure the classifier already located
    assert harness["planner_calls"] == 1
    assert harness["policy_stages"] == [0, 1, 2], "each pass planned its policy at its stage"
    assert json.loads((tmp_path / LP.ESCALATION_FACT).read_text())["stage"] == 2
    # the authored counts walked the ladder: razor-dominant global count 1, 1, 0
    assert [c.get("body") for c in harness["render_counts"]] == [1, 1, 0]


def test_the_exhausted_ladder_hands_the_failure_back_to_the_planner(harness, tmp_path,
                                                                    monkeypatch):
    LP.write_escalation(tmp_path, 2)  # the ladder is already at its terminal stage
    _drive(tmp_path, monkeypatch)
    # pass 1 fails at stage 2 -> no rung left -> the freeform re-plan resumes for passes 2, 3
    assert harness["planner_calls"] == 3


def test_without_a_policy_every_failed_pass_still_replans(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(LP, "plan_layer_policy", lambda *a, **k: None)
    _drive(tmp_path, monkeypatch)
    assert harness["planner_calls"] == 3
    assert not (tmp_path / LP.ESCALATION_FACT).exists()


def test_a_non_layer_fatal_never_engages_the_ladder(harness, tmp_path, monkeypatch):
    harness["fatal"][:] = ["open cells"]
    _drive(tmp_path, monkeypatch)
    assert harness["planner_calls"] == 3, "a carve/topology defect is the planner's problem"
    assert not (tmp_path / LP.ESCALATION_FACT).exists()


def test_the_honest_record_lands_beside_the_case(harness, tmp_path, monkeypatch):
    _drive(tmp_path, monkeypatch)
    pol = json.loads((tmp_path / LP.LAYER_POLICY_FACT).read_text())
    assert pol["source"] == "thin_feature_classifier"
    assert pol["escalation_stage"] == 2, "the record describes the LAST authored pass"
    assert set(pol["classes"]) == {"normal", "thin", "razor"}
