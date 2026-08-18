# Responsibility: Decide what the intake loop does after each round.
# Boundaries: policy over observed rounds; it calls no model.
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.intake.diagnostics import IntakeRunExtension
from meshpipeline.agents.intake.executor import IntakeExecutionState, category_of
from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)

logger = logging.getLogger(__name__)

# The application-rendered terminals, in the priority the batch is resolved with. An impossible
# admission outranks everything (it voids authorization); a new engine selection outranks a
# submission (the user is choosing again); a submission summary wins only if nothing above it did.
TERMINAL_PRIORITY = ("admission_block", "selection_prompt", "submit_summary")


@dataclass
class IntakeLoopPolicy:

    exec_state: IntakeExecutionState
    executor: Any                              # IntakeToolExecutor - the ONLY dispatch site
    limits_: LoopLimits = field(default_factory=LoopLimits)
    role: AgentRole = AgentRole.intake

    malformed_calls: int = 0
    plaintext_text: str = ""                   # the model's reply, returned - never diagnosed
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    _last_signature: str = ""
    _round_advanced: bool = False

    # LoopDriver: the runner
    def limits(self) -> LoopLimits:
        return self.limits_

    def category_of(self, tool: str) -> str:
        return category_of(tool)

    def forced_tool(self, tally: LoopTally) -> str | None:
        return None

    def before_round(self, tally: LoopTally, messages: list[dict]) -> None:
        self._round_advanced = False

    def is_supersession(self, exc: BaseException) -> bool:
        return False

    async def execute(self, invocation: ToolInvocation) -> ToolOutcome:
        result = await self.executor.run(invocation.tool, invocation.parsed)
        if result.malformed:
            self.malformed_calls += 1
            return ToolOutcome(content=result.content, accepted=False)
        if result.advanced:
            self._round_advanced = True
        return ToolOutcome(content=result.content, accepted=result.accepted)

    # progress
    def observe(self, tally: LoopTally) -> ProgressObservation:
        signature = self.exec_state.authorization_signature()
        advanced = self._round_advanced
        self._round_advanced = False
        if advanced:
            self._last_signature = signature
            return ProgressObservation(made_progress=True, signature=signature,
                                       detail="the requirements advanced")
        return ProgressObservation(made_progress=False, signature=signature,
                                   detail="no material change to the requirements")

    def correction(self, stage: LoopStage, tally: LoopTally,
                   observation: ProgressObservation) -> str | None:
        return None

    def on_plaintext(self, tally: LoopTally) -> RoundDecision:
        return RoundDecision(message="", complete=True, payload=self.plaintext_text,
                             exit=LoopExit.turn_complete)

    async def close_out(self, tally: LoopTally) -> ToolOutcome | None:
        terminal = self.resolve_terminal()
        if terminal is None:
            return None
        return ToolOutcome(content=terminal, accepted=True, terminal=True, payload=terminal)

    def note_round(self, round_result: Any) -> None:
        self.plaintext_text = (round_result.assistant_text or "").strip()
        self.finish_reason = round_result.finish_reason or "unknown"
        self.input_tokens = round_result.input_tokens
        self.output_tokens = round_result.output_tokens

    # post-batch resolution
    def resolve_terminal(self) -> str | None:
        st = self.exec_state
        for name in TERMINAL_PRIORITY:
            text = getattr(st, name)
            if text:
                if name != "submit_summary":
                    # Nothing below this priority survives: a submission the batch superseded is
                    # not delivered, and its arguments are not returned to the caller.
                    st.submit_args = None
                    st.submit_summary = None
                return text
        return None

    # diagnostics
    def extension(self) -> IntakeRunExtension:
        st = self.exec_state
        return IntakeRunExtension(
            missing_fields=tuple(st.val_errors_seen),
            tools_invoked=tuple(st.tools_invoked),
            submit_attempts=st.submit_attempts,
            submit_rejections=st.submit_rejections,
            authorization_state=("authorized" if st.submit_args is not None
                                 else ("unauthorized" if st.submit_attempts else "")),
            selection_state=("confirmed" if (st.selection or {}).get("confirmed")
                             else ("proposed" if st.selection else "")),
            approval_state=str((st.approval or {}).get("status", "") or ""),
            recommendation_turn=st.recommended_this_turn,
            canonical_revision=str(st.revision or ""))


__all__ = ["TERMINAL_PRIORITY", "IntakeLoopPolicy"]
