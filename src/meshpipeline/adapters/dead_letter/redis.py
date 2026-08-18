# Responsibility: Store work no consumer could complete, so it is inspectable rather than lost.
# Boundaries: the Redis implementation of the dead-letter contract; it decides nothing about when work is dead.
from __future__ import annotations

import json
from typing import Any

from meshpipeline.adapters._shared.redis_client import sync_client

_DLQ_KEY = "simulation:dlq"
_KEEP = 1000


class RedisDeadLetterSink:
    def _client(self):
        # Not cached: this is a rare, best-effort write on a failing path, and the caller
        # closes the client so a dying process does not leave the connection behind.
        return sync_client(socket_connect_timeout=1, socket_timeout=1)

    def append(self, record: dict[str, Any]) -> None:
        r = self._client()
        try:
            r.lpush(_DLQ_KEY, json.dumps(record, default=str))
            r.ltrim(_DLQ_KEY, 0, _KEEP - 1)
        finally:
            r.close()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        r = self._client()
        try:
            raw = r.lrange(_DLQ_KEY, 0, max(0, limit - 1))
        finally:
            r.close()
        out: list[dict[str, Any]] = []
        for item in raw or []:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                continue        # a malformed record must not hide the readable ones
        return out
