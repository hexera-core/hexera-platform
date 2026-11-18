# Responsibility: Verify a run is marked where it started in its conversation: linking a job to a session puts the
# run's id on the last message so far, so the console draws the run's box right after it on a reload - and the mark
# changes nothing the model reads.
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from meshpipeline.persistence.repositories.session_repository import SessionRepository


def _link(messages, job):
    session = SimpleNamespace(job_id=None, messages=messages)
    repo = SessionRepository()

    async def _get(db, session_id):
        return session

    repo.get_internal = _get
    asyncio.run(repo.link_job(None, uuid.uuid4(), job))
    return session


def test_the_message_a_run_started_after_carries_its_id():
    job = uuid.uuid4()
    said = [{"role": "assistant", "content": "Shall I proceed?"}, {"role": "user", "content": "yes, proceed"}]
    s = _link(said, job)
    assert s.job_id == job
    assert s.messages == [{"role": "assistant", "content": "Shall I proceed?"},
                          {"role": "user", "content": "yes, proceed", "job_id": str(job)}]
    # a new list and a new dict: the stored column sees the change, the caller's list is untouched
    assert said[-1] == {"role": "user", "content": "yes, proceed"}


def test_a_re_run_marks_its_own_message_and_keeps_the_first_runs():
    first, second = uuid.uuid4(), uuid.uuid4()
    s = _link([{"role": "user", "content": "yes, proceed"}], first)
    s.messages = s.messages + [{"role": "assistant", "content": "Done."},
                               {"role": "user", "content": "Run it again with the same requirements."}]
    repo = SessionRepository()

    async def _get(db, session_id):
        return s

    repo.get_internal = _get
    asyncio.run(repo.link_job(None, uuid.uuid4(), second))
    assert [m.get("job_id") for m in s.messages] == [str(first), None, str(second)]


def test_the_mark_never_reaches_the_model_or_the_approval_revision():
    from meshpipeline.agents.intake import admission_token as at
    from meshpipeline.agents.intake.agent import _build_llm_messages

    plain = [{"role": "assistant", "content": "Shall I proceed?"}, {"role": "user", "content": "yes, proceed"}]
    marked = _link([dict(m) for m in plain], uuid.uuid4()).messages
    assert _build_llm_messages("sys", marked) == _build_llm_messages("sys", plain)
    assert at.revision_of(marked) == at.revision_of(plain)


def test_a_conversation_with_nothing_said_is_left_alone():
    s = _link([], uuid.uuid4())
    assert s.messages == []
