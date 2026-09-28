# Responsibility: Verify the unit question proposes millimetres with the part's size in every unit
# once the part is measured, that "ok" and a unit named anywhere in the reply settle it, that a
# reply naming no unit is an ordinary turn the intake answers, and that an approval given while
# the question is open is held on it.
# Boundaries: the message authority's settle step with a fake repository; the store is stood in
# for at the application seam. Nothing here changes the approval grammar - it is only consulted.
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.message as msg
import meshpipeline.agents.intake.unit_clarification as uc
from meshpipeline.agents.intake import approval as ap
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts.geometry_units import LengthUnit

SID = uuid.UUID("cccc1111-2222-4222-b222-cccccccccccc")
SIZE = [1048.8, 400.0, 120.0]


# ----------------------------------------------------------------------------- the words ----
def test_the_question_proposes_millimetres_with_the_part_in_every_unit_when_it_was_measured():
    q = uc.question_for(SIZE)
    assert "1.05 m if millimetres" in q and "10.5 m if centimetres" in q
    assert "1.05 km if metres" in q and "26.6 m if inches" in q
    assert "I'll take millimetres - say ok" in q
    assert uc.question_for(None) == uc.QUESTION          # nothing measured: nothing proposed
    assert uc.before_run(SIZE, uc.PROPOSED).startswith("One thing before I start the run.")
    assert uc.before_run(None, uc.PROPOSED).endswith("Or say ok to take millimetres.")


@pytest.mark.parametrize("answer,unit", [
    ("mm", LengthUnit.millimetre), ("Metres.", LengthUnit.metre), ("in", LengthUnit.inch),
    ("yes, millimetres", LengthUnit.millimetre), ("no - metres", LengthUnit.metre),
    # the sentence shared dev refused (session ahmed_25deg.stl), and its kin
    ("the coordinates are in millimetres", LengthUnit.millimetre), ("mm I think", LengthUnit.millimetre),
    ("it's in inches", LengthUnit.inch), ("in inches", LengthUnit.inch), ("It's in cm.", LengthUnit.centimetre),
    ("The file is in meters, I believe", LengthUnit.metre), ("should be millimeters", LengthUnit.millimetre),
])
def test_a_unit_named_anywhere_in_the_reply_is_the_answer(answer, unit):
    assert uc.classify(answer) is unit


@pytest.mark.parametrize("answer", ["it's about 2 metres long", "1.05 m", "not inches, millimetres",
                                    "what do you mean?", "not sure", "furlongs", "ok", "",
                                    "not millimetres", "it is not in metres", "it isn't inches", "no mm",
                                    "somewhere between mm and cm"])
def test_a_reply_that_names_no_unit_or_states_or_rejects_one_names_nothing(answer):
    assert uc.classify(answer) is None


def test_a_plain_ok_confirms_only_a_proposed_unit():
    assert uc.classify("ok", LengthUnit.millimetre) is LengthUnit.millimetre
    assert uc.classify("Yes.", LengthUnit.millimetre) is LengthUnit.millimetre
    assert uc.classify("ok but metres", LengthUnit.millimetre) is LengthUnit.metre
    assert uc.classify("ok", None) is None
    assert uc.classify("ok then let's go", LengthUnit.millimetre) is None
    assert uc.proposed(uc.asked({}, uc.PROPOSED)) is LengthUnit.millimetre
    assert uc.proposed(uc.asked({})) is None and uc.proposed({"unit_question": {"proposed": "furlong"}}) is None


# ------------------------------------------------------------------------------ the turn ----
def _locked(**over):
    base = {"id": SID, "owner_id": "alice", "job_id": None, "geometry_source_id": uuid.uuid4(),
            "geometry_interpretation_id": None, "intake_gate": {}, "messages": []}
    base.update(over)
    return SimpleNamespace(**base)


async def _settle(content, *, gate, monkeypatch, size=SIZE, recorded=None, held=None):
    monkeypatch.setattr(gh, "measured_size_mm", lambda sid: size)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: (held.append(sid) if held is not None else None))

    async def _record(db, *, owner_id, geometry_source_id, unit, organization_id=""):
        if recorded is not None:
            recorded.append(unit)
        return SimpleNamespace(interpretation_id=str(uuid.uuid4()))
    monkeypatch.setattr(uc, "record", _record)
    repo = MagicMock()
    repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock(); repo.bind_geometry_interpretation = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=content, organization_id="org-1")
    outcome = await msg._settle(inbound, AsyncMock(), gate=dict(gate), locked=_locked(intake_gate=dict(gate)),
                                messages=[], revision="r1", session_repo=repo, logger=MagicMock())
    return outcome, repo


@pytest.mark.asyncio
async def test_the_first_message_is_asked_the_question_with_the_proposal_and_ok_settles_it(monkeypatch):
    outcome, repo = await _settle("water through this elbow", gate={}, monkeypatch=monkeypatch)
    assert outcome.status is msg.MessageStatus.unit_question and outcome.awaiting_confirmation
    assert "I'll take millimetres" in outcome.reply and "1.05 m if millimetres" in outcome.reply
    gate = repo.set_intake_gate.await_args.args[2]
    assert gate["unit_question"] == {"asked": True, "proposed": "mm"}
    assert repo.append_message.await_args.args[2:] == ("assistant", outcome.reply)
    recorded: list = []
    outcome, _ = await _settle("ok", gate=gate, monkeypatch=monkeypatch, recorded=recorded)
    assert outcome.status is msg.MessageStatus.unit_recorded and recorded == [LengthUnit.millimetre]


@pytest.mark.asyncio
async def test_without_a_measurement_the_plain_question_is_asked_and_ok_is_not_an_answer(monkeypatch):
    outcome, repo = await _settle("mesh it", gate={}, size=None, monkeypatch=monkeypatch)
    assert outcome.reply == uc.QUESTION
    gate = repo.set_intake_gate.await_args.args[2]
    assert gate["unit_question"] == {"asked": True}
    recorded: list = []
    outcome, repo = await _settle("ok", gate=gate, size=None, monkeypatch=monkeypatch, recorded=recorded)
    assert outcome.status is msg.MessageStatus.proceed and recorded == []    # an ordinary turn; the question stays open
    assert repo.append_message.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,unit", [
    ("the coordinates are in millimetres", LengthUnit.millimetre),
    ("mm I think", LengthUnit.millimetre),
    ("it's in inches", LengthUnit.inch),
])
async def test_a_sentence_that_names_the_unit_settles_it_and_the_naming_gets_its_turn(monkeypatch, answer, unit):
    """What shared dev refused with "I could not read that as a unit" - and then sat at scouted
    for the harness's whole wait, because the naming is only queued once the unit is known."""
    recorded: list = []
    held: list = []
    outcome, repo = await _settle(answer, gate=uc.asked({}, uc.PROPOSED), monkeypatch=monkeypatch,
                                  recorded=recorded, held=held)
    assert outcome.status is msg.MessageStatus.unit_recorded and recorded == [unit]
    assert held == [str(SID)]                              # the geometry check gets its turn at once
    assert repo.append_message.await_count == 0            # no refusal in the conversation


@pytest.mark.asyncio
async def test_a_reply_that_names_no_unit_is_an_ordinary_turn_and_the_naming_waits_for_the_unit(monkeypatch):
    gate = uc.asked({}, uc.PROPOSED)
    held: list = []
    outcome, repo = await _settle("what do you mean by unit?", gate=gate, monkeypatch=monkeypatch, held=held)
    assert outcome.status is msg.MessageStatus.proceed
    repo.set_intake_gate.assert_not_awaited(); repo.append_message.assert_not_awaited()
    assert held == []                       # the words are not handed to the naming before the scale is known
    recorded: list = []
    outcome, _ = await _settle("ah, the coordinates are in inches", gate=gate, monkeypatch=monkeypatch,
                               recorded=recorded, held=held)
    assert outcome.status is msg.MessageStatus.unit_recorded and recorded == [LengthUnit.inch]
    assert held == [str(SID)]               # now the geometry check gets its turn


def _live_approval() -> dict:
    return {"id": "snap", "status": ap.AWAITING, "expires_at": time.time() + 600,
            "expected_confirmation_msg_count": 3}


@pytest.mark.asyncio
async def test_an_approval_given_while_the_unit_is_open_is_held_on_the_question(monkeypatch):
    gate = {"approval": _live_approval(), **uc.asked({}, uc.PROPOSED)}
    outcome, repo = await _settle("yes, proceed", gate=gate, monkeypatch=monkeypatch)
    assert outcome.status is msg.MessageStatus.unit_question and outcome.awaiting_confirmation
    assert outcome.reply.startswith("One thing before I start the run.") and "say ok" in outcome.reply
    written = repo.set_intake_gate.await_args.args[2]
    assert written["approval"]["expected_confirmation_msg_count"] == 4         # the approval waits one turn
    assert written["approval"]["status"] == ap.AWAITING and written["unit_question"]["proposed"] == "mm"


@pytest.mark.asyncio
async def test_an_approval_that_answers_the_unit_in_the_same_breath_goes_to_dispatch(monkeypatch):
    gate = {"approval": _live_approval(), **uc.asked({}, uc.PROPOSED)}
    recorded: list = []
    outcome, _ = await _settle("yes", gate=gate, monkeypatch=monkeypatch, recorded=recorded)
    assert outcome.status is msg.MessageStatus.approve and recorded == [LengthUnit.millimetre]


@pytest.mark.asyncio
async def test_a_unit_answered_during_a_live_approval_keeps_the_approval_waiting_for_a_clear_yes(monkeypatch):
    gate = {"approval": _live_approval(), **uc.asked({}, uc.PROPOSED)}
    recorded: list = []
    outcome, repo = await _settle("metres", gate=gate, monkeypatch=monkeypatch, recorded=recorded)
    assert outcome.status is msg.MessageStatus.approval_deferred and recorded == [LengthUnit.metre]
    assert outcome.reply.startswith("Noted: the file is in metres.") and ap.CLARIFICATION in outcome.reply
    written = repo.set_intake_gate.await_args.args[2]
    assert "unit_question" not in written and written["approval"]["expected_confirmation_msg_count"] == 4


@pytest.mark.asyncio
async def test_a_hedge_or_correction_during_a_live_approval_is_the_approvals_as_before(monkeypatch):
    gate = {"approval": _live_approval(), **uc.asked({}, uc.PROPOSED)}
    outcome, _ = await _settle("not sure", gate=gate, monkeypatch=monkeypatch)
    assert outcome.status is msg.MessageStatus.approval_deferred and outcome.reply == ap.CLARIFICATION
    outcome, _ = await _settle("make the inlet bigger", gate=gate, monkeypatch=monkeypatch)
    assert outcome.status is msg.MessageStatus.approval_invalidated
