# Responsibility: Verify a stream ticket is single-use, job-bound, and refused when the store is down.
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from meshpipeline.adapters.ws_ticket.memory import MemoryWsTicketStore
from meshpipeline.adapters.ws_ticket.redis import RedisWsTicketStore
from meshpipeline.api.v1.ws import router as ws_router

_JOB = str(uuid.UUID("ccccdddd-3333-4333-b333-cccccccccccc"))
_OTHER_JOB = str(uuid.UUID("eeeeffff-5555-4555-b555-eeeeeeeeeeee"))


# store semantics

def test_issue_then_consume_returns_the_binding():
    s = MemoryWsTicketStore()
    t = asyncio.run(s.issue("user-1", _JOB, 30))
    assert asyncio.run(s.consume(t)) == ("user-1", _JOB)


def test_second_consume_fails_single_use_replay():
    s = MemoryWsTicketStore()
    t = asyncio.run(s.issue("user-1", _JOB, 30))
    assert asyncio.run(s.consume(t)) == ("user-1", _JOB)
    assert asyncio.run(s.consume(t)) is None            # replay is refused


def test_expired_ticket_consumes_to_none():
    s = MemoryWsTicketStore()
    t = asyncio.run(s.issue("user-1", _JOB, 0))          # already expired
    assert asyncio.run(s.consume(t)) is None


def test_malformed_or_unknown_ticket_consumes_to_none():
    s = MemoryWsTicketStore()
    assert asyncio.run(s.consume("never-issued")) is None
    assert asyncio.run(s.consume("")) is None


def test_two_tickets_are_distinct_and_independently_single_use():
    s = MemoryWsTicketStore()
    a = asyncio.run(s.issue("user-1", _JOB, 30))
    b = asyncio.run(s.issue("user-1", _JOB, 30))
    assert a != b
    assert asyncio.run(s.consume(a)) == ("user-1", _JOB)
    assert asyncio.run(s.consume(b)) == ("user-1", _JOB)   # consuming a did not consume b


def test_redis_store_rejects_empty_and_oversized_tickets_without_a_client():
    s = RedisWsTicketStore()
    assert asyncio.run(s.consume("")) is None
    assert asyncio.run(s.consume("x" * 200)) is None
    assert s._redis is None                                 # never constructed a client


# issue endpoint (POST /api/v1/ws/ticket)

_app = FastAPI()
_app.include_router(ws_router, prefix="/api/v1/ws")


@asynccontextmanager
async def _fake_db():
    yield AsyncMock()


def _repo_returning(job):
    repo = MagicMock()
    repo.get_internal = AsyncMock(return_value=job)
    # scoped like the real query: a caller who does not own the job finds nothing
    async def _scoped(_db, _jid, owner_id):
        return job if job is not None and job.owner_id == owner_id else None
    repo.get_for_owner = _scoped
    return repo


def test_ticket_issued_for_an_owned_job():
    job = SimpleNamespace(id=uuid.UUID(_JOB), owner_id="user-1")
    with (
        patch("meshpipeline.api.security.verify_identity", lambda k, u, s: "user-1"),
        patch("meshpipeline.persistence.session.get_db", _fake_db),
        patch("meshpipeline.persistence.repositories.job_repository.JobRepository",
              return_value=_repo_returning(job)),
    ):
        r = TestClient(_app).post("/api/v1/ws/ticket", json={"job_id": _JOB})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"] and body["expires_in_seconds"] > 0
    # the ticket is opaque and is NOT any of the caller's long-lived credentials
    assert body["ticket"] not in ("user-1", "")


def test_ticket_refused_for_a_job_the_caller_does_not_own():
    someone_elses = SimpleNamespace(id=uuid.UUID(_JOB), owner_id="somebody-else")
    with (
        patch("meshpipeline.api.security.verify_identity", lambda k, u, s: "user-1"),
        patch("meshpipeline.persistence.session.get_db", _fake_db),
        patch("meshpipeline.persistence.repositories.job_repository.JobRepository",
              return_value=_repo_returning(someone_elses)),
    ):
        r = TestClient(_app).post("/api/v1/ws/ticket", json={"job_id": _JOB})
    assert r.status_code == 404


def test_ticket_refused_for_a_malformed_job_id():
    with (
        patch("meshpipeline.api.security.verify_identity", lambda k, u, s: "user-1"),
        patch("meshpipeline.persistence.session.get_db", _fake_db),
    ):
        r = TestClient(_app).post("/api/v1/ws/ticket", json={"job_id": "not-a-uuid"})
    assert r.status_code == 422


# ticket-STORE failure is actionable, not an unexplained 500

class _FailingTicketStore:
    async def issue(self, *a, **k):
        raise RuntimeError("ticket store unavailable")

    async def consume(self, *a, **k):
        raise RuntimeError("ticket store unavailable")


def test_issuance_returns_503_when_the_ticket_store_is_down():
    import meshpipeline.contracts.ws_ticket as WT
    job = SimpleNamespace(id=uuid.UUID(_JOB), owner_id="user-1")
    prev = WT._store
    WT.set_ws_ticket_store(_FailingTicketStore())
    try:
        with (
            patch("meshpipeline.api.security.verify_identity", lambda k, u, s: "user-1"),
            patch("meshpipeline.persistence.session.get_db", _fake_db),
            patch("meshpipeline.persistence.repositories.job_repository.JobRepository",
                  return_value=_repo_returning(job)),
        ):
            r = TestClient(_app).post("/api/v1/ws/ticket", json={"job_id": _JOB})
    finally:
        WT.set_ws_ticket_store(prev)
    assert r.status_code == 503, r.text
    assert "temporarily unavailable" in r.json()["detail"].lower()
