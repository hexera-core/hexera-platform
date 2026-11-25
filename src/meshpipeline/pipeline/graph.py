# Responsibility: Assemble the pipeline state machine and the routing between its nodes.
# Owns: node registration, every routing function, and the compiled graph with its checkpointer.
# Boundaries: structure and routing only - no node's work lives here.
# Collaborates with: the node modules in this package and application/fenced_checkpointer.py.
from __future__ import annotations

import logging
from typing import Final, Literal

from langgraph.graph import END, START, StateGraph

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.agents.builder.agent import node_builder
from meshpipeline.agents.intake.agent import node_intake
from meshpipeline.agents.reviewer.visual import node_reviewer
from meshpipeline.cad.repair.contracts import RepairGoal
from meshpipeline.contracts.failure_cause import retry_can_help
from meshpipeline.contracts.pipeline_state import PipelineState
from meshpipeline.pipeline import cfd_wrap as _wrap
from meshpipeline.pipeline import repair_handback as _handback_mod
from meshpipeline.pipeline.cfd_wrap import node_cfd_wrap
from meshpipeline.pipeline.classifier import node_classifier
from meshpipeline.pipeline.engine_fallback import another_engine_takes_over, node_engine_fallback
from meshpipeline.pipeline.engine_select import node_engine_select
from meshpipeline.pipeline.enums import Verdict
from meshpipeline.pipeline.executor import node_executor
from meshpipeline.pipeline.geometry_admission import node_geometry_admission
from meshpipeline.pipeline.repair_attempt import node_repair_attempt
from meshpipeline.pipeline.repair_deliver import node_repair_deliver
from meshpipeline.pipeline.repair_handback import node_repair_handback
from meshpipeline.pipeline.repair_inspect import node_repair_inspect

logger = logging.getLogger(__name__)

# langgraph exports END as a plain `str`, which would widen every routing function's return
# type and cost us the exhaustiveness checking the Literal annotations buy. Bind the literal
# once. The graph tests compare routing results against langgraph's real END, so the two
# cannot drift apart unnoticed.
_END: Final[Literal["__end__"]] = "__end__"


async def node_failure_handler(state: PipelineState) -> dict:
    api_failure = state.get("api_failure", "")
    job_id      = state.get("job_id", "unknown")

    from meshpipeline.errors import classify_api_failure, user_message_for
    fc = classify_api_failure(api_failure)

    logger.warning(
        "node_failure_handler: SYSTEM FAILURE api_failure=%s class=%s - job_id=%s",
        api_failure, fc.value, job_id,
    )
    return {"outcome_message": user_message_for(fc)}



async def node_infra_retry(state: PipelineState) -> dict:
    """A TRANSIENT system failure (provider brownout, dependency blip) killed the builder
    attempt before any mesh was judged. Wait out the brownout and replay the SAME attempt:
    retry_count steps back one so the builder re-prepares the same attempt number - an
    infra replay is not a new mesh attempt and spends none of the mesh-retry ladder.
    Admission is the route's job: retryable class and replay budget remaining."""
    job_id = state.get("job_id", "unknown")
    replay = int(state.get("infra_retry_count", 0) or 0) + 1
    logger.warning(
        "node_infra_retry: transient system failure '%s' - replay %d/%d after %ds backoff "
        "- job_id=%s", state.get("api_failure", ""), replay, bcfg.BUILDER_INFRA_RETRY_MAX,
        bcfg.BUILDER_INFRA_RETRY_BACKOFF_S, job_id)
    try:
        # Best-effort narration only: the very dependency that browned out may be the
        # publisher's transport, and a failed note must never cost the replay.
        from meshpipeline.application.execution_publisher import execution_publisher
        await execution_publisher(job_id, agent="builder").anote(
            "A temporary service issue interrupted this attempt - waiting briefly, then retrying",
            op_id=f"infra-retry:{replay}")
    except Exception:  # noqa: BLE001
        pass
    import asyncio
    await asyncio.sleep(bcfg.BUILDER_INFRA_RETRY_BACKOFF_S)
    return {"api_failure": "",
            "infra_retry_count": replay,
            "retry_count": max(0, int(state.get("retry_count", 1) or 1) - 1)}


def review_can_rerun(state: PipelineState) -> bool:
    """Whether a review that ended WITHOUT a verdict is started again on the same mesh.

    Only a mesh the executor validated is reviewed again - there is nothing else to judge - and
    only for a non-verdict another look can change: the conversation stalled or ran out of rounds,
    the renderer fell over, or a provider or dependency browned out. A pre-loop refusal (a malformed
    assurance plan, missing deterministic evidence, absent review targets) and a provider that is
    down for good repeat identically, so they go to the sink as before. Bounded per mesh by
    REVIEWER_RERUN_MAX (every builder attempt resets the count) - a count, never the time left
    (docs/RULES.md TIME-1): the run's own deadline is a guard (TIME-5), and a rerun review it
    stops ends the run as ours, the same as any review that cannot conclude."""
    import meshpipeline.agents.reviewer.settings as rcfg
    from meshpipeline.agents.reviewer.loop_policy import RERUNNABLE_MARKERS
    from meshpipeline.errors import classify_api_failure
    marker = str(state.get("api_failure") or "").strip()
    if marker.startswith("<<API_FAILURE:") and marker.endswith(">>"):
        marker = marker[len("<<API_FAILURE:"):-2].strip()
    if not marker or state.get("executor_success") is not True:
        return False
    if int(state.get("review_rerun_count", 0) or 0) >= rcfg.REVIEWER_RERUN_MAX:
        return False
    transient = classify_api_failure(marker).is_retryable
    return marker in RERUNNABLE_MARKERS or transient


def guard_trip_of(state: PipelineState) -> dict:
    """The guard trip that ended the attempt the executor just judged ({} when none): its cause is
    the run running past a guard clock (executor: ENGINE_TIMED_OUT with a `guard` fact)."""
    from meshpipeline.contracts.failure_cause import FailureCause
    facts = state.get("executor_failure_facts") or {}
    if (state.get("executor_success") is True
            or str(state.get("executor_failure_cause") or "") != FailureCause.ENGINE_TIMED_OUT.value
            or not isinstance(facts, dict) or not facts.get("guard")):
        return {}
    return dict(facts["guard"]) if isinstance(facts["guard"], dict) else {"where": str(facts["guard"])}


def _after_guard_trip(state: PipelineState) -> str:
    """TIME-3, decided from counts alone: replay the attempt, or end the run as ran out of time.
    The decision is cited in the run's why record."""
    from meshpipeline.contracts import decision_record as dr
    from meshpipeline.contracts.work_limits import (
        GUARD_REPLAYS,
        TIME_SAME_ON_REPLAY,
        after_trip,
        trip_words,
    )
    trip = guard_trip_of(state)
    inputs = {"replays": int(state.get("guard_replay_count", 0) or 0), "allowed": GUARD_REPLAYS,
              "where": str(trip.get("where") or ""), "same_on_replay": trip.get("same_on_replay") is True}
    outcome, rule = after_trip(inputs)
    dr.cite(str(state.get("job_id") or ""), stage=dr.STAGE_GUARD, decided="after_trip",
            value=outcome, rule=rule, inputs=inputs, source=dr.SOURCE_GUARD_CLOCK,
            words=(f"{trip_words(trip)}: "
                   + ("the attempt is run again as it was (an infrastructure stop)."
                      if outcome == "replay" else
                      "that step does the same planned work on the same mesh when run again, so it is not "
                      "run again and the run ends as ran out of time." if rule == TIME_SAME_ON_REPLAY else
                      "it already had its replay, so the run ends as ran out of time.")))
    return outcome


async def node_guard_replay(state: PipelineState) -> dict:
    """A GUARD CLOCK STOPPED THE ATTEMPT (docs/RULES.md TIME-3): an infrastructure stop, not a result
    about the shape. Run the SAME attempt again, as it was: retry_count steps back one, so the
    builder prepares the same attempt number in the same workspace - no lever, no coarser plan, no
    other engine. The workspace is put back as the attempt found it (its plan memories, meshes and
    reshuffled surfaces gone), so the replay decides as the attempt did on a quiet machine, and its
    trips are cleared so the replay is judged on its own. Admission (one replay) is
    route_after_executor's job."""
    from meshpipeline.engines import determinism as det
    job_id = state.get("job_id", "unknown")
    replay = int(state.get("guard_replay_count", 0) or 0) + 1
    trip = guard_trip_of(state)
    ws = str(state.get("openfoam_workspace") or "")
    if ws:
        det.restore_attempt_start(ws)
        det.clear_guard_trips(ws)
    logger.warning("node_guard_replay: a guard clock stopped attempt %s (%s) - replay %d, the same "
                   "attempt as it was - job_id=%s", state.get("retry_count"), trip.get("where"),
                   replay, job_id)
    try:
        from meshpipeline.application.execution_publisher import execution_publisher
        await execution_publisher(job_id, agent="builder").anote(
            "This attempt ran past its guard clock - an infrastructure stop, not a result about "
            "your part. Running the same attempt again, unchanged.", op_id=f"guard-replay:{replay}")
    except Exception:  # noqa: BLE001 - narration never costs the replay
        pass
    return {"guard_replay_count": replay,
            "retry_count": max(0, int(state.get("retry_count", 1) or 1) - 1),
            "executor_success": False, "executor_failed_gate": "",
            "executor_failure_cause": "", "executor_failure_facts": {}}


#: The least time a rerun review is started with. A review that opens its renderer and makes a
#: few model rounds needs minutes, not seconds; below this the run's own timeout is the truthful end.
REVIEW_RERUN_MIN_WINDOW_S = 300


async def node_review_retry(state: PipelineState) -> dict:
    """A review of a VALIDATED mesh ended without a verdict. Start it again on the same mesh: no
    rebuild, no new mesh attempt (retry_count is untouched), only a fresh review. After a transient
    provider or dependency failure it first waits the brownout out; a stalled review starts again
    at once. The reviewer says so itself when it opens (agents/reviewer/visual.py), so this node
    publishes nothing. Admission is route_after_reviewer's job (review_can_rerun)."""
    import asyncio

    import meshpipeline.agents.reviewer.settings as rcfg
    from meshpipeline.errors import classify_api_failure
    job_id = state.get("job_id", "unknown")
    marker = str(state.get("api_failure") or "")
    rerun = int(state.get("review_rerun_count", 0) or 0) + 1
    wait = rcfg.REVIEWER_RERUN_BACKOFF_S if classify_api_failure(marker).is_retryable else 0
    deadline = state.get("pipeline_deadline_epoch")
    if deadline and wait > 0:
        # never sleep into the window the rerun review needs (admission already checked it fits;
        # this holds it if time passed between the route and here)
        from meshpipeline.application.pipeline_budget import remaining_seconds
        wait = int(max(0.0, min(float(wait), remaining_seconds(float(deadline))
                                - REVIEW_RERUN_MIN_WINDOW_S)))
    logger.warning(
        "node_review_retry: review ended without a verdict ('%s') - rerun %d/%d on the same "
        "mesh after %ds - job_id=%s", marker, rerun, rcfg.REVIEWER_RERUN_MAX, wait, job_id)
    if wait > 0:
        await asyncio.sleep(wait)
    return {"api_failure": "", "review_rerun_count": rerun}


def route_after_builder(
    state: PipelineState,
) -> Literal["node_executor", "node_failure_handler", "node_infra_retry", "node_cfd_wrap", "__end__"]:
    if state.get("builder_stop") and _wrap.wanted(state):
        # THE RUN'S TIME RAN OUT before a mesh: the wrap rung brings its own (LAD-cfd-5)
        logger.warning("route_after_builder: the run's time ran out with no mesh → node_cfd_wrap "
                       "(LAD-cfd-5) - job_id=%s", state.get("job_id"))
        return "node_cfd_wrap"
    if state.get("builder_stop"):
        # The builder built nothing on purpose: another attempt provably cannot change the outcome
        # (a review's retry wrote exactly the case that review rejected). The verdict already
        # reached stands - validating and reviewing the same mesh again would only re-roll it.
        logger.warning("route_after_builder: builder stop %r → END on the verdict already "
                       "reached - job_id=%s", state.get("builder_stop"), state.get("job_id"))
        return _END
    api_failure = state.get("api_failure")
    if api_failure:
        from meshpipeline.errors import classify_api_failure
        fc = classify_api_failure(api_failure)
        if (fc.is_retryable
                and int(state.get("infra_retry_count", 0) or 0) < bcfg.BUILDER_INFRA_RETRY_MAX):
            # The corpus's largest recoverable failure class: 23 baseline episodes died on
            # exactly this signature (transient marker, attempt 1, no gate reached) while a
            # 60-90s wait would have outlived the brownout. Deterministic failures still go
            # to the sink: is_retryable excludes them by class.
            return "node_infra_retry"
        return "node_failure_handler"
    return "node_executor"


def route_after_engine_select(state: PipelineState) -> Literal["node_repair_inspect", "node_reviewer"]:
    if state.get("user_dispute") and not state.get("reviewer_verdict"):
        logger.info("route_after_engine_select: dispute run → re-review delivered mesh first "
                    "- job_id=%s", state.get("job_id"))
        return "node_reviewer"
    # A normal run inspects the input for CAD defects (evidence), then the admission gate judges
    # it (the decision). The dispute bypass above is untouched: a re-review of a mesh already
    # delivered has nothing to inspect on the way in.
    return "node_repair_inspect"


def route_after_repair_attempt(
    state: PipelineState,
) -> Literal["node_repair_deliver", "node_geometry_admission"]:
    """A STANDALONE repair job ends with the repair; every other run goes on to be meshed.

    Spec amendment 1: three entry points, ONE loop, and only the finishing verdict differs. A
    standalone job's verdict is kernel-clean plus a fast meshability score plus the semantic
    review - the product mesher is not involved - so the admission gate, the builder and the
    executor are not on its path at all. `meshing`, `benchmark` and an unset goal keep today's one
    unconditional edge, and so does every deployment with the posture off
    (`pipeline/repair_goal.from_state` consults it).
    """
    from meshpipeline.pipeline.repair_goal import from_state

    asked = from_state(state)
    if asked is not None and asked.goal is RepairGoal.standalone:
        logger.info("route_after_repair_attempt: standalone repair job → deliver the repair, no "
                    "mesher - job_id=%s", state.get("job_id"))
        return "node_repair_deliver"
    return "node_geometry_admission"


def route_after_geometry_admission(
    state: PipelineState,
) -> Literal["node_builder", "node_executor"]:
    if state.get("geometry_unsuitable_reason"):
        return "node_executor"
    return "node_builder"


def _sound_or_end(
    state: PipelineState,
) -> Literal["node_deliver_sound", "node_cfd_wrap", "__end__"]:
    """THE RUN ENDS ON A FAILED ATTEMPT: when an earlier attempt built a sound mesh, that mesh is
    delivered instead of the failure (LAD-cfd-3, node_deliver_sound) - a retry that came out broken
    never throws an earlier sound mesh away. With none, a flow run whose engines all failed on the
    surface as drawn meshes a wrapped copy of it once (LAD-cfd-5, node_cfd_wrap). Otherwise the
    run ends as it always did."""
    from meshpipeline.engines import resolution_policy as _rp
    if not state.get("user_dispute") and _rp.deliverable_sound(state) is not None:
        logger.warning("route_after_executor: the last attempt failed, and an earlier attempt "
                       "built a sound mesh → node_deliver_sound (LAD-cfd-3) - job_id=%s",
                       state.get("job_id"))
        return "node_deliver_sound"
    from meshpipeline.pipeline.engine_fallback import refused_as_wrong_region
    if refused_as_wrong_region(state):
        # LAD-cfd-8 applied to the region: a wrapped copy of the same staging meshes the same wrong region
        logger.warning("route_after_executor: the mesh filled another region than the fluid - no wrap rung on "
                       "the same staging (LAD-cfd-8) → END - job_id=%s", state.get("job_id"))
        return _END
    if _wrap.wanted(state):
        logger.warning("route_after_executor: every engine failed on the surface as drawn, and "
                       "no attempt built a sound mesh → node_cfd_wrap (LAD-cfd-5) - job_id=%s",
                       state.get("job_id"))
        return "node_cfd_wrap"
    return _END


def route_after_handback(
    state: PipelineState,
) -> Literal["node_geometry_admission", "node_deliver_sound", "node_cfd_wrap", "__end__"]:
    """After the ONE handback: the re-mesh, or the end the mesher already wrote.

    A handback that COMMITTED a repair promoted new geometry, so the run goes back through the
    admission gate - which re-measures the repaired file - and on to one more build and one more
    mesh. A handback that committed nothing changed no bytes, so re-meshing would run the same
    mesh again for the same answer: the run ends exactly where `route_after_executor` would have
    ended it, with the handback's own record attached (`_sound_or_end`, so an earlier sound mesh
    is still delivered).
    """
    record = state.get("repair_handback") or {}
    if record.get("committed"):
        logger.warning("route_after_handback: the scoped repair committed -> one re-mesh through "
                       "the admission gate - job_id=%s", state.get("job_id"))
        return "node_geometry_admission"
    logger.warning("route_after_handback: the scoped repair committed nothing (%s) -> the mesher's "
                   "own refusal stands - job_id=%s", record.get("outcome"), state.get("job_id"))
    return _sound_or_end(state)


def route_after_executor(
    state: PipelineState,
) -> Literal["node_reviewer", "node_classifier", "node_guard_replay", "node_deliver_sound",
             "node_cfd_wrap", "node_repair_handback",
             "__end__"]:
    success            = state.get("executor_success", False)
    retry_count        = state.get("retry_count", 0)
    solvability_failed = state.get("solvability_failed", False)

    if success:
        from meshpipeline.engines import resolution_policy as _rp
        caveats = state.get("requirement_caveats") or []
        changeable = _rp.retry_can_change(caveats, state)
        if caveats and changeable and retry_count <= bcfg.MAX_BUILDER_RETRIES:
            # a QUALITY-passing mesh that near-missed a stated requirement, or a sound mesh short
            # of the resolution floor with a rung of the ladder left (LAD-cfd-1): while attempts
            # remain, the ladder retries for FULL conformance - delivery-with-caveats is the
            # floor at exhaustion, never a first-attempt shortcut (invariant I5). The
            # classifier hands the builder the gate's own measured diagnostic.
            logger.info(
                "route_after_executor: quality PASS with %d requirement caveat(s), "
                "attempt=%d/%d → classifier (retrying for full conformance)",
                len(caveats), retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS)
            return "node_classifier"
        if caveats and changeable:
            # A SWITCH DOES NOT SPEND THE LADDER (LAD-cfd-2): the engine the run moved to built a
            # sound mesh short of the floor on its first try - it still gets one rung, inside the
            # run's ceiling of attempts. The time left never decides it (TIME-1)
            ok, why = _rp.rung_after_switch(state,
                                            attempts_ceiling=bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
                                            attempt_seconds=float(bcfg.BUILDER_LOOP_TIMEOUT))
            if ok:
                logger.info("route_after_executor: sound mesh short of the floor on the engine "
                            "the run switched to - its one rung (LAD-cfd-2) → classifier, "
                            "attempt=%d/%d - job_id=%s", retry_count,
                            bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, state.get("job_id"))
                return "node_classifier"
            logger.info("route_after_executor: no rung after the switch (%s) - job_id=%s", why,
                        state.get("job_id"))
        if not state.get("user_dispute") and _rp.engine_choice(state) is not None:
            # THE BETTER OF TWO ENGINES' MESHES (CFDREQ-28): a gate moved the run on and both engines built a
            # sound mesh - the run ends on the better by the product's own measured verdict (the first on a
            # tie), and the one not chosen is said on the result
            logger.warning("route_after_executor: sound meshes from two engines → node_deliver_sound, the better "
                           "by the measured verdict (%s) - job_id=%s", _rp.ENGINE_BETTER_OF_TWO,
                           state.get("job_id"))
            return "node_deliver_sound"
        if caveats and _rp.better_earlier_sound(state) is not None:
            # THE BEST SOUND MESH IS DELIVERED (LAD-cfd-3): this attempt's rung came out with
            # fewer cells across the narrowest gap than an earlier sound mesh of the run
            logger.warning("route_after_executor: ladder spent, and an earlier sound mesh "
                           "resolves the narrowest gap better → node_deliver_sound (LAD-cfd-3) "
                           "- job_id=%s", state.get("job_id"))
            return "node_deliver_sound"
        if caveats:
            logger.warning(
                "route_after_executor: quality PASS with %d requirement caveat(s) and the "
                "ladder spent → reviewer (caveated delivery candidate) - job_id=%s",
                len(caveats), state.get("job_id"))
        return "node_reviewer"

    # A GUARD CLOCK STOPPED THE ATTEMPT (docs/RULES.md TIME-3): an infrastructure stop, never a
    # result about the shape. It never reaches the classifier or the fallback ladder - no lever, no
    # coarser plan, no other engine. The same attempt is replayed once; a second trip ends the run
    # as ran out of time.
    if guard_trip_of(state):
        from meshpipeline.engines import resolution_policy as _rp
        if not state.get("user_dispute") and _rp.deliverable_sound(state) is not None:
            # A KEPT MESH IS HANDED BACK WHEN THE NEXT ATTEMPT RUNS OUT OF TIME (CFDREQ-28): a replay would spend
            # the same time again with a mesh already in hand (abc 00025560: cfMesh ran into the wall clock and
            # the kept snappyHexMesh mesh was never delivered)
            logger.warning("route_after_executor: a guard clock stopped the attempt and the run holds a kept "
                           "sound mesh → node_deliver_sound (%s) - job_id=%s", _rp.ENGINE_BETTER_OF_TWO,
                           state.get("job_id"))
            return "node_deliver_sound"
        if _after_guard_trip(state) == "replay":
            return "node_guard_replay"
        logger.warning("route_after_executor: a guard clock stopped the attempt again after its "
                       "replay → END (ran out of time), unless an earlier attempt built a sound "
                       "mesh (LAD-cfd-3) - job_id=%s", state.get("job_id"))
        return _sound_or_end(state)

    # THE MESHER NAMED THE FACES IT FAILED ON (MESH-2) and a geometry check had already flagged
    # them: the one refusal that tells repair what the mesher actually disliked instead of leaving
    # it to guess. ONE scoped repair of those faces and ONE re-mesh (pipeline/repair_handback.py),
    # latched by `repair_handback` on state so a job gets one ever. Every other refusal, and every
    # refusal at all with the posture off, routes exactly as it did.
    _handback, _why = _handback_mod.wanted(state)
    if _handback:
        logger.warning("route_after_executor: the mesh failed at the faces a check flagged "
                       "(MESH-2) -> node_repair_handback, once - job_id=%s", state.get("job_id"))
        return "node_repair_handback"
    if _why not in (_handback_mod.NOT_LIVE, _handback_mod.NOT_A_CAD_DEFECT):
        logger.info("route_after_executor: no mesher handback (%s) - job_id=%s", _why,
                    state.get("job_id"))

    if _wrap.tried(state):
        # THE WRAP RUNG IS THE LAST (LAD-cfd-5): a failure on the wrapped surface moves to the
        # rung's next engine on the same skin; with none left the run ends - on an earlier sound
        # mesh when there is one, else on the failure
        from meshpipeline.pipeline.engine_fallback import refused_as_wrong_region
        if _wrap.next_step(state) and not refused_as_wrong_region(state):
            logger.warning("route_after_executor: the wrapped surface did not mesh on %s → the "
                           "rung's next engine - job_id=%s", state.get("engine"), state.get("job_id"))
            return "node_cfd_wrap"
        logger.warning("route_after_executor: the wrapped surface did not mesh on any engine → END "
                       "- job_id=%s", state.get("job_id"))
        return _sound_or_end(state)

    # RETRY ONLY WHAT A RETRY CAN FIX. Some failures are decided by inputs the next attempt
    # receives unchanged: a case authored from the approval disagrees with it (the next attempt
    # writes the same case), or the geometry was refused (the same file comes back). Job ac1daa3e
    # spent a second 13-minute mesh on exactly such a failure. The cause the executor recorded
    # decides; an unknown or absent cause keeps the ladder as it was.
    _cause = state.get("executor_failure_cause", "") or ""
    if not retry_can_help(_cause, state.get("executor_failure_facts") or {}):
        # ...the SAME engine's next attempt. Another engine may change it (its declared limit -
        # a passage count, a form - is not the next engine's): when the ladder moves the run now,
        # the classifier and the ladder take it (engines/fitness ENG-5)
        if retry_count <= bcfg.MAX_BUILDER_RETRIES and another_engine_takes_over(state):
            logger.info("route_after_executor: executor FAIL with cause %r, which another attempt "
                        "on this engine cannot change - another engine takes over → classifier "
                        "- job_id=%s", _cause, state.get("job_id"))
            return "node_classifier"
        logger.warning(
            "route_after_executor: executor FAIL with cause %r, which another attempt cannot "
            "change - not retrying (attempt=%d/%d) → END - job_id=%s",
            _cause, retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, state.get("job_id"))
        return _sound_or_end(state)

    # A RETRY CHANGES WHAT WAS REFUSED, OR IT DOES NOT RUN (LAD-cfd-8): a gate this engine's next
    # attempt has nothing to answer with (engine_fallback.GATE_LEVERS) moves the run to the next engine
    # at once when one delivers the approved mesh; with none, the run's best sound mesh - this one,
    # delivered with the refusal said, unless an earlier one is better - is delivered, never rebuilt
    from meshpipeline.pipeline.engine_fallback import refusal_without_lever, refused_as_wrong_region
    if refused_as_wrong_region(state) and not state.get("user_dispute"):
        # ...and a mesh of another region than the fluid is the staging's: no retry, no engine, no wrap
        logger.warning("route_after_executor: the refusal is the staging's (the mesh filled another region than "
                       "the fluid, or its openings were bound to other faces than the ones confirmed) - every "
                       "engine stages it the same way, so no other runs; the sound mesh, if any, is delivered with "
                       "the refusal said (LAD-cfd-8) - job_id=%s", state.get("job_id"))
        return _sound_or_end(state)
    if refusal_without_lever(state) and not state.get("user_dispute"):
        if retry_count <= bcfg.MAX_BUILDER_RETRIES and another_engine_takes_over(state):
            logger.info("route_after_executor: %s refused the mesh and this engine has nothing to "
                        "change for it - another engine takes over (LAD-cfd-8) → classifier - "
                        "job_id=%s", state.get("executor_failed_gate"), state.get("job_id"))
            return "node_classifier"
        logger.warning("route_after_executor: %s refused the mesh, no retry can change that and no "
                       "other engine delivers the approved mesh - the run's best sound mesh, if any, "
                       "is delivered with the refusal said (LAD-cfd-8) - job_id=%s",
                       state.get("executor_failed_gate"), state.get("job_id"))
        return _sound_or_end(state)

    if retry_count <= bcfg.MAX_BUILDER_RETRIES:
        logger.info(
            "route_after_executor: executor FAIL attempt=%d/%d → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    # All attempts exhausted and the executor never produced a VALIDATED mesh
    # (contract / manifest / solvability gates all count). Such a mesh is not
    # deliverable, so the reviewer - which judges the VISUAL quality of an
    # already-valid mesh - must be bypassed. Routing an un-validated mesh to the
    # reviewer is exactly how an unchecked mesh could be PASS'd and shipped; end the
    # graph, and let the application render the honest failure from durable facts -
    # unless an EARLIER attempt validated a sound mesh, which is delivered (LAD-cfd-3).
    logger.warning(
        "route_after_executor: executor FAIL all %d attempts exhausted "
        "(solvability_failed=%s) → END (TERMINAL - mesh never validated, "
        "reviewer bypassed) - job_id=%s",
        bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, solvability_failed, state.get("job_id"),
    )
    return _sound_or_end(state)


async def node_deliver_sound(state: PipelineState) -> dict:
    """LAD-cfd-3: THE RUN DELIVERS ITS BEST SOUND MESH (resolution_policy.deliverable_sound). The
    last attempt failed and none is left - or it is sound but resolves the narrowest gap less well -
    and an earlier attempt's mesh passed every blocking gate and the trial solve: the run is moved
    back onto that attempt (its workspace, manifest, engine, parameters and notes) and goes to the
    review as that mesh, exactly as a caveated delivery candidate does. Nothing is re-meshed or
    re-judged here; the attempt that failed stays on the ladder's record."""
    from meshpipeline.engines import resolution_policy as _rp
    from meshpipeline.pipeline import engine_fallback as _lad
    best = _rp.deliverable_sound(state)
    if best is None:                    # the route asked first; a vanished mesh ends as it was
        return {}
    _last_sound = bool(state.get("executor_success"))
    ladder = (dict(state.get("engine_ladder") or {}) if _last_sound
              else _lad.with_attempt(state, _lad.classify(state)))
    ladder["delivered_from"] = {
        "attempt": best.get("attempt"), "engine": best.get("engine"),
        "rule": _rp.LAD_DELIVER_SOUND,
        "why": ((f"attempt {state.get('retry_count')} resolved the narrowest gap less well than "
                 f"attempt {best.get('attempt')}") if _last_sound else
                (f"the {best.get('refused')} check refused attempt {best.get('attempt')}'s sound mesh, "
                 "and no retry or other engine could change that (LAD-cfd-8)") if best.get("refused") else
                (f"attempt {state.get('retry_count')} did not pass the automatic checks, and "
                 f"attempt {best.get('attempt')} built a sound mesh")),
        "short_of": [str(c.get("kind")) for c in best.get("requirement_caveats") or []]}
    _choice = _rp.engine_choice(state)
    if _choice is not None:
        # CFDREQ-28: the mesh of the other engine that was not chosen, and why, in one plain line
        ladder["delivered_from"].update(why=_choice["line"], rule=_choice["rule"])
    try:
        from meshpipeline.contracts import decision_record as dr
        dr.cite(str(state.get("job_id") or ""), stage="resolution_ladder", decided="delivered sound",
                value=best.get("attempt"), rule=_rp.LAD_DELIVER_SOUND,
                inputs={("last_attempt" if _last_sound else "failed_attempt"):
                        state.get("retry_count"),
                        "notes": [c.get("kind") for c in best.get("requirement_caveats") or []]},
                words=("the run's sound mesh that resolves the narrowest gap best is delivered"
                       if _last_sound else
                       "the last attempt failed; the best sound mesh of the run is delivered"))
    except Exception:  # noqa: BLE001 - a record never changes the delivery
        logger.debug("node_deliver_sound: why record not kept", exc_info=True)
    logger.warning("node_deliver_sound: delivering attempt %s (%s) - the last attempt (%s) failed "
                   "at %s - job_id=%s", best.get("attempt"), best.get("engine"),
                   state.get("retry_count"), state.get("executor_failed_gate") or "finalize",
                   state.get("job_id"))
    return {
        "engine": str(best.get("engine") or state.get("engine") or ""),
        "engine_params": dict(best.get("engine_params") or {}),
        "engine_ladder": ladder,
        "openfoam_workspace": str(best.get("workspace")),
        "mesh_manifest": dict(best.get("manifest") or {}),
        "executor_success": True,
        "executor_failed_gate": "",
        "executor_failure_cause": "",
        "executor_failure_facts": {},
        "solvability_failed": False,
        "requirement_caveats": [dict(c) for c in best.get("requirement_caveats") or []],
        "api_failure": "",
    }


def limits_end_the_review(state: PipelineState) -> bool:
    """VER-5: once the measured limits decide (VERDICT_BY_LIMITS), the review of a mesh every gate
    passed ends the run whatever it said - and whether or not it concluded. Its points are notes;
    the verdict is the measured one, worked out at terminal assembly. A user's dispute is the user's
    own decision and keeps its routing."""
    from meshpipeline.settings import policy as _polcfg
    return bool(_polcfg.VERDICT_BY_LIMITS and state.get("executor_success") is True
                and not state.get("executor_failed_gate") and not state.get("user_dispute"))


def route_after_reviewer(
    state: PipelineState,
) -> Literal["__end__", "node_classifier", "node_failure_handler", "node_review_retry"]:
    if limits_end_the_review(state):
        # THE REVIEW ONLY ADDS NOTES, AND A REVIEW THAT DID NOT CONCLUDE ADDS NONE (VER-5). No rerun
        # of a review that cannot change the outcome, no failure because a model was busy or down:
        # the measured limits decide pass, concerns or fail on the mesh the gates passed.
        logger.info("route_after_reviewer: the measured limits decide (VER-5) → END "
                    "(review %s, attempt=%d/%d) - job_id=%s",
                    ("did not conclude: " + str(state.get("api_failure"))) if state.get("api_failure")
                    else f"concluded {state.get('reviewer_verdict')}, a note only",
                    int(state.get("retry_count", 0) or 0), bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
                    state.get("job_id"))
        try:
            from meshpipeline.contracts import decision_record as dr
            dr.cite(str(state.get("job_id") or ""), stage="review_route", decided="review route",
                    value="end", rule="VER-5",
                    inputs={"review_concluded": not state.get("api_failure"),
                            "reviewer_verdict": str(state.get("reviewer_verdict") or ""),
                            "attempt": int(state.get("retry_count", 0) or 0)},
                    words="the review only adds notes: the measured limits decide this run")
        except Exception:  # noqa: BLE001 - a record never changes a route
            logger.debug("route_after_reviewer: why record not kept", exc_info=True)
        return _END
    if state.get("api_failure"):
        # A review that could not conclude says nothing about the mesh. When another look can
        # change that, look again rather than throw away a mesh that passed every gate.
        if review_can_rerun(state):
            return "node_review_retry"
        return "node_failure_handler"

    verdict     = state.get("reviewer_verdict", Verdict.FAIL)
    retry_count = state.get("retry_count", 0)

    _dispute = state.get("user_dispute") or {}
    if _dispute and retry_count == 0 and _dispute.get("mode") != "accept":
        # Dispute INITIAL review (of the parent job's mesh) in REBUILD mode: the user
        # wants a different mesh - this review exists to generate targeted feedback,
        # not to veto. Route to the classifier→builder rebuild regardless of verdict;
        # the POST-rebuild reviews (retry_count >= 1) follow the normal routing below.
        logger.info("route_after_reviewer: dispute initial review (verdict=%s) → rebuild "
                    "- job_id=%s", verdict, state.get("job_id"))
        return "node_classifier"

    if _dispute.get("mode") == "accept" and retry_count == 0:
        # ACCEPT mode: the user inspected the mesh and amended the ACCEPTANCE CRITERIA
        # (their statement joined the review brief). The reviewer has just re-judged the
        # SAME mesh against that corrected bar, so its verdict is HONOURED - pass and
        # deliver, or fail and fall through to the normal retry path. No forced rebuild:
        # rebuilding would throw away the very mesh the user said was good enough.
        logger.info("route_after_reviewer: dispute ACCEPT re-review (verdict=%s) - honouring "
                    "the verdict - job_id=%s", verdict, state.get("job_id"))

    from meshpipeline.settings import policy as _polcfg
    if _polcfg.VERDICT_BY_LIMITS:
        # THE REVIEW ONLY ADDS NOTES (docs/RULES.md VER-1..4). A mesh reaches the reviewer only
        # after every measured gate passed, so whatever the review says, the run ends here: the
        # measured limits decide pass or concerns at terminal assembly, and the review's points
        # are shown as notes. No rebuild, no bonus attempt, no wrong-problem failure. (A user's
        # dispute in rebuild mode, above, is the user asking for a rebuild, not the review.)
        logger.info("route_after_reviewer: review concluded (verdict=%s, a note only) → END; "
                    "the measured limits decide (attempt=%d/%d) - job_id=%s", verdict,
                    retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, state.get("job_id"))
        return _END

    # SHADOW (VERDICT_BY_LIMITS off): the review-led routing below still decides. Retired when the
    # limits verdict is switched on: asks_for_rebuild, concern_retry_spent and the bonus attempt.
    if verdict == Verdict.PASS:
        logger.info(
            "route_after_reviewer: PASS - job_id=%s attempt=%d/%d",
            state.get("job_id"), retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return _END

    # A REBUILD ONLY FOR A CHANGE THE BUILDER CAN MAKE. A review that did not pass but names no
    # cited, buildable change (agents/reviewer/review_policy) has nothing a rebuild could act on:
    # the run ends here and terminal assembly delivers the mesh with the concerns listed - or,
    # for a confirmed wrong-problem finding, fails it with the class named.
    from meshpipeline.agents.reviewer.review_policy import asks_for_rebuild, concern_retry_spent
    if not asks_for_rebuild(state.get("reviewer_axis_findings")):
        logger.info("route_after_reviewer: review did not pass but asks for no buildable change "
                    "→ END (attempt=%d/%d) - job_id=%s", retry_count,
                    bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, state.get("job_id"))
        return _END

    # ONE IMPROVEMENT RETRY FOR CONCERNS. A review whose open points are all concerns (nothing
    # makes the mesh the wrong problem) buys at most one same-engine improvement retry per run;
    # after it the run ends and delivers its best valid mesh (application/review_delivery) with
    # the concerns listed. Job 0d0fc370 spent three attempts - and an engine switch - on one
    # layer-coverage concern over a production-grade mesh. (The retry stays on this engine: the
    # ladder never moves a run on a review - pipeline/engine_fallback.)
    if concern_retry_spent(state):
        logger.info("route_after_reviewer: only concerns left and the run's improvement retry for "
                    "concerns is spent → END, delivering the best valid mesh (attempt=%d/%d) "
                    "- job_id=%s", retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
                    state.get("job_id"))
        return _END

    if retry_count <= bcfg.MAX_BUILDER_RETRIES:
        logger.info(
            "route_after_reviewer: FAIL attempt=%d/%d → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    if retry_count == bcfg.MAX_BUILDER_RETRIES + 1 and state.get("executor_success", False):
        logger.info(
            "route_after_reviewer: FAIL attempt=%d/%d - granting reviewer-feedback retry → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    logger.warning(
        "route_after_reviewer: FAIL all %d attempts exhausted → END",
        bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
    )
    return _END



def _fenced(name: str, fn):
    async def _wrapped(state):
        # The ONE place every node is admitted. A worker whose generation/token was superseded is
        # refused HERE, before the node runs: no model call, no native launch, no workspace
        # mutation, and no result of its accepted into state. Placing it in the shared wrapper
        # (rather than in each node) means a NEW node is fenced by construction.
        from meshpipeline.application import execution_fence as _fence
        await _fence.assert_current_owner(f"graph node {name}")
        return await fn(state)

    _wrapped.__name__ = f"fenced_{name}"
    return _wrapped


def build_graph(checkpointer):
    if checkpointer is None:
        raise ValueError(
            "build_graph: checkpointer is required - pass a MemorySaver or "
            "an AsyncPostgresSaver obtained via 'async with AsyncPostgresSaver.from_conn_string(...)'"
        )

    b = StateGraph(PipelineState)

    # Every node runs inside a trace SPAN via the module-level, best-effort `_traced` wrapper
    # (the ONE shared instrumentation point - nodes stay uninstrumented). See _traced for the
    # fault-isolation contract.
    b.add_node("node_intake",          _fenced("node_intake", node_intake))
    b.add_node("node_engine_select",   _fenced("node_engine_select", node_engine_select))
    b.add_node("node_repair_inspect",   _fenced("node_repair_inspect", node_repair_inspect))
    b.add_node("node_repair_attempt",   _fenced("node_repair_attempt", node_repair_attempt))
    b.add_node("node_repair_handback",  _fenced("node_repair_handback", node_repair_handback))
    b.add_node("node_repair_deliver",   _fenced("node_repair_deliver", node_repair_deliver))
    b.add_node("node_geometry_admission", _fenced("node_geometry_admission", node_geometry_admission))
    b.add_node("node_builder",         _fenced("node_builder", node_builder))
    b.add_node("node_executor",        _fenced("node_executor", node_executor))
    b.add_node("node_classifier",      _fenced("node_classifier", node_classifier))
    b.add_node("node_engine_fallback", _fenced("node_engine_fallback", node_engine_fallback))
    b.add_node("node_reviewer",        _fenced("node_reviewer", node_reviewer))
    b.add_node("node_failure_handler", _fenced("node_failure_handler", node_failure_handler))
    b.add_node("node_infra_retry",     _fenced("node_infra_retry", node_infra_retry))
    b.add_node("node_review_retry",    _fenced("node_review_retry", node_review_retry))
    b.add_node("node_guard_replay",    _fenced("node_guard_replay", node_guard_replay))
    b.add_node("node_deliver_sound",   _fenced("node_deliver_sound", node_deliver_sound))
    b.add_node("node_cfd_wrap",        _fenced("node_cfd_wrap", node_cfd_wrap))

    b.add_edge(START, "node_intake")
    b.add_conditional_edges(
        "node_intake",
        lambda s: "node_failure_handler" if s.get("api_failure") else "node_engine_select",
        {"node_engine_select": "node_engine_select", "node_failure_handler": "node_failure_handler"},
    )
    b.add_conditional_edges(
        "node_engine_select",
        route_after_engine_select,
        {"node_repair_inspect": "node_repair_inspect", "node_reviewer": "node_reviewer"},
    )
    # INSPECT, THEN FIX, THEN JUDGE. Inspection is evidence and the attempt refuses rather than
    # ending a run, so both have a single unconditional edge and the admission gate still decides -
    # on whichever geometry the attempt left in place.
    b.add_edge("node_repair_inspect", "node_repair_attempt")
    # ...UNLESS THE JOB IS THE REPAIR. A standalone repair job's deliverable is the repaired CAD
    # and the goal's own finishing verdict, so it ends here and never reaches a mesher (spec
    # amendment 1). Every other run - meshing, benchmark, no goal at all, or any deployment with
    # `cad-repair-standalone-jobs` off - takes the one unconditional edge it always took.
    b.add_conditional_edges(
        "node_repair_attempt",
        route_after_repair_attempt,
        {"node_repair_deliver": "node_repair_deliver",
         "node_geometry_admission": "node_geometry_admission"},
    )
    # THE STANDALONE JOB'S END. The node states the ending; the orchestrator's terminal path
    # delivers the bytes and the report it named, because that is where this worker's fence is.
    b.add_edge("node_repair_deliver", END)
    b.add_conditional_edges(
        "node_geometry_admission",
        route_after_geometry_admission,
        {"node_builder": "node_builder", "node_executor": "node_executor"},
    )

    b.add_conditional_edges(
        "node_builder",
        route_after_builder,
        {"node_executor": "node_executor", "node_failure_handler": "node_failure_handler",
         "node_infra_retry": "node_infra_retry", "node_cfd_wrap": "node_cfd_wrap", END: END},
    )
    b.add_edge("node_infra_retry", "node_builder")
    b.add_conditional_edges(
        "node_executor",
        route_after_executor,
        {
            "node_reviewer":      "node_reviewer",
            "node_classifier":    "node_classifier",
            "node_guard_replay":  "node_guard_replay",
            "node_deliver_sound": "node_deliver_sound",
            "node_cfd_wrap":      "node_cfd_wrap",
            "node_repair_handback": "node_repair_handback",
            END:                  END,
        },
    )
    # THE MESHER'S HANDBACK (MESH-2): one scoped repair, then one re-mesh through the admission
    # gate - or the refusal the mesher already wrote, when the repair committed nothing
    b.add_conditional_edges(
        "node_repair_handback",
        route_after_handback,
        {"node_geometry_admission": "node_geometry_admission",
         "node_deliver_sound": "node_deliver_sound", "node_cfd_wrap": "node_cfd_wrap", END: END},
    )
    # A GUARD-CLOCK REPLAY runs the same attempt again (docs/RULES.md TIME-3)
    b.add_edge("node_guard_replay", "node_builder")
    # THE CFD LADDER'S LAST RUNG (LAD-cfd-5): one more attempt, on a wrapped copy of the surface
    b.add_edge("node_cfd_wrap", "node_builder")
    # THE NEWEST SOUND MESH, when the last attempt failed (LAD-cfd-3): reviewed and delivered as a
    # caveated delivery candidate is
    b.add_edge("node_deliver_sound", "node_reviewer")
    # THE FALLBACK LADDER sits between classifying a failure and the next attempt: it keeps the
    # engine, or moves the run to the next engine that delivers the same approved mesh
    # (pipeline/engine_fallback.py). It never ends a run, so its one edge is the builder.
    b.add_edge("node_classifier", "node_engine_fallback")
    b.add_edge("node_engine_fallback", "node_builder")
    b.add_conditional_edges(
        "node_reviewer",
        route_after_reviewer,
        {
            END:                    END,
            "node_classifier":      "node_classifier",
            "node_failure_handler": "node_failure_handler",
            "node_review_retry":    "node_review_retry",
        },
    )
    b.add_edge("node_review_retry", "node_reviewer")
    b.add_edge("node_failure_handler", END)

    return b.compile(checkpointer=checkpointer)
