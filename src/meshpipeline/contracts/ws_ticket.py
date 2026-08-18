# Responsibility: Declare how a WebSocket connection is authorised.
# Boundaries: a Protocol and its binding; a ticket is single-use, and the stores implement that.
from __future__ import annotations

from typing import Protocol, runtime_checkable

# Long enough to cover the issue->open round trip, short enough that a ticket captured from a log
# is near-worthless. Single-use is what makes the short window the real guarantee, not secrecy.
WS_TICKET_TTL_SECONDS = 30


class WsTicketError(RuntimeError):
    pass


@runtime_checkable
class WsTicketStore(Protocol):
    async def issue(self, owner_id: str, job_id: str, ttl_seconds: int) -> str:
        ...

    async def consume(self, ticket: str) -> tuple[str, str] | None:
        ...


_store: WsTicketStore | None = None


def set_ws_ticket_store(store: WsTicketStore | None) -> None:
    global _store
    _store = store


async def issue_ticket(owner_id: str, job_id: str, ttl_seconds: int = WS_TICKET_TTL_SECONDS) -> str:
    if _store is None:
        raise WsTicketError("no ws-ticket store configured - runtime composition must call "
                            "set_ws_ticket_store()")
    return await _store.issue(owner_id, job_id, ttl_seconds)


async def consume_ticket(ticket: str) -> tuple[str, str] | None:
    if _store is None:
        raise WsTicketError("no ws-ticket store configured - runtime composition must call "
                            "set_ws_ticket_store()")
    return await _store.consume(ticket)
