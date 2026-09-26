"""The geometry agent produced no plan and the customer was told nothing at all.

`_do_submit_requirements` composed the agent's findings and its risk list together, inside one
`if isinstance(plan, dict):` with no else. So when the step failed the two blocks vanished together
and the confirmation was byte for byte the one a job with no agent gets: not even a blank line where
they had been. The customer then approved compute without being told that the second read of their
part had not happened. The silence is the defect, not the fallback: the part still meshes and the
reviewer still runs.

WHY THE BRANCH IS ON THE STATUS AND NOT ON THE PLAN. `geometry_step` keeps three states apart on
purpose, in its own words: "a step that failed and a step that had nothing to do are different facts".
A branch on "is there a plan dict" fires on all three, so a job whose survey never ran the step would
be told it lost a block that was never on offer for it, which is a fact that lies.

This drives `agent_words_for_confirmation` itself rather than a copy of the composition, so the check
fails differently from the thing it checks.
"""
from __future__ import annotations

from meshpipeline.application import geometry_step as gst
from meshpipeline.agents.intake.executor import agent_words_for_confirmation

PLAN = {"summary_for_user": "One inlet at o1, three outlets, the shell is the wall.",
        "risks": [{"severity": "high", "effect": "changes_bc",
                   "consequence": "the outlet o6 has only 1.2 D of run",
                   "recommendation": "extend it or do not quote the o6 pressure drop"}]}


def test_a_step_that_planned_reads_exactly_as_before():
    said = agent_words_for_confirmation({"status": gst.PLANNED, "plan": PLAN})
    assert "WHAT THE GEOMETRY AGENT FOUND" in said
    assert "the shell is the wall" in said
    assert "WORTH KNOWING BEFORE I RUN THIS" in said
    assert "1.2 D of run" in said
    assert "-> extend it" in said
    assert "NO SECOND READ" not in said, "nothing is missing on this job"


def test_a_step_that_failed_says_so():
    said = agent_words_for_confirmation(
        {"status": gst.FAILED, "reason": "the plan was sent back three times"})
    assert "NO SECOND READ ON THIS PART" in said
    assert "did not finish" in said
    assert "measurement alone" in said


def test_the_failed_line_does_not_claim_the_mesh_is_worse():
    """It collides with the reviewer if it overstates: the mesh is still built and still gated."""
    said = agent_words_for_confirmation({"status": gst.FAILED, "reason": "no model configured"})
    assert "still built" in said and "checked the same way" in said
    for word in ("degraded", "worse", "lower quality", "unreliable", "may fail"):
        assert word not in said.lower(), f"{word!r} promises something this line cannot know"


def test_the_stored_reason_never_reaches_the_customer():
    reason = "contract.intake.binds_late refused topic openings[].role"
    said = agent_words_for_confirmation({"status": gst.FAILED, "reason": reason})
    assert reason not in said
    assert "binds_late" not in said and "contract" not in said


def test_a_job_the_step_never_ran_on_is_told_nothing():
    """A row with no geometry_step at all never had a survey to read: nothing was lost."""
    assert agent_words_for_confirmation(None) == ""
    assert agent_words_for_confirmation({}) == ""


def test_a_planned_step_with_no_words_and_no_loud_risks_says_nothing():
    assert agent_words_for_confirmation({"status": gst.PLANNED, "plan": {}}) == ""
    quiet = {"summary_for_user": "", "risks": [{"severity": "info", "effect": "none",
                                               "consequence": "the fillet radius is small"}]}
    assert agent_words_for_confirmation({"status": gst.PLANNED, "plan": quiet}) == ""


def test_a_status_we_do_not_know_with_no_plan_is_treated_as_a_failure():
    """A new status word must not silently become the no-agent path: that is how this got missed."""
    said = agent_words_for_confirmation({"status": "abandoned"})
    assert "NO SECOND READ ON THIS PART" in said


def test_the_block_is_spaced_for_the_summary():
    for step in ({"status": gst.FAILED, "reason": "x"}, {"status": gst.PLANNED, "plan": PLAN}):
        said = agent_words_for_confirmation(step)
        assert said.startswith(chr(10) * 2), "the caller lstrips this when the block opens the message"
