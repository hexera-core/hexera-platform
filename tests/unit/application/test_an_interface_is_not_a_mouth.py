# Responsibility: flow may not enter or leave the domain through a face where two regions meet.
# Boundaries: the measured classification and the submission gate; what the panel says is its own test.
"""Seventeen pressure outlets, twelve of them on the inside of the part.

MEASURED on the stored `cht_enclosing_2region` document: 18 openings, of which the measurement
classifies 12 as `interface` - the faces where the inner body meets the outer one - and 6 as planar
faces. One real run submitted with seventeen pressure outlets, twelve of them on those interfaces,
which tells the solver that fluid leaves the domain through the inside of the part. The mesh builds
and every mass balance is nonsense.

On a different phrasing of the same part the geometry agent caught it and said so in as many words:
"o7: I proposed outlet, and it is wall - interface face of r1 against r2". So whether the customer got
a usable job came down to which model call happened to run.

THE MEASUREMENT HAD ALREADY ANSWERED IT. Every opening row carries `classification`, and a grep of
src/meshpipeline for that field returned nothing but unrelated uses of the word. The panel's only
source for "is this a port" was the look's per-opening guess - which is also the reading the panel
claims to be checking against the measurement. One source doing both jobs, so there was never a
disagreement to detect, and an interface face genuinely does look like a mouth in a render.

Two regions either share a face or they do not, so this is topology and not a judgement, which is why
this class of error can be refused rather than merely flagged.
"""
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.application import geometry_survey as gs

FIXTURES = Path(__file__).parents[2] / "fixtures" / "geometry_survey"
CASE = "cht_enclosing_2region"


def _doc() -> dict:
    return json.loads((FIXTURES / f"{CASE}.json").read_text(encoding="utf-8"))


def _state() -> dict:
    doc = _doc()
    bt = FIXTURES / f"{CASE}.brief.txt"
    brief = bt.read_text(encoding="utf-8") if bt.is_file() else "internal cfd, air through it"
    return doc, gs.carry_answers(None, gs.compose(doc, purpose="conjugate_heat_transfer", brief=brief))


def _patch(name: str, role: str, oid: str) -> dict:
    return {"name": name, "type": role, "opening_id": oid}


def _settled(state: dict) -> dict:
    """Answer every role question, so `role_problems` gets past its own open-question refusal.

    It returns early while any role question is unanswered - rightly, since an unconfirmed role is the
    bigger problem - so a test that skips this never reaches the patch checks at all. The first version
    of this file did skip it and asserted on the open-question message instead, which is a test that
    passes for the wrong reason waiting to happen.
    """
    for view in [v for v in gs.question_views(state) if v["about"] == "opening.role"]:
        state = gs.record_answer(state, question_id=view["id"], words="you decide everything",
                                 latest_user_message="you decide everything", accepted_proposal=True,
                                 principal="dev-user")
    return state


def _interfaces(doc: dict) -> list[str]:
    return [str(o["id"]) for o in doc["openings"]
            if str(o.get("classification") or "") == gs.INTERFACE_CLASSIFICATION]


def test_the_part_really_does_carry_twelve_of_them():
    """The premise, read off the stored document rather than asserted."""
    doc = _doc()
    assert len(doc["openings"]) == 18
    assert len(_interfaces(doc)) == 12


def test_the_measurements_own_word_is_readable():
    doc = _doc()
    first = _interfaces(doc)[0]
    assert gs.measured_classification(doc, first) == gs.INTERFACE_CLASSIFICATION
    assert gs.is_a_region_interface(doc, first) is True
    assert gs.measured_classification(doc, "nothing-like-this") == ""
    assert gs.is_a_region_interface(None, first) is False


def test_an_outlet_on_a_region_interface_is_refused():
    doc, state = _state()
    state = _settled(state)
    oid = _interfaces(doc)[0]
    problems = gs.role_problems(state, doc, [_patch("outlet_o7", "outlet", oid)])
    hit = [p for p in problems if oid in p]
    assert hit, problems
    assert "the face where two regions meet" in hit[0]
    assert "not a mouth to the outside" in hit[0]


def test_an_inlet_on_one_is_refused_too():
    doc, state = _state()
    state = _settled(state)
    oid = _interfaces(doc)[0]
    problems = gs.role_problems(state, doc, [_patch("inlet", "inlet", oid)])
    assert any(oid in p and "two regions meet" in p for p in problems), problems


def test_a_wall_on_one_is_fine():
    """The role it should carry. Refusing this too would cost every conjugate job its plan."""
    doc, state = _state()
    state = _settled(state)
    oid = _interfaces(doc)[0]
    problems = gs.role_problems(state, doc, [_patch("wall", "wall", oid)])
    assert not any(oid in p and "two regions meet" in p for p in problems), problems


def test_a_planar_face_is_left_alone_by_this_rule():
    """Only the measured interfaces. A planar end may well be a port and that is another rule's call."""
    doc, state = _state()
    state = _settled(state)
    planar = next(str(o["id"]) for o in doc["openings"]
                  if str(o.get("classification") or "") == "planar_face")
    problems = gs.role_problems(state, doc, [_patch("inlet", "inlet", planar)])
    assert not any("two regions meet" in p for p in problems), problems


def test_our_own_proposal_cannot_open_it():
    """A default of ours must never put flow through a region boundary. `roles_we_proposed` is what
    separates a role the customer typed from one they waved through, and only the first may overrule a
    measured fact."""
    doc, state = _state()
    state = _settled(state)
    oid = _interfaces(doc)[0]
    problems = gs.role_problems(state, doc, [_patch("outlet", "outlet", oid)])
    assert any(oid in p for p in problems), (
        "a role we proposed and they waved through must not license flow through an interface")
