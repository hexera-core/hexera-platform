# Responsibility: Pin LAD-cfd-8 - a retry changes what was refused, or it does not run: the gate/lever table, the switch at once, the refused-but-sound delivery with its concern, the facts the next plan reads.
from __future__ import annotations

import json

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines import resolution_policy as rp
from meshpipeline.engines.gate_facts import GATE_FACTS_FILE, missed_ports, write_gate_facts
from meshpipeline.pipeline import engine_fallback as lad
from meshpipeline.pipeline.graph import node_deliver_sound, route_after_executor

EXTERNAL = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]


@pytest.fixture(autouse=True)
def _ladder_on(monkeypatch):
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 2)
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", True)


def _external(**kw) -> dict:
    s = {"job_id": "j", "engine": "snappy", "engine_source": lad.SOURCE_SUGGESTED,
         "purpose": "external_cfd", "input_kind": "body-surface", "dimensionality": "3D",
         "intake_patches": [dict(p) for p in EXTERNAL], "engine_params": {},
         "request_txt": "External flow around a wave energy absorber at 2 m/s.",
         "review_brief_txt": "", "retry_count": 1, "executor_success": False,
         "executor_failed_gate": "cad_fidelity", "executor_failure_cause": "patch_not_captured",
         "executor_failure_facts": {}, "builder_deadline_epoch": 0.0,
         "pipeline_deadline_epoch": 0.0, "engine_ladder": {}}
    s.update(kw)
    return s


#: snappy and cfMesh both tried: no same-contract hex engine with layers is left
_BOTH_TRIED = {"approved": "snappy", "source": lad.SOURCE_SUGGESTED,
               "attempts": [{"attempt": 1, "engine": "snappy", "kind": lad.ENGINE,
                             "cause": "patch_not_captured"}],
               "switches": [{"attempt": 2, "from": "snappy", "to": "cfmesh"}]}


def _fidelity(missed=0.146, off=0.02, vol_err=0.01, hidden=0.21, verdicts=None) -> dict:
    """An RM3-like record: an assembly whose hidden faces were set aside (cad_hidden_share)."""
    return {"surface": {"cad_to_mesh": {"cad_missed_share": missed, "cad_hidden_share": hidden},
                        "mesh_to_cad": {"wall_off_share": off, "p95_cells": 0.8}},
            "volume": {"what": "body", "error": vol_err},
            "verdicts": verdicts or {"cad_to_mesh": "rebuild", "mesh_to_cad": "pass",
                                     "volume": "pass"}}


def _sound_ws(tmp_path, name, quality) -> str:
    ws = tmp_path / name
    ws.mkdir()
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"mesh_mode": "snappy", "cell_count": 6_700_000, "quality": {"fatal": [], **quality}}))
    return str(ws)


# the table

def test_the_table_says_which_retry_bears_on_which_refusal():
    assert lad.lever_bears("port_areas", "snappy") and lad.lever_bears("port_areas", "cfmesh")
    assert lad.lever_bears("quality_floor", "snappy")
    for gate in ("cad_fidelity", "solvability", "boundary_types"):
        assert not lad.lever_bears(gate, "snappy") and not lad.lever_bears(gate, "cfmesh")
    # a model-led builder reads the gate's words; a gate not listed keeps the ladder as it was
    assert lad.lever_bears("cad_fidelity", "gmsh")
    assert lad.lever_bears("resolution_floor", "snappy")


def test_a_refusal_with_a_lever_keeps_its_retry():
    s = _external(executor_failed_gate="port_areas")
    f = lad.classify(s)
    assert f is not None and f.kind == lad.FIXABLE
    assert not lad.refusal_without_lever(s)
    assert route_after_executor(s) == "node_classifier"


def test_a_refusal_with_no_lever_is_an_engine_failure_said_as_the_gate():
    f = lad.classify(_external())
    assert f is not None and f.kind == lad.ENGINE
    assert "did not follow your CAD" in f.reason and "nothing its next attempt changes" in f.reason


def test_no_lever_moves_the_run_to_the_open_engine_at_once():
    # cfMesh first: snappyHexMesh delivers the same contract (farfield, layers) and is untried
    s = _external(engine="cfmesh")
    d = lad.decide(s, record={})
    assert d.switch_to == "snappy"
    assert lad.another_engine_takes_over(s)
    assert route_after_executor(s) == "node_classifier"


def test_no_lever_and_no_open_engine_never_rebuilds(tmp_path):
    s = _external(engine="cfmesh", engine_ladder=_BOTH_TRIED, retry_count=2)
    d = lad.decide(s, record=_BOTH_TRIED)
    assert not d.switch_to
    assert d.why.startswith("no untried engine delivers the approved mesh, and nothing")
    # with no sound mesh the run ends (or wraps) - never the same case again
    assert route_after_executor(s) in ("__end__", "node_cfd_wrap")


# RM3-like: an assembly with hidden faces, refused by cad_fidelity on both hex engines

async def test_an_assembly_refused_for_fidelity_is_delivered_with_the_concern(tmp_path):
    ws1 = _sound_ws(tmp_path, "attempt_1", {"cad_fidelity": _fidelity()})
    ws2 = _sound_ws(tmp_path, "attempt_2", {"cad_fidelity": _fidelity(missed=0.19)})
    s = _external(engine="cfmesh", engine_ladder=_BOTH_TRIED, retry_count=2,
                  sound_attempts=[{"attempt": 1, "workspace": ws1, "engine": "snappy",
                                   "requirement_caveats": [], "refused": "cad_fidelity"},
                                  {"attempt": 2, "workspace": ws2, "engine": "cfmesh",
                                   "requirement_caveats": [], "refused": "cad_fidelity"}])
    assert route_after_executor(s) == "node_deliver_sound"
    out = await node_deliver_sound(s)
    assert out["executor_success"] is True and out["executor_failed_gate"] == ""
    assert "LAD-cfd-8" in out["engine_ladder"]["delivered_from"]["why"]
    # the concern is the delivered manifest's own fidelity record, read at the terminal (a concern,
    # never a pass)
    from meshpipeline.engines.cad_fidelity import concerns
    said = concerns(out["mesh_manifest"]["quality"]["cad_fidelity"])
    assert said and said[0].startswith("About")


def test_a_concern_names_the_finding_with_its_number_and_the_way_on():
    c = lad.deliverable_refusal("cad_fidelity", {"cad_fidelity": _fidelity()}, {})
    assert c is not None and c["kind"] == lad.GATE_CONCERN
    assert "14.6%" in c["words"] and c["way_on"]


def test_a_single_body_more_than_half_missed_stays_a_failure():
    assert lad.deliverable_refusal("cad_fidelity", {"cad_fidelity": _fidelity(missed=0.6, hidden=0)},
                                   {}) is None
    assert lad.deliverable_refusal("cad_fidelity", {"cad_fidelity": _fidelity(missed=0.1, off=0.7)},
                                   {}) is None
    big = _fidelity(missed=0.01, vol_err=-0.6, verdicts={"volume": "rebuild"})
    assert lad.deliverable_refusal("cad_fidelity", {"cad_fidelity": big}, {}) is None


def test_a_volume_refusal_is_said_as_the_volume():
    rec = _fidelity(missed=0.01, vol_err=0.3, verdicts={"volume": "rebuild", "cad_to_mesh": "pass"})
    c = lad.deliverable_refusal("cad_fidelity", {"cad_fidelity": rec}, {})
    assert c is not None and "volume is 30.0% off" in c["words"]


# the other deliverable gates

def test_a_port_area_concern_names_the_opening_and_both_areas():
    facts = {"misses": [{"name": "outlet", "delivered_m2": 0.0031, "expected_m2": 0.0012}]}
    c = lad.deliverable_refusal("port_areas", {}, facts)
    assert c is not None
    assert "'outlet'" in c["words"] and "3,100 mm2" in c["words"] and "1,200 mm2" in c["words"]
    assert lad.deliverable_refusal("port_areas", {}, {}) is None


def test_plain_skew_inside_the_solver_limit_is_deliverable_and_nothing_else_is():
    skew = {"checks": [{"key": "skew_fraction", "measured": 0.002}]}
    q = {"max_non_ortho": 72.0, "skew_fraction": 0.002}
    c = lad.deliverable_refusal("quality_floor", q, skew)
    assert c is not None and "72 degrees" in c["words"] and "correctors" in c["way_on"]
    assert lad.deliverable_refusal("quality_floor", {**q, "max_non_ortho": 88.0}, skew) is None
    for key in ("wall_faces", "rc", "timed_out", "layer_coverage_pct"):
        assert lad.deliverable_refusal("quality_floor", q, {"checks": [{"key": key}]}) is None
    # hard validity: broken cells are refused as today
    assert lad.deliverable_refusal("quality_floor", q, {"fatal": ["negative volume cells"]}) is None
    assert lad.deliverable_refusal("cad_fidelity", {"fatal": ["x"], "cad_fidelity": _fidelity()},
                                   {}) is None


def test_a_trial_solve_concern_says_what_the_trial_found():
    c = lad.deliverable_refusal("solvability", {"max_non_ortho": 40.0},
                                {"residual": 0.3, "iters": 500})
    assert c is not None and "did not converge" in c["words"] and "500 iterations" in c["words"]


def test_a_gate_outside_the_set_is_never_delivered():
    for gate in ("patch_contract", "boundary_types", "manifest_valid", "cell_limit"):
        assert lad.deliverable_refusal(gate, {}, {"misses": [{"name": "x"}]}) is None


# the best by the measured limits, not the last

def test_a_mesh_no_gate_refused_is_delivered_before_a_refused_one(tmp_path):
    note = {"kind": rp.RESOLUTION_SHORT, "cells_across": 8.0, "floor": 12.0}
    clean = _sound_ws(tmp_path, "attempt_1", {})
    refused = _sound_ws(tmp_path, "attempt_2", {})
    s = {"sound_attempts": [
        {"attempt": 1, "workspace": clean, "engine": "snappy", "requirement_caveats": [note]},
        {"attempt": 2, "workspace": refused, "engine": "cfmesh", "requirement_caveats": [],
         "refused": "cad_fidelity"}]}
    assert rp.deliverable_sound(s)["workspace"] == clean


# the facts the next attempt's plan reads

def test_the_refused_openings_reach_the_next_plan(tmp_path):
    write_gate_facts(tmp_path, "port_areas",
                     {"misses": [{"name": "outlet_2", "ratio": 2.6}], "detail": "x", "noise": 1})
    rec = json.loads((tmp_path / GATE_FACTS_FILE).read_text())
    assert rec["gate"] == "port_areas" and "noise" not in rec["facts"]
    assert missed_ports(tmp_path) == {"outlet_2"}
    write_gate_facts(tmp_path, "cad_fidelity", {"fidelity": {}})
    assert missed_ports(tmp_path) == set()
    assert missed_ports(tmp_path / "nowhere") == set()


def test_the_levers_are_carried_to_the_next_attempt():
    from meshpipeline.agents.builder.attempt import CARRY_FILES
    assert ".sealed_ports.json" in CARRY_FILES and GATE_FACTS_FILE in CARRY_FILES


def test_a_trial_solve_that_did_not_converge_moves_the_run_at_once():
    s = _external(engine="cfmesh", executor_failed_gate="solvability",
                  executor_failure_cause="not_solvable")
    assert lad.decide(s, record={}).switch_to == "snappy"
    assert route_after_executor(s) == "node_classifier"


def test_a_port_area_refusal_on_cfmesh_keeps_its_lever():
    s = _external(engine="cfmesh", executor_failed_gate="port_areas")
    assert lad.classify(s).kind == lad.FIXABLE


def test_the_delivery_reads_the_concern_first_with_the_way_on():
    from meshpipeline.application.final_result import (
        TerminalStatus,
        build_final_result,
        render_message,
    )
    facts = {"misses": [{"name": "outlet", "delivered_m2": 0.0031, "expected_m2": 0.0012}]}
    concern = lad.deliverable_refusal("port_areas", {}, facts)
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=3, attempts_max=3, required_ready=True, delivered_types=["mesh"],
        optional_warnings=[], requirement_caveats=[concern])
    msg = render_message(fr)
    assert msg.startswith("Delivered with concerns - one of our checks did not pass")
    lines = msg.splitlines()
    assert lines[1].startswith("  - Opening 'outlet' was built at 3,100 mm2")
    assert lines[2].startswith("    To get a better mesh: check the opening")
    assert "near-wall layer coverage" not in msg
