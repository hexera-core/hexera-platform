# Responsibility: Run an engine's deterministic build driver and report what it produced.
# Boundaries: the seam for engine-owned deterministic authoring, including the superseded-generation breadcrumb.
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.agents.builder.diagnostics import BuilderRunExtension
from meshpipeline.agents.loop.accounting import AgentRunAccountant
from meshpipeline.application import execution_fence as _fence
from meshpipeline.contracts.agent_loop import AgentRole, LoopExit, LoopLimits

logger = logging.getLogger(__name__)

# The Builder strategy that produced a record. Consumers use this to tell an interactive agent
# invocation from an engine-owned deterministic one WITHOUT inferring it from empty counters.
STRATEGY_CANONICAL_LOOP = "canonical_loop"
STRATEGY_ENGINE_DRIVER = "engine_driver"


@dataclass(frozen=True)
class BuildDriverOutcome:

    produced_deliverable: bool
    terminal_value: str = ""
    exit: LoopExit = LoopExit.policy_abort
    failure_marker: str = ""


@dataclass
class BuilderDriverRun:

    job_id: str
    engine: str
    mode: str                                   # "initial" | "retry" | "rebuild"
    pipeline_attempt: int = 0
    agent_attempt: int = 0
    deadline_s: float | None = None
    strategy: str = STRATEGY_ENGINE_DRIVER

    _acct: AgentRunAccountant = field(init=False)
    _started: float = field(default_factory=time.monotonic)
    _authoring_ops: int = 0
    _native_runs: int = 0
    _native_successes: int = 0
    _plan_calls: int = 0
    _delivered: bool = False
    _finished: bool = False

    def __post_init__(self) -> None:
        self._acct = AgentRunAccountant(
            role=AgentRole.builder, job_id=self.job_id,
            limits=LoopLimits(total_timeout_s=self.deadline_s),
            pipeline_attempt=self.pipeline_attempt, agent_attempt=self.agent_attempt)

    # ownership
    @property
    def execution_generation(self) -> int:
        own = _fence.current_ownership()
        return int(getattr(own, "execution_generation", 0) or 0)

    async def fence(self, phase: str) -> None:
        await _fence.assert_current_owner(f"builder driver {phase}")

    # accounting
    @property
    def plan_call_index(self) -> int:
        """1-based index of the NEXT planner call, derived purely from this run's control
        flow. BuilderDriverRun is rebuilt per node execution, so a crash-resume re-execution
        replays the same sequence and regenerates byte-identical planner op_ids - replays
        dedupe, genuine disagreements still quarantine."""
        return self._plan_calls + 1

    def note_plan_round(self, round_result: Any) -> None:
        if round_result is None:
            return
        self._plan_calls += 1
        self._acct.record_round(round_result)

    def note_authoring(self) -> None:
        self._authoring_ops += 1

    def note_native_run(self, *, produced_usable_mesh: bool) -> None:
        self._native_runs += 1
        if produced_usable_mesh:
            self._native_successes += 1

    def note_delivered(self) -> None:
        self._delivered = True

    # diagnostics
    def extension(self) -> BuilderRunExtension:
        return BuilderRunExtension(
            engine=self.engine,
            mode=self.mode,
            strategy=self.strategy,
            authored_spec=self._authoring_ops > 0,
            run_mesh_calls=self._native_runs,
            run_mesh_successes=self._native_successes,
            plan_calls=self._plan_calls,
            submitted=self._delivered,
        )

    def superseded(self) -> None:
        self._finished = True
        # resolved per call, exactly as the canonical runner does, so there is ONE sink
        from meshpipeline.agents.loop.diagnostics import emit_superseded
        emit_superseded(role=AgentRole.builder, job_id=self.job_id,
                        pipeline_attempt=self.pipeline_attempt,
                        agent_attempt=self.agent_attempt, tally=self._acct.tally())

    def finish(self, *, exit: LoopExit, failure_marker: str = "") -> Any:
        if self._finished:
            logger.warning("Builder driver run for job %s finished twice - ignoring the second",
                           self.job_id)
            return None
        self._finished = True
        from meshpipeline.agents.loop.diagnostics import emit
        record = self._acct.report(exit=exit, extension=self.extension(),
                                   failure_marker=failure_marker)
        emit(record)
        return record

    # terminal outcome
    def outcome(self, *, produced_deliverable: bool, exhausted: bool = False,
                failure_marker: str = "") -> BuildDriverOutcome:
        if produced_deliverable:
            self.note_delivered()
            return BuildDriverOutcome(True, TERMINAL_SUCCESS, LoopExit.terminal_action)
        return BuildDriverOutcome(
            False, "",
            LoopExit.attempts_exhausted if exhausted else LoopExit.policy_abort,
            failure_marker)


# The Builder's terminal contract, shared with the canonical path (executor.BuilderToolResult).
TERMINAL_SUCCESS = "submit_mesh:success"


__all__ = ["STRATEGY_CANONICAL_LOOP", "STRATEGY_ENGINE_DRIVER", "TERMINAL_SUCCESS",
           "BuildDriverOutcome", "BuilderDriverRun"]
