# Responsibility: Choose which gate-passing mesh a finished run delivers, when it built more than one.
# Owns: the ranking of a run's reviewed meshes and the swap of the final state onto the chosen one.
# Boundaries: pure over the graph's final state and the attempt workspaces on disk; it re-runs
#   nothing, re-judges nothing, and leaves a run with no reviewed mesh exactly as it was.
"""DELIVER, DON'T DISCARD.

A user who waited an hour for a mesh must not be told "it failed" when the run built one that passed
every gate. A rebuild the review asked for can come out worse - a later attempt failing a gate, or
reviewed with more open points - and the graph ends on that last attempt. Here the run's CONCLUDED
reviews (state.review_history, one per reviewed mesh) are ranked and the best one is delivered:

  fewest wrong-problem findings, then fewest requested changes, then fewest concerns, then latest.

Two things are deliberately left alone: a run whose last mesh was validated but whose review did
not finish keeps its own delivery path (final_result.review_inconclusive_caveat), and a dispute
judges the engineer's own flags on the mesh they disputed.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


def _bare(marker: str) -> str:
    raw = str(marker or "").strip()
    if raw.startswith("<<API_FAILURE:") and raw.endswith(">>"):
        raw = raw[len("<<API_FAILURE:"):-2].strip()
    return raw


def _rank(entry: Mapping) -> tuple:
    rows = [f for f in (entry.get("findings") or []) if isinstance(f, Mapping)
            and f.get("passed") is False]
    blocking = sum(1 for f in rows if f.get("blocking"))
    improve = sum(1 for f in rows if f.get("improve") is True)
    return (blocking, improve, len(rows) - blocking, -int(entry.get("attempt") or 0))


def _reviewed(state: Mapping) -> list[Mapping]:
    return [h for h in (state.get("review_history") or [])
            if isinstance(h, Mapping) and h.get("verdict") in ("PASS", "FAIL")
            and h.get("workspace")]


def select(state: Mapping) -> dict:
    """The final state to derive the run's outcome from: the given one, or the same run moved onto
    its best reviewed, gate-passing mesh. Fail-safe: anything unreadable keeps the given state."""
    out = dict(state)
    history = _reviewed(state)
    if not history or state.get("user_dispute"):
        return out
    api = _bare(str(state.get("api_failure") or ""))
    if api.startswith("reviewer_") and state.get("executor_success") is True:
        return out          # the review of a validated mesh did not finish: its own path decides
    current = None
    if (state.get("executor_success") is True and not state.get("executor_failed_gate")
            and not api and state.get("reviewer_verdict") in ("PASS", "FAIL")):
        here = str(state.get("openfoam_workspace") or "")
        current = next((h for h in reversed(history) if str(h.get("workspace")) == here), None)
    best = min(history, key=_rank)
    if current is not None and _rank(current) <= _rank(best):
        return out
    ws = Path(str(best.get("workspace")))
    try:
        manifest = json.loads((ws / "mesh_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("review_delivery: the best reviewed mesh (attempt %s) is no longer readable "
                       "at %s - keeping the run's own outcome", best.get("attempt"), ws)
        return out
    logger.info("review_delivery: delivering attempt %s (rank %s) instead of the run's last "
                "state (gates passed=%s, verdict=%s, api_failure=%s)", best.get("attempt"),
                _rank(best), state.get("executor_success"), state.get("reviewer_verdict"),
                api or "-")
    out.update({
        "openfoam_workspace": str(ws),
        "mesh_manifest": manifest,
        # THAT attempt's facts: it was validated by the executor (every review in the history was
        # of a validated mesh - node_reviewer refuses any other) and it concluded a review
        "executor_success": True,
        "executor_failed_gate": "",
        "executor_failure_cause": "",
        "executor_failure_facts": {},
        "solvability_failed": False,
        "reviewer_verdict": best.get("verdict"),
        "reviewer_axis_findings": list(best.get("findings") or []),
        "reviewer_feedback": str(best.get("feedback") or ""),
        "reviewer_rebuild_required": False,
        "requirement_caveats": list(best.get("requirement_caveats") or []),
        "api_failure": "",
    })
    return out


__all__ = ["select"]
