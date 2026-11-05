# Responsibility: Verify the reviewer model's own words reach the user as plain engineering text, and that it is told to write them so.
# Boundaries: the closing message's finding line and the reviewer prompt; the job route's reasoning field is tested with the route.
from __future__ import annotations

from meshpipeline.application.final_result import TerminalStatus, build_final_result, render_message


def _succeeded_with(finding: str):
    caveat = {"kind": "layer_coverage", "coverage_pct": 62.5, "cells_with": 5, "cells_targeted": 8,
              "finding": finding}
    return build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="s",
        executor_success=True, reviewer_verdict="FAIL", failed_gate="", api_failure="",
        attempts=3, attempts_max=3, required_ready=True, delivered_types=["mesh"],
        optional_warnings=[], requirement_caveats=[caveat])


def test_a_finding_written_in_latex_is_read_plain_in_the_closing_message():
    msg = render_message(_succeeded_with(
        r"Layers collapse on `wing_1` near the tip; the first cell sits at \(y^+ \approx 80\), "
        r"outside the \(30\text{–}300\) band only locally."))
    assert ("Reviewer's finding: Layers collapse on `wing_1` near the tip; the first cell sits at "
            "y⁺ ≈ 80, outside the 30–300 band only locally.") in msg


def test_a_plain_finding_is_unchanged():
    finding = "Layers thin out at the trailing edge."
    assert f"Reviewer's finding: {finding}" in render_message(_succeeded_with(finding))


def test_the_reviewer_is_told_its_words_are_shown_as_plain_text():
    import meshpipeline.settings.policy as polcfg

    prompt = polcfg.prompts.reviewer_system
    assert "shown to the user, as plain text with no math" in prompt
    assert "never LaTeX or other math markup" in prompt
    # the template is still a valid .format() template - the rule added no stray braces
    filled = prompt.format(workflow="w", request="r", review_brief="b", quality_checks="q",
                           review_rubric="x")
    assert "y⁺ ≈ 1" in filled
