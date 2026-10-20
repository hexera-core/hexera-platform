# Responsibility: Verify rule SW-16: a system-picked engine that made no mesh, or a broken one, hands the run
# to the next ranked engine that differs only in the KIND of mesh (format, cell type) - announced, recorded,
# and never for an engine the user named or for a mesh that only missed a measured bar.
# Boundaries: pipeline/engine_fallback.py and contracts/decision_record.py over plain states; no mesher runs.
from __future__ import annotations

import pytest

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.contracts import decision_record as dr
from meshpipeline.pipeline import engine_fallback as lad

PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
           {"name": "wall", "type": "wall"}]


@pytest.fixture(autouse=True)
def _ladder_on(monkeypatch):
    monkeypatch.setattr(polcfg, "ENGINE_FALLBACK_ENABLED", True)
    monkeypatch.setattr(bcfg, "MAX_BUILDER_RETRIES", 1)


def _vmtk_failed(cause: str, *, source: str = lad.SOURCE_SUGGESTED, facts: dict | None = None,
                 layers: bool = True) -> dict:
    """VMTK, the home engine for a tube, picked by the ranking and confirmed; its first attempt
    failed with `cause`."""
    return {"job_id": "j", "engine": "vmtk", "engine_source": source, "purpose": "internal_cfd",
            "input_kind": "fluid-domain", "dimensionality": "3D",
            "intake_patches": [dict(p) for p in PATCHES], "engine_params": {},
            "request_txt": "Internal CFD mesh: water at 5 m/s."
                           + (" Near-wall: 5 prism layers on the wall." if layers else ""),
            "review_brief_txt": "", "retry_count": 1, "executor_success": False,
            "executor_failed_gate": "finalize", "executor_failure_cause": cause,
            "executor_failure_facts": facts or {"meshers_started": 1},
            "builder_deadline_epoch": 0.0, "pipeline_deadline_epoch": 0.0,
            "geometry": {"ref": {"original_filename": "pipe.step", "suffix_hint": ".step"}},
            "engine_ladder": {"approved": "vmtk", "source": source,
                              "order": ["vmtk", "snappy", "cfmesh", "gmsh"]}}


def _decide(st):
    failure = lad.classify(st)
    return lad.decide(st, record=lad.with_attempt(st, failure), now=0.0)


def test_another_kind_of_mesh_is_an_offer_only_rung_unless_only_the_kind_differs():
    rungs = {r.engine: r for r in lad.ladder(_vmtk_failed("engine_timed_out"))}
    # snappy and cfMesh: an OpenFOAM hex-dominant mesh instead of VMTK's tetrahedral .vtu - kind only
    assert not rungs["snappy"].same_contract and rungs["snappy"].kind_only
    assert not rungs["cfmesh"].same_contract and rungs["cfmesh"].kind_only
    # Gmsh would also drop the requested prism layers: more than the kind, never moved to
    assert not rungs["gmsh"].kind_only


@pytest.mark.parametrize("cause,facts", [
    ("engine_timed_out", {"meshers_started": 1}),                      # the Fluent aorta
    ("mesh_quality", {"meshers_started": 1, "fatal": ["TetGen rejected the boundary"]}),  # orifice
    ("not_built", {"meshers_started": 0}),
])
def test_a_system_pick_that_made_no_usable_mesh_moves_on_across_kinds(cause, facts):
    d = _decide(_vmtk_failed(cause, facts=facts))
    assert d.switch_to == "snappy", d
    assert dr.switch_rule(d.why) == "SW-16"
    assert any("OpenFOAM" in c for c in d.changes) and any("cells instead of" in c for c in d.changes)


def test_never_for_an_engine_the_user_named():
    d = _decide(_vmtk_failed("engine_timed_out", source=lad.SOURCE_USER))
    assert not d.switch and dr.switch_rule(d.why) == "SW-8"


def test_a_mesh_that_only_missed_a_measured_bar_stays_on_its_kind():
    # an under-resolved VMTK mesh exists: its own levers answer it, never another kind of mesh
    st = _vmtk_failed("under_resolved")
    st["executor_failed_gate"] = "resolution_floor"
    d = _decide(st)
    assert d.switch_to != "snappy" and dr.switch_rule(d.why) != "SW-16", d


def test_no_regular_attempt_left_no_switch():
    st = _vmtk_failed("engine_timed_out")
    st["retry_count"] = 2
    assert not _decide(st).switch


def test_the_switch_is_said_with_what_changes_and_recorded_under_its_rule():
    d = _decide(_vmtk_failed("engine_timed_out"))
    note = lad.switch_note("vmtk", d.switch_to, d.failure, d.changes)
    assert note.startswith("VMTK could not mesh this shape")
    assert "you will get an OpenFOAM mesh instead of a VTK (.vtu) mesh" in note
    rec = lad.with_switch({}, attempt=2, frm="vmtk", to=d.switch_to, failure=d.failure, why=d.why,
                          changes=d.changes)
    sw = rec["switches"][0]
    assert sw["changes"] == list(d.changes)
    to, rule = dr.switch({"why": sw["why"], "kind": sw["kind"], "from": "vmtk", "to": sw["to"]})
    assert (to, rule) == ("snappy", "SW-16")
    rec = {**rec, "delivered_by": "snappy"}
    assert "you get an OpenFOAM mesh instead of a VTK (.vtu) mesh" in lad.delivered_note(rec)


def test_a_same_kind_switch_still_says_nothing_changes():
    from meshpipeline.pipeline.engine_fallback import Failure
    note = lad.switch_note("cfmesh", "snappy", Failure(lad.ENGINE, "mesh_quality", "it broke"))
    assert note.endswith("same boundaries, same units, same settings you approved.")


def test_the_pick_record_names_the_rule_the_recommendation_used():
    base = {"engine_source": "suggested_confirmed", "approved": "vmtk", "recommended": "vmtk",
            "measured": True}
    assert dr.engine_pick({**base, "recommended_rule": "ENG-2"}) == ("vmtk", "ENG-2")
    assert dr.engine_pick({**base, "recommended_rule": "ENG-3"}) == ("vmtk", "ENG-3")
    assert dr.engine_pick(base) == ("vmtk", "ENG-4")
    assert dr.engine_pick({**base, "recommended_rule": "ENG-6"}) == ("vmtk", "ENG-4")


def test_the_best_of_several_meshes_still_says_it_is_another_kind():
    # review_delivery picked attempt 2 of a run that moved VMTK -> snappyHexMesh (SW-16): the
    # delivered note names the attempt AND the change of mesh kind
    record = {"delivered_by": "snappy",
              "delivered_from": {"attempt": 2, "why": "it measured best of this run's meshes"},
              "switches": [{"attempt": 1, "from": "vmtk", "to": "snappy", "reason": "it timed out",
                            "changes": ["you would get an OpenFOAM mesh instead of a VTK one"]}]}
    note = lad.delivered_note(record)
    assert "attempt 2" in note
    assert "another kind of mesh than the one approved: you get an OpenFOAM mesh" in note
