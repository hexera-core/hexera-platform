# Responsibility: Decide what the builder loop does after each round: continue, nudge, or stop.
# Owns: action categorisation, argument normalisation, repeat detection and the stop conditions.
# Boundaries: policy over observed rounds; it calls no model and executes no tool.
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meshpipeline.agents.builder.executor import BuilderToolExecutor

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.builder.diagnostics import BuilderRunExtension
from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopLimits,
    LoopStage,
    LoopTally,
    ProgressObservation,
)
from meshpipeline.contracts.event_stream import StaleExecutionPublish

logger = logging.getLogger(__name__)

# PRESERVED EXACTLY. The retired StuckLoopDetector aborted on the 4th materially identical call
# and warned on the 3rd; both points are kept. Builder is NOT standardised onto the Reviewer's
# 3 - the shared runtime owns the counting mechanism, each policy owns its own threshold and
# progress semantics.
BUILDER_NO_PROGRESS_THRESHOLD = 4
BUILDER_STUCK_WARNING_AT = BUILDER_NO_PROGRESS_THRESHOLD - 1
# The existing redirect point for a repeated configure_mesh, which is idempotent: repeating it
# never helps, and the generic "change strategy" hint misfires there.
CONFIGURE_REDIRECT_AT = 2

_CATEGORIES = {
    "write_file": "authoring", "configure_mesh": "authoring",
    "read_file": "inspection", "list_directory": "inspection",
    "geometry_report": "inspection", "measure_scales": "inspection",
    "web_search": "research",
    "run_mesh": "execution", "run_python": "execution",
    "submit_mesh": "delivery",
}


def category_of(tool: str) -> str:
    return _CATEGORIES.get(tool, "unknown")


def normalize_arguments(args: dict | None) -> str:
    if args is None:
        return "<malformed>"
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(sorted(args.items()))


def action_signature(tool: str, args: dict | None) -> str:
    return hashlib.sha256(f"{tool}|{normalize_arguments(args)}".encode()).hexdigest()[:16]


@dataclass
class _RoundExecution:

    tool: str
    args: dict
    result: dict            # parsed tool result, {} when the output was not JSON
    malformed: bool = False


@dataclass
class BuilderLoopPolicy:

    engine: str
    mode: str
    limits_: LoopLimits
    #: the ONLY tool dispatch site. Stated precisely so its typed publisher is reachable.
    executor: BuilderToolExecutor
    context: Any = None                # BuilderContextPreparer - Builder-owned context work
    max_rounds: int = 0                # for the nudge/warning points, which are round-based
    role: AgentRole = AgentRole.builder

    # attempt-local engineering state
    authored_spec: bool = False
    run_mesh_calls: int = 0
    run_mesh_successes: int = 0
    submitted: bool = False
    auto_submitted: bool = False
    truncated_rounds: int = 0
    checkpoint_recoveries: int = 0
    checkpoint_recovery_tokens: tuple[int, ...] = ()
    forced_tools: list[str] = field(default_factory=list)
    runmesh_ok_without_submit: int = 0
    malformed_calls: int = 0
    last_signature: str = ""
    last_tool: str = ""
    repeat_count: int = 0
    _forced: str | None = None
    _round: list[_RoundExecution] = field(default_factory=list)
    _round_progressed: bool = False
    _round_signature: str = ""
    _pending_note: str | None = None
    _nudged: bool = False
    _valid_mesh_this_round: bool = False

    # LoopDriver: the runner
    def limits(self) -> LoopLimits:
        return self.limits_

    def category_of(self, tool: str) -> str:
        return category_of(tool)

    def forced_tool(self, tally: LoopTally) -> str | None:
        return self._forced

    def before_round(self, tally: LoopTally, messages: list[dict]) -> None:
        if self.context is None:
            return
        rebuilt = self.context.prepare(tally.rounds, messages)
        if rebuilt is not None:
            # hard recovery replaces the conversation IN PLACE, so the runner's own list stays
            # the one it appends to
            messages[:] = rebuilt
            # A recovery is the one context event worth counting in the run record. Counted HERE,
            # at the only place it can actually happen, so the diagnostic cannot drift from it.
            self.note_checkpoint_recovery(self.context.last_recovery_tokens)

    async def execute(self, invocation: ToolInvocation) -> ToolOutcome:
        result = await self.executor.run(invocation.tool, invocation.parsed,
                                         call_index=invocation.call_index)
        if result.malformed:
            self.malformed_calls += 1
            self._round.append(_RoundExecution(invocation.tool, {}, {}, malformed=True))
            return ToolOutcome(content=result.content, accepted=False)

        self.note_execution(result.tool, result.args, result.result)
        if self.context is not None and self.context.wrote_knowledge_block(
                result.tool, result.args, json.dumps(result.result)):
            self.context.note_knowledge_block_written()
        if result.tool in ("configure_mesh", "write_file"):
            self._note_authoring(result)
        if result.terminal:
            self.submitted = True
            return ToolOutcome(content=result.content, accepted=True, terminal=True,
                               payload=result.terminal_value)
        if result.tool == "run_mesh" and result.result.get("mesh_ok"):
            self.runmesh_ok_without_submit += 1
            self._valid_mesh_this_round = True
        return ToolOutcome(content=result.content, accepted=result.accepted)

    def _note_authoring(self, result) -> None:
        from meshpipeline.agents.builder.tools import get_spec_run_files
        if result.tool == "configure_mesh":
            self.authored_spec = True
            return
        path = str(result.args.get("path", "")).lower().lstrip("./")
        if any(path.endswith(rel.lower()) for rel in get_spec_run_files(self.engine)):
            self.authored_spec = True

    # application-controlled close-out
    def is_supersession(self, exc: BaseException) -> bool:
        from meshpipeline.application.execution_fence import StaleWorkerFenced
        return isinstance(exc, StaleWorkerFenced)

    async def close_out(self, tally: LoopTally) -> ToolOutcome | None:
        import meshpipeline.agents.builder.settings as bcfg
        if self.submitted or self.auto_submitted:
            return None                                   # at most one submission, ever
        if self.runmesh_ok_without_submit < bcfg.BUILDER_AUTO_SUBMIT_AFTER:
            return None
        # THE fenced dispatch site. A lost lease raises out of here, before any side effect,
        # any published event and any terminal value.
        result = await self.executor.run("submit_mesh", {},
                                         call_index=tally.tool_calls + 1)
        if not result.terminal:
            # an invalid or not-yet-ready deliverable is never submitted
            return None
        logger.warning(
            "Builder: AUTO-submitting after %d valid run_mesh results without submit_mesh "
            "(engine deliverable present)", self.runmesh_ok_without_submit)
        self.submitted = self.auto_submitted = True
        publish = self.executor.publish
        if publish is not None:
            try:
                await publish.anote(
                    "Auto-submitting - the mesh is valid and the deliverable exists",
                    op_id="auto-submit")
            except StaleExecutionPublish:
                raise
            except Exception:  # noqa: BLE001
                pass
        return ToolOutcome(content=result.content, accepted=True, terminal=True,
                           payload=result.terminal_value)

    def note_execution(self, tool: str, args: dict, result: dict) -> None:
        self._round.append(_RoundExecution(tool, args, result))
        if tool == "run_mesh":
            self.run_mesh_calls += 1
            if result.get("mesh_ok") or result.get("success"):
                self.run_mesh_successes += 1

    def note_authored(self) -> None:
        self.authored_spec = True

    def note_truncated(self) -> None:
        self.truncated_rounds += 1

    def note_checkpoint_recovery(self, prompt_tokens: int = 0) -> None:
        self.checkpoint_recoveries += 1
        if prompt_tokens > 0:
            self.checkpoint_recovery_tokens += (int(prompt_tokens),)

    # progress
    def observe(self, tally: LoopTally) -> ProgressObservation:
        executions = tuple(self._round)
        self._round = []
        # Forced progression is decided from THIS round's authoritative results, before the
        # round's state is cleared.
        self.decide_forced_tool(executions)

        if not executions:                       # plain text or a truncated no-tool round
            return self._no_progress("__no_tool_call__", "no action was taken")

        # The FIRST call of the round carries the round's identity, exactly as the detector's
        # per-round signature did.
        head = executions[0]
        self.last_tool = head.tool
        signature = action_signature(head.tool, None if head.malformed else head.args)
        advanced = any(self._is_advance(e) for e in executions)
        if advanced:
            self.repeat_count = 0
            self.last_signature = signature
            return ProgressObservation(made_progress=True, signature=signature,
                                       detail="the build advanced")
        return self._no_progress(signature, "no material change to the build")

    def _is_advance(self, e: _RoundExecution) -> bool:
        if e.malformed:
            return False
        if e.tool == "submit_mesh":
            return bool(e.result.get("success"))
        if e.tool == "run_mesh":
            # A run that produced a usable mesh advances; a failed run repeated does not.
            return bool(e.result.get("mesh_ok") or e.result.get("mesh_already_production_grade"))
        if e.tool in ("configure_mesh", "write_file"):
            # Authoring advances when it SUCCEEDS and the action is not the one just taken.
            if not (e.result.get("success") or e.result.get("written")):
                return False
            return action_signature(e.tool, e.args) != self.last_signature
        return False

    def _no_progress(self, signature: str, detail: str) -> ProgressObservation:
        if signature == self.last_signature:
            self.repeat_count += 1
        else:
            self.repeat_count = 1
            self.last_signature = signature
        return ProgressObservation(made_progress=False, signature=signature, detail=detail)

    # corrections
    def correction(self, stage: LoopStage, tally: LoopTally,
                   observation: ProgressObservation) -> str | None:
        notes: list[str] = []
        submit_now = self._submit_discipline_note(tally)
        if submit_now:
            notes.append(submit_now)
        if self._pending_note:
            notes.append(self._pending_note)
            self._pending_note = None
        stuck = self._stuck_hint(observation)
        if stuck:
            notes.append(stuck)
        nudge = self._authoring_nudge(tally)
        if nudge:
            notes.append(nudge)
        budget = self._budget_warning(tally)
        if budget:
            notes.append(budget)
        return "\n".join(notes) if notes else None

    def _submit_discipline_note(self, tally: LoopTally) -> str | None:
        if self.submitted or not self._valid_mesh_this_round:
            return None
        self._valid_mesh_this_round = False
        return ("[SYSTEM] run_mesh reports a VALID mesh (no fatal defects). "
                f"Rounds used: {tally.rounds}/{self.max_rounds}. "
                "If the mesh already reflects your refinement strategy, call submit_mesh() NOW - "
                "do not keep re-refining a valid mesh; the reviewer judges quality downstream.")

    def _authoring_nudge(self, tally: LoopTally) -> str | None:
        if self._nudged or self.authored_spec:
            return None
        if tally.rounds < (15 if self.mode == "initial" else 10):
            return None
        self._nudged = True
        return (f"[SYSTEM] {tally.rounds} rounds used and the mesh spec has not been authored "
                "yet. After geometry_report, AUTHOR the mesh NOW (configure_mesh with your "
                "strategy, or write the engine's spec file), then run_mesh. Stop "
                "over-deliberating.")

    def _budget_warning(self, tally: LoopTally) -> str | None:
        if not self.max_rounds or tally.rounds != self.max_rounds - 10:
            return None
        remaining = self.max_rounds - tally.rounds
        return (f"[SYSTEM] Budget warning: {tally.rounds}/{self.max_rounds} rounds used. "
                f"You have {remaining} rounds remaining. "
                "If run_mesh reports a valid mesh, call submit_mesh() now. "
                "If run_mesh has not yet succeeded, fix the authored mesh spec and run_mesh "
                "again.")

    def note_truncated_continuation(self) -> str:
        return ("[SYSTEM] Your previous response was truncated, but its tool call(s) were "
                "executed - see the result(s) above. Continue from that result with your next "
                "concrete tool call. Do NOT restate prior reasoning or rewrite work that "
                "already succeeded.")

    def note_truncated_no_tool_call(self) -> str:
        return ("[SYSTEM] Your previous response was truncated before any tool call was "
                "recorded - likely because reasoning ran long, not because the script is wrong. "
                "Do NOT restart or re-explain at length. In 2-3 lines, restate the plan you had "
                "reached (geometry class, domain sizing, patch names), then make your next "
                "concrete tool call (write_file or run_mesh).")

    def _stuck_hint(self, observation: ProgressObservation) -> str | None:
        if observation.made_progress:
            return None
        if self.last_tool == "configure_mesh" and self.repeat_count >= CONFIGURE_REDIRECT_AT:
            return ("CORRECTIVE: configure_mesh has ALREADY written the mesh spec - repeating it "
                    "does NOTHING. Call run_mesh NOW to build the mesh. Reconfigure only if a "
                    "concrete run_mesh failure tells you which value to change.")
        if self.repeat_count == BUILDER_STUCK_WARNING_AT:
            return (f"CORRECTIVE: you took the SAME action {self.repeat_count} times - the result "
                    "will not change. CHANGE your strategy NOW: a carve/validation failure → "
                    "raise max_cells (finer surface seals the body); high max_skewness → "
                    "quality='strict'; a timeout → lower max_cells; low layer coverage → fewer "
                    "n_layers. One more identical call aborts the attempt.")
        return None

    def queue_note(self, note: str) -> None:
        self._pending_note = f"{self._pending_note}\n{note}" if self._pending_note else note

    def on_plaintext(self, tally: LoopTally) -> RoundDecision:
        notes: list[str] = []
        if self._pending_note:
            notes.append(self._pending_note)
            self._pending_note = None
        notes.append("You stopped without calling submit_mesh(). "
                     "If mesh.msh exists, call submit_mesh() now. "
                     "If there are remaining steps, continue.")
        return RoundDecision(message="\n".join(notes), complete=False)

    def _malformed_correction(self, tool: str) -> str:
        return (f"[SYSTEM] The arguments for {tool} were not valid JSON, so NOTHING was executed "
                "and no result exists. Call the tool again with well-formed JSON arguments. Do "
                "not assume the action succeeded.")

    # forced progression (Builder)
    def decide_forced_tool(self, executions: tuple[_RoundExecution, ...]) -> str | None:
        forced: str | None = None
        for e in executions:
            if e.malformed:
                continue
            if e.tool in ("configure_mesh", "run_mesh") and e.result.get(
                    "mesh_already_production_grade"):
                forced = "submit_mesh"
            elif e.tool == "configure_mesh" and e.result.get("success"):
                forced = "run_mesh"
            elif e.tool == "run_mesh" and e.result.get("mesh_ok"):
                forced = "submit_mesh"
            elif e.tool == "run_mesh":
                forced = None
        self._forced = forced
        if forced:
            self.forced_tools.append(forced)
        return forced

    # diagnostics
    def extension(self) -> BuilderRunExtension:
        from meshpipeline.agents.builder.driver_run import STRATEGY_CANONICAL_LOOP
        return BuilderRunExtension(
            strategy=STRATEGY_CANONICAL_LOOP,
            engine=self.engine, mode=self.mode, authored_spec=self.authored_spec,
            run_mesh_calls=self.run_mesh_calls, run_mesh_successes=self.run_mesh_successes,
            submitted=self.submitted, auto_submitted=self.auto_submitted,
            forced_tools=tuple(self.forced_tools),
            repeated_tool_signature=(self.last_signature if self.repeat_count > 1 else ""),
            truncated_rounds=self.truncated_rounds,
            checkpoint_recoveries=self.checkpoint_recoveries,
            checkpoint_recovery_tokens=self.checkpoint_recovery_tokens)


__all__ = [
    "BUILDER_NO_PROGRESS_THRESHOLD",
    "BUILDER_STUCK_WARNING_AT",
    "CONFIGURE_REDIRECT_AT",
    "BuilderLoopPolicy",
    "action_signature",
    "category_of",
    "normalize_arguments",
]
