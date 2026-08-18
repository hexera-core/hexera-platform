# Responsibility: Decide what happens after each round, from the policy the role supplied.
# Boundaries: the decision seam; the policies themselves live with their roles.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
    RunExtension,
)


@dataclass(frozen=True)
class ToolOutcome:

    content: Any = None
    accepted: bool = True
    terminal: bool = False
    payload: Any = None


@dataclass(frozen=True)
class RoundDecision:

    message: str = ""
    complete: bool = False
    payload: Any = None
    exit: LoopExit = LoopExit.turn_complete


class LoopDriver(Protocol):

    role: AgentRole

    def limits(self) -> LoopLimits: ...

    def category_of(self, tool: str) -> str: ...

    async def execute(self, invocation: ToolInvocation) -> ToolOutcome: ...

    def observe(self, tally: LoopTally) -> ProgressObservation: ...

    def correction(self, stage: LoopStage, tally: LoopTally,
                   observation: ProgressObservation) -> str | None: ...

    def before_round(self, tally: LoopTally, messages: list[dict]) -> None:
        ...

    def forced_tool(self, tally: LoopTally) -> str | None:
        ...

    def on_plaintext(self, tally: LoopTally) -> RoundDecision:
        return RoundDecision(message="", complete=False)

    def is_supersession(self, exc: BaseException) -> bool:
        return False

    async def close_out(self, tally: LoopTally) -> ToolOutcome | None:
        return None

    def extension(self) -> RunExtension: ...


@dataclass(frozen=True)
class LoopResult:

    exit: LoopExit
    record: Any                       # AgentRunRecord[E]
    payload: Any = None               # the driver's own terminal payload, when terminal
    failure_marker: str = ""


__all__ = ["LoopDriver", "LoopResult", "RoundDecision", "ToolOutcome"]
