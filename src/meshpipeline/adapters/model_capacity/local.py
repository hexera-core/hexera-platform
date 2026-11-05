# Responsibility: Admit concurrent model calls against a capacity limit, within one process.
# Boundaries: the in-process controller: correct for one worker and for tests, never shared across processes.
# Collaborates with: adapters/model_capacity/redis.py, which is the cross-process implementation.
from __future__ import annotations

import asyncio
import time
import uuid

from meshpipeline.contracts.model_capacity import Lease


class LocalCapacityController:
    def __init__(self, *, lease_ttl_s: float = 1900.0, poll_interval_s: float = 0.01) -> None:
        self._lease_ttl_s = float(lease_ttl_s)
        self._poll_interval_s = float(poll_interval_s)
        # domain_key -> {token: expiry_monotonic}
        self._active: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()

    def _reap(self, domain_key: str) -> dict[str, float]:
        now = time.monotonic()
        live = {t: e for t, e in self._active.get(domain_key, {}).items() if e > now}
        self._active[domain_key] = live
        return live

    async def acquire(self, domain_key: str, limit: int, deadline_s: float) -> Lease | None:
        started = time.monotonic()
        token = uuid.uuid4().hex
        while True:
            async with self._lock:
                live = self._reap(domain_key)
                active = len(live)
                if active < int(limit):
                    live[token] = time.monotonic() + self._lease_ttl_s
                    return Lease(domain_key=domain_key, token=token,
                                 waited_s=time.monotonic() - started,
                                 active_at_admission=active)
            waited = time.monotonic() - started
            if waited >= deadline_s:
                return None
            await asyncio.sleep(min(self._poll_interval_s, max(0.0, deadline_s - waited)))

    async def release(self, lease: Lease) -> None:
        async with self._lock:
            self._active.get(lease.domain_key, {}).pop(lease.token, None)

    async def depth(self, domain_key: str) -> int:
        async with self._lock:
            return len(self._reap(domain_key))
