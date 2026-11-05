# Responsibility: Keep a mesh that is BUILT and has PASSED the executor's gates from being lost to
# anything that goes wrong after that point - a reviewer that crashes, a checkpoint write, a
# notification - by finishing the run from the last state the graph saved.
# Owns: running the graph, finishing it from its last saved state on a late crash, and the rescue rule.
# Boundaries: decides nothing about the mesh. The rescued state goes through the ordinary terminal
# path, where final_result.review_inconclusive_caveat delivers it with a note that the review did
# not conclude.
"""lp_base, dev-areen 25 Oct: a valid mesh three hours in the making passed every gate, then the
reviewer raised (`f"{None:g}"` in agents/reviewer/visual.py) and the job failed. kart_chassis the
same night: a reviewer PASS, then a checkpoint write met a dead connection, and the job failed. In
both the mesh was fine and was thrown away.

THE RULE: once the executor has passed the mesh, an exception in a later step never fails the job.
The run is finished from the latest state the graph saved (the checkpoint of the last step that
completed; in memory on a MemorySaver run), and delivered:
  - with no review verdict yet: marked `reviewer_crashed`, which the terminal path delivers as a
    mesh whose review did not conclude, saying so (final_result.review_inconclusive_caveat);
  - with a verdict already in hand (the crash came after the review): as that verdict says.
Never rescued: a run whose executor did not pass, a mesh a gate failed, a mesh the reviewer
REJECTED while the review still decides (a rebuild was under way; once the measured limits decide,
VER-5, the rejection is a note and the mesh is rescued), a dispute (the engineer's flags need a judgement), and a fence
refusal or a drain (another owner, or a hand-back, decides those)."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The non-verdict a rescued run carries when its review never concluded. A `reviewer_` marker, so
#: the delivery predicate treats it as a review that did not finish (never as a mesh failure).
REVIEWER_CRASHED = "reviewer_crashed"


def _never_rescued(exc: BaseException) -> bool:
    from meshpipeline.application.worker_handoff import WorkerDraining
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced
    return isinstance(exc, (StaleWorkerFenced, StaleExecutionPublish, WorkerDraining))


def rescue(state: dict | None, exc: BaseException) -> dict | None:
    """The state to finish the run from, when `exc` came after the executor passed the mesh in
    `state` (the latest state the graph saved); None when it must be raised as before."""
    if not isinstance(state, dict) or _never_rescued(exc):
        return None
    if state.get("executor_success") is not True or state.get("executor_failed_gate"):
        return None
    verdict = str(state.get("reviewer_verdict") or "").strip().upper()
    if state.get("user_dispute"):
        return None
    # A review FAIL blocks the rescue only while the review decides (a rebuild was under way). Once
    # the measured limits decide (VER-5) the review is a note and no rebuild follows it, so the mesh
    # the gates passed is delivered as the limits say.
    from meshpipeline.application.final_result import limits_decide
    if not limits_decide() and (verdict == "FAIL" or state.get("reviewer_rebuild_required")):
        return None
    out = dict(state)
    if not verdict:
        out["api_failure"] = REVIEWER_CRASHED
    return out


async def run_to_end(graph, graph_input, config, *, jlog: Any = logger) -> dict:
    """graph.ainvoke - and if a step raises after the executor passed the mesh, the run finishes
    from the latest state the graph saved (graph.aget_state: the checkpoint of the last step that
    completed, read through the retrying saver) instead of failing (see `rescue`). Otherwise, or
    when that state cannot be read, the exception is raised unchanged."""
    try:
        return await graph.ainvoke(graph_input, config=config)
    except Exception as exc:
        if _never_rescued(exc):
            raise
        try:
            snap = await graph.aget_state(config)
            latest = dict(getattr(snap, "values", None) or {})
        except Exception as read_exc:  # noqa: BLE001 - nothing to finish from: the crash stands
            jlog.warning("post-gate rescue: the last saved state could not be read (%s)", read_exc)
            raise exc from None
        rescued = rescue(latest, exc)
        if rescued is None:
            raise
        how = ("its review marked as not concluded" if rescued.get("api_failure") == REVIEWER_CRASHED
               else f"with its {rescued.get('reviewer_verdict')} verdict")
        jlog.error("A step after the executor passed the mesh failed (%s: %s) - the mesh is "
                   "delivered from the last saved state, %s", type(exc).__name__, exc, how)
        try:
            from meshpipeline.metrics import inc as _minc
            _minc("post_gate_rescue", "pipeline")
        except Exception:  # noqa: BLE001 - metrics never change an outcome
            pass
        return rescued


__all__ = ["REVIEWER_CRASHED", "rescue", "run_to_end"]
