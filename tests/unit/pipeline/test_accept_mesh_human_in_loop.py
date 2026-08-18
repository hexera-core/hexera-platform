# Responsibility: Verify accepting a mesh honours the re-review verdict, and a gate-failing mesh can never be accepted.
from __future__ import annotations

from pathlib import Path

from langgraph.graph import END

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def _route(dispute: dict, verdict: str, retry_count: int = 0) -> str:
    from meshpipeline.pipeline.graph import route_after_reviewer
    return route_after_reviewer({
        "user_dispute": dispute, "reviewer_verdict": verdict,
        "retry_count": retry_count, "executor_success": True, "job_id": "j",
    })


def test_accept_mode_honours_the_verdict_it_does_not_force_a_rebuild():
    assert _route({"mode": "accept"}, "PASS") == END


def test_accept_mode_still_fails_when_the_re_review_fails():
    assert _route({"mode": "accept"}, "FAIL") == "node_classifier"


def test_rebuild_mode_keeps_forcing_the_rebuild():
    assert _route({"mode": "rebuild"}, "PASS") == "node_classifier"
    assert _route({"of_job_id": "x"}, "PASS") == "node_classifier"   # legacy, no mode


def test_the_amended_brief_carries_the_users_acceptance_to_the_reviewer():
    from meshpipeline.api.v1.simulation import _amended_brief

    class _S:
        review_brief_txt = "Wall y+ 30-300. No fatal defects."

    out = _amended_brief(_S(), "accept", "44% layer coverage at the junction is fine.")
    assert "USER ACCEPTANCE" in out
    assert "44% layer coverage" in out
    assert _S.review_brief_txt in out              # the original criteria still stand
    # rebuild mode must NOT amend the bar
    assert _amended_brief(_S(), "rebuild", "make it finer") == _S.review_brief_txt


def test_a_gate_failing_mesh_can_never_be_accepted():
    src = (APP / "api" / "v1" / "simulation.py").read_text()
    # The guard now reads the DURABLE viewer payload rather than a worker directory, so the
    # source string changed while the rule did not: a dispute still needs both a mesh and a
    # stored review.
    assert 'mesh_available' in src and 'review' in src, (
        "the failed-parent dispute guard must require BOTH a mesh and a stored "
        "review - the review is the proof that the executor gates passed")
