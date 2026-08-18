# Responsibility: Declare where mesh-duration samples are recorded and read back.
# Boundaries: a Protocol and its binding; it neither times anything nor decides what a sample means.
from __future__ import annotations

from typing import Protocol, runtime_checkable


class MeshTimingError(RuntimeError):
    pass


@runtime_checkable
class MeshTimingStore(Protocol):
    def append_sample(self, engine: str, purpose: str, seconds: float, cap: int) -> None:
        ...

    def read_samples(self, engine: str, purpose: str) -> list[float]:
        ...


_store: MeshTimingStore | None = None


def set_mesh_timing_store(store: MeshTimingStore | None) -> None:
    global _store
    _store = store


def append_sample(engine: str, purpose: str, seconds: float, cap: int) -> None:
    if _store is None:
        raise MeshTimingError(
            "no mesh-timing store configured - runtime composition must call set_mesh_timing_store()")
    _store.append_sample(engine, purpose, seconds, cap)


def read_samples(engine: str, purpose: str) -> list[float]:
    if _store is None:
        raise MeshTimingError(
            "no mesh-timing store configured - runtime composition must call set_mesh_timing_store()")
    return _store.read_samples(engine, purpose)
