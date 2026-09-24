# Responsibility: Step 4 - the measurement checking the look and the look checking the measurement - runs on THIS
#                 platform's own survey path, and a contradiction it finds becomes a question the customer is put.
# Boundaries: pure functions over stored measurement fixtures and a look block built here. The package decides
#             what contradicts what; this file only pins that the platform reaches the layer and feeds the finder.
#
# WHY THIS FILE EXISTS. `geometry_agent.chain.job.run_job` runs step 4 between the look and the questions, and
# NOTHING IN THIS PLATFORM CALLS run_job. This module composes its own document out of `hexera.report_measured`
# and finds its questions with `ask.intake.ask_intake`, so the layer was wired for the package's own chain, its
# tests and its eval, and an upload never reached it. `ask.uncertainty.dispute_uncertainty` sat behind
# `if reconciliation is None: return []` and this platform passed no reconciliation on any line, so on every job
# any customer has ever run it returned an empty list.
#
# A function that exists is not a function that runs. What is pinned below is the CALL and the QUESTION, and the
# before-and-after is measured on the same document rather than argued: the finder is run once with step 4's
# output and once without, and the difference is the question.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests._surveyor_package import require

require("geometry_agent.reconcile.joint", needs="step 4, the reconciliation between the measurement and the look")

from meshpipeline.application import geometry_survey as gs  # noqa: E402

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"

#: A reply that answers every field the shipped prompt asks and DISAGREES with the measurement about the thin
#: regions: it names one where the measurement found no thin cluster. `thin_parts_vs_clusters` is used because it
#: is the commonest dispute on the corpus and because it reads neither the stance detector nor the flow profile,
#: neither of which this platform can give step 4 (see `geometry_survey._reconciled`).
SAW = {
    "looks_like": "a straight length of pipe",
    "confidence": "medium",
    "openings_seen": [{"id": "o1", "looks_like": "open end", "likely_role": "inlet", "mouth": "circular",
                       "why": "a round opening at one end"},
                      {"id": "o2", "looks_like": "open end", "likely_role": "outlet", "mouth": "circular",
                       "why": "a round opening at the other end"}],
    "internal_features": ["a plain bore, unobstructed along its length"],
    "thin_parts": ["the pipe wall looks thin"],
    "orientation": "the longest axis runs along X",
    "symmetry": "mirror symmetric about the mid plane",
    "sharp_edges": ["the mouth rims are sharp"],
    "notes": "nothing else of note",
}
#: The stored measurement the dispute lands on. A fluid domain with two mouths and no thin cluster.
CASE = "transition_007_fluid"


def _look() -> dict:
    from geometry_agent.agent import hexera
    from geometry_agent.vision import prompt as vp
    imp, _ = vp.impression_from_reply(json.dumps(SAW))
    return hexera.look_block(imp, model="test", provider="test", seconds=0.0, views=["view"],
                             drawn={"marks": [{"id": "A", "cut": 1, "kind": "void"}], "stops": []})


def _document(case: str = CASE, *, look: dict | None = None) -> dict:
    doc = dict(json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8")))
    if look is not None:
        doc["look"] = look
    return doc


@pytest.fixture(scope="module")
def disputed():
    return gs.composition(_document(look=_look()), purpose="internal_cfd", brief="")


def test_step_four_runs_on_the_platforms_own_survey_path(disputed):
    from geometry_agent.reconcile import verdict as rv
    joint = disputed["joint"]
    assert joint is not None, "the platform composed a survey over a look and reconciled nothing"
    assert isinstance(joint.reconciliation, rv.Reconciliation)
    assert sum(joint.reconciliation.counts.values()) > 0, joint.reconciliation.counts


def test_a_contradiction_becomes_a_question_this_platform_actually_puts(disputed):
    """The whole point, and the before is measured rather than remembered.

    The finder is the same one, over the same document, run twice: once as this module now calls it and once as
    it called it until today, with no reconciliation. `q_look_dispute` exists in the first and not the second.
    """
    joint = disputed["joint"]
    assert joint.contradictions >= 1, joint.reconciliation.counts
    assert "u_dispute" in [u.id for u in disputed["asked"].uncertainties]
    assert "q_look_dispute" in [q.id for q in disputed["asked"].questions]

    from geometry_agent.ask import intake as ask_intake
    before = ask_intake.ask_intake(disputed["composed"], declared=None, brief=None)
    assert "q_look_dispute" not in [q.id for q in before.questions]
    assert "u_dispute" not in [u.id for u in before.uncertainties]


def test_the_question_has_a_default_and_does_not_resolve_the_dispute_itself(disputed):
    """A contradiction means ONE OF THESE TWO IS WRONG, and the measurement has its own failure modes. A
    question with no default stops the job; one that says the look is wrong has decided what it is asking."""
    q = next(q for q in disputed["asked"].questions if q.id == "q_look_dispute")
    assert q.default, "a dispute with no default is a question that stops the job"
    words = " ".join([str(q.text), str(q.default), *(str(o) for o in (q.options or []))]).lower()
    assert "the look is wrong" not in words
    assert "the measurement is wrong" not in words


def test_a_document_with_no_look_reconciles_nothing_and_composes_as_it_always_did():
    """One source is nothing to reconcile, and the survey is what it was before step 4 existed."""
    made = gs.composition(_document(), purpose="internal_cfd", brief="")
    assert made["joint"] is None
    assert "q_look_dispute" not in [q.id for q in made["asked"].questions]


def test_a_failed_look_is_not_read_as_a_reply_that_agreed():
    from geometry_agent.agent import hexera
    failed = hexera.look_block(None, status="failed", reason="the look did not happen")
    made = gs.composition(_document(look=failed), purpose="internal_cfd", brief="")
    assert made["joint"] is None, "a look that failed has one source and nothing to reconcile"


def test_step_four_never_costs_the_composition(monkeypatch):
    """Pure addition. A fault inside step 4 costs the composition its reconciliation and nothing else."""
    from geometry_agent.reconcile import joint as rjoint

    def boom(*a, **k):
        raise RuntimeError("step 4 broke")

    monkeypatch.setattr(rjoint, "for_job", boom)
    made = gs.composition(_document(look=_look()), purpose="internal_cfd", brief="")
    assert made["joint"] is None
    assert made["survey"] is not None
    assert [q.kind for q in made["asked"].questions]


def test_what_this_platform_cannot_give_step_four_is_recorded_and_not_guessed(disputed):
    """THE TRIANGLES ARE NOT HERE. `composition` holds a stored measurement and a look, never the bytes, so the
    41-station profile refuses and every claim and signal behind the axial channel abstains. The absence is
    carried on the block rather than read as agreement."""
    joint = disputed["joint"]
    missing = sorted(k for k, ok in (joint.readers or {}).items() if not ok)
    assert missing == ["axial_profile", "mouth_march", "stance"], joint.readers
    assert joint.profile is not None and joint.profile.refused_by == "no_mesh"
    assert "no file was named" in joint.profile.why_not
    assert joint.typed_block()["channels_not_read"] == missing
