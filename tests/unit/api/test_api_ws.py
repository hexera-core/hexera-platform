# Responsibility: Verify the socket authorises by ticket, replays exactly what was missed, and hands over at its cap.
# Boundaries: nothing outside the closed event set, and no model identity, reaches a browser.
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.api.v1.ws import router as ws_router
from meshpipeline.persistence.models import JobStatus
from meshpipeline.settings.modes import ProductModes, TraceDisclosure

_app = FastAPI()
_app.include_router(ws_router, prefix="/api/v1/ws")

_VALID_JOB_ID = str(uuid.UUID("ccccdddd-3333-4333-b333-cccccccccccc"))


class FakeSubscription:

    def __init__(self, job_id, frames=(), backlog=()):
        self.job_id = job_id
        self._frames = list(frames)
        self._backlog = list(backlog)
        self.calls: list[str] = []

    async def open(self):
        self.calls.append("open")

    async def backlog(self):
        self.calls.append("backlog")
        return list(self._backlog)

    async def next_event(self, timeout):
        self.calls.append("next_event")
        return self._frames.pop(0) if self._frames else None

    async def close(self):
        self.calls.append("close")


def _wire(monkeypatch, job, frames=(), backlog=()):
    from meshpipeline.api import security as auth
    monkeypatch.setattr(auth, "verify_identity", lambda k, u, s: "user-1")

    import meshpipeline.persistence.session as dbs

    @asynccontextmanager
    async def fake_db():
        yield None
    monkeypatch.setattr(dbs, "get_db", fake_db)

    import meshpipeline.persistence.repositories.job_repository as jr

    class Repo:
        async def get_internal(self, db, jid):
            return job

        async def get_for_owner(self, db, jid, owner_id):
            # the real repository scopes in SQL, so a foreign owner simply finds nothing
            return job if job is not None and job.owner_id == owner_id else None
    monkeypatch.setattr(jr, "JobRepository", lambda: Repo())

    from meshpipeline.contracts import event_stream
    made: list[FakeSubscription] = []

    def _factory(job_id):
        sub = FakeSubscription(job_id, frames=frames, backlog=backlog)
        made.append(sub)
        return sub

    # monkeypatch (not set_subscription_factory) so the injection auto-reverts and a test
    # factory can never leak into another test.
    monkeypatch.setattr(event_stream, "_subscription_factory", _factory)
    return made


def test_invalid_job_id_closes_ws_immediately():
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/ws/not-a-valid-uuid/stream"):
            pass
    assert exc_info.value.code == 1008


def test_unknown_job_closes_ws(monkeypatch):
    _wire(monkeypatch, job=None)
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream"):
            pass
    assert exc_info.value.code == 1008


def test_other_users_job_closes_ws(monkeypatch):
    _wire(monkeypatch, job=SimpleNamespace(owner_id="someone-else",
                                           status=JobStatus.running))
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream"):
            pass
    assert exc_info.value.code == 1008


def test_stream_delivers_events_and_ends_on_closing(monkeypatch):
    import meshpipeline.events as E
    note = json.dumps(E.note("builder", "Designing the mesh").wire())
    closing = json.dumps(E.closing("your mesh is ready").wire())
    _wire(monkeypatch,
          job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=[note, closing])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "note" and first["stage"] == "builder"
        assert first["text"] == "Designing the mesh"
        second = json.loads(ws.receive_text())
        assert second["type"] == "closing" and second["text"] == "your mesh is ready"


def test_a_payload_outside_the_closed_set_never_reaches_the_browser(monkeypatch):
    import meshpipeline.events as E
    leak = json.dumps({"level": "INFO", "msg": "[MANIFEST_VALIDATION_FAILED] /srv/data/..."})
    closing = json.dumps(E.closing("done").wire())
    _wire(monkeypatch,
          job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=[leak, closing])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        # the leak is NOT the first frame - it was dropped; `closing` is
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "closing", f"a non-event payload reached the browser: {frame}"


def test_a_raw_backlog_replayed_under_a_safe_deployment_reaches_no_browser(monkeypatch):
    import meshpipeline.settings.policy as P
    from meshpipeline.trace.policy import RAW, project
    # MODES is what the projection reads. Setting PUBLIC_TRACE_MODE here created an attribute
    # nothing consults, so the deployment under test was never safe - the assertion below held
    # only while 'safe' happened to be the default, and proved nothing about a safe deployment.
    monkeypatch.setattr(P, "MODES", ProductModes(data_collection_enabled=False,
                                                 trace_disclosure=TraceDisclosure.SAFE))

    stored_raw = [
        json.dumps(project({"type": "reasoning", "stage": "builder", "id": "r1",
                            "agent": "builder", "phase": "completed", "status": "success",
                            "duration_ms": 10, "content": "DELIBERATION-SENTINEL"}, RAW)),
        json.dumps(project({"type": "tool_call", "stage": "reviewer", "id": "c1",
                            "agent": "reviewer", "tool_name": "go_to_coordinates",
                            "arguments": {"x": 0.932}, "status": "started"}, RAW)),
        json.dumps(project({"type": "screenshot", "stage": "reviewer",
                            "image": "IMAGE-SENTINEL"}, RAW)),
    ]
    import meshpipeline.events as E
    closing = json.dumps(E.closing("done").wire())
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=stored_raw, frames=[closing])

    client = TestClient(_app)
    seen = []
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        while True:
            frame = json.loads(ws.receive_text())
            seen.append(frame)
            if frame["type"] == "closing":
                break
    flat = json.dumps(seen)
    assert "DELIBERATION-SENTINEL" not in flat, "raw reasoning text reached the browser"
    assert "IMAGE-SENTINEL" not in flat, "a raw inspection image reached the browser"
    assert "go_to_coordinates" not in flat, "a raw tool name reached the browser"
    reasoning = [f for f in seen if f["type"] == "reasoning"]
    assert reasoning and reasoning[0]["content"] is None, "the event was dropped, not projected"


def test_model_identity_pushed_through_the_publication_path_never_reaches_the_browser(monkeypatch):
    import meshpipeline.events as E
    from meshpipeline.trace.policy import RAW, project
    set_modes(monkeypatch, disclosure="raw")

    frames = [
        # structured identity keys on a tool result
        json.dumps(project({
            "type": "tool_result", "stage": "builder", "id": "r1", "tool_call_id": "c1",
            "agent": "builder", "tool_name": "run_mesh", "status": "success",
            "result": {"cells": 12, "model": "GLM-5.2", "provider": "zai-org",
                       "deployment": "prod-a", "system_fingerprint": "fp_44709d6fcb",
                       "nested": {"fallback_model": "Kimi-K2.5"}}}, RAW)),
        # identity inside free reasoning prose
        json.dumps(project({
            "type": "reasoning", "stage": "builder", "id": "r2", "agent": "builder",
            "phase": "completed", "status": "success", "duration_ms": 10,
            "content": "As GLM 5.2 on zai-org I deferred to GPT-5.6."}, RAW)),
        # identity inside an application-authored rationale
        json.dumps(E.rationale("builder", "Builder", "Configuration ready",
                               "planned by moonshotai/Kimi-K2.5").wire()),
        json.dumps(E.closing("done").wire()),
    ]
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=frames)

    client = TestClient(_app)
    seen = []
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        while True:
            frame = json.loads(ws.receive_text())
            seen.append(frame)
            if frame["type"] == "closing":
                break
    flat = json.dumps(seen).lower()
    for identity in ("glm", "zai-org", "gpt-5.6", "kimi", "moonshotai",
                     "fp_44709d6fcb", "prod-a"):
        assert identity.lower() not in flat, f"{identity!r} reached the browser"
    # ...and the engineering content around it survived, or the censorship is useless
    result = next(f for f in seen if f["type"] == "tool_result")
    assert result["result"]["cells"] == 12


def test_already_terminal_job_gets_closing_event_immediately(monkeypatch):
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1",
                                           status=JobStatus.succeeded))
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "closing"
        assert "succeeded" in frame["text"]


def test_a_reconnecting_client_gets_exactly_what_it_missed_and_nothing_twice(monkeypatch):
    import meshpipeline.events as E

    def _ev(seq, text):
        w = E.note("builder", text).wire()
        w["seq"] = seq
        return json.dumps(w)

    backlog = [_ev(1, "one"), _ev(2, "two"), _ev(3, "three"), _ev(4, "four")]
    # 4 arrives again on the live channel (it was published as we subscribed) and 5 is new
    frames = [_ev(4, "four"), _ev(5, "five"), json.dumps(E.closing("done").wire())]

    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=frames, backlog=backlog)
    client = TestClient(_app)
    with client.websocket_connect(
            f"/api/v1/ws/{_VALID_JOB_ID}/stream?since=2") as ws:
        got = []
        for _ in range(4):
            got.append(json.loads(ws.receive_text()))

    texts = [e.get("text") for e in got if e["type"] == "note"]
    assert texts == ["three", "four", "five"], f"replay is wrong: {texts}"
    assert got[-1]["type"] == "closing"
    seqs = [e["seq"] for e in got if e.get("seq")]
    assert seqs == sorted(set(seqs)), f"an event was delivered twice: {seqs}"


def test_the_socket_hands_over_at_the_session_cap_with_code_1012(monkeypatch):
    monkeypatch.setattr(rtcfg, "WS_MAX_SESSION_SECONDS", -1)   # deadline already in the past
    _wire(monkeypatch,
          job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=[], backlog=[])
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
            ws.receive_text()          # server closes 1012 before sending anything
    assert ei.value.code == 1012, f"handover used close code {ei.value.code}, not 1012"


def test_the_socket_opens_the_stream_before_it_replays(monkeypatch):
    import meshpipeline.events as E
    made = _wire(monkeypatch,
                 job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
                 frames=[json.dumps(E.closing("done").wire())])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        ws.receive_text()
    assert made, "the socket never asked the contract for a subscription"
    calls = made[0].calls
    assert calls[0] == "open", f"the socket did not open the stream first: {calls}"
    assert calls[1] == "backlog", f"the socket did not replay right after opening: {calls}"


def test_the_socket_always_releases_its_subscription(monkeypatch):
    import meshpipeline.events as E
    made = _wire(monkeypatch,
                 job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
                 frames=[json.dumps(E.closing("done").wire())])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        ws.receive_text()
    assert "close" in made[0].calls, "the socket did not release its subscription"


def test_the_socket_is_given_the_job_it_was_asked_for(monkeypatch):
    _made = _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.succeeded))
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        ws.receive_text()
    assert _made[0].job_id == _VALID_JOB_ID


def test_a_normal_session_does_not_hand_over_early(monkeypatch):
    monkeypatch.setattr(rtcfg, "WS_MAX_SESSION_SECONDS", 3000)
    import meshpipeline.events as E
    note = json.dumps(E.note("builder", "working").wire())
    _wire(monkeypatch,
          job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          frames=[note, json.dumps(E.closing("done").wire())])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "note"          # delivered, not handed over


# ticket-authenticated connection (the browser path - no long-lived creds in the URL)
import asyncio  # noqa: E402

from tests.product_modes import set_modes

import meshpipeline.contracts.ws_ticket as _WT  # noqa: E402


def _issue(owner, job):
    return asyncio.run(_WT.issue_ticket(owner, job))


def test_a_valid_ticket_grants_the_stream(monkeypatch):
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=[json.dumps(E.closing("done").wire())])
    ticket = _issue("user-1", _VALID_JOB_ID)
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}") as ws:
        assert json.loads(ws.receive_text())["type"] == "closing"


def test_a_ticket_for_another_job_is_rejected(monkeypatch):
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running))
    other_job = str(uuid.UUID("eeeeffff-5555-4555-b555-eeeeeeeeeeee"))
    ticket = _issue("user-1", other_job)
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}"):
            pass
    assert ei.value.code == 1008


def test_a_ticket_is_single_use_a_replay_is_rejected(monkeypatch):
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=[json.dumps(E.closing("done").wire())])
    ticket = _issue("user-1", _VALID_JOB_ID)
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}") as ws:
        ws.receive_text()
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}"):
            pass
    assert ei.value.code == 1008


def test_a_malformed_ticket_is_rejected(monkeypatch):
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running))
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket=never-issued"):
            pass
    assert ei.value.code == 1008


def test_a_ticket_store_failure_closes_with_1013_and_does_not_log_the_ticket(monkeypatch, caplog):
    import meshpipeline.contracts.ws_ticket as _WT

    class _FailingStore:
        async def issue(self, *a, **k):
            raise RuntimeError("store down")

        async def consume(self, *a, **k):
            raise RuntimeError("store down")

    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running))
    monkeypatch.setattr(_WT, "_store", _FailingStore())
    client = TestClient(_app)
    with caplog.at_level("WARNING"):
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket=SEKRET-TICKET-abc123"):
                pass
    assert ei.value.code == 1013
    assert "SEKRET-TICKET-abc123" not in caplog.text, "the ticket value must never be logged"


# replay-window and ticket-concurrency matrix
# The socket takes its resume point from `?since=<seq>` and replays this job's backlog after it.
# These close the gaps the public-boundary audit could not evidence from adjacent tests: what a
# cursor ahead of the stream does, what an emptied backlog does, and whether one ticket can open
# two sockets.

def _seqd(event, seq):
    body = event.wire()
    body["seq"] = seq
    return json.dumps(body)


def test_a_cursor_replays_only_events_after_it(monkeypatch):
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=[_seqd(E.note("builder", "one"), 1),
                   _seqd(E.note("builder", "two"), 2),
                   _seqd(E.closing("done"), 3)])
    client = TestClient(_app)
    seen = []
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?since=1") as ws:
        while True:
            ev = json.loads(ws.receive_text())
            seen.append(ev)
            if ev["type"] == "closing":
                break
    texts = [e.get("text") for e in seen if e["type"] == "note"]
    assert "one" not in texts, "an event at or before the cursor was replayed"
    assert "two" in texts, "the event after the cursor was not replayed"


def test_a_cursor_ahead_of_the_stream_replays_nothing_stale(monkeypatch):
    # A client claiming a resume point past everything the backlog holds must not be shown older
    # frames. It waits for genuinely new ones; the terminal close is what ends it.
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.succeeded),
          backlog=[_seqd(E.note("builder", "old"), 1), _seqd(E.note("builder", "older"), 2)])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?since=99999") as ws:
        ev = json.loads(ws.receive_text())
    assert ev["type"] == "closing", f"a stale frame was replayed past the cursor: {ev}"


def test_an_emptied_backlog_still_closes_a_terminal_job(monkeypatch):
    # The event log carries a TTL, so a client that reconnects late finds nothing to replay. That
    # must still end cleanly on the terminal state rather than hang on an empty list.
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.succeeded),
          backlog=[])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?since=42") as ws:
        ev = json.loads(ws.receive_text())
    assert ev["type"] == "closing"


def test_a_malformed_cursor_is_treated_as_the_beginning(monkeypatch):
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=[_seqd(E.closing("done"), 1)])
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?since=not-a-number") as ws:
        assert json.loads(ws.receive_text())["type"] == "closing"


def test_one_ticket_cannot_open_a_second_socket(monkeypatch):
    # SINGLE USE ACROSS SOCKETS, not merely across calls: the first connection consumes the ticket,
    # so a second attempt with the same string is refused even while the first is still open.
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-1", status=JobStatus.running),
          backlog=[json.dumps(E.closing("done").wire())])
    ticket = _issue("user-1", _VALID_JOB_ID)
    client = TestClient(_app)
    with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}") as first:
        first.receive_text()
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(
                    f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}"):
                pass
        assert ei.value.code == 1008, "a consumed ticket opened a second socket"


def test_a_ticket_cannot_be_issued_across_owners_and_never_reaches_a_frame(monkeypatch):
    # Issued for user-1's job; user-2 holding the same string still cannot use it, and no frame
    # the browser receives carries the ticket back.
    import meshpipeline.events as E
    _wire(monkeypatch, job=SimpleNamespace(owner_id="user-2", status=JobStatus.running),
          backlog=[json.dumps(E.closing("done").wire())])
    ticket = _issue("user-1", _VALID_JOB_ID)
    client = TestClient(_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(f"/api/v1/ws/{_VALID_JOB_ID}/stream?ticket={ticket}"):
            pass
    assert ei.value.code == 1008
