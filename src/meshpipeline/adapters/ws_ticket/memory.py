# Responsibility: Issue and consume WebSocket tickets within one process.
# Boundaries: the in-process store: correct for one instance and for tests, never shared across processes.
from __future__ import annotations

import secrets
import time


class MemoryWsTicketStore:
    def __init__(self) -> None:
        self._d: dict[str, tuple[str, str, float]] = {}

    async def issue(self, owner_id: str, job_id: str, ttl_seconds: int) -> str:
        ticket = secrets.token_urlsafe(32)
        self._d[ticket] = (owner_id, job_id, time.monotonic() + ttl_seconds)
        return ticket

    async def consume(self, ticket: str) -> tuple[str, str] | None:
        rec = self._d.pop(ticket, None)   # single-use: pop, so a second consume finds nothing
        if rec is None:
            return None
        owner_id, job_id, expires_at = rec
        if time.monotonic() > expires_at:
            return None
        return (owner_id, job_id)
