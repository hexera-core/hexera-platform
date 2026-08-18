# Responsibility: Perform one provider round and normalise what came back.
# Boundaries: one call and its result shape; retries and route choice belong to the inference layer.
from __future__ import annotations

import asyncio
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
from meshpipeline.contracts.model_inference import ModelRoundResult


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
