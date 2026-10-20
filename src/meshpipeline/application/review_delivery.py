# Responsibility: Choose which valid mesh a finished run delivers, when it built more than one.
# Owns: the fixed ranking of a run's valid meshes by measured facts, the swap of the final state onto
#   the chosen one, and the account of which attempt was delivered and why.
# Boundaries: pure over the graph's final state and the attempt workspaces on disk; it re-runs
#   nothing, re-judges nothing, and leaves a run with no valid mesh exactly as it was.
"""DELIVER THE BEST VALID MESH, DON'T DISCARD IT.

A user who waited an hour for a mesh must not be told "it failed", or be handed a worse mesh, when
the run built a better one on an earlier attempt. A retry can come out worse - a later attempt
failing a gate, or another engine's mesh missing what the plan builds - and the graph ends on that
last attempt. Here every CANDIDATE is ranked and the best one is delivered.

CANDIDATES are the attempts that produced a complete, valid mesh: the executor validated it
(polyMesh complete, checkMesh's hard validity, the solvability gate - node_reviewer refuses any
other, so every reviewed mesh in state.review_history is one) and its manifest still reads with no
fatal defect. An invalid mesh is never delivered.

THE RANKING is a fixed order of MEASURED facts (rank_key), never an AI judgement:

  1. passes every fixed quality limit the engine declares (yes before no);
  2. the number of quality checks it fails (fewer first);
  3. the approved plan's requirements it misses (engine_fallback.plan_requirements: prism layers
     present on the wall, the wake refinement present; plus each measured requirement near-miss);
  4. the worst cells: max non-orthogonality, then max skewness (lower first) - read first to
     the whole degree (NON_ORTHO_STEP), where a review that PASSED breaks the tie, then exactly;
  5. layer coverage (higher first);
  6. the earlier attempt wins a tie.

The review's concerns are LISTED on the delivery, never ranked against a measured difference. The
one place a passed review counts is a tie on everything measured above to the whole degree:
two meshes 0.01 degrees apart are the same mesh to a solver, and a run that built one its review
passed must not deliver the other with the review's concerns (job 35d86388 delivered attempt 2,
55.79 degrees, with a concern, over attempt 3, 55.80, which passed). The one review fact that sets a
candidate aside is a CONFIRMED wrong-problem finding (agents/reviewer/review_policy: cited, backed
by an inspection, not contradicted by a measurement) - the only way a review fails a run at all,
so ranking such a mesh first would fail a run that holds a deliverable one. It only orders
candidates against each other; it never removes the last one.

THE DELIVERED ATTEMPT CARRIES ITS OWN ENGINE. A run the fallback ladder moved to another engine
ends with `engine` naming the LAST engine; the attempt delivered here may have been built by the
first. Job 0d0fc370 delivered snappyHexMesh's attempt 1 under cfMesh's name, and every upload was
refused ("manifest engine 'snappy' disagrees with the execution's engine 'cfmesh'"). So the swap
moves `engine` and `engine_params` onto the delivered attempt's (its review record, else its own
manifest), records the last attempt on the ladder under the engine that really ran it, and says on
the ladder record which attempt was delivered, by which engine, why it was chosen over the others,
and what it is short of (engine_fallback.delivered_note).

A dispute is left alone: it judges the engineer's own flags on the mesh they disputed.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _bare(marker: str) -> str:
    raw = str(marker or "").strip()
    if raw.startswith("<<API_FAILURE:") and raw.endswith(">>"):
        raw = raw[len("<<API_FAILURE:"):-2].strip()
    return raw


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


@dataclass(frozen=True)
class Candidate:
    """One attempt's complete, valid mesh and the measured facts it is ranked by."""
    attempt: int
    engine: str
    workspace: str
    manifest: Mapping
    #: the history entry it came from ({} for the run's last state when that was not reviewed)
    entry: Mapping = field(default_factory=dict)
    #: whether it is the run's own last state (nothing to swap)
    current: bool = False
    failed_checks: tuple[str, ...] = ()
    unmet: tuple[str, ...] = ()
    max_non_ortho: float | None = None
    max_skewness: float | None = None
    layer_coverage_pct: float | None = None
    wrong_problem: int = 0
    #: its own review did not pass (it left concerns) - read only on a measured tie (rank_key)
    review_left_concerns: bool = False

    def shortfalls(self) -> list[str]:
        """What this mesh is short of, measured, in plain words."""
        return [*self.failed_checks, *self.unmet]


#: the plain words for a plan requirement a candidate misses
_UNMET_WORDS = {"prism_layers": "no prism layers measured on the wall",
                "layers_unmeasured": "no prism-layer coverage was measured on the wall",
                "wake_refinement": "no wake refinement behind the body"}
#: Worst non-orthogonality is compared to this many degrees before a passed review breaks a tie.
NON_ORTHO_STEP = 1.0
#: The wall coverage (percent) at or under which a mesh counts as carrying no prism layers at all -
#: a stray layer cell or two is not the layered wall the plan builds.
_LAYERS_PRESENT_PCT = 1.0


def _quality_rows(engine: str, manifest: Mapping) -> list[dict]:
    from meshpipeline.engines.quality_criteria import (
        BUILD_TIME_KEYS,
        evaluate,
        measurements_from_manifest,
    )
    try:
        rows = evaluate(engine, measurements_from_manifest(dict(manifest)))
    except Exception:  # noqa: BLE001 - an engine that cannot be evaluated ranks on the rest
        return []
    return [r for r in rows if r["key"] not in BUILD_TIME_KEYS]


def _measure(state: Mapping, *, attempt: int, engine: str, workspace: str, manifest: Mapping,
             entry: Mapping, current: bool, findings: list, caveats: list) -> Candidate | None:
    """The candidate this mesh makes, or None when it is not a complete, valid mesh."""
    raw = manifest.get("quality")
    quality: Mapping = raw if isinstance(raw, Mapping) else {}
    if quality.get("fatal"):
        return None                         # a broken mesh is never delivered
    rows = _quality_rows(engine, manifest)
    failed = tuple(f"{r['label']} (measured {r['measured']}, limit {r['ok_when']})"
                   for r in rows if r["passed"] is False)
    from meshpipeline.pipeline.engine_fallback import REQ_PRISM_LAYERS, REQ_WAKE, plan_requirements
    unmet: list[str] = []
    coverage = _num(quality.get("layer_coverage_pct"))
    for req in plan_requirements(state):
        if req == REQ_PRISM_LAYERS and coverage is None:
            unmet.append(_UNMET_WORDS["layers_unmeasured"])
        elif req == REQ_PRISM_LAYERS and coverage is not None and coverage <= _LAYERS_PRESENT_PCT:
            unmet.append(_UNMET_WORDS[req])
        if req == REQ_WAKE:
            wake = quality.get("wake_refinement")
            if not (isinstance(wake, Mapping) and wake.get("enabled") and wake.get("tiers")):
                unmet.append(_UNMET_WORDS[req])
    unmet.extend(f"short of a stated requirement ({c.get('kind') or 'requirement'})"
                 for c in caveats if isinstance(c, Mapping))
    # the one review fact that orders candidates: a CONFIRMED wrong-problem finding (the only way a
    # review fails a run) - never a concern
    from meshpipeline.agents.reviewer.review_policy import blocking_findings
    blocking = len(blocking_findings([dict(f) for f in findings if isinstance(f, Mapping)]))
    verdict = str(entry.get("verdict") or (state.get("reviewer_verdict") if current else "") or "")
    return Candidate(attempt=attempt, engine=engine, workspace=workspace, manifest=manifest,
                     entry=entry, current=current, failed_checks=failed, unmet=tuple(unmet),
                     max_non_ortho=_num(quality.get("max_non_ortho")),
                     max_skewness=_num(quality.get("max_skewness")),
                     layer_coverage_pct=_num(quality.get("layer_coverage_pct")),
                     wrong_problem=blocking,
                     review_left_concerns=verdict.strip().upper() == "FAIL")


def rank_key(c: Candidate) -> tuple:
    """THE FIXED ORDER (lower first) - see the module docstring. An unmeasured cell metric ranks
    after every measured one; an unmeasured coverage counts as none."""
    inf = float("inf")
    return (c.wrong_problem > 0,
            bool(c.failed_checks),
            len(c.failed_checks),
            len(c.unmet),
            (math.floor(c.max_non_ortho / NON_ORTHO_STEP) if c.max_non_ortho is not None else inf),
            c.review_left_concerns,
            c.max_non_ortho if c.max_non_ortho is not None else inf,
            c.max_skewness if c.max_skewness is not None else inf,
            -(c.layer_coverage_pct or 0.0),
            c.attempt)


#: What each position of rank_key means, said as why the winner beat the runner-up.
_WHY = (
    "the review confirmed the other does not match what you asked for",
    "it passes every fixed quality limit and the other does not",
    "it fails fewer quality checks",
    "it meets more of the approved plan (layers, wake refinement, stated sizes)",
    "its worst cells are better shaped (lower non-orthogonality)",
    "its review passed and the other's left concerns, and they measure the same",
    "its worst cells are better shaped (lower non-orthogonality)",
    "its worst cells are better shaped (lower skewness)",
    "more of its wall is covered by prism layers",
    "it came first and measures the same",
)


def why_chosen(best: Candidate, other: Candidate) -> str:
    """The first measured fact, in the fixed order, that put `best` ahead of `other`."""
    for i, (x, y) in enumerate(zip(rank_key(best), rank_key(other), strict=True)):
        if x != y:
            return _WHY[i]
    return _WHY[-1]


def _read_manifest(ws: str) -> Mapping | None:
    try:
        data = json.loads((Path(ws) / "mesh_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _engine_of(manifest: Mapping, entry: Mapping, state: Mapping) -> str:
    """THE ENGINE THAT BUILT A MESH: the one its own manifest names (what the engine wrote, and
    what the uploader holds the delivery to), else its review record's, else the run's."""
    return (str(manifest.get("mesh_mode") or "") or str(entry.get("engine") or "")
            or str(state.get("engine") or ""))


def candidates(state: Mapping) -> list[Candidate]:
    """Every attempt of this run that produced a complete, valid mesh still on disk."""
    out: list[Candidate] = []
    here = str(state.get("openfoam_workspace") or "")
    api = _bare(str(state.get("api_failure") or ""))
    current_valid = (state.get("executor_success") is True
                     and not state.get("executor_failed_gate")
                     and (not api or api.startswith("reviewer_")))
    seen: set[str] = set()
    if current_valid and here:
        held = state.get("mesh_manifest")
        manifest = (held if isinstance(held, Mapping) and held else None) or _read_manifest(here)
        if manifest is not None:
            entry = next((h for h in reversed(state.get("review_history") or [])
                          if isinstance(h, Mapping) and str(h.get("workspace")) == here), {})
            c = _measure(state, attempt=int(state.get("retry_count") or 0),
                         engine=_engine_of(manifest, {}, state),
                         workspace=here, manifest=manifest, entry=entry, current=True,
                         findings=list(state.get("reviewer_axis_findings") or []),
                         caveats=list(state.get("requirement_caveats") or []))
            if c is not None:
                out.append(c)
                seen.add(here)
    for h in state.get("review_history") or []:
        if not (isinstance(h, Mapping) and h.get("verdict") in ("PASS", "FAIL")
                and h.get("workspace")):
            continue
        ws = str(h.get("workspace"))
        if ws in seen:
            continue
        manifest = _read_manifest(ws)
        if manifest is None:
            logger.warning("review_delivery: attempt %s's mesh is no longer readable at %s - not "
                           "a candidate", h.get("attempt"), ws)
            continue
        c = _measure(state, attempt=int(h.get("attempt") or 0),
                     engine=_engine_of(manifest, h, state),
                     workspace=ws, manifest=manifest, entry=h, current=False,
                     findings=list(h.get("findings") or []),
                     caveats=list(h.get("requirement_caveats") or []))
        if c is not None:
            out.append(c)
            seen.add(ws)
    return out


def _why_not_last(state: Mapping) -> str:
    last = int(state.get("retry_count") or 0)
    if state.get("executor_success") is not True or state.get("executor_failed_gate"):
        return f"attempt {last} did not pass the automatic checks"
    return f"attempt {last} measured worse"


def _account(state: Mapping, best: Candidate, pool: list[Candidate]) -> dict:
    """Which attempt is delivered, by which engine, why it beat the others, and what it is short
    of - the ladder record's `delivered_from` (engine_fallback.delivered_note says it)."""
    others = [c for c in pool if c is not best]
    if others:
        runner = min(others, key=rank_key)
        why = (f"it was chosen over attempt {runner.attempt} ({runner.engine}) because "
               f"{why_chosen(best, runner)}")
    else:
        why = f"it is the only valid mesh this run built - {_why_not_last(state)}"
    return {"attempt": best.attempt, "engine": best.engine, "why": why,
            "short_of": best.shortfalls(),
            "candidates": [{"attempt": c.attempt, "engine": c.engine,
                            "rank": [None if isinstance(x, float) and not math.isfinite(x) else x
                                     for x in rank_key(c)]} for c in pool]}


def _params_of(state: Mapping, best: Candidate) -> dict:
    """THE PARAMETERS THAT BUILT THE DELIVERED MESH: its review record's, else its own manifest's,
    else - when another engine built it and neither recorded them - that engine's declared
    parameters filled from the run's (never the last engine's own, which mean nothing to it)."""
    for raw in (best.entry.get("engine_params"), best.manifest.get("engine_params")):
        if isinstance(raw, Mapping) and raw:
            return dict(raw)
    given = dict(state.get("engine_params") or {})
    if best.engine == str(state.get("engine") or ""):
        return given
    try:
        from meshpipeline.engines.registry import resolve_engine_params
        return resolve_engine_params(best.engine, given)
    except Exception:  # noqa: BLE001 - an unknown engine keeps no parameters
        return {}


def select(state: Mapping) -> dict:
    """The final state to derive the run's outcome from: the given one, or the same run moved onto
    its best valid mesh. Fail-safe: anything unreadable keeps the given state."""
    out = dict(state)
    if state.get("user_dispute"):
        return out
    pool = candidates(state)
    if not pool:
        return out
    best = min(pool, key=rank_key)
    from meshpipeline.pipeline import engine_fallback as _lad
    if best.current:
        if len(pool) > 1:
            # the last attempt IS the best: delivered as it stands, with the account of why it
            # beat the earlier ones
            ladder = _lad.with_attempt(state, _lad.classify(state))
            ladder["delivered_from"] = _account(state, best, pool)
            out["engine_ladder"] = ladder
        return out
    entry = best.entry
    logger.info("review_delivery: delivering attempt %s (engine %s, rank %s) instead of the run's "
                "last state (engine %s, gates passed=%s, verdict=%s) - job_id=%s",
                best.attempt, best.engine, rank_key(best), state.get("engine"),
                state.get("executor_success"), state.get("reviewer_verdict"),
                state.get("job_id", "unknown"))
    # the ladder's record of the attempt that just ended, under the engine that REALLY ran it -
    # taken before the swap, so the closing record never files it under the delivered engine
    ladder = _lad.with_attempt(state, _lad.classify(state))
    ladder["delivered_from"] = _account(state, best, pool)
    out.update({
        # THE DELIVERED ATTEMPT'S ENGINE: the uploader holds the manifest to it, and the terminal
        # record, the ladder's account and the user's message all name it
        "engine": best.engine,
        "engine_params": _params_of(state, best),
        "engine_ladder": ladder,
        "openfoam_workspace": best.workspace,
        "mesh_manifest": dict(best.manifest),
        # THAT attempt's facts: it was validated by the executor (every review in the history was
        # of a validated mesh - node_reviewer refuses any other) and it concluded a review
        "executor_success": True,
        "executor_failed_gate": "",
        "executor_failure_cause": "",
        "executor_failure_facts": {},
        "solvability_failed": False,
        "reviewer_verdict": entry.get("verdict"),
        "reviewer_axis_findings": list(entry.get("findings") or []),
        "reviewer_feedback": str(entry.get("feedback") or ""),
        "reviewer_rebuild_required": False,
        "requirement_caveats": list(entry.get("requirement_caveats") or []),
        "api_failure": "",
    })
    return out


__all__ = ["Candidate", "candidates", "rank_key", "select", "why_chosen"]
