# Responsibility: Classify how one builder turn ended, and turn that into the attempt's outcome.
# Boundaries: classification of an already-finished turn.
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from meshpipeline.agents.builder.attempt import BuilderAttempt
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.application.execution_publisher import StaleExecutionPublish
from meshpipeline.contracts.agent_loop import LoopExit
from meshpipeline.contracts.event_stream import ExecutionEventPublisher

# Module scope, as `agent.py` had it: the engine-spec lookup is a real seam that callers and tests
# resolve through this module, and a function-local import would silently rebind past any of them.
from meshpipeline.engines.registry import get_spec

logger = logging.getLogger(__name__)

#: The marker the tool loop raises a provider outage as.
API_FAILURE_MARK = "[API_FAILURE]"


@dataclass(frozen=True)
class TurnOutcome:

    spec_authored: bool = False
    final_text: str = ""
    messages_out: list = field(default_factory=list)
    api_failure: str = ""
    timed_out: bool = False

    @property
    def provider_failed(self) -> bool:
        return bool(self.api_failure)


async def _run_engine_driver(driver, attempt: BuilderAttempt, state, *, job_id: str,
                             publish: ExecutionEventPublisher,
                             timeout_s: int) -> tuple[bool, str]:
    from meshpipeline.application import execution_fence as fence

    run = BuilderDriverRun(
        job_id=job_id, engine=attempt.engine, mode=attempt.mode,
        pipeline_attempt=int(state.get("retry_count", 0) or 0),
        agent_attempt=int(state.get("retry_count", 0) or 0),
        deadline_s=float(timeout_s))
    exit_reason, marker = LoopExit.policy_abort, ""
    try:
        spec_authored, final_text, outcome = await driver(
            attempt.workspace, state, job_id=job_id, publish=publish,
            source_path=attempt.source_path, run=run)
        exit_reason, marker = outcome.exit, outcome.failure_marker
    except fence.StaleWorkerFenced:
        # No authoritative record - only the breadcrumb. The exception propagates to the boundary
        # that owns generation semantics.
        run.superseded()
        raise
    except asyncio.CancelledError:
        # CANCELLED. No authoritative record - the same rule the canonical loop follows. The driver
        # did not fail; the run was torn down, and `builder_driver_raised` would be a false account
        # of why this attempt ended.
        raise
    except BaseException:
        run.finish(exit=LoopExit.executor_failed, failure_marker="builder_driver_raised")
        raise
    else:
        run.finish(exit=exit_reason, failure_marker=marker)
    return spec_authored, final_text


async def _run_shared_loop(attempt: BuilderAttempt, state, *, job_id: str,
                           publish: ExecutionEventPublisher,
                           timeout_s: int) -> tuple[str, list]:
    from meshpipeline.agents.builder.loop import _run_tool_loop

    return await asyncio.wait_for(
        _run_tool_loop(
            attempt.messages, attempt.workspace,
            job_id=job_id,
            user_id=state.get("user_id", ""),
            max_rounds=attempt.max_rounds,
            mode=attempt.mode,
            tool_calls_out=attempt.tool_calls,
            publish=publish,
            engine=attempt.engine,
            loop_timeout=timeout_s,          # at most the aggregate budget remaining
            # SOFT authoring input only: shifts the engine's own recommendation, never a gate, a
            # hard limit, a required artifact, or review.
            mesh_fidelity=str(state.get("effective_mesh_fidelity", "") or ""),
            attempt=int(state.get("retry_count", 0) or 0),
            # THE verified execution geometry, read from state ONCE and handed down typed. Tools
            # never re-derive it: a workspace file names no unit, so a tool holding only a path
            # cannot tell a millimetre part from a metre one.
            geometry=attempt.geometry,
            execution_id=str(state.get("execution_id", "") or ""),
            execution_generation=attempt.generation,
            # The engineer's flags, so submit_mesh can require a declaration per flag.
            user_dispute=state.get("user_dispute") or None,
        ),
        timeout=timeout_s,
    )


async def run_attempt(attempt: BuilderAttempt, state, *, job_id: str,
                      publish: ExecutionEventPublisher,
                      timeout_s: int) -> TurnOutcome:
    from meshpipeline.application import execution_fence as fence

    try:
        driver = get_spec(attempt.engine).build_driver
        if driver is not None:
            spec_authored, final_text = await _run_engine_driver(
                driver, attempt, state, job_id=job_id, publish=publish, timeout_s=timeout_s)
            return TurnOutcome(spec_authored=spec_authored, final_text=final_text)
        final_text, messages_out = await _run_shared_loop(
            attempt, state, job_id=job_id, publish=publish, timeout_s=timeout_s)
        return TurnOutcome(spec_authored=True, final_text=final_text, messages_out=messages_out)
    except (fence.StaleWorkerFenced, StaleExecutionPublish):
        # SUPERSESSION, not a Builder failure. This worker lost execution ownership: a newer
        # generation is authoritative, has its own workspace and will produce its own result. It
        # used to fall into the generic RuntimeError branch, be swallowed, and return an
        # ordinary-looking Builder state dict a reader could mistake for current output.
        logger.error("Builder: superseded mid-attempt - job_id=%s; a newer generation owns this "
                     "job. Writing no state.", job_id)
        raise
    except asyncio.CancelledError:
        # Never converted, never recorded, never swallowed.
        raise
    except TimeoutError:
        logger.error("Builder asyncio timeout - job_id=%s", job_id)
        return TurnOutcome(timed_out=True)
    except RuntimeError as exc:
        if API_FAILURE_MARK in str(exc):
            failure = str(exc).replace(f"{API_FAILURE_MARK} ", "")
            logger.error("Builder API failure - job_id=%s: %s", job_id, failure)
            return TurnOutcome(api_failure=failure)
        logger.exception("Builder unexpected RuntimeError - job_id=%s", job_id)
        return TurnOutcome()
    except Exception:
        logger.exception("Builder unexpected error - job_id=%s", job_id)
        return TurnOutcome()


__all__ = ["API_FAILURE_MARK", "TurnOutcome", "run_attempt"]
