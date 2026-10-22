# Responsibility: Verify an FEA job is reviewed by its own checklist and can never be failed by a review when the measured checks call its mesh valid, while a CFD review is unchanged.
# Boundaries: engines/fea_review (validity and the measured facts), review_policy's judgement, the run's terminal status in shadow (the review-led verdict, as dev runs today), the composed rubric, and the facts in the reviewer's brief. Pure; nothing meshes.
"""The real-path test of 2026-10-22: 14 of 24 valid FEA meshes were failed as "the wrong problem"
(requirement_absent on group_completeness, deck_conformance and restraint_load_surfaces) because the
viewer showed the outer surface as ONE group, "free", while every solver file named each CAD face.
These replay that review against the new rules."""
from __future__ import annotations

import json
import logging

import pytest

from meshpipeline.agents.reviewer.eligibility import AxisFinding
from meshpipeline.agents.reviewer.review_policy import (
    FEA_NEVER_FAILED,
    fea_valid,
    judge,
    measurements_from,
)
from meshpipeline.application import final_result as fr
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, InspectionTargetRef
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines import fea_review
from meshpipeline.engines.quality_criteria import compose_review_rubric
from meshpipeline.settings import policy as polcfg

jlog = logging.getLogger(__name__)

FEA_BRIEF = ("Mesh the bracket (mm, 60 x 50 x 10 mm) for static stress: a solid mesh of quadratic "
             "tetrahedra with every face named. Every face present as a named group, well-shaped "
             "elements, thin walls and small features kept.")


def _quality(**over) -> dict:
    q = {"cells": 244594, "nodes": 361167, "element_order": 2, "fatal": [], "fea_inverted": 0,
         "deck": {"face_sets": {f"Face_{k}": [] for k in range(1, 35)}},
         "fea": {"plan": True, "through_wall_min": 2.15, "limit": 8000000,
                 "exports": {"nastran": {"file": "mesh.bdf", "faces": 34, "elements": 244594},
                             "ansys": {"file": "mesh.cdb", "faces": 34, "elements": 244594}}}}
    q.update(over)
    return q


@pytest.fixture
def ledger():
    L = EvidenceLedger()
    ids = {"group": L.add_target_inspection(InspectionTargetRef(TargetKind.GROUP, "group:free"),
                                            "toggle_patch", image_ok=True, covered=True),
           "iso": L.add_render_view("iso", "mesh_paths.surface", "open", image_ok=True)}
    return L, ids


# the "free" group claim as the reviewer filed it on fea0100 (job 78be2f6e)
def _free_group_claim(ids, axis="group_completeness"):
    return AxisFinding(axis_key=axis, passed=False, evidence_ids=(ids["group"],),
                       finding="The available physical-group list contains only the patch free.",
                       wrong_problem="requirement_absent",
                       brief_requirement="every face present as a named group",
                       builder_change="boundaries", change_request="name every face separately")


# ------------------------------------------------------------------- measured validity ----
@pytest.mark.parametrize("quality, valid", [
    (_quality(), True),
    (_quality(fea_inverted=3), False),
    (_quality(fatal=["degenerate elements (SICN <= 0)"]), False),
    ({"cells": 10, "fatal": []}, None),                        # no FEA measurement: never said valid
    ({"fea": {"quality": {"fea_inverted": 0}}, "fatal": []}, True),
])
def test_validity_is_read_from_the_measurements_alone(quality, valid):
    assert fea_review.measured_valid(quality) is valid


def test_the_saved_purpose_decides_what_an_fea_job_is():
    assert fea_review.is_fea("structural")
    assert not fea_review.is_fea("internal_cfd") and not fea_review.is_fea("")


def test_the_measurements_carry_the_purpose_and_the_fea_validity():
    m = measurements_from({"quality": _quality()}, {"purpose": "structural"})
    assert m["purpose"] == "structural" and m["fea_valid"] is True and fea_valid(m)
    cfd = measurements_from({"quality": {"regions": 1}}, {"purpose": "external_cfd"})
    assert "fea_valid" not in cfd and not fea_valid(cfd)


# ------------------------------------------------------------------ the review's judgement ----
@pytest.mark.parametrize("axis", ["group_completeness", "deck_conformance", "restraint_load_surfaces",
                                  "fea_faces_named"])
def test_a_wrong_problem_claim_never_fails_a_valid_fea_mesh(ledger, axis):
    L, ids = ledger
    measured = measurements_from({"quality": _quality()}, {"purpose": "structural"})
    j = judge(_free_group_claim(ids, axis), brief_text=FEA_BRIEF, levers=("refinement", "boundaries"),
              ledger=L, measured=measured)
    assert j.blocking == "" and j.improve is False          # no failure, and no rebuild on a review's say
    assert j.reason == FEA_NEVER_FAILED and j.severity == "material"   # still shown, as a concern


def test_an_fea_mesh_the_measurements_call_invalid_keeps_the_ordinary_rules(ledger):
    L, ids = ledger
    measured = measurements_from({"quality": _quality(fea_inverted=2)}, {"purpose": "structural"})
    j = judge(_free_group_claim(ids), brief_text=FEA_BRIEF, levers=("refinement", "boundaries"),
              ledger=L, measured=measured)
    assert j.blocking == "requirement_absent"


def test_the_same_claim_on_a_cfd_mesh_still_blocks(ledger):
    L, ids = ledger
    j = judge(_free_group_claim(ids), brief_text=FEA_BRIEF, levers=("refinement", "boundaries"),
              ledger=L, measured=measurements_from({"quality": {"regions": 1}}, {"purpose": "internal_cfd"}))
    assert j.blocking == "requirement_absent"


# ------------------------------------------------------------- the run's terminal status ----
def _state(quality: dict, purpose: str = "structural") -> dict:
    findings = [{"axis_key": "fea_faces_named", "passed": False, "attempt": 0,
                 "finding": "The preview shows one group, free.", "blocking": "requirement_absent",
                 "improve": True, "severity": "material"}]
    return {"job_id": "f1", "engine": "gmsh", "purpose": purpose, "flow_topology": "",
            "engine_source": "suggested_confirmed", "intake_patches": [], "request_txt": FEA_BRIEF,
            "review_brief_txt": "", "api_failure": "", "executor_success": True, "executor_failed_gate": "",
            "reviewer_verdict": "FAIL", "retry_count": 0, "reviewer_axis_findings": findings,
            "requirement_caveats": [], "user_dispute": {}, "engine_ladder": {}, "review_history": [],
            "mesh_manifest": {"quality": quality, "patch_types": {"free": "free"}}}


@pytest.fixture
def shadow(monkeypatch):
    # dev-areen today: the review-led verdict decides (VERDICT_BY_LIMITS off)
    monkeypatch.setattr(polcfg, "VERDICT_BY_LIMITS", False)


def test_a_review_recorded_as_blocking_cannot_fail_a_valid_fea_run(shadow):
    outcome = fr.RunOutcome.from_graph_state(_state(_quality()))
    assert fr.fea_measured_valid(outcome)
    assert fr.review_blocking(outcome) == []
    assert fr.derive_terminal_status(outcome, job_id="j", jlog=jlog).succeeded


def test_an_invalid_fea_mesh_or_a_cfd_run_keeps_the_review_led_rule(shadow):
    bad = fr.RunOutcome.from_graph_state(_state(_quality(fea_inverted=1)))
    assert not fr.fea_measured_valid(bad) and fr.review_blocking(bad)
    cfd = fr.RunOutcome.from_graph_state(_state({"regions": 1}, purpose="internal_cfd"))
    assert not fr.fea_measured_valid(cfd) and fr.review_blocking(cfd)


# ------------------------------------------------------------------- the FEA checklist ----
def test_an_fea_job_is_reviewed_by_the_fea_checklist():
    names = [ax.name for ax in compose_review_rubric("gmsh", "structural")]
    for want in ("fea_right_part", "fea_faces_named", "fea_element_validity", "fea_detail",
                 "fea_solver_limits", "fea_honest_notes", "fea_files_load"):
        assert want in names
    # the CFD-era group/deck/element-order axes and the restraint/load axis are gone for FEA
    for gone in ("group_completeness", "deck_conformance", "element_order_appropriateness",
                 "restraint_load_surfaces"):
        assert gone not in names
    assert "element_quality_distribution" in names          # the engine's own quality read stays


def test_a_cfd_rubric_on_the_same_engine_is_unchanged():
    names = [ax.name for ax in compose_review_rubric("gmsh", "internal_cfd")]
    assert "group_completeness" in names and "deck_conformance" in names
    assert not any(n.startswith("fea_") for n in names)


# -------------------------------------------------------------- the facts in the brief ----
def test_the_brief_carries_the_measured_facts_per_solver_file(tmp_path):
    (tmp_path / "quality.json").write_text(json.dumps(_quality()))
    (tmp_path / "fea_plan.json").write_text(json.dumps({"faces": [{"name": f"Face_{k}"} for k in range(1, 35)],
                                                        "hot_spots": [{"id": "H1"}, {"id": "H2"}]}))
    for name in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        (tmp_path / name).write_text("x")
    text = fea_review.facts_text(tmp_path)
    assert "VALID - no inverted element" in text
    assert "CAD faces in the part: 34." in text
    for f in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        assert f"{f} (" in text and "34 named faces" in text
    assert "244,594 elements" in text
    assert fea_review.ONE_PREVIEW_GROUP in text
    assert fea_review.facts_text(tmp_path / "nothing") == ""
