# Responsibility: Verify a user who does not know the file's unit is never asked it again and again:
# measured, the deferral takes the proposal; not measured yet, the part is drawn and the stage
# proposes the unit from its size (its Proceed records it), as an FEA part's unit is settled.
# Boundaries: the message authority's settle step with a fake repository and a stubbed check store.
"""Real path, dev-areen, 2026-10-28: an Ahmed body STL (no unit in the file). The user answered the
unit question "Not sure. Please use your best guess from the part's size." The part had not been
measured yet - the geometry check's naming waits for the unit - so nothing could propose one, and
the reply was handed to the model, which asked the unit again. Sixteen turns later the part had
never been drawn. "Not sure" and "best guess" were not even read as leaving it to us."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.message as msg
import meshpipeline.agents.intake.unit_clarification as uc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts.geometry_units import LengthUnit

SID = uuid.UUID("cccc1111-2222-4222-b222-eeeeeeeeeeee")
AHMED_M = [1.044, 0.389, 0.338]
PURPOSE = [{"role": "user", "content": "External aero around this, 30 m/s air."}]
NOT_SURE = "Not sure. Please use your best guess from the part's size."


def _locked(gate, messages=()):
    return SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=uuid.uuid4(),
                           geometry_interpretation_id=None, intake_gate=dict(gate), messages=list(messages))


async def _settle(content, *, gate, monkeypatch, size, hold="queue"):
    monkeypatch.setattr(gh, "measured_size_mm", lambda sid: size)
    monkeypatch.setattr(gh, "measured_unit_inference", lambda sid, words="": None)
    monkeypatch.setattr(gh, "hold_applies", lambda sid: hold)
    monkeypatch.setattr(gh, "stored_thermal", lambda sid: (False, ""))
    monkeypatch.setattr(gh, "upload_check_mode", AsyncMock(return_value=""))
    monkeypatch.setattr(gh, "interpretation_payload", AsyncMock(return_value=None))
    queued: list = []
    monkeypatch.setattr(gh, "queue_naming", lambda sid, owner, purpose, interp: queued.append(interp) or True)
    recorded: list = []

    async def _record(db, *, owner_id, geometry_source_id, unit, organization_id=""):
        recorded.append(unit)
        return SimpleNamespace(interpretation_id=str(uuid.uuid4()))
    monkeypatch.setattr(uc, "record", _record)
    repo = MagicMock()
    repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock(); repo.bind_geometry_interpretation = AsyncMock()
    messages = PURPOSE + [{"role": "user", "content": content}]
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=content, organization_id="org-1")
    outcome = await msg._settle(inbound, AsyncMock(), gate=dict(gate), locked=_locked(gate, messages),
                                messages=messages, revision="r1", session_repo=repo, logger=MagicMock())
    return outcome, recorded, queued


@pytest.mark.parametrize("said", [NOT_SURE, "not sure", "I'm unsure", "no clue", "use your best guess",
                                  "can't say, guess it from the size", "I don't know", "you choose",
                                  "Use a sensible default."])
def test_not_knowing_the_unit_is_leaving_it_to_us(said):
    assert uc.leaves_unit_to_us(said)


@pytest.mark.parametrize("said", ["not sure, maybe millimetres", "is it in metres?", "best guess is inches",
                                  "not mm", "metres", "air at 30 m/s", "a 1 m long body", "yes"])
def test_a_unit_named_or_a_question_is_not_leaving_it_to_us(said):
    assert not uc.leaves_unit_to_us(said)


@pytest.mark.asyncio
async def test_not_sure_before_the_part_is_measured_draws_it_and_the_stage_proposes_the_unit(monkeypatch):
    outcome, recorded, queued = await _settle(NOT_SURE, gate=uc.asked({}), monkeypatch=monkeypatch, size=None)
    assert outcome.status is msg.MessageStatus.geometry_hold and outcome.queued_naming
    assert outcome.reply.startswith(gh.DRAWING_MARK) and uc.ON_STAGE in outcome.reply
    assert recorded == [] and queued == [None]              # nothing guessed: the stage records the unit


@pytest.mark.asyncio
async def test_not_sure_once_measured_takes_the_proposal_from_the_size(monkeypatch):
    outcome, recorded, _ = await _settle(NOT_SURE, gate=uc.asked({}), monkeypatch=monkeypatch, size=AHMED_M)
    assert outcome.status is msg.MessageStatus.geometry_hold and recorded == [LengthUnit.metre]
    assert uc.ON_STAGE not in outcome.reply


@pytest.mark.asyncio
async def test_without_a_check_to_draw_the_turn_goes_on_as_before(monkeypatch):
    outcome, recorded, queued = await _settle(NOT_SURE, gate=uc.asked({}), monkeypatch=monkeypatch, size=None,
                                              hold=None)
    assert outcome.status is msg.MessageStatus.proceed and recorded == [] and queued == []


@pytest.mark.asyncio
async def test_an_unasked_not_sure_still_gets_the_question(monkeypatch):
    # nothing asked yet: "not sure" about something else is no answer to a question never put
    outcome, recorded, queued = await _settle("not sure about the speed yet", gate={}, monkeypatch=monkeypatch,
                                              size=None)
    assert outcome.status is msg.MessageStatus.unit_question and recorded == [] and queued == []
