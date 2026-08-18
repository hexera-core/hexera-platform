# Responsibility: Declare how concurrent model demand is admitted against a provider's capacity.
# Owns: the lease value and the controller Protocol; a caller holds a lease for the duration of its call.
# Boundaries: admission only; it selects no model and performs no inference.
# Collaborates with: adapters/model_capacity/ for the local and Redis controllers.
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class CapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:

    domain_key: str
    token: str
    waited_s: float = 0.0
    active_at_admission: int = 0


@runtime_checkable
class CapacityController(Protocol):
    async def acquire(self, domain_key: str, limit: int, deadline_s: float) -> Lease | None:
        ...

    async def release(self, lease: Lease) -> None:
        ...

    async def depth(self, domain_key: str) -> int:
        ...


_controller: CapacityController | None = None


def set_capacity_controller(controller: CapacityController | None) -> None:
    global _controller
    _controller = controller


def get_capacity_controller() -> CapacityController:
    if _controller is None:
        raise CapacityError(
            "no capacity controller configured - runtime composition must call "
            "set_capacity_controller()")
    return _controller


async def acquire(domain_key: str, limit: int, deadline_s: float) -> Lease | None:
    return await get_capacity_controller().acquire(domain_key, limit, deadline_s)


async def release(lease: Lease) -> None:
    await get_capacity_controller().release(lease)


async def depth(domain_key: str) -> int:
    return await get_capacity_controller().depth(domain_key)
