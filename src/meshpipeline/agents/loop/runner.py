# Responsibility: Run the one agent loop Intake, Builder and Reviewer all execute through.
# Owns: the round sequence, budget enforcement, ownership checks between rounds, and the loop result.
# Boundaries: it drives rounds.
# Collaborates with: agents/loop/driver.py, provider_round.py, tool_execution.py and contracts/agent_loop.py.
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from meshpipeline.agents.loop.accounting import AgentRunAccountant
from meshpipeline.agents.loop.budget import LoopBudget
from meshpipeline.agents.loop.driver import LoopDriver, LoopResult, ToolOutcome
from meshpipeline.agents.loop.provider_round import (
    ExecutionProviderRound,
    ProviderRound,
    RoundEnded,
    assistant_turn,
)
from meshpipeline.agents.loop.tool_execution import (
    ExecutionToolExecution,
    ToolExecution,
    being_cancelled,
)
from meshpipeline.agents.loop.tracing import ExecutionTraceContext, TraceContext
from meshpipeline.contracts.agent_loop import LoopExit, LoopStage
from meshpipeline.contracts.model_inference import ModelRoundResult


async def run_agent_loop(
    *,
    driver: LoopDriver,
    provider_call: Any,
    messages: list[dict],
    tools: list[dict],
    job_id: str = "",
    user_id: str = "",
    pipeline_attempt: int = 0,
    agent_attempt: int = 0,
    trace: TraceContext | None = None,
    # The BUILDER supplies this instead of `trace`: the same loop, with every trace event
    # published under an ownership check. Reviewer and intake supply `trace` and are unaffected.
    execution_trace: ExecutionTraceContext | None = None,
    deadline_s: float | None = None,
    append_tool_result: Any,
    on_round: Any = None,
    # The BUILDER's round hook. Separate and explicitly typed rather than awaiting `on_round`,
    # because intake's hook is synchronous and must stay that way.
    on_round_awaited: Callable[[ModelRoundResult], Awaitable[None]] | None = None,
    provider_failure_marker: Any = None,
    record_sink: Any = None,
) -> LoopResult:
    limits = driver.limits()
    acct = AgentRunAccountant(role=driver.role, job_id=job_id, limits=limits,
                              pipeline_attempt=pipeline_attempt, agent_attempt=agent_attempt)
    budget = LoopBudget.of(limits, deadline_s)
    rounds: ProviderRound
    calls: ToolExecution
    if execution_trace is not None:
        rounds = ExecutionProviderRound(provider_call=provider_call, tools=tools, job_id=job_id,
                                        user_id=user_id, execution_trace=execution_trace)
        calls = ExecutionToolExecution(driver=driver, acct=acct,
                                       append_tool_result=append_tool_result,
                                       execution_trace=execution_trace)
    else:
        rounds = ProviderRound(provider_call=provider_call, tools=tools, job_id=job_id,
                               user_id=user_id, trace=trace)
        calls = ToolExecution(driver=driver, acct=acct, append_tool_result=append_tool_result,
                              trace=trace)
    payload: Any = None

    def _finish(reason: LoopExit, failure: str = "") -> LoopResult:
        record = acct.report(exit=reason, extension=driver.extension(), failure_marker=failure)
        # WHERE the record goes is the caller's, not the loop's. Agents do not all run in the
        # same process: a worker may write the corpus directly, while an agent running in the API
        # request path must hand its record to the transport that carries it to the worker -
        # instantiating a corpus writer there would violate single-writer ownership.
        if record_sink is not None:
            record_sink(record)
        else:
            from meshpipeline.agents.loop.diagnostics import emit
            emit(record)
        return LoopResult(exit=reason, record=record, payload=payload, failure_marker=failure)

    def _exhausted(reason: LoopExit) -> LoopResult:
        return _finish(reason, _marker(provider_failure_marker, reason))

    def _nudge(note: str | None) -> None:
        if note:
            messages.append({"role": "user", "content": note})

    def _superseded() -> None:
        from meshpipeline.agents.loop.diagnostics import emit_superseded
        emit_superseded(role=driver.role, job_id=job_id, pipeline_attempt=pipeline_attempt,
                        agent_attempt=agent_attempt, tally=acct.tally())

    while budget.may_start_round(acct.tally().rounds):
        if budget.expired():                          # no NEW round starts after exhaustion
            return _exhausted(LoopExit.deadline_exhausted)
        driver.before_round(acct.tally(), messages)

        answer = await rounds.invoke(
            messages, acct=acct, forced_tool=driver.forced_tool(acct.tally()),
            remaining=budget.remaining(), round_no=acct.tally().rounds)
        if isinstance(answer, RoundEnded):
            return _finish(answer.exit, answer.failure_marker
                           or _marker(provider_failure_marker, answer.exit))
        result = answer.result
        messages.append(assistant_turn(result))
        if on_round is not None:
            on_round(result)
        if on_round_awaited is not None:
            await on_round_awaited(result)

        if not result.tool_calls:
            # Zero tool calls. What that MEANS belongs to the agent: a round that achieved
            # nothing, or a finished conversational turn.
            decision = driver.on_plaintext(acct.tally())
            if decision.complete:
                payload = decision.payload
                return _finish(decision.exit)
            _nudge(decision.message)
            continue

        round_index = acct.tally().rounds
        terminated = False
        for call in result.tool_calls:
            if budget.expired():
                return _exhausted(LoopExit.deadline_exhausted)
            outcome = await calls.run(call, messages, round_index=round_index,
                                      remaining=budget.remaining())
            if outcome.timed_out:
                return _exhausted(LoopExit.deadline_exhausted)
            if outcome.raised is not None:
                if outcome.superseded:
                    # This generation is no longer authoritative, so it writes no authoritative
                    # record - only a breadcrumb - and the exception propagates to the boundary
                    # that owns generation semantics.
                    _superseded()
                    raise outcome.raised
                # An UNEXPECTED executor failure is a real, recordable outcome: one canonical
                # record through the normal finish path, then the truthful exception propagates
                # to the caller that already knows how to classify it.
                _exhausted(LoopExit.executor_failed)
                raise outcome.raised
            if outcome.terminal:
                payload = outcome.payload
                acct.record_terminal_action()
                terminated = True
                break
        if terminated:
            return _finish(LoopExit.terminal_action)

        # CLOSE-OUT: the agent may complete its OWN terminal action when its state warrants it.
        # A TYPED hook on LoopDriver - never reflective discovery, which would silently disable
        # the whole path on a rename. The runner never decides what the action is.
        try:
            closing: ToolOutcome | None = await driver.close_out(acct.tally())
        except asyncio.CancelledError:
            # CANCELLED. The same rule as a cancelled tool call: propagate, record nothing. The
            # close-out either completed its fenced action or it did not, and a cancelled attempt
            # is not an executor failure.
            raise
        except BaseException as exc:
            if being_cancelled():
                # A cleanup failure masking the cancellation - see `tool_execution`.
                raise asyncio.CancelledError from exc
            # Close-out performs a real, fenced action, so it loses ownership the same way a tool
            # call does and is classified identically.
            if driver.is_supersession(exc):
                _superseded()
                raise
            _exhausted(LoopExit.executor_failed)
            raise
        if closing is not None and closing.terminal:
            payload = closing.payload
            acct.record_terminal_action()
            return _finish(LoopExit.terminal_action)

        # PROGRESS: the agent's own judgement of whether this round moved its work forward.
        observation = driver.observe(acct.tally())
        acct.record_progress(observation)
        tally = acct.tally()
        from meshpipeline.agents.loop.progress import stalled
        if stalled(tally.consecutive_no_progress, limits):
            # A stalled run is still TOLD why it is ending, in the agent's own words.
            _nudge(driver.correction(LoopStage.closing, tally, observation))
            return _exhausted(LoopExit.no_progress)
        _nudge(driver.correction(tally.stage(limits), tally, observation))

    return _exhausted(LoopExit.rounds_exhausted)


def _marker(resolver: Any, exit_reason: LoopExit | None) -> str:
    return "" if resolver is None or exit_reason is None else str(resolver(exit_reason) or "")


__all__ = ["run_agent_loop"]
