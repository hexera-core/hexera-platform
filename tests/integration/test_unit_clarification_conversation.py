# Responsibility: Verify the unit question is asked once, only when needed, and nothing but an answer settles it.
from __future__ import annotations

import os
import uuid

import pytest

from meshpipeline.agents.intake import unit_clarification as uc

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

pytestmark = pytest.mark.asyncio

_OWNER = "tenant-unit-chat"

_MODEL_REPLY = "Understood. What inlet velocity should I use?"


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    import meshpipeline.agents.intake.agent as agent

    async def _canned(_state):
        return {"messages": [{"role": "assistant", "content": _MODEL_REPLY}]}

    monkeypatch.setattr(agent, "node_intake", _canned)


async def _session_with_unresolved_geometry(db, tmp_path, store):
    from tests._geometry_support import persisted_source

    from meshpipeline.application.job_service import JobService
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    session_id = await JobService().create_session(db, _OWNER)
    _row, ref = await persisted_source(db, owner_id=_OWNER, tmp_path=tmp_path)
    await db.commit()                       # the source must exist before anything references it
    session = await SessionRepository().get_internal(db, session_id)
    session.geometry_source_id = uuid.UUID(ref.source_id)
    await db.commit()
    return session_id, ref


async def _say(session_id, text):
    from meshpipeline.api.schemas.chat import ChatMessageIn
    from meshpipeline.api.v1.chat import chat_message
    return await chat_message(ChatMessageIn(session_id=session_id, content=text), owner_id=_OWNER)


async def _session(session_id):
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        return await SessionRepository().get_for_owner(db, session_id, _OWNER)


# the grammar itself

@pytest.mark.parametrize("answer,expected", [
    ("mm", "mm"), ("millimetres", "mm"), ("Millimeters", "mm"),
    ("cm", "cm"), ("centimetres", "cm"),
    ("m", "m"), ("metres", "m"), ("meters", "m"),
    ("in", "in"), ("inches", "in"), ("INCH", "in"),
])
def test_the_four_units_are_understood(answer, expected):
    assert uc.classify(answer).value == expected


@pytest.mark.parametrize("answer", [
    "", "   ", "furlongs", "about 5", "yes", "the usual",
    "it is not in metres",             # mentions a unit, states nothing
    "somewhere between mm and cm",     # two units is not an answer
])
def test_anything_that_is_not_an_answer_is_refused(answer):
    assert uc.classify(answer) is None


# through the real conversation

async def test_the_question_is_asked_once_and_stored_in_the_session_history(db, store, tmp_path):
    session_id, _ = await _session_with_unresolved_geometry(db, tmp_path, store)

    reply = await _say(session_id, "Mesh this duct for 5 m/s inlet flow.")

    assert reply.reply == uc.QUESTION
    assert reply.awaiting_confirmation

    session = await _session(session_id)
    roles = [(m.get("role"), m.get("content")) for m in (session.messages or [])]
    # the user's own message and the question are both ordinary history, in order
    assert roles[-2][0] == "user"
    assert roles[-1] == ("assistant", uc.QUESTION)


async def test_a_confirmed_unit_is_recorded_and_bound_to_the_session(db, store, tmp_path):
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.session import get_db

    session_id, ref = await _session_with_unresolved_geometry(db, tmp_path, store)
    await _say(session_id, "mesh it")                       # provokes the question
    after = await _say(session_id, "millimetres")           # answers it

    # answering hands the conversation back to intake rather than ending it
    assert after.reply == _MODEL_REPLY

    session = await _session(session_id)
    assert session.geometry_interpretation_id is not None

    async with get_db() as fresh:
        recorded = await GeometryInterpretationRepository().get_for_owner(
            fresh, session.geometry_interpretation_id, _OWNER)
    assert recorded.unit.value == "mm"
    assert recorded.basis.value == "user_confirmed"      # a person answered; the file did not
    assert recorded.scale_to_metres == 1e-3
    assert recorded.geometry_source_id == ref.source_id


async def test_an_unusable_answer_asks_again_and_records_nothing(db, store, tmp_path):
    session_id, _ = await _session_with_unresolved_geometry(db, tmp_path, store)
    await _say(session_id, "mesh it")

    again = await _say(session_id, "furlongs")

    assert again.reply == uc.REFUSAL
    session = await _session(session_id)
    assert session.geometry_interpretation_id is None, "an unusable answer must record nothing"


async def test_it_keeps_asking_rather_than_letting_the_run_proceed(db, store, tmp_path):
    session_id, _ = await _session_with_unresolved_geometry(db, tmp_path, store)
    await _say(session_id, "mesh it")

    for evasion in ("just use whatever", "the normal one", "you decide"):
        assert (await _say(session_id, evasion)).reply == uc.REFUSAL

    assert (await _session(session_id)).geometry_interpretation_id is None


async def test_once_answered_the_question_is_not_asked_again(db, store, tmp_path):
    session_id, _ = await _session_with_unresolved_geometry(db, tmp_path, store)
    await _say(session_id, "mesh it")
    await _say(session_id, "inches")

    session = await _session(session_id)
    assert session.geometry_interpretation_id is not None
    assert not uc.needs_confirmation(session)
    assert not uc.already_asked(session.intake_gate), "the pending question was not cleared"


async def test_a_session_whose_file_declared_its_unit_is_never_asked(db, store, tmp_path):
    from tests._geometry_support import persisted_geometry

    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    geom = await persisted_geometry(db, store=store, tmp_path=tmp_path, owner_id=_OWNER,
                                    unit=LengthUnit.millimetre,
                                    basis=ResolutionBasis.file_declared)

    from meshpipeline.application.job_service import JobService
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    session_id = await JobService().create_session(db, _OWNER)
    await db.commit()
    row = await SessionRepository().get_internal(db, session_id)
    row.geometry_source_id = uuid.UUID(geom.source_id)
    row.geometry_interpretation_id = uuid.UUID(geom.interpretation_id)
    await db.commit()

    assert not uc.needs_confirmation(await _session(session_id))


async def test_a_session_with_no_upload_is_never_asked(db, store, tmp_path):
    from meshpipeline.application.job_service import JobService
    session_id = await JobService().create_session(db, _OWNER)
    await db.commit()

    assert not uc.needs_confirmation(await _session(session_id))
