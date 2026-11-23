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
    # SW-17, with true words: the mesh was built and a named check refused it
    from meshpipeline.contracts import decision_record as dr
    assert d.why.startswith("the mesh was built but a check refused it") and "cad_fidelity" in d.why
    assert dr.switch_rule(d.why) == "SW-17"
    assert dr.switch({"why": d.why, "kind": "engine", "from": "cfmesh", "to": "snappy"}) == ("snappy", "SW-17")
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
    # LAD-cfd-11: past the solver's hard bar a valid mesh is still the last resort (pinned below)
    assert lad.deliverable_refusal("quality_floor", {**q, "max_non_ortho": None}, skew) is None
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


# LAD-cfd-9: two meshes the same gate refused - nearest the bar, then quality and the wall ask, never ladder order

def _refused(tmp_path, name, attempt, engine, *, ratio, coverage=None, non_ortho=60.0):
    import math
    q = {"max_non_ortho": non_ortho, **({} if coverage is None else {"layer_coverage_pct": coverage})}
    # the distance the executor records (gate_distance, pinned on its own below): past the 0.6 edge
    return {"attempt": attempt, "workspace": _sound_ws(tmp_path, name, q), "engine": engine,
            "requirement_caveats": [], "refused": "port_areas",
            "refused_distance": max(math.log(0.6 / ratio), math.log(ratio / 1.6), 0.0)}


def test_the_distance_from_the_port_band_is_the_log_ratio_past_its_edge():
    import math
    near = lad.gate_distance("port_areas", {}, {"misses": [{"name": "a", "ratio": 0.55}]})
    far = lad.gate_distance("port_areas", {}, {"misses": [{"name": "a", "ratio": 0.2},
                                                          {"name": "b", "ratio": 1.0}]})
    assert near == pytest.approx(math.log(0.6 / 0.55))
    assert far == pytest.approx(math.log(0.6 / 0.2))
    assert lad.gate_distance("port_areas", {}, {"misses": [{"name": "a", "ratio": 4.0}]}) == pytest.approx(
        math.log(4.0 / 1.6))
    # a port bound elsewhere is measured against the confirmed size's own band
    assert lad.gate_distance("port_areas", {}, {"misses": [{"name": "a", "ratio": 0.3,
                                                           "bound_elsewhere": True}]}) == pytest.approx(
        math.log((1 / 3) / 0.3))
    assert lad.gate_distance("port_areas", {}, {}) is None


def test_of_two_port_refusals_the_one_nearer_the_bar_is_delivered_whatever_came_last(tmp_path):
    near = _refused(tmp_path, "attempt_4", 4, "snappy", ratio=0.55)
    far = _refused(tmp_path, "attempt_5", 5, "cfmesh", ratio=0.2)
    assert rp.deliverable_sound({"sound_attempts": [near, far]})["workspace"] == near["workspace"]
    assert rp.deliverable_sound({"sound_attempts": [far, near]})["workspace"] == near["workspace"]


def test_as_far_off_as_each_other_the_wall_ask_decides_not_the_ladder(tmp_path):
    # abc 00001167-like: the inlet at 0.447 and 0.438 of its opening on both - the same miss - one mesh
    # keeps the layers asked on 89% of the wall, the other on 54%
    layered = _refused(tmp_path, "attempt_4", 4, "cfmesh", ratio=0.438, coverage=89.0, non_ortho=68.2)
    thin = _refused(tmp_path, "attempt_5", 5, "snappy", ratio=0.447, coverage=53.5, non_ortho=68.6)
    assert rp.deliverable_sound({"sound_attempts": [layered, thin]})["workspace"] == layered["workspace"]
    assert rp.deliverable_sound({"sound_attempts": [thin, layered]})["workspace"] == layered["workspace"]


def test_as_far_off_and_as_layered_the_cell_shape_decides(tmp_path):
    good = _refused(tmp_path, "attempt_2", 2, "snappy", ratio=0.5, coverage=90.0, non_ortho=62.0)
    worse = _refused(tmp_path, "attempt_3", 3, "cfmesh", ratio=0.5, coverage=91.0, non_ortho=79.0)
    past = _refused(tmp_path, "attempt_4", 4, "cfmesh", ratio=0.5, coverage=99.0, non_ortho=88.0)
    assert rp.deliverable_sound({"sound_attempts": [good, worse]})["workspace"] == good["workspace"]
    # past the solver's hard limit loses to one inside it, whatever its layers
    assert rp.deliverable_sound({"sound_attempts": [worse, past]})["workspace"] == worse["workspace"]


def test_a_mesh_no_gate_refused_still_beats_the_nearest_refused_one(tmp_path):
    clean = {"attempt": 1, "workspace": _sound_ws(tmp_path, "attempt_1", {}), "engine": "snappy",
             "requirement_caveats": []}
    near = _refused(tmp_path, "attempt_2", 2, "cfmesh", ratio=0.59)
    assert rp.deliverable_sound({"sound_attempts": [clean, near]})["workspace"] == clean["workspace"]


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


# LAD-cfd-8 applied to the region: a mesh of the part's metal or the space outside it

def _siphon_rec(off=0.8106, p50=47.5, source="fluid boundary staged from your file", flow="internal"):
    return {"flow": flow, "cad_source": source,
            "surface": {"mesh_to_cad": {"wall_off_share": off, "p50_cells": p50, "max_m": 0.1106}}}


def test_a_mesh_of_another_region_is_said_as_that():
    from meshpipeline.engines.cad_fidelity import wrong_region
    said = wrong_region(_siphon_rec())
    assert said.startswith("the mesh filled the part's metal or the space outside it, not the fluid")
    assert "81%" in said and "48 cells" in said and "111 mm" in said
    # a fidelity miss lies a cell or two off; an external mesh and a reference that bounds the metal cannot tell
    assert wrong_region(_siphon_rec(p50=1.4)) == ""
    assert wrong_region(_siphon_rec(off=0.3)) == ""
    assert wrong_region(_siphon_rec(flow="external")) == ""
    assert wrong_region(_siphon_rec(source="staged part surface and lids")) == ""


def test_a_wrong_region_runs_no_other_engine_and_no_wrap(monkeypatch):
    import meshpipeline.pipeline.graph as graph
    monkeypatch.setattr(graph._wrap, "wanted", lambda state: True)
    s = _external(engine="cfmesh", purpose="internal_cfd",
                  executor_failure_facts={"wrong_region": True, "detail": "the mesh filled the part's metal"})
    f = lad.classify(s)
    assert f is not None and f.kind == lad.NEVER and "every engine meshes the same staged region" in f.reason
    assert not lad.decide(s, record={}).switch_to
    assert lad.refused_as_wrong_region(s)
    assert route_after_executor(s) == "__end__"
    # and on a model-led engine too: no retry of the same region
    assert route_after_executor({**s, "engine": "gmsh"}) == "__end__"


def test_the_gate_refuses_a_wrong_region_with_its_reason_and_the_way_on(monkeypatch, tmp_path):
    import meshpipeline.settings.policy as pol
    from meshpipeline.engines import cad_fidelity as F
    from meshpipeline.engines.gates import facts_of
    monkeypatch.setattr(pol, "CAD_FIDELITY_RETRY_ENABLED", True)
    monkeypatch.setattr(F, "measure", lambda ws, **k: {**_siphon_rec(), "verdict": F.REBUILD,
                                                       "verdicts": {"mesh_to_cad": F.REBUILD}})
    monkeypatch.setattr(F, "record_into_manifest", lambda ws, rec: None)

    class _Ctx:
        workspace, intake_patches, manifest = tmp_path, [], {}

        def manifest_or_load(self):
            return {}
    ok, fb = F.gate_cad_fidelity(_Ctx())
    assert not ok and "filled the part's metal or the space outside it" in fb
    assert "Check the openings on the geometry stage" in fb and "no other engine was tried" in fb
    assert facts_of(fb).get("wrong_region") is True


# THE OPENING THE USER CONFIRMED: GL45's three 5.87 mm holes built as the 3.0 mm holes under their counterbores

GL45_PORTS = [{"name": n, "type": t, "diameter_mm": d} for n, t, d in (
    ("inlet", "inlet", 5.87), ("outlet_1", "outlet", 5.87), ("outlet_2", "outlet", 5.87),
    ("outlet_3", "outlet", 2.99), ("outlet_4", "outlet", 2.99), ("outlet_5", "outlet", 2.99))]


class _PortCtx:
    def __init__(self, manifest, ports):
        self._m, self.intake_patches = manifest, ports

    def manifest_or_load(self):
        return self._m


def _gl45_manifest():
    a = 7.0422e-6
    names = [p["name"] for p in GL45_PORTS]
    # the staging measured the same 3.0 mm holes it bound, so the measure agrees with the mesh
    return {"quality": {"patch_areas_m2": dict.fromkeys(names, a), "port_openings_m2": dict.fromkeys(names, a)}}


def test_a_port_built_far_from_the_confirmed_opening_is_refused_naming_both_areas():
    from meshpipeline.engines.gates import facts_of
    from meshpipeline.engines.region_check import gate_port_areas
    ok, fb = gate_port_areas(_PortCtx(_gl45_manifest(), GL45_PORTS))
    assert not ok
    misses = facts_of(fb)["misses"]
    assert sorted(m["name"] for m in misses) == ["inlet", "outlet_1", "outlet_2"]
    assert all(m["bound_elsewhere"] for m in misses)
    assert "'inlet' is 0.26 times the opening you confirmed (7 mm2 against 27 mm2)" in fb
    # a typed size the bore rounds (about 20 mm on a 32 mm bore: 2.6 in area) stays the measure's
    tee = [{"name": "inlet", "type": "inlet", "diameter_mm": 20.0}]
    bore = 3.1416 * 0.016 ** 2
    m = {"quality": {"patch_areas_m2": {"inlet": bore}, "port_openings_m2": {"inlet": bore}}}
    assert gate_port_areas(_PortCtx(m, tee)) == (True, "")


def test_a_port_bound_elsewhere_runs_no_other_engine_and_is_delivered_with_its_concern():
    from meshpipeline.engines.gates import facts_of
    from meshpipeline.engines.region_check import gate_port_areas
    _ok, fb = gate_port_areas(_PortCtx(_gl45_manifest(), GL45_PORTS))
    s = _external(engine="cfmesh", purpose="internal_cfd", executor_failed_gate="port_areas",
                  executor_failure_facts=facts_of(fb))
    f = lad.classify(s)
    assert f is not None and f.kind == lad.NEVER and "bound to other faces" in f.reason
    assert lad.refused_as_wrong_region(s)
    c = lad.deliverable_refusal("port_areas", {}, facts_of(fb))
    assert c is not None and "Opening 'inlet' was built at 7 mm2 against the 27 mm2 confirmed" in c["words"]
    assert "another face than the one you confirmed" in c["words"]


def test_an_opening_the_mesh_lost_reaches_the_next_plan(tmp_path):
    # abc 00063401: two cfMesh attempts of the same plan lost the 933 mm2 outlet_3 (zero faces), 24 and 23 min each
    write_gate_facts(tmp_path, "manifest_valid", {"patches": ["outlet_3"], "detail": "zero faces", "noise": 1})
    assert missed_ports(tmp_path) == {"outlet_3"}
    write_gate_facts(tmp_path, "manifest_valid", {"missing": ["car wall"], "renamed": {"car wall": "car_wall"}})
    assert missed_ports(tmp_path) == set(), "a renamed patch is the contract's, not an opening the mesher lost"


# LAD-cfd-11: a checkMesh-valid mesh past the solver's hard non-orthogonality is the last resort, never nothing

def _past(n: float) -> dict:
    return {"checks": [{"key": "max_non_ortho", "measured": n, "op": "<=", "threshold": 85.0}]}


def test_a_valid_mesh_past_the_hard_bar_is_kept_with_its_number_and_the_way_on():
    c = lad.deliverable_refusal("quality_floor", {"max_non_ortho": 86.56}, _past(86.56))
    assert c is not None and c["kind"] == lad.GATE_CONCERN and c["past_hard_non_ortho"] == 86.56
    assert "86.6 degrees" in c["words"] and "85 degrees" in c["words"] and "correctors" in c["way_on"]
    # broken cells stay refused, past the bar or not
    assert lad.deliverable_refusal("quality_floor", {"max_non_ortho": 86.5}, {**_past(86.5), "fatal": ["x"]}) is None
    assert lad.deliverable_refusal("quality_floor", {"max_non_ortho": 86.5, "fatal": ["x"]}, _past(86.5)) is None


def test_past_the_hard_bar_only_an_engine_with_a_lever_retries():
    assert not lad.lever_bears("quality_floor", "vmtk", _past(86.5))
    assert not lad.lever_bears("quality_floor", "gmsh", _past(86.5))
    assert lad.lever_bears("quality_floor", "snappy", _past(86.5))
    assert lad.lever_bears("quality_floor", "cfmesh", _past(86.5))
    # inside the bar the table speaks as before
    assert lad.lever_bears("quality_floor", "vmtk", _past(80.0)) and lad.lever_bears("quality_floor", "vmtk")


def _internal_vmtk(**kw) -> dict:
    base = {"engine": "vmtk", "purpose": "internal_cfd", "input_kind": "fluid-domain",
            "engine_params": {"wall_layers": "on"}, "executor_failed_gate": "quality_floor",
            "executor_failure_cause": "mesh_quality", "executor_failure_facts": _past(86.56),
            "intake_patches": [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
                               {"name": "wall", "type": "wall"}],
            "request_txt": "Internal flow through a pipe at 1 m/s."}
    return _external(**{**base, **kw})


def test_vmtk_past_the_hard_bar_moves_the_run_to_the_next_engine_at_once():
    s = _internal_vmtk()
    f = lad.classify(s)
    assert f is not None and f.kind == lad.ENGINE
    assert lad.refusal_without_lever(s)
    d = lad.decide(s, record={})
    assert d.switch_to and d.switch_to != "vmtk"
    assert route_after_executor(s) == "node_classifier"


def test_a_mesh_past_the_hard_bar_ranks_under_every_mesh_inside_it(tmp_path):
    # nearer its own bar and better layered, but past 85: the one inside 85 is delivered, in either order
    past = _refused(tmp_path, "attempt_1", 1, "vmtk", ratio=0.59, coverage=99.0, non_ortho=86.5)
    inside = _refused(tmp_path, "attempt_2", 2, "cfmesh", ratio=0.2, coverage=40.0, non_ortho=79.0)
    assert rp.deliverable_sound({"sound_attempts": [past, inside]})["workspace"] == inside["workspace"]
    assert rp.deliverable_sound({"sound_attempts": [inside, past]})["workspace"] == inside["workspace"]
    # ...and another gate's refusal inside 85 beats it too
    fid = {"attempt": 3, "workspace": _sound_ws(tmp_path, "attempt_3", {"max_non_ortho": 70.0}), "engine": "snappy",
           "requirement_caveats": [], "refused": "cad_fidelity", "refused_distance": 0.3}
    past_q = {**past, "refused": "quality_floor", "refused_distance": 86.5}
    assert rp.deliverable_sound({"sound_attempts": [fid, past_q]})["workspace"] == fid["workspace"]
    # alone, it is delivered: never nothing
    assert rp.deliverable_sound({"sound_attempts": [past_q]})["workspace"] == past["workspace"]


def test_vmtks_gate_refuses_past_the_hard_bar_and_names_the_number(tmp_path):
    from meshpipeline.engines.gates import GateCtx, facts_of
    from meshpipeline.engines.vmtk import criteria
    from meshpipeline.engines.vmtk.gates import _gate_tet_quality_floor
    assert criteria.HARD_NON_ORTHO == lad.SOLVER_HARD_NON_ORTHO
    for n, ok in ((86.56, False), (84.9, True), (None, True)):
        (tmp_path / "mesh_manifest.json").write_text(json.dumps(
            {"quality": {"min_quality": 0.2, "fatal": [], **({} if n is None else {"max_non_ortho": n})}}))
        got, why = _gate_tet_quality_floor(GateCtx(workspace=tmp_path, engine="vmtk", domain="", intake_patches=[],
                                                   engine_params={}))
        assert got is ok
        if not ok:
            assert "86.6 degrees" in str(why) and lad.past_hard_non_ortho(facts_of(why)) == 86.56


def test_when_no_later_engine_does_better_the_kept_mesh_is_delivered(tmp_path):
    kept = {"attempt": 1, "workspace": _sound_ws(tmp_path, "attempt_1", {"max_non_ortho": 86.56}), "engine": "vmtk",
            "requirement_caveats": [lad.deliverable_refusal("quality_floor", {"max_non_ortho": 86.56},
                                                            _past(86.56))],
            "refused": "quality_floor", "refused_distance": 86.56}
    # cfMesh took over and stopped before it finished; nothing is left to try
    s = _internal_vmtk(engine="cfmesh", executor_failed_gate="", executor_failure_cause="engine_crashed",
                       executor_failure_facts={}, retry_count=bcfg.MAX_BUILDER_RETRIES + 1, sound_attempts=[kept])
    assert route_after_executor(s) == "node_deliver_sound"
    assert rp.deliverable_sound(s)["workspace"] == kept["workspace"]
