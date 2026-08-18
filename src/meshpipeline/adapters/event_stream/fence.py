# Responsibility: Mirror the active PostgreSQL claim into Redis so Redis can refuse a stale write itself.
# Owns: the operations that install, refresh and revoke that mirror.
# Boundaries: PostgreSQL remains the durable ownership authority; this is a fail-closed mirror of it.
from __future__ import annotations

import logging

from meshpipeline.adapters._shared.redis_client import sync_client
from meshpipeline.contracts.event_stream import fence_fingerprint
from meshpipeline.events.channels import fence_key_for

logger = logging.getLogger(__name__)

#: Refresh only what is still ours. Never SET: a missing key means the claim was revoked or has
#: expired, and a delayed heartbeat from a superseded worker must not bring it back.
_REFRESH_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
  return 1
end
return 0
"""

#: Delete only what is still ours, so a late release cannot remove the next owner's fence.
_REVOKE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

#: re-exported so the claim authority reaches identity and transport through one seam
fingerprint = fence_fingerprint


def install(job_id: str, value: str, ttl_seconds: int) -> bool:
    # The TTL is the remaining PostgreSQL lease, so the mirror can only ever expire EARLIER
    # than the claim it stands for.
    return bool(sync_client().set(fence_key_for(job_id), value, ex=max(1, int(ttl_seconds))))


def refresh(job_id: str, value: str, ttl_seconds: int) -> bool:
    return bool(sync_client().eval(_REFRESH_LUA, 1, fence_key_for(job_id), value,
                                   str(max(1, int(ttl_seconds)))))


def revoke(job_id: str, value: str) -> bool:
    return bool(sync_client().eval(_REVOKE_LUA, 1, fence_key_for(job_id), value))


def current(job_id: str) -> str:
    raw = sync_client().get(fence_key_for(job_id))
    if raw is None:
        return ""
    return raw.decode() if isinstance(raw, bytes) else str(raw)


__all__ = ["current", "fingerprint", "install", "refresh", "revoke"]
