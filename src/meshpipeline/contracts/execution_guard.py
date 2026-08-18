# Responsibility: Declare the fence that stops long-running work once it no longer owns the run.
# Owns: the stale-worker exception and the owner-check seam every side effect is expected to pass first.
# Boundaries: it defines the check.
# Collaborates with: application/execution_fence.py and persistence/lease.py.
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar

#: Installed by application/execution_fence.execution_ownership for the life of a fenced run.
_CHECKER: ContextVar[Callable[[], Awaitable[bool]] | None] = ContextVar(
    "execution_owner_checker", default=None)


class StaleWorkerFenced(RuntimeError):

    def __init__(self, where: str, job_id: str = "", execution_generation: int = 0,
                 token_hash: str = "") -> None:
        # The token HASH is operator observability - it identifies which worker lost without ever
        # exposing the raw token, which never leaves the process that minted it. Passed in as a
        # string rather than derived here: contracts/ does not import the lease.
        detail = (f"stale worker fenced at {where}: job {job_id} is no longer owned by "
                  f"generation {execution_generation}")
        super().__init__(f"{detail} (token {token_hash})" if token_hash else detail)
        self.where = where
        self.job_id = str(job_id)
        self.execution_generation = execution_generation
        self.token_hash = token_hash


@contextmanager
def owner_checker(check: Callable[[], Awaitable[bool]] | None):
    token = _CHECKER.set(check)
    try:
        yield
    finally:
        _CHECKER.reset(token)


async def still_owner() -> bool:
    check = _CHECKER.get()
    return True if check is None else await check()


async def assert_still_owner(where: str) -> None:
    if not await still_owner():
        raise StaleWorkerFenced(where)


__all__ = ["StaleWorkerFenced", "assert_still_owner", "owner_checker", "still_owner"]
