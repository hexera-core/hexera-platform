# Responsibility: Declare how the product publishes and subscribes to typed job events.
# Owns: the publisher and subscription Protocols and the factories runtime composition binds.
# Boundaries: it defines no event payloads (events/ owns those) and speaks no transport.
# Collaborates with: events/ for the vocabulary and api/v1/ws.py for delivery to browsers.
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable


class EventStreamError(RuntimeError):
    pass


@runtime_checkable
class EventPublisher(Protocol):

    def emit(self, event: Any) -> None: ...
    def stage(self, op_id: str = "") -> None: ...
    def attempt(self, n: int, of: int, op_id: str = "") -> None: ...
    def note(self, text: str, tone: str = "info", op_id: str = "") -> None: ...
    def warn(self, text: str, op_id: str = "") -> None: ...
    def error(self, text: str, op_id: str = "") -> None: ...
    def check(self, statement: str, *, ok: bool) -> None: ...
    def action(self, actions: list[str]) -> None: ...
    def search(self, query: str) -> None: ...
    def screenshot(self, image_b64: str, op_id: str = "") -> None: ...
    def file(self, display_path: str, byte_count: int,
             operation: str = "created", op_id: str = "") -> None: ...
    def reasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                  token_count: int | None = None, content: str | None = None,
                  status: str = "active") -> None: ...
    def rationale(self, conclusion: str, because: str = "") -> None: ...
    def tool_call(self, cid: str, tool_name: str, arguments: object = None,
                  status: str = "started", op_id: str = "") -> None: ...
    def tool_result(self, rid: str, call_id: str, tool_name: str, result: object = None,
                    status: str = "success", duration_ms: int | None = None,
                    op_id: str = "") -> None: ...
    def meshing(self, engine: str, budget_s: int, history: dict | None = None,
                op_id: str = "") -> None: ...
    def meshed(self, cells: int | None = None, op_id: str = "") -> None: ...
    def verdict(self, verdict: str, summary: str = "") -> None: ...
    def closing(self, text: str, event_id: str = "") -> None: ...

    def publish_terminal(self, text: str, event_id: str = "") -> None:
        ...


@runtime_checkable
class EventStreamSubscription(Protocol):

    async def open(self) -> None:
        ...

    async def backlog(self) -> list[str]:
        ...

    async def next_event(self, timeout: float) -> str | None:
        ...

    async def close(self) -> None:
        ...


# THE fingerprint of an exact claim. The raw worker token is high-entropy and stays in
# PostgreSQL; what a mirror may hold is this fixed-length digest, which proves the claim
# without carrying the credential that grants it.
def fence_fingerprint(job_id: str, generation: int, worker_token: object) -> str:
    import hashlib
    return hashlib.sha256(
        f"{job_id}:{int(generation)}:{worker_token}".encode()).hexdigest()[:32]


#: What Redis must find in the fence key for this publication to be allowed. Context-local and
#: empty by default: only the ownership gate sets it, and only around its own delegation, so
#: intake, terminal and maintenance publication are unaffected.
_EXPECTED_FENCE: ContextVar[str] = ContextVar("expected_execution_fence", default="")


def expected_fence() -> str:
    return _EXPECTED_FENCE.get()


@contextmanager
def expecting_fence(value: str):
    token = _EXPECTED_FENCE.set(value)
    try:
        yield
    finally:
        _EXPECTED_FENCE.reset(token)


class StaleExecutionPublish(EventStreamError):
    # Raised when a worker tries to publish an execution-scoped event it no longer owns. It lives
    # with the contract because every layer that publishes must be able to let it through: it is
    # the one publication failure that is never an observability blip.
    pass


@runtime_checkable
class ExecutionEventPublisher(Protocol):
    # Execution-scoped publication. Every method is async because it must verify current
    # PostgreSQL ownership before it may touch Redis, and that check cannot be made from a
    # synchronous caller without bridging event loops.
    async def anote(self, text: str, tone: str = "info", op_id: str = "") -> None: ...
    async def awarn(self, text: str, op_id: str = "") -> None: ...
    async def aerror(self, text: str, op_id: str = "") -> None: ...
    async def astage(self, op_id: str = "") -> None: ...
    async def aattempt(self, n: int, of: int, op_id: str = "") -> None: ...
    async def acheck(self, statement: str, *, ok: bool) -> None: ...
    async def aaction(self, actions: list[str]) -> None: ...
    async def asearch(self, query: str) -> None: ...
    async def ascreenshot(self, image_b64: str, op_id: str = "") -> None: ...
    async def afile(self, display_path: str, byte_count: int,
                    operation: str = "created", op_id: str = "") -> None: ...
    async def areasoning(self, rid: str, phase: str, *, duration_ms: int | None = None,
                         token_count: int | None = None, content: str | None = None,
                         status: str = "active") -> None: ...
    async def arationale(self, conclusion: str, because: str = "") -> None: ...
    async def atool_call(self, cid: str, tool_name: str, arguments: object = None,
                         status: str = "started", op_id: str = "") -> None: ...
    async def atool_result(self, rid: str, call_id: str, tool_name: str, result: object = None,
                           status: str = "success", duration_ms: int | None = None,
                           op_id: str = "") -> None: ...
    async def ameshing(self, engine: str, budget_s: int,
                       history: dict | None = None, op_id: str = "") -> None: ...
    async def ameshed(self, cells: int | None = None, op_id: str = "") -> None: ...
    async def averdict(self, verdict: str, summary: str = "") -> None: ...
    async def aclosing(self, text: str, event_id: str = "") -> None: ...


def require_execution_publisher(candidate: Any, *, awaits: tuple[str, ...],
                                where: str = "publish") -> ExecutionEventPublisher:
    # Prove an object really is THIS port before a collaborator stores it. Held here, beside the
    # protocol, so there is one answer to "is this a live execution publisher?" - a caller that
    # invents its own probe drifts from the port the moment the port changes, which is exactly
    # how a synchronous `hasattr(publish, "warn")` outlived the move to the awaited spellings.
    #
    # `runtime_checkable` proves the NAMES are present; it cannot prove they are awaitable, and a
    # publisher that is not awaitable would reach Redis without the ownership check the awaited
    # form exists to make. So the capabilities this collaborator actually awaits are checked for
    # being coroutine functions too.
    from collections.abc import Mapping
    from inspect import iscoroutinefunction

    if candidate is None or isinstance(candidate, Mapping):
        raise TypeError(
            f"{where} must be the live execution publisher, not copied state: a snapshot of a "
            "stream is not a stream, and nothing downstream could publish through it")
    # The EXACT capabilities this collaborator awaits - not the whole port, so a narrow
    # publication surface stays a narrow requirement. A synchronous spelling fails here too:
    # present but not a coroutine function is the shape that would reach Redis without the
    # ownership check the awaited form exists to make.
    unusable = sorted(name for name in awaits
                      if not iscoroutinefunction(getattr(candidate, name, None)))
    if unusable:
        raise TypeError(
            f"{where} must publish through ExecutionEventPublisher, the execution-scoped port, "
            f"and {type(candidate).__name__} cannot await {', '.join(unusable)}. Execution events "
            "are authorized against the current PostgreSQL claim before they touch Redis, so "
            "this port is asynchronous throughout: a synchronous publisher belongs to intake, "
            "terminal or maintenance publication, never here")
    return candidate


_factory = None
_subscription_factory = None


def set_publisher_factory(factory) -> None:
    global _factory
    _factory = factory


def publisher(job_id: str, agent: str | None = None) -> EventPublisher:
    if _factory is None:
        raise EventStreamError(
            "no event-publisher factory configured - runtime composition must call set_publisher_factory()")
    return _factory(job_id, agent=agent)


def set_subscription_factory(factory) -> None:
    global _subscription_factory
    _subscription_factory = factory


def subscription(job_id: str) -> EventStreamSubscription:
    if _subscription_factory is None:
        raise EventStreamError(
            "no event-subscription factory configured - runtime composition must call "
            "set_subscription_factory()")
    return _subscription_factory(job_id)
