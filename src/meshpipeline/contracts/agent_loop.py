# Responsibility: Define the neutral accountability vocabulary Intake, Builder and Reviewer all report in.
# Owns: the role/stage/exit enums, the per-round and per-tool-call records, and the tally an agent run is judged by.
# Boundaries: pure types and counting.
# Collaborates with: agents/loop/ (which produces these records) and capture/ (which exports them).
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

# The ONLY value types a sanitized diagnostic field may take. Scalars and tuples of scalars carry
# counts, identifiers, enum values and measurements; they cannot carry a prompt, a reasoning trace,
# a raw argument blob, geometry, a credential or a signed URL. An int tuple is admitted on the same
# reasoning as a str tuple - a series of measurements (the prompt-token count each context recovery
# fired at, say) is a count per entry and can hold nothing else.
DiagnosticValue = str | int | float | bool | tuple[str, ...] | tuple[int, ...]


class AgentRole(str, Enum):

    intake = "intake"
    builder = "builder"
    reviewer = "reviewer"


class LoopStage(str, Enum):

    running = "running"
    warned = "warned"        # remaining budget crossed the agent's warning threshold
    closing = "closing"      # reserved closure phase: finish, do not start new exploration


class LoopExit(str, Enum):

    terminal_action = "terminal_action"            # the agent reached its own terminal action
    rounds_exhausted = "rounds_exhausted"
    deadline_exhausted = "deadline_exhausted"
    no_progress = "no_progress"
    provider_failed = "provider_failed"            # the ROUTER exhausted its route
    policy_abort = "policy_abort"                  # the agent's own policy stopped the run
    executor_failed = "executor_failed"            # the agent's own tool or driver raised
    # The agent finished its CONVERSATIONAL turn - it answered, and there is nothing more
    # to do until the user speaks again. Not a terminal action (nothing was submitted),
    # not exhaustion, not a failure. Intake ends this way on an ordinary reply; an agent
    # whose plain text means "keep going" never reports it.
    turn_complete = "turn_complete"
    # An engine-owned deterministic Builder driver iterates over a bounded number of BUILD
    # ATTEMPTS, not provider rounds, so `rounds_exhausted` would misdescribe it. This is the
    # only value the deterministic strategy needed: success is a genuine `terminal_action`
    # (it reaches the Builder's terminal contract), and its own refusal to proceed is a
    # genuine `policy_abort`.
    attempts_exhausted = "attempts_exhausted"

    # NOTE: there is deliberately no `superseded` member. A superseded generation produces NO
    # authoritative record at all - see agents/loop/diagnostics.emit_superseded, which writes a
    # separate non-authoritative breadcrumb that can never be read as pipeline state.


@dataclass(frozen=True)
class CategoryCount:

    category: str
    count: int


def count_for(counts: tuple[CategoryCount, ...], category: str) -> int | None:
    for c in counts:
        if c.category == category:
            return c.count
    return None


@dataclass(frozen=True)
class LoopLimits:

    max_rounds: int | None = None
    total_timeout_s: float | None = None
    warn_at_remaining_rounds: int | None = None
    closing_at_remaining_rounds: int | None = None
    no_progress_threshold: int | None = None


@dataclass(frozen=True)
class LoopTally:

    rounds: int = 0
    tool_calls: int = 0
    calls_by_category: tuple[CategoryCount, ...] = ()
    provider_attempts: int = 0
    malformed_calls: int = 0
    plaintext_turns: int = 0
    progress_count: int = 0
    consecutive_no_progress: int = 0
    elapsed_s: float = 0.0

    def remaining_rounds(self, limits: LoopLimits) -> int | None:
        return None if limits.max_rounds is None else max(0, limits.max_rounds - self.rounds)

    def remaining_s(self, limits: LoopLimits) -> float | None:
        if limits.total_timeout_s is None:
            return None
        return max(0.0, limits.total_timeout_s - self.elapsed_s)

    def stage(self, limits: LoopLimits) -> LoopStage:
        left = self.remaining_rounds(limits)
        if left is None:
            return LoopStage.running
        if limits.closing_at_remaining_rounds is not None \
                and left <= limits.closing_at_remaining_rounds:
            return LoopStage.closing
        if limits.warn_at_remaining_rounds is not None \
                and left <= limits.warn_at_remaining_rounds:
            return LoopStage.warned
        return LoopStage.running


@dataclass(frozen=True)
class RoundRecord:

    index: int
    provider_attempts: int = 0
    finish_reason: str = ""
    had_tool_calls: bool = False
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    stage: LoopStage = LoopStage.running


@dataclass(frozen=True)
class ToolCallRecord:

    round_index: int
    call_index: int
    tool: str
    category: str
    malformed: bool = False      # the provider's arguments did not parse
    accepted: bool = False       # the agent's executor took it as a well-formed, actioned call
    duration_ms: int = 0


@dataclass(frozen=True)
class ProgressObservation:

    made_progress: bool
    signature: str = ""
    detail: str = ""


@runtime_checkable
class RunExtension(Protocol):

    def sanitized(self) -> Mapping[str, DiagnosticValue]: ...


E = TypeVar("E", bound=RunExtension)


@dataclass(frozen=True)
class AgentRunRecord(Generic[E]):

    role: AgentRole
    job_id: str
    pipeline_attempt: int
    agent_attempt: int
    limits: LoopLimits
    tally: LoopTally
    exit: LoopExit
    extension: E
    rounds: tuple[RoundRecord, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    failure_marker: str = ""     # the application-owned marker (errors.py), "" on a clean exit


@runtime_checkable
class AgentLoopPolicy(Protocol):

    role: AgentRole

    def limits(self) -> LoopLimits: ...

    def category_of(self, tool: str) -> str: ...

    def observe(self, tally: LoopTally) -> ProgressObservation: ...

    def extension(self) -> RunExtension: ...


__all__ = [
    "AgentLoopPolicy",
    "AgentRole",
    "AgentRunRecord",
    "CategoryCount",
    "DiagnosticValue",
    "LoopExit",
    "LoopLimits",
    "LoopStage",
    "LoopTally",
    "ProgressObservation",
    "RoundRecord",
    "RunExtension",
    "ToolCallRecord",
    "count_for",
]
