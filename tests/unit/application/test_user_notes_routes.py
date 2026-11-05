# Responsibility: Verify the user's own notes reach the AI steps of each route family (CFD, FEA, ECXML electronics), ride the dispatch into the run, never act as a check, and come back as an optional line on the result.
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from meshpipeline.contracts import user_notes as un

SPEC = ("Assembly of 6 solids, mm.\n"
        "- The small vent holes on the side can be removed for meshing.\n"
        "- Refine around the four bolt holes.\n"
        "- Load: 900 daN on the lug, ramped over 3 s, held 0.5 s.")
NOTES = un.clean([un.make(SPEC, source=un.SOURCE_CHAT, turn=2)])


# CFD: the snappy planner reads them, and its one-line remark reaches the result

def test_cfd_the_snappy_plan_is_shown_the_notes_and_its_remark_is_kept(tmp_path, monkeypatch):
    import meshpipeline.cad.analysis as analysis
    import meshpipeline.cad.prepared_surface as prepared
    import meshpipeline.contracts.execution_guard as guard
    from meshpipeline.contracts import model_inference as llm
    from meshpipeline.contracts.model_inference import ModelRoundResult
    from meshpipeline.engines.snappy import planner

    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
    un.write_file(tmp_path, NOTES)
    monkeypatch.setattr(prepared, "require_metre_surface", lambda *a, **k: None)
    monkeypatch.setattr(analysis, "analyze_surface", lambda s: {
        "diag": 1.0, "extent": [1, 1, 1], "surface_area": 1.0, "min_feature": 0.01})
    monkeypatch.setattr(analysis, "recommend_refinement", lambda a, **k: {
        "surface_level": 3, "feature_level": 4})

    async def _owner(*a, **k):
        return None
    monkeypatch.setattr(guard, "assert_still_owner", _owner)
    seen: dict = {}

    async def _plan(messages, **kw):
        seen["user"] = messages[-1]["content"]
        return ModelRoundResult(assistant_text=json.dumps(
            {"approach": "body-fitted", "n_layers": 10, "max_cells": 2_000_000,
             "notes_remark": "refined the four bolt holes; the vent holes stay (not removed)"}),
            finish_reason="stop")
    monkeypatch.setattr(llm, "call_planner_model", _plan)
    out = asyncio.run(planner.plan_with_accounting(workspace=tmp_path, job_id="j", request_txt="r"))
    assert SPEC in seen["user"] and "GUIDANCE" in seen["user"]
    assert out.plan and "notes_remark" not in out.plan
    assert un.read_remarks(tmp_path) == [
        "refined the four bolt holes; the vent holes stay (not removed)"]


def test_cfd_a_run_without_notes_shows_the_planner_exactly_what_it_did(tmp_path):
    assert un.workspace_block(tmp_path, consumer="snappy_planner") == ""


# FEA: the judgement calls are shown them; a run without notes asks the question it always did

def _record():
    return {"faces": {"1": {"thickness_min_mm": 4.0}}, "bodies": 6, "hot_spots": [], "micro": [],
            "thickness": {"min_mm": 4.0}}


def test_fea_the_ai_step_is_shown_the_notes_verbatim_and_may_say_what_it_did():
    from meshpipeline.engines import fea_ai as FA
    with_notes = FA.brief(_record(), {"user_notes": NOTES}, [], diag_m=0.5, limit=500_000)
    bare = FA.brief(_record(), {}, [], diag_m=0.5, limit=500_000)
    assert SPEC in with_notes["user_notes"] and "set aside" in with_notes["user_notes_fea"]
    assert "user_notes" not in bare and "user_notes_fea" not in bare
    # the tool gains the optional remark only with notes, so the decision cache keys are unchanged
    assert FA.decision_tool() == FA.decision_tool(with_notes=False)
    props = FA.decision_tool(with_notes=True)["function"]["parameters"]
    assert "notes_remark" in props["properties"] and "notes_remark" not in props["required"]
    answer = json.dumps({"analysis": "static", "matters": "the bolt holes", "zones": [],
                         "features": [], "joints": [], "budget_elements": 0, "words": [],
                         "notes_remark": "refined at the bolt holes; the vent holes stay"})
    decision, _ = FA.validate(answer, with_notes)
    assert decision["notes_remark"] == "refined at the bolt holes; the vent holes stay"
    # a remark with no notes shown is not kept
    assert "notes_remark" not in FA.validate(answer, bare)[0]


# ECXML (electronics): no AI step in the snapgrid build, so the intake asks and the reviewer reads

def test_electronics_the_reviewer_reads_them_as_context_never_as_a_criterion(tmp_path, monkeypatch):
    from meshpipeline.agents.reviewer import visual

    class _Pub:
        async def astage(self, **k): ...
        async def anote(self, *a, **k): ...
        async def ascreenshot(self, *a, **k): ...
        async def averdict(self, *a, **k): ...
        async def awarn(self, *a, **k): ...
    monkeypatch.setattr(visual, "execution_publisher", lambda *a, **k: _Pub())
    monkeypatch.setattr(visual, "validate_plan", lambda spec, plan: (False, ["stop here"]))
    seen: dict = {}

    async def _stop(inputs, *a, **k):
        seen["inputs"] = inputs
        return {}
    monkeypatch.setattr(visual, "_nonverdict", _stop)
    state = {"job_id": "j", "openfoam_workspace": str(tmp_path), "engine": "snapgrid",
             "purpose": "conjugate_heat_transfer", "executor_success": True,
             "mesh_manifest": {"mesh_units": "m", "mesh_paths": {"surface": "x"}},
             "request_txt": "Cool the board.", "review_brief_txt": "Every part its own region.",
             "user_notes": NOTES}
    asyncio.run(visual.node_reviewer(state))
    inputs = seen["inputs"]
    assert SPEC in inputs.review_brief and "CONTEXT ONLY" in inputs.review_brief
    # the text a finding may quote to fail or rebuild never carries a note (NOTE-3)
    assert SPEC not in inputs.brief_text


# the dispatch carries them into the run; the result says what was done with them

def test_the_dispatch_carries_the_approved_notes_and_the_boxes_since(monkeypatch):
    import meshpipeline.application.dispatch_contract as dc
    from meshpipeline.agents.intake import approval as ap
    captured: dict = {}
    monkeypatch.setattr(dc, "build", lambda **f: captured.setdefault("f", f))
    ref = SimpleNamespace(to_payload=lambda: {})
    box = un.make("Solver: Abaqus/Explicit", source=un.SOURCE_CONSOLE)
    snapshot = {"id": "a1", "payload": {"user_notes": NOTES, "patches": []}, "intent_canonical": {}}
    session = SimpleNamespace(llm_metadata=[], user_notes=[box])
    ap._build_dispatch_payload(job_id="j", owner_id="u", session_id="s", session=session,
                               snapshot=snapshot, source_ref=ref, interp_ref=ref)
    assert [n["text"] for n in captured["f"]["user_notes"]] == [SPEC, "Solver: Abaqus/Explicit"]
    captured.clear()
    ap._build_dispatch_payload(job_id="j", owner_id="u", session_id="s",
                               session=SimpleNamespace(llm_metadata=[], user_notes=None),
                               snapshot={"id": "a1", "payload": {"patches": []}},
                               source_ref=ref, interp_ref=ref)
    assert "user_notes" not in captured["f"]          # a run without notes: the payload it always had
    assert "user_notes" in dc.accepted_keys()


def test_the_run_state_holds_them_verbatim():
    from meshpipeline.application.pipeline_run import JobRequest
    from meshpipeline.pipeline.state_factory import make_pipeline_state
    req = JobRequest("j", user_notes=NOTES)
    assert [n["text"] for n in req.user_notes] == [SPEC]
    assert make_pipeline_state("j", "u", user_notes=req.user_notes)["user_notes"][0]["text"] == SPEC
    assert make_pipeline_state("j", "u")["user_notes"] == []


def test_the_result_says_what_was_done_with_them_and_a_note_never_fails_it():
    from meshpipeline.application import final_result as fr
    base = fr.build_final_result(
        job_id="j", owner_id="u", status=fr.TerminalStatus.succeeded, engine="gmsh",
        purpose="structural", dimensionality="3D", approved_snapshot_id="", executor_success=True,
        reviewer_verdict="PASS", failed_gate="", api_failure="", attempts=1, attempts_max=3,
        required_ready=True, delivered_types=["mesh"], optional_warnings=[])
    told = fr.with_notes_remarks(base, ["refined at the bolt holes; the vent holes stay"])
    assert told.status is base.status and told.reviewer_verdict == base.reviewer_verdict
    msg = fr.render_message(told)
    assert "From your notes: refined at the bolt holes; the vent holes stay" in msg
    assert fr.render_message(base) == fr.render_message(fr.with_notes_remarks(base, []))
    back = fr.FinalResult.from_dict(told.to_dict())
    assert back.notes_remarks == told.notes_remarks


def test_the_notes_line_never_vanishes_when_the_run_moved_to_another_engine(tmp_path):
    """The exhaust header of the 2 Nov dry run: VMTK could not finish, snappyHexMesh built the mesh,
    and no step left a remark - the result lost its "From your notes" line. It now says where the
    notes went; a step's own remark still wins."""
    from meshpipeline.application.pipeline_run import _notes_remarks
    state = {"user_notes": NOTES, "openfoam_workspace": str(tmp_path), "engine": "snappy",
             "engine_ladder": {"attempts": [{"attempt": 1, "engine": "vmtk", "kind": "engine"},
                                            {"attempt": 2, "engine": "snappy", "kind": "passed"}]}}
    (line,) = _notes_remarks(state)
    assert line.startswith("they went to every attempt of this run (VMTK, then snappyHexMesh)")
    assert un.result_lines([line])[0].startswith("From your notes: they went to every attempt")
    un.record_remark(tmp_path, "refined the merge region", by="snappy_planner")
    assert _notes_remarks(state) == ["refined the merge region"]
    assert _notes_remarks({"openfoam_workspace": str(tmp_path)}) == [], "no notes, no line"


def test_the_why_record_and_run_stamp_name_the_notes(tmp_path):
    from meshpipeline.application import run_reproducibility, why_record
    un.record_remark(tmp_path, "refined at the bolt holes", by="fea_ai")
    state = {"user_notes": NOTES, "openfoam_workspace": str(tmp_path)}
    entry = why_record.user_notes_entry(state)
    assert entry.rule == un.RULE_EVERY_ROUTE and entry.value == 1
    assert "refined at the bolt holes" in entry.words
    assert why_record.user_notes_entry({}) is None
    rec = run_reproducibility.build("j", state)
    assert rec["user_notes"]["count"] == 1 and rec["user_notes"]["remarks"] == ["refined at the bolt holes"]
