# Responsibility: Let a worker publish only while it is still the run's owner, checked per event.
# Owns: the ownership decision that must succeed before an execution-scoped event reaches Redis.
# Boundaries: authorization only; the event vocabulary, idempotency keys and transport stay where they are.
from __future__ import annotations

import functools

from meshpipeline.application import execution_fence as _fence
from meshpipeline.contracts.event_stream import (
    ExecutionEventPublisher,
    StaleExecutionPublish,
    expecting_fence,
    fence_fingerprint,
    publisher,
)
from meshpipeline.persistence.lease import ExecutionOwnership

__all__ = ["OwnershipCheckedPublisher", "StaleExecutionPublish", "execution_publisher"]


def _healing(method):
    """One recovery per publish for a lapsed fence MIRROR while the durable claim holds.

    The Redis gate refuses (-2) both when a takeover really happened and when the mirror
    merely expired between heartbeats - the seam cannot tell them apart on its own. On the
    first StaleExecutionPublish, re-verify the claim under the row lock (which renews the
    lease and NX-heals the mirror - it can never resurrect a revoked fence) and retry the
    whole gated method once; the retry re-runs the PostgreSQL ownership check too. A second
    refusal propagates: that IS a supersession, and it fails closed exactly as before.
    """
    @functools.wraps(method)
    async def run(self, *args, **kwargs):
        try:
            return await method(self, *args, **kwargs)
        except StaleExecutionPublish:
            own = _fence.current_ownership()
            if own is None or str(own.job_id) != str(self._inner.job_id):
                raise                     # nothing to re-verify - the refusal stands
            await _fence.reverify_and_heal_fence(own, what=method.__name__)
            return await method(self, *args, **kwargs)
    return run


class OwnershipCheckedPublisher:
    # Generation-scoped operation keys give replay idempotency; current ownership is what
    # authorizes the publication. The check is a PostgreSQL round trip, so every method is async.
    def __init__(self, inner) -> None:
        self._inner = inner

    # The PostgreSQL check below and the Redis write it authorizes are two steps; the claim can
    # move between them. This carries the exact claim just verified into the write, where Redis
    # compares it atomically with everything else the event does.
    def _fenced(self, own):
        return expecting_fence(fence_fingerprint(str(own.job_id), own.execution_generation,
                                                 own.worker_token))

    async def _authorized(self, what: str) -> ExecutionOwnership:
        own = _fence.current_ownership()
        if own is None:
            raise StaleExecutionPublish(
                f"{what}: no execution ownership is bound - refusing to publish for "
                f"job {self._inner.job_id}")
        if str(own.job_id) != str(self._inner.job_id):
            raise StaleExecutionPublish(
                f"{what}: the bound ownership is for another job - refusing to publish")
        if not await _fence.is_current_owner():
            raise StaleExecutionPublish(
                f"{what}: this execution no longer owns job {self._inner.job_id} "
                f"(generation {own.execution_generation}) - refusing to publish")
        return own

    @_healing
    async def anote(self, text: str, tone: str = "info", op_id: str = "") -> None:
        _own = await self._authorized("note")
        with self._fenced(_own):
            self._inner.note(text, tone, op_id=op_id)

    @_healing
    async def awarn(self, text: str, op_id: str = "") -> None:
        _own = await self._authorized("warn")
        with self._fenced(_own):
            self._inner.warn(text, op_id=op_id)

    @_healing
    async def aerror(self, text: str, op_id: str = "") -> None:
        _own = await self._authorized("error")
        with self._fenced(_own):
            self._inner.error(text, op_id=op_id)

    @_healing
    async def astage(self, op_id: str = "") -> None:
        _own = await self._authorized("stage")
        with self._fenced(_own):
            self._inner.stage(op_id=op_id)

    @_healing
    async def aattempt(self, n: int, of: int, op_id: str = "") -> None:
        _own = await self._authorized("attempt")
        with self._fenced(_own):
            self._inner.attempt(n, of, op_id=op_id)

    # The rest of the execution vocabulary. Every one is spelled out rather than generated, so a
    # reader sees which events a worker may publish, static typing resolves the receiver, and the
    # authority scanner can tell an execution publication from a system one without guessing.
    # `emit` is the transport primitive and `publish_terminal` belongs to the terminal authority -
    # a terminal event is published precisely when execution ownership is gone - so neither has a
    # gated counterpart here.

    @_healing
    async def acheck(self, statement: str, *, ok: bool) -> None:
        _own = await self._authorized("check")
        with self._fenced(_own):
            self._inner.check(statement, ok=ok)

    @_healing
    async def aaction(self, actions: list[str]) -> None:
        _own = await self._authorized("action")
        with self._fenced(_own):
            self._inner.action(actions)

    @_healing
    async def asearch(self, query: str) -> None:
        _own = await self._authorized("search")
        with self._fenced(_own):
            self._inner.search(query)

    @_healing
    async def ascreenshot(self, image_b64: str, op_id: str = "") -> None:
        _own = await self._authorized("screenshot")
        with self._fenced(_own):
            self._inner.screenshot(image_b64, op_id=op_id)

    @_healing
    async def afile(self, display_path: str, byte_count: int,
                    operation: str = "created", op_id: str = "") -> None:
        _own = await self._authorized("file")
        with self._fenced(_own):
            self._inner.file(display_path, byte_count, operation, op_id=op_id)

    @_healing
    async def areasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                         token_count: int | None = None, content: str | None = None,
                         status: str = "active") -> None:
        _own = await self._authorized("reasoning")
        with self._fenced(_own):
            self._inner.reasoning(rid, phase, duration_ms=duration_ms, token_count=token_count,
                                  content=content, status=status)

    @_healing
    async def arationale(self, conclusion: str, because: str = "") -> None:
        _own = await self._authorized("rationale")
        with self._fenced(_own):
            self._inner.rationale(conclusion, because)

    @_healing
    async def atool_call(self, cid: str, tool_name: str, arguments: object = None,
                         status: str = "started", op_id: str = "") -> None:
        _own = await self._authorized("tool_call")
        with self._fenced(_own):
            self._inner.tool_call(cid, tool_name, arguments, status, op_id=op_id)

    @_healing
    async def atool_result(self, rid: str, call_id: str, tool_name: str, result: object = None,
                           status: str = "success", duration_ms: int | None = None,
                           op_id: str = "") -> None:
        _own = await self._authorized("tool_result")
        with self._fenced(_own):
            self._inner.tool_result(rid, call_id, tool_name, result, status, duration_ms,
                                    op_id=op_id)

    @_healing
    async def ameshing(self, engine: str, budget_s: int, history: dict | None = None,
                       op_id: str = "") -> None:
        _own = await self._authorized("meshing")
        with self._fenced(_own):
            self._inner.meshing(engine, budget_s, history, op_id=op_id)

    @_healing
    async def ameshed(self, cells: int | None = None, op_id: str = "") -> None:
        _own = await self._authorized("meshed")
        with self._fenced(_own):
            self._inner.meshed(cells, op_id=op_id)

    @_healing
    async def averdict(self, verdict: str, summary: str = "") -> None:
        _own = await self._authorized("verdict")
        with self._fenced(_own):
            self._inner.verdict(verdict, summary)

    @_healing
    async def aclosing(self, text: str, event_id: str = "") -> None:
        _own = await self._authorized("closing")
        with self._fenced(_own):
            self._inner.closing(text, event_id)


def execution_publisher(job_id: str, agent: str | None = None) -> ExecutionEventPublisher:
    # The same configured adapter, wrapped so no synchronous emitting method is reachable: worker
    # code cannot fall back to the unowned contract by losing a context variable.
    return OwnershipCheckedPublisher(publisher(job_id, agent))
