# Responsibility: Verify the message authority holds a turn for the geometry check - under its lock,
# once, with the user's words handed on and the holding line stored - and lets every other turn
# through.
# Boundaries: the authority's settle step with a fake repository; the store and the queue are stood
# in for at the application seam.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.message as msg
from meshpipeline.application import geometry_hold as gh

SID = uuid.UUID("dddd1111-2222-4222-b222-dddddddddddd")


def _locked(**over):
    base = {"id": SID, "owner_id": "alice", "job_id": None, "geometry_source_id": None,
            "geometry_interpretation_id": None, "intake_gate": {}, "messages": []}
    base.update(over)
    return SimpleNamespace(**base)


async def _settle(monkeypatch, *, applies: bool, queued: bool = True, messages=None):
    monkeypatch.setattr(gh, "hold_applies", lambda sid: applies)
    handed: dict = {}

    def _queue(session_id, owner_id, purpose, interpretation):
        handed.update(session_id=session_id, owner_id=owner_id, purpose=purpose, interpretation=interpretation)
        return queued
    monkeypatch.setattr(gh, "queue_naming", _queue)
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="water through this elbow",
                                 organization_id="org-1")
    msgs = messages or [{"role": "assistant", "content": "What are you meshing this for?"},
                        {"role": "user", "content": "water through this elbow"}]
    outcome = await msg._settle(inbound, AsyncMock(), gate={}, locked=_locked(), messages=msgs,
                                revision="r1", session_repo=repo, logger=MagicMock())
    return outcome, repo, handed


@pytest.mark.asyncio
async def test_the_first_answer_is_held_with_the_users_words_handed_to_the_naming(monkeypatch):
    outcome, repo, handed = await _settle(monkeypatch, applies=True)
    assert outcome.status is msg.MessageStatus.geometry_hold and outcome.answered
    assert outcome.reply == gh.HOLD_REPLY
    assert handed["session_id"] == str(SID) and handed["owner_id"] == "alice"
    assert handed["purpose"] == "water through this elbow" and handed["interpretation"] is None
    repo.append_message.assert_awaited_once()
    assert repo.append_message.await_args.args[2:] == ("assistant", gh.HOLD_REPLY)


@pytest.mark.asyncio
async def test_a_turn_the_check_is_not_waiting_for_proceeds_to_the_intake(monkeypatch):
    outcome, repo, handed = await _settle(monkeypatch, applies=False)
    assert outcome.status is msg.MessageStatus.proceed and outcome.continues_to_intake
    assert handed == {} and repo.append_message.await_count == 0


@pytest.mark.asyncio
async def test_when_nothing_can_run_the_naming_the_intake_carries_on(monkeypatch):
    outcome, repo, _ = await _settle(monkeypatch, applies=True, queued=False)
    assert outcome.status is msg.MessageStatus.proceed
    assert repo.append_message.await_count == 0


def test_the_hold_is_an_answered_turn_the_route_relays_without_a_model():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.geometry_hold, reply=gh.HOLD_REPLY)
    assert outcome.answered and not outcome.continues_to_intake
