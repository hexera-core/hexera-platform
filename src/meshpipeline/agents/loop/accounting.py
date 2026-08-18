# Responsibility: Count what an agent run actually did, in the shared vocabulary.
# Boundaries: measurement only; it never changes the run it measures.
# Collaborates with: contracts/agent_loop.py.
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.contracts.agent_loop import (
    AgentRole,
    CategoryCount,
    LoopExit,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
    RoundRecord,
    RunExtension,
    ToolCallRecord,
)
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

UNCATEGORIZED = "uncategorized"


@dataclass
class ToolInvocation:

    round_index: int
    call_index: int
    tool: str
    raw_arguments: str = ""
    parsed: dict | None = None          # None => the arguments did not parse
    result: Any = None
    error: BaseException | None = None
    started_at: float = field(default_factory=time.monotonic)

    @classmethod
    def of(cls, request: ToolCallRequest, *, round_index: int, call_index: int,
           parsed: dict | None = None) -> ToolInvocation:
        return cls(round_index=round_index, call_index=call_index, tool=request.name,
                   raw_arguments=request.arguments, parsed=parsed)

    @property
    def malformed(self) -> bool:
        return self.parsed is None and bool(self.raw_arguments.strip())


class AgentRunAccountant:

    def __init__(self, *, role: AgentRole, job_id: str, limits: LoopLimits,
                 pipeline_attempt: int = 0, agent_attempt: int = 0) -> None:
        self.role = role
        self.job_id = job_id
        self.limits = limits
        self.pipeline_attempt = pipeline_attempt
        self.agent_attempt = agent_attempt

        self._started = time.monotonic()
        self._rounds: list[RoundRecord] = []
        self._tool_calls: list[ToolCallRecord] = []
        self._by_category: dict[str, int] = {}
        self._provider_attempts = 0
        self._malformed = 0
        self._plaintext = 0
        self._progress = 0
        self._consecutive_no_progress = 0
        self._last_signature: str | None = None
        self._terminal_action_done = False

    # observation
    def record_round(self, result: ModelRoundResult) -> RoundRecord:
        self._provider_attempts += result.provider.attempts
        if not result.tool_calls and not result.failure_marker:
            self._plaintext += 1
        record = RoundRecord(
            index=len(self._rounds) + 1,
            provider_attempts=result.provider.attempts,
            finish_reason=result.finish_reason,
            had_tool_calls=bool(result.tool_calls),
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
            duration_ms=0,
            stage=self.stage())
        self._rounds.append(record)
        return record

    def record_tool_call(self, invocation: ToolInvocation, *, category: str = UNCATEGORIZED,
                         accepted: bool = True) -> ToolCallRecord:
        if invocation.malformed:
            self._malformed += 1
        self._by_category[category] = self._by_category.get(category, 0) + 1
        record = ToolCallRecord(
            round_index=invocation.round_index,
            call_index=invocation.call_index,
            tool=invocation.tool,
            category=category,
            malformed=invocation.malformed,
            accepted=accepted and not invocation.malformed and invocation.error is None,
            duration_ms=int((time.monotonic() - invocation.started_at) * 1000))
        self._tool_calls.append(record)
        return record

    def record_progress(self, observation: ProgressObservation) -> None:
        from meshpipeline.agents.loop.progress import advance
        self._progress, self._consecutive_no_progress, self._last_signature = advance(
            observation, progress_count=self._progress,
            consecutive_no_progress=self._consecutive_no_progress,
            last_signature=self._last_signature)

    def record_terminal_action(self) -> None:
        self._terminal_action_done = True

    # readings
    def tally(self) -> LoopTally:
        return LoopTally(
            rounds=len(self._rounds),
            tool_calls=len(self._tool_calls),
            calls_by_category=tuple(CategoryCount(c, n)
                                    for c, n in sorted(self._by_category.items())),
            provider_attempts=self._provider_attempts,
            malformed_calls=self._malformed,
            plaintext_turns=self._plaintext,
            progress_count=self._progress,
            consecutive_no_progress=self._consecutive_no_progress,
            elapsed_s=round(time.monotonic() - self._started, 3))

    def stage(self) -> LoopStage:
        return self.tally().stage(self.limits)

    @property
    def terminal_action_done(self) -> bool:
        return self._terminal_action_done

    def report(self, *, exit: LoopExit, extension: RunExtension,
               failure_marker: str = "") -> Any:
        from meshpipeline.contracts.agent_loop import AgentRunRecord
        return AgentRunRecord(
            role=self.role, job_id=self.job_id,
            pipeline_attempt=self.pipeline_attempt, agent_attempt=self.agent_attempt,
            limits=self.limits, tally=self.tally(), exit=exit, extension=extension,
            rounds=tuple(self._rounds), tool_calls=tuple(self._tool_calls),
            failure_marker=failure_marker)


__all__ = ["UNCATEGORIZED", "AgentRunAccountant", "ToolInvocation"]
