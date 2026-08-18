# Responsibility: Issue and consume WebSocket tickets across every API instance.
# Boundaries: a ticket is single-use and expires; consumption is atomic so two sockets cannot share one.
from __future__ import annotations

import secrets

from meshpipeline.adapters._shared.redis_client import async_client

_PREFIX = "wsticket:"
_SEP = "\x00"          # owner ids / job ids never contain a NUL, so this splits unambiguously


class RedisWsTicketStore:
    def __init__(self) -> None:
        self._redis = None

    def _client(self):
        if self._redis is None:
            # 1s timeouts: minting/redeeming a ticket must never hang the request path.
            self._redis = async_client(decode_responses=True,
                                       socket_connect_timeout=1, socket_timeout=1)
        return self._redis

    async def issue(self, owner_id: str, job_id: str, ttl_seconds: int) -> str:
        ticket = secrets.token_urlsafe(32)
        await self._client().set(f"{_PREFIX}{ticket}", f"{owner_id}{_SEP}{job_id}", ex=ttl_seconds)
        return ticket

    async def consume(self, ticket: str) -> tuple[str, str] | None:
        if not ticket or len(ticket) > 128:
            return None
        raw = await self._client().getdel(f"{_PREFIX}{ticket}")   # atomic single-use
        if not raw or _SEP not in raw:
            return None
        owner_id, job_id = raw.split(_SEP, 1)
        return (owner_id, job_id)
