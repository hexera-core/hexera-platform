# Responsibility: Parse a tool call, run it, and report the outcome the loop needs.
# Owns: argument parsing, the call outcome, and cancellation detection.
# Boundaries: malformed arguments are returned to the model as a result, because it can correct a call but not a crash.
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from meshpipeline.agents.loop import tracing
from meshpipeline.agents.loop.accounting import AgentRunAccountant, ToolInvocation
from meshpipeline.agents.loop.driver import LoopDriver
from meshpipeline.agents.loop.tracing import ExecutionTraceContext, TraceContext
from meshpipeline.contracts.model_inference import ToolCallRequest

logger = logging.getLogger(__name__)


def being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def parse_arguments(raw: str) -> dict | None:
    import json
    if not (raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class CallOutcome:

    payload: Any = None
    terminal: bool = False
    timed_out: bool = False
    raised: BaseException | None = None
    superseded: bool = False


@dataclass(frozen=True)
class ToolExecution:

    driver: LoopDriver
    acct: AgentRunAccountant
    append_tool_result: Any
    trace: TraceContext | None = None

    # The three trace seams, so the one copy of the classification below serves both routes.
    # Awaiting a coroutine that never suspends does not reschedule, so the synchronous route
    # publishes in exactly the order and at exactly the points it always did.
    def _cid(self, round_index: int, call_index: int, provider_id: str) -> str:
        return tracing.call_id(self.trace, round_index, call_index, provider_id)

    async def _began(self, cid: str, name: str, parsed: Any, *, status: str) -> None:
        tracing.tool_call(self.trace, cid, name, parsed, status=status)

    async def _ended(self, cid: str, name: str, result: Any, status: str, t0: float) -> None:
        tracing.tool_result(self.trace, cid, name, result, status, t0)

    async def run(self, call: ToolCallRequest, messages: list[dict], *,
                  round_index: int, remaining: float | None) -> CallOutcome:
        parsed = parse_arguments(call.arguments)
        call_index = self.acct.tally().tool_calls + 1
        inv = ToolInvocation.of(call, round_index=round_index, call_index=call_index,
                                parsed=parsed)
        cid = self._cid(round_index, call_index, call.id)
        malformed = parsed is None
        await self._began(cid, call.name, parsed,
                          status="blocked" if malformed else "started")
        t0 = time.monotonic()
        try:
            outcome = await asyncio.wait_for(self.driver.execute(inv), timeout=remaining)
        except asyncio.CancelledError:
            # CANCELLED. Never swallowed, never classified, never counted. The run is being torn
            # down, so this call did not happen - and a call that did not happen must not be
            # recorded as one that ran and failed. Nothing is published either, for the same
            # reason supersession publishes nothing: neither may tell a reader that its work
            # landed. There is no `cancelled` status in the trace vocabulary and this does not
            # invent one; overloading `failure` for observability is exactly the untruthful
            # record being removed.
            raise
        except TimeoutError:
            self._count(inv, call, accepted=False)
            await self._ended(cid, call.name, None, "failure", t0)
            return CallOutcome(timed_out=True)
        except BaseException as exc:
            if being_cancelled():
                # A cleanup failure surfaced while this task was being torn down, MASKING the
                # CancelledError that caused it - `finally: raise` replaces the exception in
                # flight. The run is still being cancelled, so cancellation is still the real
                # reason this call ended, and recording the cleanup error as an executor failure
                # would be the same false account by a different route. The cleanup error is a
                # diagnostic: logged, chained onto the re-raise, never an outcome.
                logger.warning("agent loop: %s raised %s while the run was being cancelled - "
                               "reporting cancellation, not a tool failure",
                               call.name, type(exc).__name__)
                raise asyncio.CancelledError from exc
            if self.driver.is_supersession(exc):
                # SUPERSEDED. This generation is no longer authoritative. It publishes NO tool
                # result: a superseded generation must not tell a reader that its work landed.
                return CallOutcome(raised=exc, superseded=True)
            self._count(inv, call, accepted=False)
            # a truthful failed result - the message only, never a stack trace
            await self._ended(cid, call.name, None, "failure", t0)
            return CallOutcome(raised=exc)
        self._count(inv, call, accepted=outcome.accepted)
        # A refusal of MALFORMED arguments is `blocked` - the tool never ran. A refusal of
        # well-formed arguments is `failure` - it ran and said no.
        await self._ended(cid, call.name, outcome.content,
                          ("blocked" if malformed else
                           "success" if outcome.accepted else "failure"), t0)
        self.append_tool_result(messages, call.id, outcome.content)
        return CallOutcome(payload=outcome.payload, terminal=outcome.terminal)

    def _count(self, inv: ToolInvocation, call: ToolCallRequest, *, accepted: bool) -> None:
        self.acct.record_tool_call(inv, category=self.driver.category_of(call.name),
                                   accepted=accepted)


# The BUILDER's route. It runs the identical call mechanics above - one copy, so a classification
# rule can never differ between agents - and differs in exactly one respect: every trace event is
# published through the ownership-checked contract. A worker that has lost its claim learns so
# here, because StaleExecutionPublish is an EventStreamError and the execution helpers re-raise
# it; reviewer and intake keep TraceContext and the synchronous helpers.
@dataclass(frozen=True)
class ExecutionToolExecution(ToolExecution):

    execution_trace: ExecutionTraceContext | None = None

    def _cid(self, round_index: int, call_index: int, provider_id: str) -> str:
        return tracing.execution_call_id(self.execution_trace, round_index, call_index,
                                         provider_id)

    async def _began(self, cid: str, name: str, parsed: Any, *, status: str) -> None:
        await tracing.atool_call(self.execution_trace, cid, name, parsed, status=status)

    async def _ended(self, cid: str, name: str, result: Any, status: str, t0: float) -> None:
        await tracing.atool_result(self.execution_trace, cid, name, result, status, t0)


__all__ = ["CallOutcome", "ExecutionToolExecution", "ToolExecution", "being_cancelled",
           "parse_arguments"]
