# Responsibility: Verify the engine fallback ladder switches only when another engine delivers the approved mesh, and offers - never delivers - anything less.
from __future__ import annotations

import asyncio
import time

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.registry import engine_names, engines_producing_topology, get_spec
from meshpipeline.pipeline import engine_fallback as lad

EXTERNAL = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
INTERNAL = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "wall", "type": "wall"}]


def _external(**kw) -> dict:
    s = {"job_id": "j", "engine": "cfmesh", "engine_source": lad.SOURCE_SUGGESTED,
         "purpose": "external_cfd", "input_kind": "body-surface", "dimensionality": "3D",
         "intake_patches": [dict(p) for p in EXTERNAL], "engine_params": {},
         "request_txt": "External flow around a wing at 40 m/s, standard mesh.",
         "review_brief_txt": "", "retry_count": 1, "executor_success": False,
         "executor_failed_gate": "finalize", "builder_deadline_epoch": 0.0,
         "pipeline_deadline_epoch": 0.0, "engine_ladder": {}}
    s.update(kw)
    return s


def _fluid_domain(**kw) -> dict:
    s = {"job_id": "j", "engine": "gmsh", "engine_source": lad.SOURCE_SUGGESTED,
         "purpose": "internal_cfd", "input_kind": "fluid-domain", "dimensionality": "3D",
         "intake_patches": [dict(p) for p in INTERNAL], "engine_params": {"element_order": "2"},
         "request_txt": "Internal flow through a blade-row passage.", "review_brief_txt": "",
         "retry_count": 1, "executor_success": False, "executor_failed_gate": "sicn_floor",
         "builder_deadline_epoch": 0.0, "pipeline_deadline_epoch": 0.0, "engine_ladder": {}}
    s.update(kw)
    return s


@pytest.fixture
def quiet(monkeypatch):
    """Record what the node would publish and capture, instead of reaching a stream."""
    import meshpipeline.pipeline.engine_select as es

    said: list[str] = []
    events: list[dict] = []

    async def _pub(job_id, text, op_id):
        said.append(text)

    def _log(job_id, payload, *, op_id="engine-select"):
        events.append({"op_id": op_id, **payload})

    monkeypatch.setattr(es, "_publish", _pub)
    monkeypatch.setattr(es, "_log_selection", _log)
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 1)
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", True)
    return said, events


def _node(state: dict) -> dict:
    return asyncio.run(lad.node_engine_fallback(state))


# the declarations the ladder stands on

def test_every_engine_that_produces_a_topology_is_on_its_ladder():
    for topo, order in lad.FALLBACK_ORDER.items():
        missing = set(engines_producing_topology(topo)) - set(order)
        assert not missing, f"{sorted(missing)} produce {topo} flow but are not on its ladder"


def test_every_implemented_engine_declares_what_it_delivers():
    for name in engine_names():
        assert get_spec(name).delivered_mesh is not None, (
            f"{name} does not declare its delivered mesh - the ladder cannot compare it")


def test_every_failure_cause_has_a_ladder_class_and_every_gate_declares_one():
    # ONE vocabulary: the ladder classifies the executor's causes, never gate keys, so a new cause
    # or a gate without a declared cause cannot slip past it
    from meshpipeline.contracts.failure_cause import FailureCause
    assert set(lad._LADDER_CLASS) == set(FailureCause)
    for name in engine_names():
        for gate in get_spec(name).gates:
            assert gate.cause in lad._LADDER_CLASS, f"{name}.{gate.key} declares no known cause"


def test_a_cause_the_retry_policy_calls_hopeless_never_moves_the_run_unless_an_engine_changes_it():
    from meshpipeline.contracts.failure_cause import FailureCause, retry_can_help
    for cause in FailureCause:
        f = lad.classify({"engine": "cfmesh", "executor_success": False,
                          "executor_failed_gate": "manifest_valid",
                          "executor_failure_cause": cause.value})
        if not retry_can_help(cause):
            assert f.kind == lad.NEVER or cause in lad._ONLY_ANOTHER_ENGINE_CHANGES, (
                f"{cause} is hopeless to the retry policy but the ladder would switch on it")
    # the one the policy calls hopeless that another engine does change: a refused geometry
    assert lad.classify({"engine": "vmtk", "executor_failure_cause": "geometry_rejected",
                         "executor_failed_gate": "geometry"}).kind == lad.ENGINE
    # a mismatch the builder names (retry_may_fix) is retryable, and still never an engine switch
    assert lad.classify({"engine": "gmsh", "executor_failed_gate": "patch_contract",
                         "executor_failure_cause": "contract_mismatch",
                         "executor_failure_facts": {"retry_may_fix": True}}).kind == lad.NEVER


def test_a_record_without_a_cause_reads_the_cause_the_executor_would_have_recorded():
    # the gate's declared cause for this engine, else the seam's - the executor's own resolver
    assert lad.classify({"engine": "snappy", "executor_failed_gate": "patch_contract"}).kind \
        == lad.NEVER
    assert lad.classify({"engine": "snappy", "executor_failed_gate": "finalize"}).cause \
        == "engine_crashed"
    assert lad.classify({"engine": "gmsh", "executor_failed_gate": "sicn_floor"}).cause \
        == "mesh_quality"
    # nothing validated at all: the builder produced no mesh
    assert lad.classify({"engine": "cfmesh"}).kind == lad.ENGINE


# the switch happens on the right failures

@pytest.mark.parametrize("gate,cause", [
    ("finalize", "engine_crashed"), ("manifest_valid", "engine_crashed"),
    ("quality_floor", "mesh_quality"), ("resolution_floor", "under_resolved"),
    ("solvability", "not_solvable"), ("manifest_valid", "cell_budget"),
    ("manifest_valid", "patch_not_captured")])
def test_an_engine_that_cannot_mesh_the_shape_moves_to_the_same_contract_engine(quiet, gate,
                                                                              cause):
    said, events = quiet
    out = _node(_external(executor_failed_gate=gate, executor_failure_cause=cause))
    assert out["engine"] == "snappy", f"{gate} on cfMesh did not move the run to snappyHexMesh"
    assert out["builder_mode"] == "initial", "the new engine did not get a fresh build"
    assert out["builder_noop_count"] == 0
    sw = out["engine_ladder"]["switches"]
    assert sw == [sw[0]] and sw[0]["from"] == "cfmesh" and sw[0]["to"] == "snappy"
    assert said and "snappyHexMesh" in said[0] and "same boundaries" in said[0]
    assert events[0]["source"] == "fallback" and events[0]["chosen"] == "snappy"
    assert events[0]["from"] == "cfmesh" and events[0]["op_id"] == "engine-select:fallback:2"


def test_a_review_failure_is_a_retryable_failure_too(quiet):
    out = _node(_external(executor_success=True, executor_failed_gate="",
                          reviewer_verdict="FAIL"))
    assert out["engine"] == "snappy"
    assert out["engine_ladder"]["switches"][0]["because"] == "review"


def test_the_recorded_failure_cause_decides_when_it_is_present(quiet):
    # the executor's cause (contracts.failure_cause) outranks the coarse gate key
    out = _node(_external(executor_failed_gate="manifest_valid",
                          executor_failure_cause="engine_crashed"))
    assert out["engine_ladder"]["switches"][0]["kind"] == lad.ENGINE
    out = _node(_external(executor_failed_gate="manifest_valid",
                          executor_failure_cause="contract_mismatch"))
    assert "engine" not in out, "a contract mismatch - our own authoring - switched engines"


@pytest.mark.parametrize("gate,cause", [
    ("patch_contract", ""), ("boundary_types", ""), ("domain_extent", ""),
    ("patch_contract", "contract_mismatch"), ("boundary_types", "boundary_type"),
    ("domain_extent", "domain_extent"), ("manifest_valid", "contract_mismatch"),
    # a pre-flight refusal stands in for the gate it refused
    ("domain_extent", "domain_extent"), ("patch_contract", "contract_mismatch")])
def test_never_switches_on_a_failure_another_engine_cannot_fix(quiet, gate, cause):
    out = _node(_external(executor_failed_gate=gate, executor_failure_cause=cause))
    assert "engine" not in out, f"{gate}/{cause} moved the run to another engine"
    assert out["engine_ladder"]["attempts"][-1]["kind"] == lad.NEVER


def test_a_requirement_near_miss_is_never_a_reason_to_switch(quiet):
    out = _node(_external(executor_success=True, executor_failed_gate="",
                          requirement_caveats=[{"kind": "domain_extent"}]))
    assert "engine" not in out


# the switched engine keeps the approved contract

def test_a_switch_changes_the_engine_and_nothing_the_user_approved(quiet):
    before = _external()
    out = _node(before)
    assert set(out) == {"engine", "engine_params", "builder_mode", "builder_noop_count",
                        "classifier_result", "engine_ladder"}, (
        "the switch wrote a field the approval owns")
    # the new engine's planner is told what happened, not the old gate's engine-specific advice
    assert out["classifier_result"]["error_source"] == "engine_fallback"
    assert "FIRST attempt on snappyHexMesh" in out["classifier_result"]["summary"]
    # and the new engine admits the very same declaration, boundaries included
    ev = lad._declared_evidence(before, out["engine"])
    assert not get_spec(out["engine"]).admit(ev)
    assert ev.patches == tuple(lad._declared_evidence(before, "cfmesh").patches)
    assert out["engine_params"] == {}


def test_an_engine_that_cannot_build_the_declared_boundaries_is_not_a_rung():
    # a ground plane is built by snappy's box only; cfMesh refuses it at admission
    grounded = _external(engine="snappy", intake_patches=[
        {"name": "car", "type": "wall"}, {"name": "ground", "type": "wall"},
        {"name": "farfield", "type": "farfield"}])
    assert [r.engine for r in lad.ladder(grounded)] == ["snappy"]


def test_cfmesh_to_snappy_is_the_same_contract_and_the_reverse_is_not():
    up = {r.engine: r for r in lad.ladder(_external())}
    assert up["snappy"].same_contract and not up["snappy"].changes
    down = {r.engine: r for r in lad.ladder(_external(engine="snappy"))}
    assert not down["cfmesh"].same_contract
    assert any("staircased" in c for c in down["cfmesh"].changes)


def test_asked_for_layers_make_an_engine_without_reliable_layers_a_change():
    layered = _external(engine="snappy", request_txt="External aero, y+ ~1 with 8 prism layers.")
    cf = {r.engine: r for r in lad.ladder(layered)}["cfmesh"]
    assert any("prism layers" in c for c in cf.changes)
    # ...while moving UP from cfMesh keeps them: snappy delivers layers
    assert {r.engine: r for r in lad.ladder(_external(
        request_txt="8 prism layers please"))}["snappy"].same_contract


def test_a_tet_engine_is_never_the_same_contract_as_a_hex_one():
    rungs = {r.engine: r for r in lad.ladder(_fluid_domain(engine="snappy",
                                                             engine_params={}))}
    for tet in ("vmtk", "gmsh"):
        assert not rungs[tet].same_contract
        assert any("tetrahedral cells" in c for c in rungs[tet].changes)


# relaxing is offered, never silent

def test_a_relaxation_is_never_switched_to_on_its_own(quiet):
    out = _node(_fluid_domain())
    assert "engine" not in out, "the run moved from gmsh to a hex engine without asking"


def test_the_run_ends_with_an_offer_that_states_the_change():
    rec = lad.final_record(_fluid_domain(retry_count=2), succeeded=False, system_failure=False)
    offer = rec["offer"]
    assert offer["engine"] == "snappy" and offer["same_contract"] is False
    assert any("OpenFOAM" in c for c in offer["changes"])
    assert any("hex-dominant cells instead of tetrahedral" in c for c in offer["changes"])
    assert 'Reply "use snappyHexMesh"' in offer["text"]
    assert "did not switch without asking" in offer["text"]


def test_fewer_layers_is_offered_before_another_engine():
    # the mesh carries the layer-inversion signature: folded prism layers
    st = _external(engine="snappy", retry_count=2, executor_failed_gate="quality_floor",
                   request_txt="External aero around a wing, 8 prism layers, y+ 1.",
                   mesh_manifest={"quality": {"fatal": ["negative-volume cells"]}})
    offer = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    assert offer["kind"] == "fewer_layers"
    assert offer["layers_from"] == 8 and offer["layers_to"] == 4
    assert 'Reply "use 4 layers"' in offer["text"]


@pytest.mark.parametrize("gate,cause", [("quality_floor", "mesh_quality"),
                                        ("solvability", "not_solvable")])
def test_fewer_layers_needs_evidence_that_the_layers_were_to_blame(gate, cause):
    st = _external(engine="snappy", retry_count=2, executor_failed_gate=gate,
                   executor_failure_cause=cause,
                   request_txt="External aero around a wing, 8 prism layers, y+ 1.",
                   mesh_manifest={"quality": {"fatal": [], "max_non_ortho": 71.0}})
    offer = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    assert offer["kind"] == "engine", f"{gate} with no layer evidence offered fewer layers"
    # a check the executor recorded that names the layers is evidence too (#89's facts)
    named = lad.final_record({**st, "executor_failure_facts": {"checks": [
        {"key": "layer_coverage_pct", "measured": 12.0}]}}, succeeded=False,
        system_failure=False)["offer"]
    assert named["kind"] == "fewer_layers"


def test_fewer_layers_is_offered_for_a_review_only_when_the_review_faulted_layers():
    base = {"engine": "snappy", "retry_count": 2, "executor_success": True,
            "executor_failed_gate": "", "reviewer_verdict": "FAIL",
            "request_txt": "External aero, 8 prism layers."}
    layers = lad.final_record(_external(**base, reviewer_axis_findings=[
        {"axis_key": "prism_layer_coverage", "passed": False}]),
        succeeded=False, system_failure=False)["offer"]
    assert layers["kind"] == "fewer_layers"
    domain = lad.final_record(_external(**base, reviewer_axis_findings=[
        {"axis_key": "farfield_clearance", "passed": False}]),
        succeeded=False, system_failure=False)["offer"]
    assert domain["kind"] == "engine", "fewer layers was offered for a review about the domain"


def test_fewer_layers_is_not_offered_when_the_review_failed_more_than_the_layers():
    # job e0fa8ad0: prism coverage AND wake resolution failed, and the run offered fewer layers -
    # which cannot touch the wake
    base = {"engine": "snappy", "retry_count": 3, "executor_success": True,
            "executor_failed_gate": "", "reviewer_verdict": "FAIL",
            "request_txt": "Takeoff aero, 5 prism layers, wall functions."}
    offer = lad.final_record(_external(**base, reviewer_axis_findings=[
        {"axis_key": "prism_layer_coverage", "passed": False},
        {"axis_key": "wake_resolution", "passed": False},
        {"axis_key": "surface_capture", "passed": True}]),
        succeeded=False, system_failure=False)["offer"]
    assert offer is None or offer["kind"] != "fewer_layers"


def test_a_review_offer_names_the_review_never_a_mesher_that_could_not_finish():
    base = {"engine": "snappy", "retry_count": 3, "executor_success": True,
            "executor_failed_gate": "", "reviewer_verdict": "FAIL",
            "request_txt": "Takeoff aero, 5 prism layers, wall functions."}
    layers = lad.final_record(_external(**base, reviewer_axis_findings=[
        {"axis_key": "prism_layer_coverage", "passed": False}]),
        succeeded=False, system_failure=False)["offer"]
    assert layers["kind"] == "fewer_layers" and layers["layers_to"] == 2
    assert "could not finish" not in layers["text"]
    assert layers["text"].startswith("snappyHexMesh built the mesh and it passed every automatic "
                                     "check, but the review found its 5 near-wall layers")
    assert 'Reply "use 2 layers"' in layers["text"]
    # a gate failure keeps the mesher-could-not-finish wording, which is true there
    gate = lad.final_record(_external(
        engine="snappy", retry_count=2, executor_failed_gate="quality_floor",
        request_txt="External aero around a wing, 8 prism layers, y+ 1.",
        mesh_manifest={"quality": {"fatal": ["negative-volume cells"]}}),
        succeeded=False, system_failure=False)["offer"]
    assert gate["text"].startswith("snappyHexMesh could not finish this mesh with the 8")
    # an engine offer after a review says the mesh was built and reviewed, not unmeshable
    domain = lad.final_record(_external(**base, reviewer_axis_findings=[
        {"axis_key": "farfield_clearance", "passed": False}]),
        succeeded=False, system_failure=False)["offer"]
    assert domain["kind"] == "engine"
    assert domain["text"].startswith("snappyHexMesh built a mesh that passed every automatic "
                                     "check, but the review found it represents a different "
                                     "problem than you asked for.")
    assert "could not mesh this shape" not in domain["text"]


def test_after_a_switch_the_offer_names_every_engine_that_failed():
    st = _external(engine="snappy", retry_count=2, executor_failed_gate="finalize",
                   engine_ladder={"approved": "cfmesh", "source": lad.SOURCE_SUGGESTED,
                                  "attempts": [{"attempt": 1, "engine": "cfmesh",
                                                "kind": lad.ENGINE, "cause": "finalize"}],
                                  "switches": [{"attempt": 2, "from": "cfmesh",
                                                "to": "snappy"}]})
    offer = lad.final_record(st, succeeded=False, system_failure=False)["offer"]
    # external: after cfMesh and snappy, the only rung left is gmsh, which needs a fluid domain -
    # nothing is left to offer rather than an engine that cannot build the declaration
    assert offer is None
    internal = _fluid_domain(engine="snappy", engine_params={}, retry_count=2,
                             executor_failed_gate="finalize", input_kind="body-surface",
                             engine_ladder={"approved": "cfmesh", "attempts": [
                                 {"attempt": 1, "engine": "cfmesh", "kind": lad.ENGINE}]})
    offer = lad.final_record(internal, succeeded=False, system_failure=False)["offer"]
    assert offer["text"].startswith("Neither cfMesh nor snappyHexMesh could mesh this shape")
    assert offer["engine"] == "vmtk" and offer["same_contract"] is False


def test_no_offer_after_a_system_failure_or_a_success():
    assert lad.final_record(_external(), succeeded=False, system_failure=True)["offer"] is None
    rec = lad.final_record(_external(executor_success=True, executor_failed_gate="",
                                     reviewer_verdict="PASS"), succeeded=True,
                           system_failure=False)
    assert rec["offer"] is None and rec["delivered_by"] == "cfmesh"


def test_the_closing_message_carries_the_offer_and_the_switch():
    from meshpipeline.application import final_result as fr

    failed = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="gmsh",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="", failed_gate="sicn_floor", api_failure="",
        attempts=2, attempts_max=3, required_ready=False, delivered_types=[],
        optional_warnings=[])
    rec = lad.final_record(_fluid_domain(retry_count=2), succeeded=False, system_failure=False)
    text = fr.render_message(fr.with_engine_ladder(failed, rec))
    assert text.strip().endswith("if that works for you."), text
    # the record survives the durable round trip
    again = fr.FinalResult.from_dict(fr.with_engine_ladder(failed, rec).to_dict())
    assert again.engine_ladder == rec

    switched = _external(engine="snappy", retry_count=2, executor_success=True,
                         executor_failed_gate="", reviewer_verdict="PASS",
                         engine_ladder={"approved": "cfmesh", "switches": [
                             {"attempt": 2, "from": "cfmesh", "to": "snappy",
                              "reason": "it stopped before it finished the mesh"}],
                             "attempts": [{"attempt": 1, "engine": "cfmesh", "kind": "engine"}]})
    ok = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.succeeded, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=2, attempts_max=3, required_ready=True, delivered_types=["mesh_bundle"],
        optional_warnings=[])
    rec = lad.final_record(switched, succeeded=True, system_failure=False)
    text = fr.render_message(fr.with_engine_ladder(ok, rec))
    assert "The first engine, cfMesh, could not mesh this shape" in text
    assert "built with snappyHexMesh" in text


# budgets are respected

def test_with_only_one_retry_the_first_failure_goes_to_the_untried_engine(quiet):
    # MAX_BUILDER_RETRIES=1: a retry on cfMesh would leave snappy no attempt at all
    out = _node(_external(executor_failed_gate="quality_floor"))
    assert out["engine"] == "snappy"


def test_with_attempts_to_spare_a_fixable_failure_is_retried_here_first(quiet, monkeypatch):
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 3)
    out = _node(_external(executor_failed_gate="quality_floor"))
    assert "engine" not in out, "a fixable failure left the engine while attempts were spare"
    # ...until the same failure repeats on it
    again = _external(retry_count=2, executor_failed_gate="quality_floor",
                      engine_ladder=out["engine_ladder"])
    out2 = _node(again)
    assert out2["engine"] == "snappy"
    assert out2["engine_ladder"]["switches"][0]["why"] == "the same failure repeated here"


def test_a_structural_failure_switches_at_once_even_with_attempts_to_spare(quiet, monkeypatch):
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 3)
    assert _node(_external(executor_failed_gate="finalize"))["engine"] == "snappy"


def test_no_switch_without_a_regular_attempt_left(quiet):
    # the reviewer-feedback bonus attempt belongs to the engine that built the mesh
    out = _node(_external(retry_count=bcfg.MAX_BUILDER_RETRIES + 1, executor_success=True,
                          executor_failed_gate="", reviewer_verdict="FAIL"))
    assert "engine" not in out


def test_no_switch_without_the_time_for_the_new_engine(quiet):
    tight = time.time() + lad.rung_seconds("snappy") / 2
    out = _node(_external(builder_deadline_epoch=tight))
    assert "engine" not in out
    # the run still ends with the engine offered
    offer = lad.final_record(_external(builder_deadline_epoch=tight, retry_count=2),
                             succeeded=False, system_failure=False)["offer"]
    assert offer["engine"] == "snappy" and offer["same_contract"] is True
    assert "no attempt or time left" in offer["text"]


def test_a_switch_spends_the_runs_own_attempt_counter(quiet):
    out = _node(_external())
    assert "retry_count" not in out, "the ladder bought itself an extra attempt"


def test_an_engine_whose_input_admission_measures_the_geometry_is_never_switched_to(
        quiet, monkeypatch):
    # geometry admission runs once, before the first build; a mid-run switch would skip it
    real = lad._measures_its_input
    assert real("vmtk") and not real("snappy") and not real("cfmesh")
    monkeypatch.setattr(lad, "_measures_its_input", lambda e: e == "snappy")
    assert "engine" not in _node(_external())


def test_the_kill_switch_keeps_every_run_on_its_engine(quiet, monkeypatch):
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", False)
    assert "engine" not in _node(_external())


# a pinned engine is not switched without asking

def test_a_user_named_engine_is_never_switched(quiet):
    out = _node(_external(engine_source=lad.SOURCE_USER))
    assert "engine" not in out
    offer = lad.final_record(_external(engine_source=lad.SOURCE_USER, retry_count=2),
                             succeeded=False, system_failure=False)["offer"]
    assert offer["engine"] == "snappy" and offer["same_contract"] is True
    assert "You chose cfMesh, so I did not switch" in offer["text"]


@pytest.mark.parametrize("source", ["", "unknown", lad.SOURCE_USER])
def test_an_engine_of_unknown_provenance_reads_as_the_users(quiet, source):
    assert "engine" not in _node(_external(engine_source=source))


def test_a_dispute_keeps_its_engine_and_offers_nothing(quiet):
    st = _external(engine_source=lad.SOURCE_DISPUTE, user_dispute={"of_job_id": "p"})
    assert "engine" not in _node(st)
    assert [r.engine for r in lad.ladder(st)] == ["cfmesh"]
    assert lad.final_record({**st, "retry_count": 2}, succeeded=False,
                            system_failure=False)["offer"] is None


def test_an_engine_the_system_resolved_may_be_switched(quiet):
    assert _node(_external(engine_source=lad.SOURCE_SYSTEM))["engine"] == "snappy"


# who chose the engine

def _complete(engine: str, source: str) -> dict:
    return {"type": "intake_complete", "payload": {"mesh_engine": engine,
                                                   "engine_source": source}}


def test_provenance_is_read_from_the_approved_submission():
    from meshpipeline.pipeline.state_factory import engine_provenance

    events = [{"type": "intake_turn", "payload": {}}, _complete("snappy", "user_direct"),
              {"type": "intake_turn", "payload": {}}, _complete("cfmesh", "suggested_confirmed")]
    assert engine_provenance(events, "cfmesh") == "suggested_confirmed"
    # the latest submission is the approved one; an older one never speaks for it
    assert engine_provenance(events, "snappy") == "user_direct"
    assert engine_provenance(events[:2], "snappy") == "user_direct"


def test_unreadable_provenance_is_the_users():
    from meshpipeline.pipeline.state_factory import engine_provenance

    assert engine_provenance([], "cfmesh") == "user_direct"
    assert engine_provenance([_complete("cfmesh", "weird")], "cfmesh") == "user_direct"
    assert engine_provenance([_complete("snappy", "suggested_confirmed")], "cfmesh") == "user_direct"
    assert engine_provenance([_complete("cfmesh", "suggested_confirmed")], "cfmesh",
                             dispute=True) == "dispute"
    assert engine_provenance([], "") == "system"


def test_provenance_reaches_the_run_with_data_collection_off():
    # with collection off no intake_complete is buffered; the approval's own provenance event is
    import inspect

    import meshpipeline.agents.intake.approval as ap
    from meshpipeline.pipeline.state_factory import engine_provenance

    ev = ap.engine_provenance_event({"mesh_engine": "cfmesh",
                                     "engine_source": "suggested_confirmed",
                                     "request_txt": "never carried"})
    assert ev == {"type": "engine_provenance",
                  "payload": {"mesh_engine": "cfmesh", "engine_source": "suggested_confirmed"}}
    assert engine_provenance([ev], "cfmesh") == "suggested_confirmed"
    # the dispatched provenance is the approved one: it wins over an older buffered submission
    assert engine_provenance([_complete("cfmesh", "user_direct"), ev], "cfmesh") \
        == "suggested_confirmed"
    src = inspect.getsource(ap._build_dispatch_payload)
    assert "engine_provenance_event(approved)" in src, "the dispatch no longer carries provenance"
    assert "data_collection" not in src, "provenance must not depend on the collection mode"


def test_an_admission_refusal_is_not_recorded_as_a_build():
    st = _fluid_domain(engine="vmtk", engine_params={"wall_layers": "on"},
                       retry_count=bcfg.MAX_BUILDER_RETRIES + 1, executor_failed_gate="geometry",
                       geometry_unsuitable_reason="[GEOMETRY_UNSUITABLE] the surface self-"
                                                  "intersects")
    rec = lad.final_record(st, succeeded=False, system_failure=False)
    assert rec["attempts"] == [{"attempt": 0, "engine": "vmtk", "kind": lad.ENGINE,
                                "cause": "geometry_rejected",
                                "reason": "it cannot take this geometry as it is",
                                "refused_before_building": True}]
    # and the refusal still ends with an out: another engine, differences stated
    assert rec["offer"]["engine"] == "snappy" and rec["offer"]["same_contract"] is False


def test_the_run_seeds_provenance_after_the_pin_and_the_dispute():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    at = src.index("engine_provenance(")
    assert src.index("pin_selected_engine(") < at and src.index("seed_dispute_run(") < at


# the graph

def test_the_ladder_sits_between_the_classifier_and_the_builder():
    from unittest.mock import patch

    import meshpipeline.pipeline.graph as g

    class _Rec:
        def __init__(self, *a, **k):
            self.nodes, self.edges = set(), []

        def add_node(self, name, fn=None):
            self.nodes.add(name)

        def add_edge(self, a, b):
            self.edges.append((a, b))

        def add_conditional_edges(self, *a, **k):
            pass

        def compile(self, **k):
            return self

    rec = _Rec()
    with patch.object(g, "StateGraph", return_value=rec):
        g.build_graph(checkpointer=object())
    assert "node_engine_fallback" in rec.nodes
    assert ("node_classifier", "node_engine_fallback") in rec.edges
    assert ("node_engine_fallback", "node_builder") in rec.edges
    assert ("node_classifier", "node_builder") not in rec.edges


# what the brief asked for

@pytest.mark.parametrize("text,requested,count", [
    ("8 prism layers at expansion 1.2", True, 8),
    ("wall-resolved, y+ ~ 1", True, None),
    ("5 boundary layers and 3 inflation layers", True, 5),
    ("no prism layers needed, a coarse draft", False, None),
    ("without boundary layers", False, None),
    ("flow around a car at 40 m/s", False, None),
])
def test_the_layer_request_is_read_from_the_brief(text, requested, count):
    lr = lad.layer_request(text)
    assert (lr.requested, lr.count) == (requested, count)


# what the corpus learns

def test_a_switch_reaches_the_training_trajectory_with_its_reason():
    from meshpipeline.capture.trajectory import _engine_select_episode

    ep = _engine_select_episode({"chosen": "snappy", "source": "fallback", "from": "cfmesh",
                                 "because": "finalize", "ladder": []})
    assert ep["decision"] == {"chosen": "snappy", "source": "fallback", "from": "cfmesh",
                              "because": "finalize"}
    # a start-of-run selection keeps its old shape
    assert _engine_select_episode({"chosen": "cfmesh", "source": "user"})["decision"] == {
        "chosen": "cfmesh", "source": "user"}
