# Responsibility: a review that could not conclude records WHICH refusal it was.
# Boundaries: `visual._nonverdict` and `visual._pre_loop_record`.
# Collaborates with: agents/loop/accounting, whose AgentRunRecord carries the reason.
"""Four delivered meshes, four identical records, six possible causes.

Jobs 40e4e578, 606af55e, b8f09ae9 and 110daca9 each delivered a mesh, a bundle and a viewer, and
each left exactly this and nothing else:

    {"kind": "review_inconclusive", "marker": "reviewer_evidence_missing", "reruns": 0}

`reviewer_evidence_missing` is shared by SIX different refusals, each of which computes a precise
diagnostic and passes it to `_nonverdict` as `chain`:

    [EXECUTOR_SUCCESS_ABSENT] reviewer reached without validated execution
    [PLAN_INVALID] {problems}
    [EVIDENCE_MISSING] deterministic evidence: {list(missing)}
    [EXPECTED_TARGET_MISSING] {absent}
    [{failure_class}] {api_failure}          - a non-verdict from inside the loop
    [{category}] {exc.detail}                - a render failure

`_nonverdict` declared that parameter and never read it. Recovering which one had fired took
lifting a saved `mesh_manifest.json` out of the workspace volume and replaying the production
path by hand; the container's logs, which did hold the sentence, were already gone.

These tests pin that the reason reaches the record. They say nothing about WHICH refusal is
right - that is `test_a_region_with_no_countable_frame_needs_its_volume` and the rest of the
reviewer suite.
"""
from __future__ import annotations

import inspect
import json

import pytest

from meshpipeline.agents.reviewer import visual as V

#: The six, verbatim in shape, as the call sites build them.
REASONS = [
    "[EXECUTOR_SUCCESS_ABSENT] reviewer reached without validated execution",
    "[PLAN_INVALID] ('axis near_interface_resolution cites no evidence',)",
    "[EVIDENCE_MISSING] deterministic evidence: ['metric:interface_ok']",
    "[EXPECTED_TARGET_MISSING] ('region:across_the_flow',)",
    "[DETERMINISTIC_EVIDENCE_MISSING] reviewer_evidence_missing",
    "[EVIDENCE_MISSING] mesh loaded no renderable geometry (0 surface points)",
]


class _Inputs:
    """What `_pre_loop_record` reads off its inputs, and nothing else."""

    def __init__(self, job_id="40e4e578-77cb-4943-9a36-8372941505dc", retry_count=0, rerun=0):
        self.job_id = job_id
        self.retry_count = retry_count
        self.rerun = rerun


def record_for(reason: str, **kw) -> dict:
    got = V._pre_loop_record(_Inputs(**kw), "reviewer_evidence_missing", reason)
    assert got is not None, "the record was not built at all"
    return got


# ------------------------------------------------------------------ the reason is kept

@pytest.mark.parametrize("reason", REASONS)
def test_the_record_says_which_refusal_it_was(reason):
    """THE REGRESSION. Before the fix every one of these produced the same record."""
    body = json.dumps(record_for(reason))
    assert reason in body, f"the record does not say why: {body[:400]}"


def test_two_different_refusals_leave_different_records():
    """The whole point: four identical records was the defect, not the marker."""
    a = json.dumps(record_for(REASONS[2]))
    b = json.dumps(record_for(REASONS[3]))
    assert a != b
    assert "metric:interface_ok" in a and "metric:interface_ok" not in b
    assert "region:across_the_flow" in b and "region:across_the_flow" not in a


def test_the_marker_is_still_recorded():
    """The reason is additional, never a replacement - `review_inconclusive_caveat` reads the
    marker and grants the delivery on it."""
    assert "reviewer_evidence_missing" in json.dumps(record_for(REASONS[3]))


def test_no_reason_records_no_reason_rather_than_an_empty_one():
    """An empty tuple, not `('',)`: a blank string in a machine-readable reasons list would read
    as a reason nobody can act on, which is the thing being fixed."""
    for blank in ("", "   ", None):
        got = V._pre_loop_record(_Inputs(), "reviewer_evidence_missing", blank or "")
        assert got is not None
        reasons = got.get("extension", {}).get("rejection_reasons", ())
        assert list(reasons) == [], f"{blank!r} became {reasons!r}"


def test_the_reason_is_whitespace_trimmed():
    got = record_for("  [PLAN_INVALID] x  ")
    reasons = got.get("extension", {}).get("rejection_reasons", ())
    assert list(reasons) == ["[PLAN_INVALID] x"]


# ------------------------------------------------------------------ nothing else moved

def test_the_record_is_still_a_policy_abort():
    got = record_for(REASONS[3])
    assert "policy_abort" in json.dumps(got)


def test_the_attempt_numbers_still_come_from_the_inputs():
    got = record_for(REASONS[3], retry_count=2)
    body = json.dumps(got)
    assert '"pipeline_attempt": 2' in body or '"pipeline_attempt":2' in body


def test_a_record_that_cannot_be_built_is_still_not_a_failure():
    """`_pre_loop_record` swallows its own errors on purpose - "diagnostics never fail a
    review" - and adding a reason must not change that."""
    class _Broken:
        job_id = "j"

        @property
        def retry_count(self):
            raise RuntimeError("no")

    assert V._pre_loop_record(_Broken(), "reviewer_evidence_missing", REASONS[0]) is None


def test_nonverdict_hands_the_reason_to_the_record():
    """The wiring, not the record: `chain` was a declared parameter nobody read."""
    src = inspect.getsource(V._nonverdict)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "_pre_loop_record(inputs, marker, chain)" in code, \
        "the reason is still being dropped at the call site"


def test_every_caller_still_passes_a_reason():
    """Six call sites, six diagnostics. A caller that stops computing one would make this fix
    silently useless for that path."""
    src = inspect.getsource(V)
    for tag in ("[EXECUTOR_SUCCESS_ABSENT]", "[PLAN_INVALID]", "[EVIDENCE_MISSING]",
                "[EXPECTED_TARGET_MISSING]"):
        assert tag in src, f"{tag} is no longer computed anywhere"


def test_the_customer_facing_note_is_not_the_diagnostic():
    """`note` is what the customer reads and these strings are not for them. The two must stay
    separate: a customer told "[EXPECTED_TARGET_MISSING] ('region:across_the_flow',)" has been
    handed our internals instead of an explanation."""
    src = inspect.getsource(V)
    for tag in ("[PLAN_INVALID]", "[EXPECTED_TARGET_MISSING]"):
        i = src.index(tag)
        window = src[max(0, i - 700):i]
        assert "problem on our side" in window, \
            f"{tag} is not preceded by a plain-words note for the customer"
