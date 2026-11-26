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


def test_a_prebuilt_reason_is_what_the_model_is_told_and_the_message_is_unchanged() -> None:
    """A rollback's specifics ride in the structured reason, not in the message: the message is what triage and
    arms.py group on, so changing it would fragment their tables."""
    cand = ProposedCandidate("run_stage", [], {"stage": "simplify"}, "ai", "simplify it")
    message = "stage 'simplify' was rolled back: it lowered the cheap score"
    rich = {"status": "declined", "code": "rolled_back", "stage": "simplify", "op": None,
            "detail": "gmsh_prod pass -> fail (no_volume); panel_passes 2 -> 1"}
    sent = _outcome_json(Outcome(cand, "declined", message=message, reason=rich))
    assert sent["message"] == message          # byte-identical to what it was before
    assert sent["reason"] == rich              # and the model gets the numbers
    # with no pre-built reason, the message is parsed as before
    assert _outcome_json(Outcome(cand, "declined", message=message))["reason"]["code"] == "rolled_back"


def test_the_search_hands_the_rollback_numbers_to_the_proposer() -> None:
    """End of the wiring: stages records WHY, the search carries it as `detail`, the proposer sends it as the
    reason's `detail`, and the message the tables group on is untouched."""
    from meshpipeline.cad.repair_v4 import search

    c = search.Candidate(search.RUN_STAGE, [], {"stage": "simplify"}, source="ai", rationale="r")
    out = search._Outcome(c, 0, "declined", ["stage 'simplify' was rolled back: it lowered the cheap score"],
                          detail="gmsh_prod pass -> fail (no_volume); panel_passes 2 -> 1")
    fed = search._feedback_for(out, parent=None, child=None, duplicate=False)
    assert fed.status == "declined"
    assert fed.message == "stage 'simplify' was rolled back: it lowered the cheap score"
    assert fed.reason["code"] == "rolled_back" and fed.reason["stage"] == "simplify"
    assert fed.reason["detail"] == "gmsh_prod pass -> fail (no_volume); panel_passes 2 -> 1"
    assert _outcome_json(fed)["reason"]["detail"].startswith("gmsh_prod pass -> fail")


def test_an_outcome_with_no_extra_detail_still_explains_itself() -> None:
    from meshpipeline.cad.repair_v4 import search

    c = search.Candidate("fix_poles", ["F1"], {}, source="ai", rationale="r")
    out = search._Outcome(c, 0, "declined", ["no_change: ShapeFix_Face.Perform changed nothing on face(s) [2]"])
    fed = search._feedback_for(out, parent=None, child=None, duplicate=False)
    assert fed.reason["code"] == "no_change" and fed.reason["detail"].startswith("ShapeFix_Face")
