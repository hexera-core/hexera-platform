# Responsibility: Verify the unit question is never asked about a unit the user has already given -
# said before it was asked, answered with a slip, or left to the intake - and that a model turn
# never erases the question the message authority asked.
# Boundaries: the message authority's settle step with a fake repository (as
# test_unit_question_proposes does), the unit reader, and the turn's gate patch.
"""The intake soak (a persona harness on the real API and intake) put an STL in front of users who
talked the way people do, and the unit question became the wall:

- "Air over the Ahmed body ... The file is in metres." was answered with "what unit is the file
  in?" - the first message was never read for a unit;
- "metrees", "metrse", "milimetres" were not a unit, so the question came back every turn;
- "no idea what unit, whatever the file uses" was not an answer, so the question came back every
  turn, although it proposed metres with the sizes beside it;
- after any model turn the "unit asked" mark was gone (the turn's gate patch replaced the stored
  gate whole), so even "the file uses metres" was no longer read as the answer and the question was
  asked again;

and the geometry check - which waits for the unit - never drew the part.
"""
from __future__ import annotations

import random
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.message as msg
import meshpipeline.agents.intake.turn as turn
import meshpipeline.agents.intake.unit_clarification as uc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts.geometry_units import LengthUnit

SID = uuid.UUID("cccc1111-2222-4222-b222-dddddddddddd")
ELBOW_MM = [1448.8, 1233.4, 539.3]           # a pipe elbow drawn in millimetres
AHMED_M = [1.044, 0.389, 0.338]              # the Ahmed body STL, drawn in metres


def _locked(gate, messages=()):
    return SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=uuid.uuid4(),
                           geometry_interpretation_id=None, intake_gate=dict(gate), messages=list(messages))


async def _settle(content, *, gate, monkeypatch, size, messages=()):
    monkeypatch.setattr(gh, "measured_size_mm", lambda sid: size)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: None)
    recorded: list = []

    async def _record(db, *, owner_id, geometry_source_id, unit, organization_id=""):
        recorded.append(unit)
        return SimpleNamespace(interpretation_id=str(uuid.uuid4()))
    monkeypatch.setattr(uc, "record", _record)
    repo = MagicMock()
    repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock(); repo.bind_geometry_interpretation = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=content, organization_id="org-1")
    outcome = await msg._settle(inbound, AsyncMock(), gate=dict(gate), locked=_locked(gate, messages),
                                messages=list(messages), revision="r1", session_repo=repo, logger=MagicMock())
    return outcome, recorded, repo


# ------------------------------------------------------------- said before it was asked ----
@pytest.mark.asyncio
@pytest.mark.parametrize("said,size,unit", [
    ("air over the Ahmed body at 40 m/s on the ground. The file is in metres.", AHMED_M, LengthUnit.metre),
    ("water through this elbow at 2 m/s, units are mm", ELBOW_MM, LengthUnit.millimetre),
    ("the model was drawn in millimeters, water at 2 m/s", ELBOW_MM, LengthUnit.millimetre),
    ("The file uses metres. Air at 40 m/s around the car.", AHMED_M, LengthUnit.metre),
    ("water through this pipe elbow at 2 m/s, units are milimetres", ELBOW_MM, LengthUnit.millimetre),
    ("it's in mm - water through the elbow", None, LengthUnit.millimetre),     # nothing measured yet: nothing to doubt
])
async def test_a_unit_said_before_it_was_asked_is_taken_and_not_asked(monkeypatch, said, size, unit):
    outcome, recorded, repo = await _settle(said, gate={}, monkeypatch=monkeypatch, size=size)
    assert outcome.status is msg.MessageStatus.unit_recorded and recorded == [unit], outcome.reply
    repo.append_message.assert_not_awaited()                 # no question in the conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("said,size", [
    ("air over the Ahmed car on the ground, the file is in mm", AHMED_M),     # a 1 mm car
    ("water through this elbow, the file is in metres", ELBOW_MM),            # a 1.4 km elbow
])
async def test_a_unit_said_that_makes_the_part_absurd_is_asked_with_the_sizes(monkeypatch, said, size):
    outcome, recorded, _ = await _settle(said, gate={}, monkeypatch=monkeypatch, size=size)
    assert outcome.status is msg.MessageStatus.unit_question and recorded == []
    assert "Its longest side would be" in outcome.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["air over the Ahmed body at 40 m/s", "it is about 1.04 m long",
                                  "is the file in metres?", "not sure if it's in metres", ""])
async def test_a_speed_a_size_a_question_or_a_doubt_names_no_unit(monkeypatch, said):
    outcome, recorded, _ = await _settle(said, gate={}, monkeypatch=monkeypatch, size=AHMED_M)
    assert outcome.status is msg.MessageStatus.unit_question and recorded == []


# ------------------------------------------------------------------- left to the intake ----
@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["no idea what unit, whatever the file uses", "I don't know, you pick",
                                  "whatever you think", "no idea"])
async def test_leaving_the_unit_to_the_intake_takes_the_proposal_once_measured(monkeypatch, said):
    outcome, recorded, _ = await _settle(said, gate=uc.asked({}, LengthUnit.metre), monkeypatch=monkeypatch,
                                         size=AHMED_M)
    assert outcome.status is msg.MessageStatus.unit_recorded and recorded == [LengthUnit.metre]
    # the plain question (asked before the part was measured) proposes nothing: the measured size does
    outcome, recorded, _ = await _settle(said, gate=uc.asked({}), monkeypatch=monkeypatch, size=AHMED_M)
    assert recorded == [LengthUnit.metre]
    outcome, recorded, _ = await _settle(said, gate=uc.asked({}), monkeypatch=monkeypatch, size=ELBOW_MM)
    assert recorded == [LengthUnit.millimetre]


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["I don't know, you pick, but not metres", "no idea - maybe mm? you decide",
                                  "whatever you think, just not inches"])
async def test_a_deferral_that_names_a_unit_is_not_a_deferral(monkeypatch, said):
    # review on #92: "you pick, but not metres" rules metres out - never take the proposal over it
    outcome, recorded, _ = await _settle(said, gate=uc.asked({}, LengthUnit.metre), monkeypatch=monkeypatch,
                                         size=AHMED_M)
    assert LengthUnit.metre not in recorded


@pytest.mark.asyncio
async def test_leaving_it_to_the_intake_before_anything_is_measured_keeps_the_question_open(monkeypatch):
    outcome, recorded, repo = await _settle("no idea", gate=uc.asked({}), monkeypatch=monkeypatch, size=None)
    assert outcome.status is msg.MessageStatus.proceed and recorded == []
    repo.set_intake_gate.assert_not_awaited()                # the question stays asked


# ----------------------------------------------------------------------- slips ----
@pytest.mark.parametrize("said,unit", [
    ("metrees", LengthUnit.metre), ("metrse", LengthUnit.metre), ("metrrs", LengthUnit.metre),
    ("milimetres", LengthUnit.millimetre), ("millimetrs", LengthUnit.millimetre), ("milimeters", LengthUnit.millimetre),
    ("inchs", LengthUnit.inch), ("centimetrs", LengthUnit.centimetre), ("its in metrees", LengthUnit.metre),
    ("The file uses metres", LengthUnit.metre),
])
def test_a_unit_word_with_a_slip_is_that_unit(said, unit):
    assert uc.classify(said) is unit


@pytest.mark.parametrize("said", ["meteor", "letters", "centre", "matters", "meterage", "inchworm"])
def test_words_that_only_look_like_units_are_not_units(said):
    assert uc.classify(said) is None
    assert uc.stated_unit(f"the file is {said}") is None


# ------------------------------------------------------------ the mark survives a model turn --
def test_a_model_turn_carries_the_unit_question_it_does_not_own():
    gate = {"selection": {"id": "s"}, "admission": None, "approval": None,
            **uc.asked({}, LengthUnit.millimetre)}
    state = {"job_id": "j", "session_id": "s", "user_id": "u", "intake_gate": gate,
             "messages": [{"role": "user", "content": "no idea"}]}
    ctx = turn.hydrate(state, state["messages"])
    patch = turn._gate_patch(ctx)
    assert patch["unit_question"] == {"asked": True, "proposed": "mm"}
    assert set(turn.MODEL_GATE_KEYS) <= set(patch)
    # what the model owns it still writes: a selection it replaced is the new one
    import dataclasses
    assert turn._gate_patch(dataclasses.replace(ctx, selection={"id": "t"}))["selection"] == {"id": "t"}


# ----------------------------------------------------------------------- the fuzz ----
_SAYINGS = ["the file is in {u}", "units are {u}", "it's in {u}", "the model was drawn in {u}",
            "The file uses {u}", "coordinates are in {u}", "everything is in {u}", "{U}"]
_SPELL = {LengthUnit.millimetre: ["mm", "millimetres", "millimeters", "MM", "milimetres"],
          LengthUnit.metre: ["metres", "meters", "metre", "metrse"],
          LengthUnit.inch: ["inches", "inch", "inchs"], LengthUnit.centimetre: ["cm", "centimetres"]}
_OPENINGS = ["water through this pipe elbow at 2 m/s", "air over the wing at 50 m/s", "flow through the duct"]


def _fuzz():
    rng = random.Random(20260929)
    out = []
    for unit, spellings in _SPELL.items():
        for say in _SAYINGS:
            for sp in spellings:
                text = say.format(u=sp, U=sp)
                opening = rng.choice(_OPENINGS)
                out.append((rng.choice((f"{opening}. {text}.", f"{text}, {opening}", text)), unit))
    return out


@pytest.mark.parametrize("said,unit", _fuzz())
def test_every_way_of_stating_the_unit_is_read_asked_or_not(said, unit):
    # asked: the answer reads anywhere in the reply; unasked: the statement reads as the file's unit
    assert uc.classify(said) is unit, said
    if said.lower().startswith(tuple(s.split("{")[0].lower() for s in _SAYINGS if not s.startswith("{"))) \
            or any(k in said.lower() for k in ("file", "units are", "it's in", "drawn in", "coordinates", "everything")):
        assert uc.stated_unit(said) is unit, said
