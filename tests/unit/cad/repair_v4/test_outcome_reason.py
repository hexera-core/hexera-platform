# Responsibility: The structured reason the proposer feeds back for every candidate that was not applied: one stable
#                 shape {status, code, detail, stage, op} parsed from the search's and stages' own messages.
# Boundaries: hermetic; no OCCT, no model.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4.proposer import Outcome, ProposedCandidate, _outcome_json, reason_of

CASES = [
    # (status, message) -> (code, stage, op, detail startswith)
    ("declined", "no_change: ShapeFix_Face.Perform changed nothing on face(s) [203]",
     ("no_change", None, None, "ShapeFix_Face.Perform")),
    ("declined", "invalidated: the result is not BRepCheck-valid", ("invalidated", None, None, "the result")),
    ("declined", "stage 'condition' changed nothing: split_angle: declined: invalidated: the result is not "
                 "BRepCheck-valid", ("invalidated", "condition", "split_angle", "the result")),
    ("declined", "stage 'edges' changed nothing: fix_poles: failed: error: v4_fix_poles: RuntimeError: boom",
     ("error", "edges", "fix_poles", "v4_fix_poles")),
    ("declined", "stage 'simplify' was rolled back: it lowered the cheap score",
     ("rolled_back", "simplify", None, "it lowered")),
    ("declined", "stage 'faces' changed nothing", ("no_change", "faces", None, "")),
    ("failed", "error: v4_x: ZeroDivisionError: float division by zero", ("error", None, None, "v4_x")),
    ("failed", "timeout: op exceeded 300s", ("timeout", None, None, "op exceeded")),
    ("invalid", "targets[0]: F79 is a face, not an edge", ("invalid_call", None, None, "targets[0]")),
    ("declined", "input carries a solid; sewing returns shells", ("declined", None, None, "input carries")),
]


@pytest.mark.parametrize("status,message,want", CASES)
def test_every_unapplied_outcome_has_one_stable_reason_shape(status, message, want) -> None:
    code, stage, op, detail = want
    r = reason_of(status, message)
    assert set(r) == {"status", "code", "detail", "stage", "op"}
    assert (r["status"], r["code"], r["stage"], r["op"]) == (status, code, stage, op)
    assert r["detail"].startswith(detail)


def test_an_applied_outcome_has_no_reason_and_the_json_carries_it() -> None:
    cand = ProposedCandidate("fix_poles", ["F1"], {}, "ai", "poles")
    assert _outcome_json(Outcome(cand, "applied"))["reason"] is None
    sent = _outcome_json(Outcome(cand, "declined", message="no_change: nothing to fix"))
    assert sent["reason"]["code"] == "no_change" and sent["message"] == "no_change: nothing to fix"
