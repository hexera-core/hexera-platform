# Responsibility: Perform one provider round and normalise what came back.
# Boundaries: one call and its result shape; retries and route choice belong to the inference layer.
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from meshpipeline.agents.loop.accounting import AgentRunAccountant
from meshpipeline.agents.loop.tracing import (
    ExecutionTraceContext,
    TraceContext,
    accepts_reasoning,
    areasoning_sink,
    around_begin,
    around_end,
    reasoning_sink,
    round_begin,
    round_end,
)
from meshpipeline.contracts.agent_loop import LoopExit
from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.contracts.execution_guard import StaleWorkerFenced
from meshpipeline.contracts.model_inference import ModelRoundResult

logger = logging.getLogger(__name__)

#: Never turned into a failed round: the ownership fences end an execution on purpose.
_NEVER_SWALLOWED: tuple[type[BaseException], ...] = (StaleExecutionPublish, StaleWorkerFenced)
#: The marker of a provider call that raised instead of answering. Role-agnostic on purpose (the
#: round does not know its role) and in the router's own wrapped form; "transient" makes it a
#: retryable provider failure (errors.classify_api_failure -> PROVIDER_TRANSIENT).
ROUND_RAISED_MARKER = "<<API_FAILURE:provider_round_transient>>"


@dataclass(frozen=True)
class RoundAnswered:

    result: ModelRoundResult


@dataclass(frozen=True)
class RoundEnded:

    exit: LoopExit
    failure_marker: str = ""


#: A round either answered or ended. Two types rather than one nullable one, so "we continued"
#: carries a result that cannot be None - the loop never has to assert its way past a maybe.
ProviderOutcome = RoundAnswered | RoundEnded


@dataclass(frozen=True)
class ProviderRound:

    provider_call: Any
    tools: list[dict]
    job_id: str = ""
    user_id: str = ""
    trace: TraceContext | None = None

    def request(self, messages: list[dict], forced_tool: str | None,
                on_reasoning: Any = None) -> dict[str, Any]:
        call_kw: dict[str, Any] = {"messages": messages, "tools": self.tools,
                                   "job_id": self.job_id, "user_id": self.user_id}
        # Offered only to a provider that declares it. The sink is an optional capability, so a
        # caller that never heard of it - a non-streamed route, a test double - must keep working
        # untouched rather than fail on an unexpected argument.
        if on_reasoning is not None and accepts_reasoning(self.provider_call):
            call_kw["on_reasoning"] = on_reasoning
        if forced_tool:
            call_kw["tool_choice"] = {"type": "function",
                                      "function": {"name": forced_tool}}
        return call_kw

    # The three trace seams, so the one round sequence below serves both routes. The sink is one
    # of them: the round id it publishes against comes from _began, so a route that overrides
    # where rounds are traced has to override where their reasoning goes too.
    def _sink(self, rid: str):
        return reasoning_sink(self.trace, rid)

    async def _began(self, round_no: int) -> str:
        return round_begin(self.trace, round_no)

    async def _ended(self, rid: str, t0: float, result: Any, *,
                     phase: str = "completed") -> None:
        round_end(self.trace, rid, t0, result, phase=phase)

    async def invoke(self, messages: list[dict], *, acct: AgentRunAccountant,
                     forced_tool: str | None, remaining: float | None,
                     round_no: int) -> ProviderOutcome:
        rid = await self._began(round_no)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self.provider_call(**self.request(messages, forced_tool, self._sink(rid))),
                timeout=remaining)
        except TimeoutError:
            await self._ended(rid, t0, None, phase="failed")
            return RoundEnded(exit=LoopExit.deadline_exhausted)
        except _NEVER_SWALLOWED:
            raise
        except Exception as exc:  # noqa: BLE001 - defence in depth behind the router
            # The router answers every failure with a marker; a provider call that RAISES anyway
            # (a stand-in, a new route, a bug) is still a failed provider round, never a crashed
            # agent node - the run's own provider-failure handling takes it from here.
            logger.error("provider round %d raised instead of answering (job_id=%s): %s: %s",
                         round_no, self.job_id, type(exc).__name__, str(exc)[:300],
                         exc_info=True)
            try:
                await self._ended(rid, t0, None, phase="failed")
            except _NEVER_SWALLOWED:
                raise
            except Exception:  # noqa: BLE001 - tracing never costs the round its outcome
                pass
            return RoundEnded(exit=LoopExit.provider_failed, failure_marker=ROUND_RAISED_MARKER)
        acct.record_round(result)
        if result.failure_marker:
            # A genuine provider failure - the ONLY thing that may be reported as provider-down.
            await self._ended(rid, t0, None, phase="failed")
            return RoundEnded(exit=LoopExit.provider_failed,
                              failure_marker=result.failure_marker)
        await self._ended(rid, t0, result)
        return RoundAnswered(result=result)


# The BUILDER's route: the identical round sequence, with both reasoning events published through
# the ownership-checked contract. Reviewer and intake keep TraceContext and round_begin/round_end.
@dataclass(frozen=True)
class ExecutionProviderRound(ProviderRound):

    execution_trace: ExecutionTraceContext | None = None

    def _sink(self, rid: str):
        return areasoning_sink(self.execution_trace, rid)

    async def _began(self, round_no: int) -> str:
        return await around_begin(self.execution_trace, round_no)

    async def _ended(self, rid: str, t0: float, result: Any, *,
                     phase: str = "completed") -> None:
        await around_end(self.execution_trace, rid, t0, result, phase=phase)


def assistant_turn(result: ModelRoundResult) -> dict:
    return {
        "role": "assistant", "content": result.assistant_text,
        "tool_calls": [{"id": c.id, "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments}}
                       for c in result.tool_calls]}


__all__ = ["ExecutionProviderRound", "ProviderOutcome", "ProviderRound", "RoundAnswered",
           "RoundEnded", "assistant_turn"]
