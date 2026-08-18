# Responsibility: Stream a job's events to the browser, and issue the ticket that authorises the socket.
# Owns: ticket issue, socket authorisation, backlog replay and the close codes.
# Boundaries: transport: it publishes nothing and decides no event's content.
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import meshpipeline.events as E
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.api.security import owner_dep
from meshpipeline.contracts.event_stream import subscription
from meshpipeline.trace.policy import project as _trace_project

router = APIRouter()
logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


class _TicketIn(BaseModel):
    job_id: str


class _TicketOut(BaseModel):
    ticket: str
    expires_in_seconds: int


@router.post("/ticket", response_model=_TicketOut)
async def issue_ws_ticket(body: _TicketIn, owner_id: str = Depends(owner_dep)):
    if not _UUID_RE.match(body.job_id):
        raise HTTPException(status_code=422, detail="Invalid job_id format")

    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        job = await JobRepository().get_for_owner(db, uuid.UUID(body.job_id), owner_id)
    # A ticket may only ever be minted for a job the caller owns - this is where "wrong user"
    # is stopped: another user cannot obtain a ticket scoped to someone else's job.
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or access denied")

    from meshpipeline.contracts.ws_ticket import WS_TICKET_TTL_SECONDS, issue_ticket
    try:
        ticket = await issue_ticket(owner_id, body.job_id)
    except Exception as exc:  # noqa: BLE001 - the ticket STORE (Redis) is down, not the caller's fault
        # A deliberate, actionable 503 - not an unexplained 500. Log the failure TYPE only:
        # never the caller's credentials, and there is no ticket yet to leak.
        logger.warning("ws-ticket issuance failed - ticket store unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Live updates are temporarily unavailable - please retry shortly.") from exc
    return _TicketOut(ticket=ticket, expires_in_seconds=WS_TICKET_TTL_SECONDS)


async def _authenticate_ws(websocket: WebSocket, job_id: str) -> str | None:
    ticket = websocket.query_params.get("ticket", "")
    if ticket:
        from meshpipeline.contracts.ws_ticket import consume_ticket
        try:
            redeemed = await consume_ticket(ticket)
        except Exception as exc:  # noqa: BLE001 - the ticket STORE is down: fail closed, but SAY so
            # 1013 = Try Again Later (a temporary condition), distinct from the 1008 policy close
            # used for a genuinely invalid ticket. Log the failure TYPE only, never the ticket.
            logger.warning("ws-ticket redemption failed - ticket store unavailable: %s", type(exc).__name__)
            await websocket.close(code=1013, reason="live updates temporarily unavailable")
            return None
        if redeemed is None:
            await websocket.close(code=1008, reason="Invalid or expired ticket")
            return None
        owner_id, ticket_job = redeemed
        if ticket_job != job_id:            # a ticket is bound to ONE job - reject cross-job use
            await websocket.close(code=1008, reason="Ticket not valid for this job")
            return None
        return owner_id

    from meshpipeline.api.security import verify_identity
    try:
        return verify_identity(
            websocket.headers.get("x-api-key", ""),
            websocket.headers.get("x-user-id", "").strip(),
            websocket.headers.get("x-user-sig", "").strip(),
        )
    except HTTPException:
        await websocket.close(code=1008, reason="Unauthorized")
        return None


@router.websocket("/{job_id}/stream")
async def stream_logs(websocket: WebSocket, job_id: str):
    if not _UUID_RE.match(job_id):
        await websocket.close(code=1008, reason="Invalid job_id format")
        return

    owner_id = await _authenticate_ws(websocket, job_id)
    if owner_id is None:
        return   # _authenticate_ws already closed the socket with a reason

    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import get_db
    _repo = JobRepository()
    async with get_db() as db:
        job = await _repo.get_for_owner(db, uuid.UUID(job_id), owner_id)

    if not job:
        await websocket.close(code=1008, reason="Job not found or access denied")
        return

    await websocket.accept()
    logger.info("WebSocket client connected for job %s", job_id)

    stream = subscription(job_id)

    # If the job is ALREADY terminal when the client connects (or becomes
    # terminal without a closing log - e.g. crash-dropped then reaped), there is no
    # future pub/sub message, so a plain `listen()` would hang forever. Poll the DB
    # status between messages and close on a terminal state.
    from meshpipeline.persistence.models import JobStatus
    _TERMINAL = {JobStatus.succeeded, JobStatus.failed}

    async def _is_terminal() -> JobStatus | None:
        try:
            async with get_db() as _db:
                _j = await _repo.get_for_owner(_db, uuid.UUID(job_id), owner_id)
            if _j is not None and _j.status in _TERMINAL:
                return _j.status
        except Exception:
            pass
        return None

    async def _terminal_closing(status: JobStatus) -> str:
        try:
            async with get_db() as _db:
                _j = await _repo.get_for_owner(_db, uuid.UUID(job_id), owner_id)
            _frd = getattr(_j, "final_result", None) if _j is not None else None
            if _frd:
                from meshpipeline.application.final_result import FinalResult, render_message
                return render_message(FinalResult.from_dict(_frd))
        except Exception:  # noqa: BLE001
            pass
        return f"Job already {status.value}."

    # WHERE THIS CLIENT GOT TO. A browser that reconnects after a dropped socket sends
    # the last `seq` it rendered; we replay everything after it. Without this, a
    # disconnect on a multi-hour job loses that slice of the timeline permanently -
    # pub/sub does not keep what nobody was listening for.
    try:
        since = int(websocket.query_params.get("since", "0") or 0)
    except ValueError:
        since = 0

    _sent = since          # the highest seq this socket has delivered
    _deadline = time.monotonic() + rtcfg.WS_MAX_SESSION_SECONDS

    async def _send(data: dict, raw: str | None = None) -> bool:
        nonlocal _sent
        if data.get("type") not in E.EVENT_TYPES:
            logger.warning("dropping non-event payload for job %s: %.80s", job_id, raw or data)
            return True
        seq = int(data.get("seq") or 0)
        if seq and seq <= _sent:
            return True                       # already delivered (replay/live overlap)
        # THE SECOND GATE. Publication already projected this event for the mode that
        # was live when it was WRITTEN - but the backlog outlives the process. A
        # deployment restarted raw -> safe would otherwise replay reasoning text, tool
        # arguments and inspection images that its current mode forbids, simply because
        # they were already in Redis under their TTL. So the projection runs again at
        # the door, against the mode running NOW. It is idempotent, so an event written
        # in safe mode passes through unchanged.
        projected = _trace_project(data)
        if projected is None:
            if seq:
                _sent = seq                   # suppressed, but the cursor still advances
            return True
        if seq:
            _sent = seq
        # the raw string is only reusable when the projection changed nothing
        body = raw if (raw is not None and projected == data) else json.dumps(projected)
        await websocket.send_text(body)
        return projected["type"] != E.CLOSING

    try:
        # SUBSCRIBE FIRST, THEN REPLAY. The other order leaves a window in which an
        # event is published after the backlog is read and before we are listening -
        # it would be lost, and lost silently.
        await stream.open()

        _backlog = await stream.backlog()
        for raw in _backlog:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not await _send(data, raw):
                return                        # the run already closed - history is all
        _term = await _is_terminal()
        if _term is not None:
            # Terminal, and the log had no closing event (crash-dropped, then reaped). Show the
            # durable application-rendered verdict, not a generic note.
            await websocket.send_text(json.dumps(E.closing(await _terminal_closing(_term)).wire()))
            return

        while True:
            if time.monotonic() > _deadline:
                # HAND OVER before the platform severs us at its own 60-minute cap. The
                # client reconnects with its cursor and misses nothing; a socket the
                # platform kills mid-frame looks like a crash instead.
                logger.info("WS session cap reached for job %s - client will resume "
                            "from seq=%d", job_id, _sent)
                await websocket.close(code=1012, reason="resume")
                return
            live = await stream.next_event(timeout=5.0)
            if live is None:
                # Silence is NORMAL: meshing can run for an hour without a single event.
                # Re-check terminal so a crashed/reaped job still ends the stream.
                _t2 = await _is_terminal()
                if _t2 is not None:
                    await websocket.send_text(json.dumps(E.closing(await _terminal_closing(_t2)).wire()))
                    return
                continue
            try:
                data = json.loads(live)
            except (json.JSONDecodeError, TypeError):
                continue
            if not await _send(data, live):
                return
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected for job %s", job_id)
    except Exception as exc:
        logger.exception("WebSocket error for job %s: %s", job_id, exc)
    finally:
        await stream.close()
        try:
            await websocket.close()
        except Exception:
            pass
