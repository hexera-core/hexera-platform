# Responsibility: Declare the guard that records and rate-limits repeated delivery attempts.
# Boundaries: a Protocol and its process-wide binding only; the implementation lives in adapters/delivery_guard/.
from __future__ import annotations

from typing import Protocol, runtime_checkable


class DeliveryGuardError(RuntimeError):
    pass


@runtime_checkable
class DeliveryGuard(Protocol):
    def record_attempt(self, job_id: str) -> int:
        ...


_guard: DeliveryGuard | None = None


def set_delivery_guard(guard: DeliveryGuard | None) -> None:
    global _guard
    _guard = guard


def record_attempt(job_id: str) -> int:
    if _guard is None:
        raise DeliveryGuardError(
            "no delivery guard configured - runtime composition must call set_delivery_guard()")
    return _guard.record_attempt(job_id)
