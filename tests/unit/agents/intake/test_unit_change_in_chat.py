# Responsibility: Verify a unit named in the chat after the unit was settled changes it through the
# application - recorded, every derived size re-read, a proposed run withdrawn, one plain reply -
# instead of reaching the model to relabel numbers (where "the file is in metres" used to
# spiral), and that sentences which only mention a unit (a speed, a size, a question) change
# nothing.
# Boundaries: the message authority's settle step with a fake repository; the unit change itself
# is covered in tests/unit/application/test_unit_change.py.
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
from meshpipeline.application import unit_change as uch
from meshpipeline.contracts.geometry_units import LengthUnit

SID = uuid.UUID("eeee1111-2222-4222-b222-eeeeeeeeeeee")


@pytest.mark.parametrize("said,unit", [
    ("the file is in metres", LengthUnit.metre),
    ("The file is in meters, not millimeters", LengthUnit.metre),
    ("it's in inches", LengthUnit.inch),
    ("actually the units are mm", LengthUnit.millimetre),
    ("the model was drawn in cm", LengthUnit.centimetre),
    ("everything is in metres, the blade is 117 m long", LengthUnit.metre),
    ("metres", LengthUnit.metre),
    ("no, metres", LengthUnit.metre),
    ("metres, not mm", LengthUnit.metre),
    ("that's metres", LengthUnit.metre),
    ("sorry, I meant inches", LengthUnit.inch),
    ("the file is in metres if that helps", LengthUnit.metre),     # an "if" later is no doubt
])
def test_a_sentence_that_says_what_unit_the_file_is_in_names_it(said, unit):
    assert uc.stated_unit(said) is unit


@pytest.mark.parametrize("said", [
    "is the file in metres?",                 # a question
    "air at 20 metres per second",            # a speed
    "the inlet velocity is 2 m/s",
    "speed in metres per second please",
    "the blade is 117 metres long",           # a size
    "it's about 2 metres long",
    "it is not in metres",                    # a rejection
    "make the far field 50 chords upstream and use snappy",
    "somewhere between mm and cm",
    "use snappy with mm",                     # a short reply about something else
    "what about metres",
    "is the file in metres",                  # a question with no question mark
    "Is it in inches",
    "could it be in metres",
    "not sure if it's in metres",             # a doubt
    "maybe the file is in inches",
    "I don't know whether the units are mm",
    "if it's in metres, use snappy",          # a condition
    "the file is in metres if that's correct",  # a statement that asks to be checked
    "it's in inches if I'm not mistaken",
    "I guess the file is in metres",
    "",
])
def test_a_sentence_that_only_mentions_a_unit_names_nothing(said):
    assert uc.stated_unit(said) is None


@pytest.mark.asyncio
async def test_a_unit_change_that_cannot_re_read_the_confirmed_sizes_changes_nothing_and_says_so(monkeypatch):
    async def _cannot(*a, **k):
        raise uch.UnitChangeError("the confirmed geometry check could not be read (ConnectionError)")
    monkeypatch.setattr(uch, "change_unit", _cannot)
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="the file is in metres", organization_id="")
    outcome = await msg._settle(inbound, AsyncMock(), gate={}, locked=_locked(), messages=[], revision="r1",
                                session_repo=repo, logger=MagicMock())
    assert outcome.status is msg.MessageStatus.unit_changed and outcome.answered
    assert outcome.reply == msg.UNIT_CHANGE_FAILED.format(unit="metres")
    assert "changed nothing" in outcome.reply
    repo.set_intake_gate.assert_not_awaited()
    assert repo.append_message.await_args.args[2:] == ("assistant", outcome.reply)


@pytest.mark.asyncio
async def test_a_stage_unit_change_withdraws_a_live_proposal_through_the_intake_authority():
    live = {"id": "snap", "status": ap.AWAITING, "expires_at": time.time() + 600}
    repo = MagicMock(); repo.set_intake_gate = AsyncMock()
    session = _locked(intake_gate={"approval": live, "selection": {"engine": "snappy"}})
    assert await msg.withdraw_proposal_for_unit_change(AsyncMock(), session, session_repo=repo) is True
    gate = repo.set_intake_gate.await_args.args[2]
    assert gate["approval"]["status"] == ap.INVALIDATED and gate["selection"] == {"engine": "snappy"}
    repo.set_intake_gate.reset_mock()
    assert await msg.withdraw_proposal_for_unit_change(AsyncMock(), _locked(), session_repo=repo) is False
    repo.set_intake_gate.assert_not_awaited()


def _locked(**over):
    base = {"id": SID, "owner_id": "alice", "job_id": None, "geometry_source_id": uuid.uuid4(),
            "geometry_interpretation_id": uuid.uuid4(), "intake_gate": {}, "messages": []}
    base.update(over)
    return SimpleNamespace(**base)


async def _settle(content, *, monkeypatch, gate=None, change=None, hold=None):
    seen: list = []

    async def _change(db, session, *, owner_id, organization_id, unit, session_repo, where="chat"):
        seen.append(unit)
        return change
    monkeypatch.setattr(uch, "change_unit", _change)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: hold)
    handed: list = []

    def _queue(session_id, owner_id, purpose, interpretation):
        handed.append(interpretation)
        return True
    monkeypatch.setattr(gh, "queue_naming", _queue)

    async def _interp(db, sess, owner_id, organization_id):
        return {"unit": "m", "basis": "user_confirmed"}          # what the change just bound
    monkeypatch.setattr(gh, "interpretation_payload", _interp)
    repo = MagicMock()
    repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=content, organization_id="org-1")
    g = dict(gate or {})
    outcome = await msg._settle(inbound, AsyncMock(), gate=g, locked=_locked(intake_gate=g), messages=[],
                                revision="r1", session_repo=repo, logger=MagicMock())
    return outcome, repo, seen


@pytest.mark.asyncio
async def test_the_file_is_in_metres_is_the_applications_answer_not_a_model_turn(monkeypatch):
    change = uch.UnitChange(old_unit="mm", new_unit="m", longest_old_m=0.117, longest_new_m=117.0,
                            reference_length_m=117.0, reread=True, approval_withdrawn=False)
    outcome, repo, seen = await _settle("the file is in metres", monkeypatch=monkeypatch, change=change)
    assert seen == [LengthUnit.metre]
    assert outcome.status is msg.MessageStatus.unit_changed and outcome.answered and not outcome.continues_to_intake
    assert outcome.reply == change.reply()
    assert outcome.reply.startswith("Noted: the file is in metres, so the part is 117 m long, not 117 mm.")
    assert "the reference length is now 117 m" in outcome.reply
    assert repo.append_message.await_args.args[2:] == ("assistant", outcome.reply)
    assert outcome.transition.change is msg.GateChange.unit_changed


@pytest.mark.asyncio
async def test_a_unit_change_during_a_live_approval_withdraws_it_and_says_so(monkeypatch):
    live = {"id": "snap", "status": ap.AWAITING, "expires_at": time.time() + 600,
            "expected_confirmation_msg_count": 3}
    withdrawn = {"approval": ap.invalidate(live, uch.WITHDRAWN_REASON)}
    change = uch.UnitChange(old_unit="mm", new_unit="m", reread=True, approval_withdrawn=True, gate=withdrawn)
    outcome, repo, _ = await _settle("yes but the file is in metres", monkeypatch=monkeypatch,
                                     gate={"approval": live}, change=change)
    assert outcome.status is msg.MessageStatus.unit_changed
    assert outcome.transition.invalidated_approval and outcome.transition.approval_status == ap.INVALIDATED
    assert "withdrawn it" in outcome.reply
    # the intake authority writes the gate the change handed back
    assert repo.set_intake_gate.await_args.args[2] == withdrawn


@pytest.mark.asyncio
async def test_the_unit_already_held_or_a_mere_mention_goes_on_as_an_ordinary_turn(monkeypatch):
    outcome, repo, seen = await _settle("the file is in metres", monkeypatch=monkeypatch, change=None)
    assert seen == [LengthUnit.metre] and outcome.status is msg.MessageStatus.proceed
    repo.append_message.assert_not_awaited()
    outcome, _, seen = await _settle("air at 20 metres per second", monkeypatch=monkeypatch, change=None)
    assert seen == [] and outcome.status is msg.MessageStatus.proceed


@pytest.mark.asyncio
async def test_before_the_picture_was_confirmed_the_change_lets_the_geometry_check_take_its_turn(monkeypatch):
    """The first answer can carry the unit ("an IEA 15 MW blade - the file is in metres"): the
    unit is recorded, and the naming is handed the words and the new unit as usual."""
    change = uch.UnitChange(old_unit="mm", new_unit="m", longest_old_m=0.117, longest_new_m=117.0)
    outcome, _, seen = await _settle("an IEA 15 MW wind turbine blade - the file is in metres",
                                     monkeypatch=monkeypatch, change=change, hold="queue")
    assert seen == [LengthUnit.metre]
    assert outcome.status is msg.MessageStatus.geometry_hold and outcome.reply == gh.HOLD_REPLY


@pytest.mark.asyncio
async def test_a_file_with_no_unit_yet_is_the_unit_questions_not_a_change(monkeypatch):
    seen: list = []

    async def _change(*a, **k):
        seen.append(k.get("unit"))
    monkeypatch.setattr(uch, "change_unit", _change)
    monkeypatch.setattr(gh, "measured_size_mm", lambda sid: None)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: None)
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="the file is in metres", organization_id="")
    outcome = await msg._settle(inbound, AsyncMock(), gate={}, locked=_locked(geometry_interpretation_id=None),
                                messages=[], revision="r1", session_repo=repo, logger=MagicMock())
    assert seen == [] and outcome.status is msg.MessageStatus.unit_question
