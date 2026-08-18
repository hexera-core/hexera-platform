# Responsibility: Admit concurrent model calls against a capacity limit shared by every process.
# Boundaries: admission and lease expiry; it selects no model and performs no call.
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from meshpipeline.adapters._shared.redis_client import async_client
from meshpipeline.contracts.model_capacity import Lease

logger = logging.getLogger(__name__)

_KEY_PREFIX = "micap"

# ARGV: now, limit, expiry_score, token. KEYS[1]: the domain set.
# Returns {admitted (0|1), active_count_after}.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local active = redis.call('ZCARD', KEYS[1])
if active < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  redis.call('EXPIRE', KEYS[1], ARGV[5])
  return {1, active}
end
return {0, active}
"""

_DEPTH_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
return redis.call('ZCARD', KEYS[1])
"""


def _key(domain_key: str) -> str:
    return f"{_KEY_PREFIX}:{domain_key}"


class RedisCapacityController:

    def __init__(self, *, lease_ttl_s: float = 1900.0, poll_interval_s: float = 0.25) -> None:
        self._redis = None
        self._lease_ttl_s = float(lease_ttl_s)
        self._poll_interval_s = float(poll_interval_s)

    def _client(self):
        if self._redis is None:
            # Short socket timeouts: admission sits in front of every model call, so a sick
            # Redis must surface as a fast, classifiable failure rather than a hang that turns
            # into the very saturation this is meant to prevent.
            self._redis = async_client(socket_connect_timeout=2, socket_timeout=2)
        return self._redis

    async def acquire(self, domain_key: str, limit: int, deadline_s: float) -> Lease | None:
        started = time.monotonic()
        token = uuid.uuid4().hex
        key = _key(domain_key)
        while True:
            now = time.time()
            try:
                admitted, active = await self._client().eval(
                    _ACQUIRE_LUA, 1, key,
                    str(now), str(int(limit)), str(now + self._lease_ttl_s), token,
                    str(int(self._lease_ttl_s * 2)),
                )
            except Exception as exc:  # noqa: BLE001
                # Fail CLOSED - see the module docstring.
                logger.error(
                    "capacity: admission store unreachable for domain=%s - refusing admission "
                    "(failing closed): %s", domain_key, exc)
                return None
            waited = time.monotonic() - started
            if int(admitted) == 1:
                if waited > 0:
                    logger.info(
                        "capacity: admitted domain=%s after %.2fs queued (active_before=%s "
                        "limit=%s)", domain_key, waited, active, limit)
                return Lease(domain_key=domain_key, token=token, waited_s=waited,
                             active_at_admission=int(active))
            if waited >= deadline_s:
                logger.warning(
                    "capacity: queue deadline %.1fs exceeded for domain=%s (active=%s limit=%s)",
                    deadline_s, domain_key, active, limit)
                return None
            await asyncio.sleep(min(self._poll_interval_s, max(0.0, deadline_s - waited)))

    async def release(self, lease: Lease) -> None:
        try:
            await self._client().zrem(_key(lease.domain_key), lease.token)
        except Exception as exc:  # noqa: BLE001
            # Not fatal: the lease expires on its own. Losing a release costs one slot for the
            # TTL, never correctness.
            logger.warning("capacity: could not release lease on domain=%s: %s",
                           lease.domain_key, exc)

    async def depth(self, domain_key: str) -> int:
        try:
            return int(await self._client().eval(_DEPTH_LUA, 1, _key(domain_key), str(time.time())))
        except Exception as exc:  # noqa: BLE001
            logger.warning("capacity: could not read depth for domain=%s: %s", domain_key, exc)
            return -1
