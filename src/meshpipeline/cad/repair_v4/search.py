# Responsibility: The v4 repair search - best-first over Workspace states ranked by the verdict
#                 score, the deterministic sweep that proposes ops, the budget, and the delivery rule.
# Owns: `Candidate`, `Generator`, `DetGenerator`, `Budget`, `Judge`, `SearchState`, `Rejection`,
#       `SearchResult`, `search`, `deliverable`.
# Boundaries: the verdict (Task 5), site finding (Task 17), the stages (Task 16) and the AI proposer (Task 19)
#             are NOT reimplemented here; this module WIRES them (see "Wiring" below). Ops come from
#             `ops.REGISTRY` and are checked by `validate_call` before they run.
"""Best-first search over Workspace states. The reward IS the metric.

    expand the best state -> every generator proposes `Candidate`s -> `validate_call` each ->
    apply the valid ones in parallel on forks of the state -> `cheap_verdict` each child ->
    push the children by `score` -> confirm the best `k_confirm` new children with the full panel.

The deterministic verifier decides; a generator (rules or a model) only proposes. A proposal that is
wrong - an unknown op, an out-of-bounds parameter, a target a previous op removed, an op that
declines or fails - is never a crash and never silently dropped: it becomes a `Rejection` handed
back to the generator that made it (`on_rejected`, if it has one) and an event in the `Trace`.

The frontier is ordered by `rank`. A confirmed state ranks by its FULL score. An unconfirmed state
cannot be compared by its cheap score (the cheap panel is a subset), so its rank is its parent's
rank moved by the cheap-score difference between child and parent: "this child is as much better
than its parent as the cheap tier says". Only fully confirmed states can become `best` (and so be
delivered). A generator or judge call that raises is recorded in the trace and skipped; it never
discards the best state found so far.

Budget. `ops` counts op applications (one per validated candidate that is run); `wall_s` is
elapsed time; `model_calls` / `model_usd` are read from generators that expose
`model_usage() -> (calls, usd)` (cumulative). `wall_s` and `ops` stop the search, checked before
every expansion; `ops` also truncates a batch so it is never overrun. Reaching `model_calls` or
`model_usd` does NOT stop it: the generators that expose `model_usage` are dropped, the search goes
on deterministic-only, and the result is marked `ai_capped`. A cap of 0 therefore means "no AI",
which is how the deterministic-only arm runs. Patience is the
number of consecutive expansions that did not improve the best confirmed score.

Wiring (Task 18b).
  * FEEDBACK. A generator with `feedback(list[proposer.Outcome])` (the AI proposer) is told, after each
    round, what became of ITS OWN candidates: the Diff, and the verdict delta (cheap tier against the parent's
    cheap score, or full against full once confirmed) for the ones that became states, the reason for the rest.
  * ONE `UnfixableRegistry` OBJECT is shared by the AI proposer and every `DetGenerator` (`ai_generators`
    builds them so; `search` refuses a mismatch), so a site the model declared unfixable is not swept either.
    What it HOLDS is per lineage: a declaration is keyed `(declaring state, site id)` and seen by that state
    and its descendants only. Site ids are not globally unique (two siblings mint the same id for different new
    geometry), so a verdict on branch A must not remove branch B's site. `viewed` ("picture already drawn") is
    per lineage for the same reason.
  * SITE IDS. Site ids are carried across rounds by a `SiteRegistry`, which follows ONE LINEAR LINEAGE and
    raises on a fork. A best-first search forks constantly, so the registry is per STATE, not per search: a
    state inherits (by reference) the registry as its parent left it, and on expansion works on a DEEP COPY of
    it, applies the state's own sites to the copy, and hands that copy to the children it makes. Every
    registry therefore only ever sees one root-to-state path, however the frontier jumps between branches.
  * STAGES. `Candidate(op="run_stage", targets=[], params={"stage": name, "params": {...}})` is routed to
    `stages.run_stage`; it is one candidate (one op of budget) and one state.
  * VIEWS. For a model generator, sites of the kinds the proposer shows pictures for are drawn once with
    `render.view(pool, ws, site)`.
  * STOP REASON. `SearchResult.ai_stop_reason` is the proposer's own reason (`file_ceiling`, `input_too_large`,
    `model_failure`, ...) or `model_budget` when this driver capped it; `ai_capped` is true for a spending
    limit only, so `input_too_large` and `model_failure` are NOT `ai_capped`.
  * INFRASTRUCTURE IS NOT A VERDICT. `InconclusiveVerdict` (from `score`) and `CheckerUnavailable` (the STEP
    export, the original's facts, the re-run layer 3, or layer 1/3 of the checker) are retried, then the
    candidate is an `inconclusive` outcome: traced, never ranked, never counted, never told to the proposer as
    a failure (it learns nothing from a dead worker), and never a rejection. A genuine candidate failure
    (`OpFailed`, a verdict that scores badly) is still a failure and is still fed back. `judge.full(start)` is
    NOT guarded: a checker that cannot judge the START propagates out of `search`, because there is nothing to
    search from; callers must treat that as "no result", not as "not clean".
  * NO VERDICT. A search in which no candidate got a usable verdict and at least one was inconclusive ends with
    `reason="no_verdict"`, `infrastructure_failed=True`; `inconclusive_candidates` / `verdict_candidates` /
    `inconclusive_rate` let a mixed run be weighed. It is never "patience".
  * KNOWN PROPERTIES, not defects of this task. (1) One `AIProposer` keeps ONE message list for the whole
    search, so the model's conversation is not per lineage: after the search jumps between branches it has been
    shown states from several, and its memory of "what I tried" mixes them (the feedback it is sent names the
    state each candidate became). (2) `Budget.model_usd` is an ADVISORY TRIGGER, not a limit: it is checked once per
    EXPANSION, so one `propose` may make up to `max_turns` calls before it is looked at again. The hard per-file
    ceiling is the proposer's `usd_ceiling`, enforced per call by `ledger.admit` against a deliberately
    pessimistic estimate (uncached input rate, full output reservation, growth floor). (2b) The proposer's
    `_pending` outcomes are flushed by the NEXT `propose` call, not by the branch that produced them.
    (3) `render.view` runs synchronously on the expansion thread. It is asked only while a model generator can
    still look (`_will_look`: not stopped, turns left), and for at most
    `MAX_VIEWS_PER_EXPANSION` sites per expansion. A render that raises is traced as `view_unavailable`
    (infrastructure; never `judge_error`, never a candidate outcome, never scored).
"""

from __future__ import annotations

import copy
import heapq
import itertools
import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from meshpipeline.cad.repair_v4 import render
from meshpipeline.cad.repair_v4.measurement import NO_VERDICT, file_unmeasured, no_verdict_failure
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.proposer import (
    IMAGE_KINDS,
    AIProposer,
    Outcome,
    ProposedCandidate,
    UnfixableRegistry,
)
from meshpipeline.cad.repair_v4.sites import SiteRegistry, find_sites
from meshpipeline.cad.repair_v4.stages import (
    DEFAULTS,
    STAGES,
    declared_edits,
    original_facts,
    run_stage,
    step_for,
)
from meshpipeline.cad.repair_v4.trace import Trace
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.verdict.assemble import (
    InconclusiveVerdict,
    Verdict,
    cheap_verdict,
    clean_of,
    score,
    verdict,
)
from meshpipeline.cad.repair_v4.verdict.faithful import DeclaredIdError, FaithfulReport, check_faithful
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.workspace import Diff, UnknownTarget, Workspace

#: the op name of the macro action: `params={"stage": name, "params": {...}}`, no targets
RUN_STAGE = "run_stage"
#: how many times a judgement that came back inconclusive is attempted before the candidate is given up on
JUDGE_ATTEMPTS = 2

Score = tuple[int, int, float, float, int]


@dataclass
class Candidate:
    op: str
    targets: list[StableId]
    params: dict
    source: Literal["det", "ai"]
    rationale: str = ""


@dataclass
class Rejection:
    """A proposal that did not become a state, told back to whoever proposed it. `stage` is
    "validate" (the call is malformed or names a dead target), "declined" (the op ran and chose to
    change nothing), "failed" (worker timeout/crash/error) or "error" (an unexpected exception)."""

    candidate: Candidate
    stage: Literal["validate", "declined", "failed", "error"]
    errors: list[str]
    state_id: str


@dataclass
class Budget:
    wall_s: float
    ops: int
    model_calls: int
    #: an ADVISORY trigger (checked once per expansion; one propose can make up to max_turns calls first), not a
    #: limit: the hard per-file ceiling is the proposer's `usd_ceiling`, enforced per call by the ledger
    model_usd: float


@dataclass
class SearchState:
    id: str
    ws: Workspace
    verdict: Any
    score: Score
    sites: list[Site]
    depth: int
    parent: str | None
    #: the candidate that produced this state from its parent (None for the start)
    via: dict | None
    tier: str = "full"
    expanded: bool = False
    #: what the proposer's observation reads as `budget`: {"spent": {...}, "limits": {...}}, set on expansion
    budget: Any = None
    #: the `SiteRegistry` of this state's lineage. Before expansion: the PARENT's, as the parent left it (never
    #: mutated again). On expansion it is replaced by this state's own deep copy with its sites applied.
    site_registry: Any = None
    #: the state ids from the start down to this state (its own last). Site ids are minted per lineage, so
    #: anything keyed by a site id (an unfixable declaration, "picture already drawn") is keyed by this too.
    lineage: tuple[str, ...] = ()
    #: score from the cheap tier (None when unknown); with `rank`, see the module docstring
    cheap_score: Score | None = None
    rank: Score | None = None


class Generator(Protocol):
    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        """At most `k` candidates for `state`. May also define `on_rejected(list[Rejection])` and
        `model_usage() -> (calls, usd)`; the driver uses them when present."""
        ...


@dataclass
class Judge:
    """The verdict and site machinery the search asks. Real ones: `default_judge()`; tests fake it.
    `cheap`/`full` take (pool, workspace, original STEP) and return a Verdict; `score` is
    `verdict.score`; `sites` is `find_sites`."""

    cheap: Callable[[Any, Workspace, str], Any]
    full: Callable[[Any, Workspace, str], Any]
    score: Callable[[Any], Score]
    sites: Callable[[Any, Workspace, Any], list[Site]]


def _clean_with(v: Verdict, faithful: FaithfulReport | None) -> bool:
    """`Verdict.clean` for a verdict whose layer 3 was replaced, by the ONE rule (`assemble.clean_of`).

    OPEN OWNER DECISION, deliberately not changed here: when the ORIGINAL has no solid, layer 3 always reports
    `volume_changed` / `body_count_changed`, so `faithful.ok` is False, `Judge.score` key 0 is permanently 0 and
    `clean` permanently False. The search then never stops at "clean", `SearchResult.clean` cannot be True, and
    ordering degrades to panel passes. Task 16's local gate (`stages._judge`, `_SOLIDLESS_OK_REASONS`) already
    treats those reasons as acceptable, so stage keep/rollback and search ranking disagree for such files.
    Resolving it changes what "clean" means; see the Task 18b report, item "escalated"."""
    return clean_of(
        tier=v.tier, inconclusive=v.inconclusive, valid=v.valid, panel=v.panel, faithful=faithful
    )


def _judged(fn: Callable[..., Verdict], pool: Any, ws: Workspace, original: str) -> Verdict:
    """`fn` (`cheap_verdict` / `verdict`) on workspace `ws`, judged as Task 16's `stages._judge` judges it.

    The checker reads STEP files, a workspace is a BREP, so the BREP is exported first (`step_for`). Declared
    edits are `F<k>` indices of the BREP the checker reads, NOT stable ids (`declared_edits` translates them),
    and a STEP round trip may renumber faces, so layer 3 is re-run on the two BREPs, where the indices are exact,
    whenever the lineage declared anything; `clean` is recomputed from that report. A declared id the shapes do not
    match is judged undeclared rather than crashing the run (as `stages._judge` does)."""
    faces, _solids = original_facts(pool, ws.original)
    v: Verdict = fn(pool, step_for(pool, ws.brep_path), original)
    declared = declared_edits(ws, faces)
    if v.valid.ok and (declared.added or declared.removed):
        try:
            report = check_faithful(
                pool, ws.original, ws.brep_path, declared_added=declared.added, declared_removed=declared.removed
            )
        except DeclaredIdError:
            return v
        v = replace(v, faithful=report, clean=_clean_with(v, report))
    return v


def default_judge(*, h_mm: float) -> Judge:
    """The real verdict (Task 5) and sites (Task 17). `h_mm` (the mesh size) is required: it sets site
    clustering and the proximity threshold, so a silent default would aim every candidate at the wrong sites.
    `original` is the STEP the product was handed; faithfulness compares against it (and, for declared edits,
    against the lineage's original BREP)."""
    return Judge(
        cheap=lambda pool, ws, original: _judged(cheap_verdict, pool, ws, original),
        full=lambda pool, ws, original: _judged(verdict, pool, ws, original),
        score=score,
        sites=lambda pool, ws, v: find_sites(pool, ws, v, h_mm=h_mm),
    )


#: Position of the panel-pass count in a `Score` (valid, panel passes, min SICN, -max_dev, -churn).
#: The delivery gate reads it by this name; `panel_passes` fails loudly if the tuple's shape changes.
SCORE_PANEL_PASSES = 1
_SCORE_LEN = 5


def panel_passes(score: Score) -> int:
    assert len(score) == _SCORE_LEN, f"score() changed shape: expected {_SCORE_LEN} fields, got {score!r}"
    return int(score[SCORE_PANEL_PASSES])


@dataclass
class SearchResult:
    best: Workspace
    best_verdict: Any
    clean: bool
    trace: Trace
    #: False when nothing beat the start (best is the start workspace)
    changed: bool = False
    #: a SPENDING limit ended the AI's part (the wave cap, the file's own ceiling, or this search's model budget)
    #: and the search finished deterministic-only. `input_too_large` and `model_failure` are NOT this.
    ai_capped: bool = False
    #: why the AI stopped, when it did: the proposer's own `stop_reason` (`wave_cap`, `file_ceiling`, `max_turns`,
    #: `model_failure`, `input_too_large`) or `model_budget` when this driver capped it; None when it never stopped
    ai_stop_reason: str | None = None
    #: `AIProposer.result()` of each proposer in the search (spend, turns, reason)
    ai: list[dict] = field(default_factory=list)
    #: why the search stopped: "clean", "patience", "exhausted", a budget reason, or "no_verdict" (nothing was
    #: measured: see `infrastructure_failed`)
    reason: str | None = None
    #: candidates that came back INCONCLUSIVE (the checker, mesher or pool failed, not the part): never ranked,
    #: never a verdict. Together with `verdict_candidates` this is how a consumer decides whether the file was
    #: measured at all (Task 21 withholds an arm's clean rate above a 10% inconclusive rate).
    inconclusive_candidates: int = 0
    #: candidates that reached the checker and got a usable verdict (scored, kept or not); declined/invalid ops
    #: and op failures never reach the checker and are not counted here
    verdict_candidates: int = 0
    #: True when the search produced NO usable verdict and at least one candidate was inconclusive: the outcome
    #: says nothing about whether the file is repairable, so it must not be read as "search failed". `reason` is
    #: then "no_verdict" (the stop that would otherwise have been reported is kept in the trace as `ended_by`).
    infrastructure_failed: bool = False

    @property
    def unmeasured(self) -> bool:
        """True when this outcome says nothing about the file (never measured, as opposed to measured and failed).
        The same rule the report applies to a row (`measurement.file_unmeasured`)."""
        return file_unmeasured(reason=self.reason, infrastructure_failed=self.infrastructure_failed,
                               inconclusive_candidates=self.inconclusive_candidates,
                               verdict_candidates=self.verdict_candidates, out_clean=self.clean)

    @property
    def inconclusive_rate(self) -> float:
        """inconclusive / (inconclusive + verdict) candidates; 0.0 when no candidate was judged at all."""
        total = self.inconclusive_candidates + self.verdict_candidates
        return self.inconclusive_candidates / total if total else 0.0


# --- the deterministic generator ----------------------------------------------------------------

_PROXIMITY_RATIOS = (0.25, 0.125, 0.06)  # gap_split's rungs
_SPLIT_ANGLES = (90, 60, 45)
_SLIVER_WIDTHS_MM = (0.001, 0.01, 0.05)
_SEW_LADDER = [1e-4, 1e-3, 1e-2]
_MAX_PAIRS = 16  # split_by_proximity's maxItems


def _of_kind(entities: list[StableId], prefix: str) -> list[StableId]:
    return [e for e in entities if e[:1] == prefix]


class DetGenerator:
    """Per site kind, a fixed parameter sweep over the ops that apply to it. Only ops present in
    `REGISTRY` are proposed. Sweeps are ordered mildest first; `k` truncates.

    `unfixable` is the registry shared with the AI proposer: a site the model declared unfixable within the
    fidelity budget is not swept (`ai_generators` builds the pair; `search` refuses a mismatch)."""

    def __init__(self, unfixable: UnfixableRegistry | None = None) -> None:
        self.unfixable = unfixable

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        out: list[Candidate] = []
        sites = self.unfixable.filter(state.sites, state.lineage) if self.unfixable is not None else state.sites
        for site in sites:
            for op, targets, params, why in self._sweep(site):
                if op in REGISTRY:
                    out.append(Candidate(op, targets, params, "det", f"{why} (site {site.id}, {site.kind})"))
        return out[:k]

    @staticmethod
    def _sweep(site: Site) -> list[tuple[str, list[StableId], dict, str]]:
        faces = _of_kind(site.entities, "F")
        solids = _of_kind(site.entities, "B")
        rows: list[tuple[str, list[StableId], dict, str]] = []
        kind = site.kind
        if kind == "proximity" and len(faces) >= 2:
            pairs = [list(p) for p in itertools.combinations(faces, 2)][:_MAX_PAIRS]
            for ratio in _PROXIMITY_RATIOS:
                rows.append(
                    (
                        "split_by_proximity",
                        [],
                        {"face_pairs": pairs, "target_ratio": ratio},
                        f"split the near faces so chords sag at most {ratio} of the gap",
                    )
                )
        if kind in ("proximity", "self_overlap", "periodic") and faces:
            for deg in _SPLIT_ANGLES:
                rows.append(("split_angle", faces, {"max_deg": deg}, f"split faces at {deg} deg"))
        if kind == "periodic" and faces:
            rows.append(("split_closed", faces, {}, "split closed periodic faces at the seam"))
        if kind == "invalid":
            if faces:
                for order in (1, 2):
                    rows.append(("split_continuity", faces, {"order": order}, f"split faces at C{order} breaks"))
            rows.append(("orient_and_close", solids, {}, "fix shell orientation"))
        if kind == "sliver":
            for width in _SLIVER_WIDTHS_MM:
                rows.append(
                    (
                        "remove_small",
                        site.entities,
                        {"max_width_mm": width},
                        f"remove entities narrower than {width} mm",
                    )
                )
            rows.append(("unify_same_domain", [], {"scope": "all", "keep": []}, "merge same-domain faces"))
        if kind == "open":
            rows.append(
                (
                    "sew",
                    site.entities if site.entities else [],
                    {"tol_ladder": _SEW_LADDER},
                    "sew at the tightest closing tolerance",
                )
            )
            rows.append(("make_volume", faces, {}, "build a solid from the loose faces"))
            rows.append(("orient_and_close", solids, {}, "orient and close the shell"))
        if kind in ("overlap", "intersection"):
            if len(solids) >= 2:
                for fuzzy in (0.0, 0.01):
                    rows.append(
                        (
                            "conformalise",
                            solids,
                            {"mode": "fragment", "fuzzy_mm": fuzzy},
                            f"fragment the bodies (fuzzy {fuzzy} mm)",
                        )
                    )
            if len(faces) >= 2:
                rows.append(
                    (
                        "remove_duplicate_faces",
                        [],
                        {"pairs": [list(p) for p in itertools.combinations(faces, 2)][:_MAX_PAIRS]},
                        "remove coincident duplicate faces",
                    )
                )
        return rows


# --- the driver ---------------------------------------------------------------------------------


def _neg(score: Score) -> tuple:
    return tuple(-x for x in score)


def _rank(state: SearchState) -> Score:
    return state.rank if state.rank is not None else state.score


def _estimate(parent: SearchState, child_cheap: Score) -> Score:
    """A child's frontier rank: its parent's rank shifted by the child-minus-parent CHEAP difference.
    Without a known cheap score for the parent there is nothing to calibrate against, so the child
    keeps its raw cheap score."""
    if parent.cheap_score is None:
        return child_cheap
    base = _rank(parent)
    return tuple(b + (c - p) for b, c, p in zip(base, child_cheap, parent.cheap_score, strict=True))  # type: ignore[return-value]


def _fingerprint(ws: Workspace) -> tuple:
    """Two workspaces with the same entity fingerprints are the same geometry, however reached."""
    return tuple(sorted(ws.ids.values()))


@dataclass
class _Outcome:
    candidate: Candidate
    gen: int
    kind: Literal["ok", "validate", "declined", "failed", "error", "inconclusive"]
    errors: list[str] = field(default_factory=list)
    ws: Workspace | None = None
    verdict: Any = None
    #: the cheap-tier score of `verdict` (computed where the verdict is, so a raising `score` stays in the branch)
    score: Score | None = None
    diffs: list[Diff] = field(default_factory=list)


def _validate(c: Candidate, ws: Workspace) -> list[str]:
    if c.op != RUN_STAGE:
        return validate_call(c.op, c.targets, c.params, ws)
    errors: list[str] = []
    if c.targets:
        errors.append(f"targets: {RUN_STAGE} takes none (name faces in the stage's own params), got {c.targets}")
    extra = sorted(set(c.params) - {"stage", "params"})
    if extra:
        errors.append(f"params: unknown key(s) {extra}; {RUN_STAGE} takes 'stage' and 'params'")
    name, inner = c.params.get("stage"), c.params.get("params", {})
    if name not in DEFAULTS:
        errors.append(f"unknown stage {name!r}; known stages: {[s.name for s in STAGES]}")
    elif not isinstance(inner, dict):
        errors.append("params.params: expected an object")
    else:
        unknown = sorted(set(inner) - set(DEFAULTS[name]) - {"timeout_s"})
        if unknown:
            errors.append(f"params.params: unknown key(s) {unknown} for stage {name!r}; it takes {sorted(DEFAULTS[name])}")
    return errors


def _stage_outcome(pool: Any, ws: Workspace, c: Candidate, gi: int, timeout_s: float) -> _Outcome:
    """Run one stage as a transaction (`stages.run_stage`). Only a stage that was KEPT and changed the workspace
    is an "ok" outcome; every other ending is told to the proposer, in the stage's own words."""
    name = c.params["stage"]
    base = ws.fork()
    result = run_stage(pool, base, name, {"timeout_s": timeout_s, **c.params.get("params", {})})
    if name in result.inconclusive:
        return _Outcome(c, gi, "inconclusive", [f"inconclusive: stage {name!r} could not be judged (checker unavailable)"])
    if name in result.rolled_back:
        return _Outcome(c, gi, "declined", [f"stage {name!r} was rolled back: it lowered the cheap score"])
    if name in result.skipped:
        return _Outcome(c, gi, "declined", [f"stage {name!r} changed nothing: {result.skipped[name]}"])
    diffs = result.ws.history[len(base.history) :]
    if result.ws is base or not diffs:
        return _Outcome(c, gi, "declined", [f"stage {name!r} changed nothing"])
    return _Outcome(c, gi, "ok", ws=result.ws, diffs=list(diffs))


def _one_diff(c: Candidate, diffs: list[Diff]) -> Diff | None:
    """The proposer's feedback takes ONE Diff; a stage is several ops, folded into one."""
    if not diffs:
        return None
    if c.op != RUN_STAGE:
        return diffs[0]
    folded = Diff(
        op=RUN_STAGE,
        params={"stage": c.params.get("stage"), "ops": [d.op for d in diffs]},
        modified={}, generated={}, removed=[], declared_added=[], declared_removed=[],
        seconds=sum(d.seconds for d in diffs),
    )
    for d in diffs:
        for k, v in d.modified.items():
            folded.modified.setdefault(k, []).extend(v)
        for k, v in d.generated.items():
            folded.generated.setdefault(k, []).extend(v)
        folded.removed.extend(d.removed)
        folded.declared_added.extend(d.declared_added)
        folded.declared_removed.extend(d.declared_removed)
    return folded


def _delta(before: Score, after: Score) -> dict:
    """metric -> [before, after] for the proposer. `Score` stores -max_dev and -churn; they are shown as the
    positive quantities."""
    return {
        "valid": [before[0], after[0]],
        "panel_passes": [before[1], after[1]],
        "min_sicn": [before[2], after[2]],
        "max_dev_mm": [-before[3], -after[3]],
        "churn": [-before[4], -after[4]],
    }


_STATUS: dict[str, Literal["applied", "declined", "failed", "invalid"]] = {
    "ok": "applied", "validate": "invalid", "declined": "declined", "failed": "failed", "error": "failed",
}  # "inconclusive" is deliberately absent: it is never turned into feedback


def _feedback_for(out: _Outcome, parent: SearchState, child: SearchState | None, duplicate: bool) -> Outcome:
    c = out.candidate
    proposed = ProposedCandidate(c.op, list(c.targets), dict(c.params), c.source, c.rationale)
    if out.kind != "ok":
        return Outcome(proposed, _STATUS[out.kind], message="; ".join(out.errors))
    assert out.score is not None
    before = parent.cheap_score if parent.cheap_score is not None else parent.score
    delta, how = _delta(before, out.score), "cheap tier (one mesher) against the parent's cheap score"
    if child is not None and child.tier == "full" and parent.tier == "full":
        delta, how = _delta(parent.score, child.score), "full panel against the parent's full score"
    if duplicate:
        message = f"duplicate: this geometry was already reached; {how}"
    elif child is None:
        message = f"scored on the {how}"
    else:
        message = f"became state {child.id}, scored on the {how}"
    return Outcome(proposed, "applied", _one_diff(c, out.diffs), delta, message)


def _shared_unfixable(generators: Sequence[Any]) -> UnfixableRegistry | None:
    """The one `UnfixableRegistry` of the search, or ValueError when the generators do not share it: a
    deterministic sweep with its own registry would keep proposing against a site the model already gave up on."""
    mine = {id(g.unfixable): g.unfixable for g in generators if getattr(g, "unfixable", None) is not None}
    if len(mine) > 1:
        raise ValueError("generators hold different UnfixableRegistry objects; construct ONE and pass it to all")
    registry = next(iter(mine.values()), None)
    if registry is not None:
        for g in generators:
            if isinstance(g, DetGenerator) and g.unfixable is not registry:
                raise ValueError(
                    "a DetGenerator must share the AI proposer's UnfixableRegistry: build both with "
                    "ai_generators(), or pass the same registry to each"
                )
    return registry


def _model_usage(g: Any) -> tuple[int, float] | None:
    """(calls, usd) of a model-backed generator, or None for a deterministic one. The AI proposer reports turns
    and ledger spend rather than a `model_usage()` method."""
    fn = getattr(g, "model_usage", None)
    if fn is not None:
        usage: tuple[int, float] = fn()
        return usage
    if isinstance(g, AIProposer):
        return g.turns_used, float(g.result()["spent_usd"])
    return None


#: the most pictures drawn for one expansion; sites are best-first, and the rest are drawn by a descendant if
#: they survive. Each draw is a kernel render on the expansion thread.
MAX_VIEWS_PER_EXPANSION = 4


def _will_look(generator: Any) -> bool:
    """True when this model generator can still ask for a picture: it has not stopped (budget, ceiling, input
    size, failures) and has turns left. A picture nobody will look at is time spent for nothing."""
    if getattr(generator, "stop_reason", None) is not None:
        return False
    turns, cap = getattr(generator, "turns_used", None), getattr(generator, "max_turns", None)
    return not (isinstance(turns, int) and isinstance(cap, int) and turns >= cap)


ARMS = ("search_det", "search_ai")


def arm_generators(arm: str, **ai_kwargs: Any) -> tuple[list[Any], AIProposer | None]:
    """The generators of an eval/engine arm: `search_det` -> the deterministic sweep only (no proposer is built,
    so no model can be reached); `search_ai` -> `ai_generators(**ai_kwargs)`. An unknown arm is a ValueError.
    The entry points that select an arm (`repair_v4._run_search`, Task 20; the eval arms, Task 21) call this."""
    if arm == "search_det":
        return [DetGenerator()], None
    if arm == "search_ai":
        return ai_generators(**ai_kwargs)
    raise ValueError(f"unknown search arm {arm!r}; the arms are {ARMS}")


def ai_generators(
    route: str,
    *,
    ledger: Any,
    max_turns: int,
    usd_ceiling: float,
    file_id: str,
    job_id: str,
    user_id: str = "",
    call: Any = None,
    prices: tuple[float, float, float] | None = None,
    **kwargs: Any,
) -> tuple[list[Any], AIProposer]:
    """The generators of an `search_ai` run: the deterministic sweeps and the AI proposer, built around ONE
    `UnfixableRegistry`, with the proposer told every stage name (so it may propose `run_stage`). `ledger` must be
    a `WaveLedger.for_shard` one (the proposer refuses any other). Returns (generators, proposer)."""
    # the (0, FILE_USD_CEILING] guard lives in AIProposer itself, where it cannot be walked around
    shared = UnfixableRegistry()
    proposer = AIProposer(
        route, max_turns=max_turns, usd_ceiling=usd_ceiling, ledger=ledger, call=call, file_id=file_id,
        job_id=job_id, user_id=user_id, stage_names=[s.name for s in STAGES], unfixable=shared, prices=prices,
        **kwargs,
    )
    return [DetGenerator(unfixable=shared), proposer], proposer


def search(
    pool: Any,
    start: Workspace,
    original: str,
    generators: list[Generator],
    budget: Budget,
    *,
    workers: int,
    k_confirm: int = 3,
    patience: int = 3,
    k_propose: int = 12,
    judge: Judge | None = None,
    h_mm: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SearchResult:
    if judge is None:
        if h_mm is None:
            raise ValueError("search needs either a judge or h_mm for the real one; there is no default")
        judge = default_judge(h_mm=h_mm)
    if not math.isfinite(budget.model_usd) or budget.model_usd < 0:
        raise ValueError(f"Budget.model_usd must be a finite, non-negative dollar cap, got {budget.model_usd!r}")
    unfixable = _shared_unfixable(generators)
    any_gens: list[Any] = list(generators)  # generators may also carry feedback()/result(), outside the protocol
    trace = Trace(clock)
    t0 = clock()
    state_ids = itertools.count()
    tie = itertools.count()
    spent = {"ops": 0}
    lineages: dict[str, tuple[str, ...]] = {}

    def usage() -> tuple[int, float]:
        calls, usd = 0, 0.0
        for g in generators:
            u = _model_usage(g)
            if u is not None:
                calls, usd = calls + u[0], usd + u[1]
        return calls, usd

    def spent_now() -> dict:
        calls, usd = usage()
        return {"wall_s": round(clock() - t0, 4), "ops": spent["ops"], "model_calls": calls, "model_usd": usd}

    def over_budget() -> str | None:
        s = spent_now()
        if s["wall_s"] >= budget.wall_s:
            return "wall"
        if s["ops"] >= budget.ops:
            return "ops"
        return None

    def new_state(
        ws: Workspace,
        verdict: Any,
        sc: Score,
        tier: str,
        depth: int,
        parent: str | None,
        via: dict | None,
        registry: SiteRegistry | None = None,
    ) -> SearchState:
        sid = f"S{next(state_ids)}"
        lineages[sid] = (lineages.get(parent, ()) if parent is not None else ()) + (sid,)
        return SearchState(
            id=sid,
            ws=ws,
            verdict=verdict,
            score=sc,
            sites=[],
            depth=depth,
            parent=parent,
            via=via,
            tier=tier,
            site_registry=registry,
            lineage=lineages[sid],
        )

    def judge_cheap(ws: Workspace) -> tuple[Any, Score]:
        """The cheap verdict and its score. An inconclusive verdict (the pool failed, not the part) is retried;
        what stays inconclusive raises `InconclusiveVerdict` / `CheckerUnavailable` to the caller, which owns the
        branch. Both are the checker's infrastructure failing, not a statement about the part."""
        last: InconclusiveVerdict | CheckerUnavailable | None = None
        for _ in range(JUDGE_ATTEMPTS):
            try:
                v = judge.cheap(pool, ws, original)
                return v, judge.score(v)
            except (InconclusiveVerdict, CheckerUnavailable) as exc:
                last = exc
        assert last is not None
        raise last

    model_gens = {gi for gi, g in enumerate(generators) if _model_usage(g) is not None}
    active = list(range(len(generators)))
    ai_capped = False
    ai_stopped: dict[int, str] = {}
    #: state id -> the site ids already drawn in its lineage (a child inherits its parent's, after the parent's draws)
    viewed_in: dict[str, frozenset[str]] = {}

    start_verdict = judge.full(pool, start, original)
    s0 = new_state(start, start_verdict, judge.score(start_verdict), "full", 0, None, None, SiteRegistry())
    try:
        s0.cheap_score = judge.score(judge.cheap(pool, start, original))
    except Exception as exc:  # children of an uncalibrated start are ranked by their cheap score
        trace.record("judge_error", where="start_cheap", error=f"{type(exc).__name__}: {exc}")
    trace.record(
        "start",
        state=s0.id,
        score=s0.score,
        clean=bool(start_verdict.clean),
        budget=budget.__dict__,
        workers=workers,
        k_confirm=k_confirm,
        patience=patience,
    )
    best = s0
    seen = {_fingerprint(start)}
    heap: list[tuple] = [(_neg(s0.score), next(tie), s0.score, s0)]
    reason: str | None = "clean" if start_verdict.clean else None
    flat = 0
    n_inconclusive = 0
    n_verdicts = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        while reason is None:
            reason = over_budget()
            if reason:
                break
            if model_gens and not ai_capped:
                calls, usd = usage()
                if calls >= budget.model_calls or usd >= budget.model_usd:
                    ai_capped = True
                    active = [gi for gi in active if gi not in model_gens]
                    trace.record("ai_capped", spent=spent_now(), budget=budget.__dict__)
            state = None
            while heap:
                _, _, pushed_score, cand_state = heapq.heappop(heap)
                if not cand_state.expanded and pushed_score == _rank(cand_state):
                    state = cand_state
                    break
            if state is None:
                reason = "exhausted"
                break
            state.expanded = True

            try:
                state.sites = judge.sites(pool, state.ws, state.verdict)
            except Exception as exc:  # expand blind rather than lose the search
                state.sites = []
                trace.record("judge_error", where="sites", state=state.id, error=f"{type(exc).__name__}: {exc}")
            else:
                state.site_registry = _carry_site_ids(state, trace)
            state.budget = {"spent": spent_now(), "limits": budget.__dict__}
            declared_unfixable = [
                s.id for s in state.sites if unfixable is not None and unfixable.is_declared(s.id, state.lineage)
            ]
            viewed = set(viewed_in.get(state.parent, frozenset())) if state.parent is not None else set()
            if any(_will_look(generators[gi]) for gi in model_gens.intersection(active)):
                drawn = 0
                for s in state.sites:
                    if s.kind in IMAGE_KINDS and s.id not in viewed and s.id not in declared_unfixable:
                        if drawn >= MAX_VIEWS_PER_EXPANSION:
                            break  # not marked viewed: a descendant may still draw it
                        viewed.add(s.id)  # the proposer shows a site's picture once, so the driver draws it once
                        drawn += 1
                        try:
                            render.view(pool, state.ws, s)  # None is a legitimate answer: no picture, still proposable
                        except Exception as exc:  # a picture is optional evidence, never a verdict
                            trace.record("view_unavailable", state=state.id, site=s.id,
                                         error=f"{type(exc).__name__}: {exc}")
            viewed_in[state.id] = frozenset(viewed)
            trace.record(
                "expand",
                state=state.id,
                parent=state.parent,
                depth=state.depth,
                via=state.via,
                tier=state.tier,
                score=state.score,
                clean=bool(state.verdict.clean),
                sites=[
                    {"id": s.id, "kind": s.kind, "entities": s.entities, "meshers_failing": s.meshers_failing}
                    for s in state.sites
                ],
                unfixable=declared_unfixable,
            )

            # gather and validate
            proposals: list[tuple[int, Candidate]] = []
            for gi in active:
                try:
                    proposals += [(gi, c) for c in generators[gi].propose(state, k_propose)]
                except Exception as exc:  # a flaky proposer costs its proposals, not the search
                    trace.record(
                        "generator_error",
                        generator=gi,
                        where="propose",
                        state=state.id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            for gi in model_gens:
                why = getattr(generators[gi], "stop_reason", None)
                if why is not None and gi not in ai_stopped:
                    ai_stopped[gi] = str(why)
                    trace.record("ai_stopped", generator=gi, reason=str(why),
                                 ai_capped=bool(getattr(generators[gi], "ai_capped", False)))
            rejections: dict[int, list[Rejection]] = {}
            #: every candidate's fate, in proposal order: (generator, outcome, the state it became or None, duplicate)
            fates: list[tuple[int, _Outcome, SearchState | None, bool]] = []
            valid: list[tuple[int, Candidate]] = []
            for gi, c in proposals:
                errors = _validate(c, state.ws)
                if errors:
                    _reject(trace, rejections, state, gi, c, "validate", errors)
                    fates.append((gi, _Outcome(c, gi, "validate", errors), None, False))
                else:
                    valid.append((gi, c))
            room = max(0, budget.ops - spent["ops"])
            for gi, c in valid[room:]:
                _record_candidate(trace, state, c, "skipped_budget")
                fates.append((gi, _Outcome(c, gi, "declined", ["not run: the op budget was exhausted"]), None, False))
            to_run = valid[:room]
            spent["ops"] += len(to_run)

            # apply on forks and take the cheap verdict, in parallel
            timeout_s = max(1.0, budget.wall_s - (clock() - t0))

            def evaluate(
                item: tuple[int, Candidate], timeout_s: float = timeout_s, state: SearchState = state
            ) -> _Outcome:
                gi, c = item
                try:
                    if c.op == RUN_STAGE:
                        out = _stage_outcome(pool, state.ws, c, gi, timeout_s)
                        if out.kind != "ok":
                            return out
                        new, diffs = out.ws, out.diffs
                    else:
                        new, diff = REGISTRY[c.op].apply(
                            pool, state.ws.fork(), list(c.targets), dict(c.params), timeout_s=timeout_s
                        )
                        diffs = [diff]
                    assert new is not None
                    v, sc = judge_cheap(new)
                    return _Outcome(c, gi, "ok", ws=new, verdict=v, score=sc, diffs=diffs)
                except OpDeclined as exc:
                    return _Outcome(c, gi, "declined", [exc.reason])
                except OpFailed as exc:
                    return _Outcome(c, gi, "failed", [f"{exc.status}: {exc.message}"])
                except (InconclusiveVerdict, CheckerUnavailable) as exc:
                    # infrastructure, not a verdict on the candidate: never a failure, never fed back
                    return _Outcome(c, gi, "inconclusive", [f"{type(exc).__name__}: {exc}"])
                except (UnknownTarget, ValueError) as exc:
                    return _Outcome(c, gi, "validate", [str(exc)])
                except Exception as exc:  # one broken branch must not end the search
                    return _Outcome(c, gi, "error", [f"{type(exc).__name__}: {exc}"])

            children: list[SearchState] = []
            for out in [f.result() for f in [executor.submit(evaluate, i) for i in to_run]]:
                if out.kind == "ok":
                    n_verdicts += 1
                if out.kind == "inconclusive":
                    n_inconclusive += 1
                    _record_candidate(trace, state, out.candidate, "inconclusive", errors=out.errors)
                    continue  # not a rejection and no fate: the generator is not told its candidate failed
                if out.kind != "ok":
                    _reject(trace, rejections, state, out.gen, out.candidate, out.kind, out.errors)
                    fates.append((out.gen, out, None, False))
                    continue
                assert out.ws is not None and out.score is not None
                fp = _fingerprint(out.ws)
                if fp in seen:
                    _record_candidate(trace, state, out.candidate, "duplicate", score=out.score)
                    fates.append((out.gen, out, None, True))
                    continue
                seen.add(fp)
                child = new_state(
                    out.ws, out.verdict, out.score, "cheap", state.depth + 1, state.id, _via(out.candidate),
                    state.site_registry,
                )
                child.cheap_score = child.score
                child.rank = _estimate(state, child.score)
                _record_candidate(
                    trace,
                    state,
                    out.candidate,
                    "evaluated",
                    child=child.id,
                    score=child.score,
                    parent_score=state.score,
                    rank=child.rank,
                )
                children.append(child)
                fates.append((out.gen, out, child, False))
                heapq.heappush(heap, (_neg(_rank(child)), next(tie), _rank(child), child))

            for gi, items in rejections.items():
                told = getattr(generators[gi], "on_rejected", None)
                if told is not None:
                    try:
                        told(items)
                    except Exception as exc:
                        trace.record(
                            "generator_error",
                            generator=gi,
                            where="on_rejected",
                            state=state.id,
                            error=f"{type(exc).__name__}: {exc}",
                        )

            # confirm the best new states with the full panel
            improved = False
            top = sorted(children, key=lambda s: s.score, reverse=True)[:k_confirm]
            futures = [executor.submit(judge.full, pool, c.ws, original) for c in top]
            for child, future in zip(top, futures, strict=True):
                try:
                    full = future.result()
                    full_score = judge.score(full)
                except Exception as exc:  # the child stays unconfirmed (cheap-ranked) and unshippable
                    trace.record("confirm_error", state=child.id, error=f"{type(exc).__name__}: {exc}")
                    continue
                cheap_score = child.score
                child.verdict, child.tier, child.score, child.rank = full, "full", full_score, full_score
                trace.record(
                    "confirm",
                    state=child.id,
                    tier="full",
                    cheap_score=cheap_score,
                    score=child.score,
                    clean=bool(full.clean),
                )
                heapq.heappush(heap, (_neg(child.score), next(tie), child.score, child))
                if child.score > best.score:
                    trace.record("best", state=child.id, score=child.score, previous=best.id, previous_score=best.score)
                    best, improved = child, True

            # tell each generator that listens what became of ITS candidates (after confirmation, so a confirmed
            # child's delta is the full-panel one)
            told_outcomes: dict[int, list[Outcome]] = {}
            for gi, out, became, duplicate in fates:
                if hasattr(generators[gi], "feedback"):
                    told_outcomes.setdefault(gi, []).append(_feedback_for(out, state, became, duplicate))
            for gi, outcomes in told_outcomes.items():
                try:
                    any_gens[gi].feedback(outcomes)
                except Exception as exc:
                    trace.record(
                        "generator_error",
                        generator=gi,
                        where="feedback",
                        state=state.id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if best.verdict.clean:
                reason = "clean"
                break
            flat = 0 if improved else flat + 1
            if flat >= patience:
                reason = "patience"

    ended_by = reason
    infrastructure_failed = no_verdict_failure(
        inconclusive_candidates=n_inconclusive, verdict_candidates=n_verdicts, best_clean=bool(best.verdict.clean))
    if infrastructure_failed:
        reason = NO_VERDICT  # never a patience/exhausted exit: nothing was measured
    ai_capped = ai_capped or any(bool(getattr(generators[gi], "ai_capped", False)) for gi in model_gens)
    ai_stop_reason = next(iter(ai_stopped.values()), "model_budget" if ai_capped else None)
    ai_results = [g.result() for g in any_gens if isinstance(g, AIProposer)]
    trace.record(
        "stop",
        reason=reason,
        ended_by=ended_by,
        inconclusive_candidates=n_inconclusive,
        verdict_candidates=n_verdicts,
        best=best.id,
        best_score=best.score,
        clean=bool(best.verdict.clean),
        best_path=_path(trace, best),
        spent=spent_now(),
        budget=budget.__dict__,
        ai_capped=ai_capped,
        ai_stop_reason=ai_stop_reason,
        ai=ai_results,
    )
    return SearchResult(
        best=best.ws,
        best_verdict=best.verdict,
        clean=bool(best.verdict.clean),
        trace=trace,
        changed=best.id != s0.id,
        ai_capped=ai_capped,
        ai_stop_reason=ai_stop_reason,
        ai=ai_results,
        reason=reason,
        inconclusive_candidates=n_inconclusive,
        verdict_candidates=n_verdicts,
        infrastructure_failed=infrastructure_failed,
    )


def _carry_site_ids(state: SearchState, trace: Trace) -> SiteRegistry:
    """Carry the site ids of `state.sites` forward from the lineage this state descends from, and return the
    registry the state's CHILDREN inherit.

    One `SiteRegistry` follows one linear lineage; the search forks, so the state works on a DEEP COPY of the
    registry its parent left (`state.site_registry`) and the original is never touched again. Siblings therefore
    never see each other's history. If the registry still refuses (it should not), the state falls back to a
    fresh registry over its own history - its ids are then the fresh anchor ids - and the error is recorded."""
    inherited = state.site_registry if state.site_registry is not None else SiteRegistry()
    own = copy.deepcopy(inherited)
    try:
        own.apply(state.ws, state.sites)
        return own
    except Exception as exc:  # LineageMismatch, or a site the registry cannot read: never end the search
        trace.record("judge_error", where="site_registry", state=state.id, error=f"{type(exc).__name__}: {exc}")
    fresh = SiteRegistry()
    try:
        fresh.apply(state.ws, state.sites)
    except Exception as exc:
        trace.record("judge_error", where="site_registry_fresh", state=state.id,
                     error=f"{type(exc).__name__}: {exc}")
    return fresh


def _via(c: Candidate) -> dict:
    return {"op": c.op, "targets": c.targets, "params": c.params, "source": c.source, "rationale": c.rationale}


def _record_candidate(trace: Trace, state: SearchState, c: Candidate, outcome: str, **extra: Any) -> None:
    trace.record("candidate", state=state.id, outcome=outcome, **_via(c), **extra)


def _reject(
    trace: Trace,
    rejections: dict[int, list[Rejection]],
    state: SearchState,
    gi: int,
    c: Candidate,
    stage: str,
    errors: list[str],
) -> None:
    rejections.setdefault(gi, []).append(Rejection(c, stage, errors, state.id))  # type: ignore[arg-type]
    _record_candidate(trace, state, c, "rejected", stage=stage, errors=errors)


def _path(trace: Trace, best: SearchState) -> list[str]:
    """Ids of the states from the start to `best`, recovered from the trace's expand events."""
    parent = {e["state"]: e["parent"] for e in trace.of_kind("expand")}
    for e in trace.of_kind("candidate"):
        if e["outcome"] == "evaluated":
            parent[e["child"]] = e["state"]
    path: list[str] = []
    node: str | None = best.id
    while node is not None:
        path.append(node)
        node = parent.get(node)
    return path[::-1]


def deliverable(
    result: SearchResult, input_verdict: Any, *, score_fn: Callable[[Any], Score] | None = None
) -> Workspace | None:
    """The workspace to ship, or None meaning "ship the input unchanged".

    The delivery rule: the best state is delivered ONLY if it passes at least as many panel
    members as the input. A clean input is never replaced, and a search that beat nothing delivers
    nothing. This is the gate against shipping a "repair" that made things worse.
    """
    if not result.changed or input_verdict.clean:
        return None
    score_of = score_fn or score
    if panel_passes(score_of(result.best_verdict)) < panel_passes(score_of(input_verdict)):
        return None
    return result.best


__all__ = [
    "Budget",
    "Candidate",
    "DetGenerator",
    "Generator",
    "Judge",
    "RUN_STAGE",
    "Rejection",
    "SearchResult",
    "SCORE_PANEL_PASSES",
    "SearchState",
    "ai_generators",
    "default_judge",
    "deliverable",
    "panel_passes",
    "search",
]
