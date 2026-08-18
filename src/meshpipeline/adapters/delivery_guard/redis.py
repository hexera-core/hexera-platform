# Responsibility: Record delivery attempts in Redis so a repeatedly redelivered job can be recognised.
# Boundaries: counting only; the redelivery limit and what to do at it belong to the caller.
from __future__ import annotations

from typing import cast

from meshpipeline.adapters._shared.redis_client import sync_client

_TTL_SECONDS = 86400  # 24h - long enough to outlive any redelivery storm, short enough to self-clean


class RedisDeliveryGuard:
    def __init__(self) -> None:
        self._redis = None

    def _client(self):
        if self._redis is None:
            self._redis = sync_client()
        return self._redis

    def record_attempt(self, job_id: str) -> int:
        r = self._client()
        key = f"job:{job_id}:deliveries"
        n = int(cast(int, r.incr(key)))   # sync redis returns the new int value
        if n == 1:
            r.expire(key, _TTL_SECONDS)
        return n
