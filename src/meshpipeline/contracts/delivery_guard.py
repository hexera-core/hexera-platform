# Responsibility: Declare the guard that records and rate-limits repeated delivery attempts.
# Boundaries: a Protocol and its process-wide binding only; the implementation lives in adapters/delivery_guard/.
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class DeliveryGuardError(RuntimeError):
    pass


@runtime_checkable
class DeliveryGuard(Protocol):
    def record_attempt(self, job_id: str) -> int:
        ...

    def forgive_attempt(self, job_id: str) -> None:
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


def forgive_attempt(job_id: str) -> None:
    """Take back the delivery the current run counted, because it ended by HANDING THE JOB BACK.

    The count exists to catch a job that keeps killing its worker (the redelivery cap in
    application/execution_fence.guard_redelivery). A job handed back because its machine was
    being shut down killed nothing: without this, a run moved three times by ordinary scale-in
    would be failed on its fourth start as a poison job, with a sentence about resources that is
    true of nothing. Best effort by design - a guard that cannot answer leaves the count as it
    was, which is the cap's old, stricter behaviour."""
    forgive = getattr(_guard, "forgive_attempt", None)
    if forgive is None:
        return
    try:
        forgive(job_id)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("could not forgive the delivery of job %s (%s) - it still counts toward "
                       "the redelivery cap", job_id, type(exc).__name__)
