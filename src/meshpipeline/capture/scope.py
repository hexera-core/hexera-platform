# Responsibility: Say who and what a capture record belongs to, for the duration of one run.
# Boundaries: scope binding including the execution generation, so a superseded worker's records stay attributable.
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureScope:
    owner_id: str
    job_id: str


_SCOPE: ContextVar[CaptureScope | None] = ContextVar("capture_scope", default=None)


def current_scope() -> CaptureScope | None:
    return _SCOPE.get()


def current_generation() -> int:
    try:
        from meshpipeline.application.execution_fence import current_ownership
        own = current_ownership()
        return int(getattr(own, "execution_generation", 0) or 0)
    except Exception:  # noqa: BLE001 - scoping must never break the run
        return 0


@contextlib.contextmanager
def capture_scope(owner_id: str, job_id: str):
    token = _SCOPE.set(CaptureScope(owner_id=owner_id, job_id=job_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def bind(owner_id: str, job_id: str) -> None:
    _SCOPE.set(CaptureScope(owner_id=owner_id, job_id=job_id))
