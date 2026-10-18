# Responsibility: Record delivery attempts in Redis so a repeatedly redelivered job can be recognised.
# Boundaries: counting only; the redelivery limit and what to do at it belong to the caller.
from __future__ import annotations

from typing import cast

from meshpipeline.adapters._shared.redis_client import sync_client
from meshpipeline.redis_keys import k

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
        key = k(f"job:{job_id}:deliveries")
        n = int(cast(int, r.incr(key)))   # sync redis returns the new int value
        if n == 1:
            r.expire(key, _TTL_SECONDS)
        return n

    def forgive_attempt(self, job_id: str) -> None:
        # Never below zero, and never creating a key that is not there: a count that already
        # expired has nothing to take back. One atomic script, so a delivery counted by another
        # worker at the same moment is not lost between a read and a write.
        self._client().eval(_FORGIVE_LUA, 1, k(f"job:{job_id}:deliveries"))


_FORGIVE_LUA = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""
