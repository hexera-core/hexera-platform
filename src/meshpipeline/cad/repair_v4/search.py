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

Budget. `ops` counts APPLIED OPS, not candidates (Task 53): a candidate is charged what the op
library actually applied for it - one for a plain op, and for a `run_stage` macro the length of the
chain its body ran (`shells` without a solid is sew + make_volume, so two). A candidate that is run
but applies nothing (it declined or failed) still costs the attempt, one. The same rule holds for
every generator, deterministic and AI alike, so `search_ai - search_det` measures judgement and not
which arm happened to route through a multi-op stage. `wall_s` is elapsed time; `model_calls` /
`model_usd` are read from generators that expose `model_usage() -> (calls, usd)` (cumulative).
`wall_s` and `ops` stop the search, checked before every expansion; `ops` also truncates a batch to
the ops still unspent, which bounds but no longer strictly caps the total: a chain's real length is
known only after its body ran, so one expansion may finish a few ops over `budget.ops`. The overrun
is reported as spent and the next expansion stops the search. Reaching `model_calls` or
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
    `stages.run_stage`; it is one candidate and one state, but costs the op budget every op its body
    applied (`StagedResult.applied_ops`), including the ops of a stage that was rolled back or whose chain
    broke halfway: that kernel work was really done.
  * VIEWS. For a model generator, sites of the kinds the proposer shows pictures for are drawn once with
    `render.view(pool, ws, site)`.
  * STOP REASON. `SearchResult.ai_stop_reason` is the proposer's own reason (`file_ceiling`, `input_too_large`,
    `model_unavailable`, ...) or `model_budget` when this driver capped it; `ai_capped` is true for a spending
    limit only, so `input_too_large` and `model_unavailable` are NOT `ai_capped`.
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

import collections
import copy
import heapq
import itertools
import json
import math
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from meshpipeline.cad.repair_v4 import render
from meshpipeline.cad.repair_v4.measurement import NO_VERDICT, TIME_STOPS, file_unmeasured, no_verdict_failure
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.ops.shells import det_sliver_widths, scaled_sew_ladder
from meshpipeline.cad.repair_v4.proposer import (
    IMAGE_KINDS,
    AIProposer,
    Outcome,
    ProposedCandidate,
    UnfixableRegistry,
    reason_of,
)
from meshpipeline.cad.repair_v4.sites import SiteRegistry, find_sites
from meshpipeline.cad.repair_v4.stages import (
    DEFAULTS,
    STAGES,
    declared_edits,
    diagonal_mm,
    original_facts,
    run_stage,
    step_for,
)
from meshpipeline.cad.repair_v4.trace import Trace
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.units import UNBOUNDED_DIAGONAL_MM
from meshpipeline.cad.repair_v4.verdict.assemble import (
    InconclusiveVerdict,
    Verdict,
    adaptive_cheap_members,
    cheap_verdict,
    clean_of,
    score,
    verdict,
)
from meshpipeline.cad.repair_v4.verdict.cache import VerdictCache, default_verdict_cache
from meshpipeline.cad.repair_v4.verdict.faithful import DeclaredIdError, FaithfulReport, check_faithful
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable, Finding, ValidReport
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
    #: how many ops `via` actually applied to reach this state: 1 for a plain op, the chain's length for a
    #: `run_stage` macro. What the op budget was charged for this step (`SearchResult.path_steps` reports it).
    ops_applied: int = 1


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
    #: `cheap` with explicit members (`assemble.adaptive_cheap_members`); None for a judge that cannot (a test's)
    cheap_with: Callable[[Any, Workspace, str, Any], Any] | None = None


def adaptive_cheap(judge: Judge, start_verdict: Any) -> Callable[[Any, Workspace, str], Any]:
    """The cheap verdict this search ranks with: `judge.cheap` over the static cheap members PLUS every member the
    start state's full verdict fails (`assemble.adaptive_cheap_members`), when the judge can take members."""
    if judge.cheap_with is None:
        return judge.cheap
    members = adaptive_cheap_members(start_verdict)
    cheap_with = judge.cheap_with
    return lambda pool, ws, original: cheap_with(pool, ws, original, members)


def _clean_with(v: Verdict, faithful: FaithfulReport | None) -> bool:
    """`Verdict.clean` for a verdict whose layer 3 was replaced, by the ONE rule (`assemble.clean_of`).

    INERT TODAY, KEPT AS THE SEAM. Since the owner's 2026-11-24 decision the rule is layer 1 + layer 2, so
    replacing layer 3 cannot change `clean` and this returns `v.clean` for every input (pinned by
    `test_clean_with_cannot_change_the_answer_under_the_current_definition`). It stays because it is the one place
    that re-derives clean after the declared-edits re-run: a future change to the definition lands in `clean_of`
    and flows through here, instead of the search growing a second copy of the rule.

    `Judge.score` key 0 is VALIDITY alone, so a state layer 3 calls unfaithful still ranks on its meshing and
    deviation is only a tie-breaker; lane B12 removed the op-level fidelity caps under the same decision.

    The deadlock this used to escalate is settled by it too: a solid-less original always reports
    `volume_changed` / `body_count_changed`, which used to force `faithful.ok` False, score key 0 to 0 and `clean`
    to False forever. Layer 3 gates nothing now, so stage keep/rollback and search ranking agree without the local
    exemption that `stages._judge` carried (deleted with the gate that needed it)."""
    return clean_of(
        tier=v.tier, inconclusive=v.inconclusive, valid=v.valid, panel=v.panel, faithful=faithful
    )


def _unbounded(pool: Any, brep: str) -> bool:
    """True only when the kernel's `valid_extent` - a bounding box, the very rule layer 1 stops at
    (`valid_kernel._unbounded_finding`) - says the BREP is unbounded. A failed call is False: the input is then judged
    exactly as before, so this check can only ever restore a verdict, never invent one."""
    return _extent(pool, brep) is not None


def _extent(pool: Any, brep: str) -> dict | None:
    """Layer 1's `unbounded_geometry` finding for the BREP (`valid_extent`, ONE bounding-box call), or None when it is
    bounded or the call failed (a failed call judges the state exactly as before)."""
    try:
        extent = pool.call("valid_extent", {"brep": brep}, timeout_s=300)
    except Exception:  # noqa: BLE001 - a failed call judges the state exactly as before; it never invents a verdict
        return None
    if not (extent.ok and extent.value is not None and extent.value.get("unbounded") is True):
        return None
    return dict(extent.value.get("finding") or {"code": "unbounded_geometry", "entities": [], "detail": {}})


def _unbounded_verdict(ws: Workspace, finding: dict, tier: Literal["cheap", "full"]) -> Verdict:
    """What layer 1 says of an unbounded STATE, without running it: invalid, `unbounded_geometry`, nothing meshed.

    Lane C9: a state an op produced is judged on its re-export, where `unbounded_geometry` no longer fires and
    check_valid runs to its wall (the 1808 s the engine spent on sha bfc8d0b5 before anything was searched). A state
    that is still unbounded is INVALID at layer 1 by definition, so that is reported, cheaply, instead of being made
    inconclusive by a timeout. The input with no history keeps D10's path (judged on its original STEP)."""
    report = ValidReport(False, [Finding("unbounded_geometry", list(finding.get("entities") or []),
                                         dict(finding.get("detail") or {}))], [], "mm",
                         sum(1 for sid in ws.ids if sid[:1] == "B"), False)
    return Verdict(report, [], None, False, tier)


def _judged(fn: Callable[..., Verdict], pool: Any, ws: Workspace, original: str,
            cache: VerdictCache | None = None, **kw: Any) -> Verdict:
    """`fn` (`cheap_verdict` / `verdict`) on workspace `ws`, judged as Task 16's `stages._judge` judges it.

    The checker reads STEP files, a workspace is a BREP, so the BREP is exported first (`step_for`). Declared
    edits are `F<k>` indices of the BREP the checker reads, NOT stable ids (`declared_edits` translates them),
    and a STEP round trip may renumber faces, so layer 3 is re-run on the two BREPs, where the indices are exact,
    whenever the lineage declared anything; `clean` is recomputed from that report. A declared id the shapes do not
    match is judged undeclared rather than crashing the run (as `stages._judge` does)."""
    faces, _solids = original_facts(pool, ws.original)
    # THE INPUT OF AN UNBOUNDED PART is judged on its ORIGINAL STEP: on the re-export layer 1's `unbounded_geometry`
    # no longer fires and check_valid runs to its timeout (lane D, sha bfc8d0b5: 4 s out of scope on the original,
    # an 1800 s exclusion on the re-export). A STATE an op produced that is still unbounded is reported invalid by
    # `_unbounded_verdict` without being checked (lane C9); a bounded state is judged as the engine exports it.
    unbounded = _extent(pool, ws.brep_path)
    if unbounded is not None and ws.history:
        return _unbounded_verdict(ws, unbounded, "cheap" if fn is cheap_verdict else "full")
    step = str(original) if unbounded is not None else step_for(pool, ws.brep_path)
    # the cache holds only the panel (the meshing); layers 1 and 3 are recomputed, so a hit never skips a check
    v: Verdict = fn(pool, step, original, **kw) if cache is None else fn(pool, step, original, cache=cache, **kw)
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


#: Where `engine_form` keeps the BREPs it reads (one per process, removed at exit with step_for's exports).
_ENGINE_FORM_DIR: list[str] = []


def engine_form(pool: Any, step_path: str, *, timeout_s: float = 300) -> str:
    """The STEP the engine actually judges for an input file: read through XCAF into a BREP (what
    `repair_v4.repair_v4` and the eval's `RealEngine.open_workspace` do) and exported back by `step_for`, which is
    what `_judged` meshes for every workspace. The two forms are NOT the same geometry to a mesher: lane D measured
    4 of 11 near-threshold files changing a panel member's pass/fail between the original STEP and this one (gmsh_alt
    0.00417 fail -> 0.0494 pass on Task 49's flipped file), while each form was bit-identical run to run. Anything
    that labels an input - the re-probe that cuts the splits - must judge this form, or its label and the arms'
    input verdict measure different files. A file the reader cannot read raises `CheckerUnavailable`."""
    import atexit
    import shutil
    import tempfile

    if not _ENGINE_FORM_DIR:
        _ENGINE_FORM_DIR.append(tempfile.mkdtemp(prefix="engine_form_"))
        atexit.register(shutil.rmtree, _ENGINE_FORM_DIR[0], True)
    brep = os.path.join(tempfile.mkdtemp(dir=_ENGINE_FORM_DIR[0]), "input.brep")
    read = pool.call("read_step_xcaf", {"path": str(step_path), "brep_out": brep}, timeout_s=timeout_s)
    if not read.ok or read.value is None:
        raise CheckerUnavailable(f"read_step_xcaf could not read {step_path!r}: {read.status}: {read.error}")
    if _unbounded(pool, read.value["brep"]):
        # infinite extent is a property of the INPUT: judged on the original, layer 1 reports `unbounded_geometry`
        # (out of scope) in seconds; the re-export would hide it and run check_valid to its timeout (see `_judged`)
        return str(step_path)
    return step_for(pool, read.value["brep"])


def h_mm_for(pool: Any, ws: Workspace) -> float:
    """The target element size, in MILLIMETRES, for the part in `ws`: the production FEA mesh's base size
    (`fea_plan.base_size`, a tenth of the part diagonal, the same function `GmshProd` meshes with), so failure
    sites cluster at the scale the clean-STEP definition is written against. `base_size` works in METRES and the
    workspace in millimetres; the conversion is the only arithmetic here, and it is tested on parts decades
    apart. A part the kernel cannot measure (or one with no extent) raises `CheckerUnavailable`: no size is
    invented, and it is infrastructure, not a verdict."""
    from meshpipeline.engines.fea_plan import base_size

    return base_size(diagonal_mm(pool, ws) / 1000.0) * 1000.0


def default_judge_for(pool: Any, ws: Workspace, *, cache: VerdictCache | None = None) -> Judge:
    """`default_judge` with `h_mm` derived from the part (`h_mm_for`); what the engine calls."""
    return default_judge(h_mm=h_mm_for(pool, ws), cache=cache)


def default_judge(*, h_mm: float, cache: VerdictCache | None = None) -> Judge:
    """The real verdict (Task 5) and sites (Task 17). `h_mm` (the mesh size) is required: it sets site
    clustering and the proximity threshold, so a silent default would aim every candidate at the wrong sites.
    `original` is the STEP the product was handed; faithfulness compares against it (and, for declared edits,
    against the lineage's original BREP).

    THE VERDICT CACHE. Both tiers read and write `cache` (default: the process-wide `default_verdict_cache()`,
    the one `stages` judges with), keyed by (STEP sha256, panel config hash, verdict code version), so a state
    the staged healer already meshed, or one reached by two branches, is not meshed again. Without it the
    verdict functions never touch a cache (only `reprobe` passed one). The panel's member count is never
    assumed here: the key follows whatever `default_members(tier)` returns."""
    cache = cache if cache is not None else default_verdict_cache()
    return Judge(
        cheap=lambda pool, ws, original: _judged(cheap_verdict, pool, ws, original, cache),
        full=lambda pool, ws, original: _judged(verdict, pool, ws, original, cache),
        score=score,
        sites=lambda pool, ws, v: find_sites(pool, ws, v, h_mm=h_mm),
        cheap_with=lambda pool, ws, original, members: _judged(cheap_verdict, pool, ws, original, cache,
                                                               members=members),
    )


#: THE VERDICT BUDGET: the share of `Budget.wall_s` the search keeps back to judge its best state with the FULL
#: panel after it stops proposing (design 5.7's "output the best state so far"; owner ruling 2026-11-24, the
#: owner's own example was the last ~20%). It is taken OUT of the wall, never added to it.
VERDICT_RESERVE = 0.2
#: Position of layer 1 in a `Score` (valid, panel passes, min SICN, -max_dev, -churn). The delivery gate reads
#: key 0 and key 1 by name, so a change to the tuple's shape cannot silently move the gate.
VALID_KEY = 0
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
    #: and the search finished deterministic-only. `input_too_large` and `model_unavailable` are NOT this.
    ai_capped: bool = False
    #: why the AI stopped, when it did: the proposer's own `stop_reason` (`wave_cap`, `file_ceiling`, `max_turns`,
    #: `model_unavailable`, `input_too_large`) or `model_budget` when this driver capped it; None when it never stopped
    ai_stop_reason: str | None = None
    #: `AIProposer.result()` of each proposer in the search (spend, turns, reason)
    ai: list[dict] = field(default_factory=list)
    #: the winning path, one entry per candidate from the start to `best`: `{"via": the candidate, "sites": the
    #: PARENT state's sites, "ops": the ops that candidate applied (1 unless it was a multi-op `run_stage`)}` -
    #: what `recipes.recipe_from_path` turns into a recipe when the file ends clean
    path_steps: list[dict] = field(default_factory=list)
    #: the search stopped on the CLOCK (`measurement.TIME_STOPS`). The row carries it so a reader can keep these
    #: rows out of a clean-rate denominator; it is NOT a reason to discard the verdict (the anytime rule).
    stopped_on_wall: bool = False
    #: the tier of the verdict being delivered. "full" means the best state was judged by the whole panel, which
    #: is what makes a wall-stopped row a measurement rather than machine time.
    best_tier: str = "full"
    #: when the final full verdict had to be run in the reserve: whether it completed, and what it cost
    final_verdict: dict | None = None
    #: B20: candidates dropped BEFORE any verdict, by reason, and the count of verdicts that were therefore not
    #: spent. A dropped candidate that would have declined instantly costs nothing; a dropped candidate that would
    #: have been EVALUATED costs a cheap verdict, which is what this counts.
    pruned: dict[str, int] = field(default_factory=dict)
    verdicts_saved: int = 0
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
        The same rule the report applies to a row (`measurement.file_unmeasured`), `best_tier` included: a wall stop
        whose best state carries a FULL verdict is a measurement (the anytime rule)."""
        return file_unmeasured(reason=self.reason, infrastructure_failed=self.infrastructure_failed,
                               inconclusive_candidates=self.inconclusive_candidates,
                               verdict_candidates=self.verdict_candidates, out_clean=self.clean,
                               out_tier=self.best_tier)

    @property
    def inconclusive_rate(self) -> float:
        """inconclusive / (inconclusive + verdict) candidates; 0.0 when no candidate was judged at all."""
        total = self.inconclusive_candidates + self.verdict_candidates
        return self.inconclusive_candidates / total if total else 0.0


# --- the deterministic generator ----------------------------------------------------------------

_PROXIMITY_RATIOS = (0.25, 0.125, 0.06)  # gap_split's rungs
_SPLIT_ANGLES = (90, 60, 45)
_MAX_PAIRS = 16  # split_by_proximity's maxItems
#: body pairs an overlap site sweeps `cut_by` over, each both ways (which body is kept is exactly what the sweep cannot
#: know; the overlap's depth and volume are measured and reported, and refuse nothing)
_MAX_CUT_PAIRS = 3
#: bodies in ONE `cut_by` / `fuse` call when a site names more than two (lane C11's one-keeper-many-victims row)
_MAX_CUT_GROUP = 6
#: trim_unbounded's padding, as a share of the part's finite diagonal: Raymond Lee's 1% (devtools/simplify), then 5%
_TRIM_PADS = (0.01, 0.05)
#: a coarse site's angle splits: the two settings the staged healer's condition stage does not use (it runs 90)
_COARSE_ANGLES = (60, 45)

# The ops below used to be reachable ONLY from the AI proposer during the search - the last two only after the
# pre-search staged healer had had its single go at them (`stages._edges` / `stages._faces`; lane C3's smaller
# finding, Task 54) - so `search_ai - search_det` credited the model with
# access to them rather than judgement about them (lane C, round-2 wiring audit). Each now has a small, evidence-gated
# deterministic sweep. "Unknown means propose" (Task 47) holds for what the KERNEL discovers; what the site's own
# facts already rule out (no free-form surface to reapproximate, a cluster the op's documented limits refuse) is not
# proposed, and the reason is in `notes`.
DET_COVERED_OPS = ("rebuild_face", "rebuild_edge_as_intersection", "reapproximate", "refit_cluster",
                   "fix_poles", "to_canonical")
#: The actions the AI can request that the deterministic sweep does NOT make, each with the reason. Not a to-do list:
#: a name here is a DECISION that a det candidate would cost budget for nothing, and the completeness test
#: (tests/unit/cad/repair_v4/test_det_coverage.py) accepts an op as AI-only only if it is listed, so an op added to
#: `REGISTRY` must be swept or explained here and cannot quietly become an AI-only advantage again.
DET_UNREACHABLE_OPS: dict[str, str] = {
    "tighten_tolerances": (
        "it changes tolerances and nothing else (its kernel half returns the shape it read, with no history), and "
        "neither half of `_fingerprint` can see a tolerance: the entity fingerprints are centroid-and-box and "
        "`ws_kernel.shape_signature` is type, counts, closedness and shell volumes. Its child is therefore ALWAYS "
        "the parent's fingerprint, so the driver discards it as a duplicate before the verdict is used - for the AI "
        "proposer exactly as much as for a det candidate, so leaving it out favours neither arm, while proposing it "
        "would spend one op per expansion on a state that is thrown away. Reaching it needs a tolerance term in the "
        "shape signature (Task 45 did the same for orientation), which is a kernel change with a native gate."),
    # B21: `run_stage` is now reachable for det in ONE case - as the second step of a kept aimed defeature (see
    # CHAIN_AFTER_DEFEATURE). The fairness objection below was about buying k ops of geometry for ONE op of budget,
    # and Task 53 removed that: the budget is charged per APPLIED op, for every generator. What stands is that det
    # gets no GENERAL macro, only this measured continuation.
    RUN_STAGE: (
        "a macro, not an op: each stage body is a fixed chain of ops the sweep already proposes one by one, and the "
        "search starts from `run_staged` at the defaults. It also costs ONE op of budget while applying several, so "
        "giving det the macro would not level the comparison but move the unfairness (lane C3's finding for the "
        "owner: charge the macro by the ops it applied, or give both arms the macro)."),
}
#: an edge is a rebuild candidate when its tolerance or its measured face-to-face gap exceeds this x diagonal: the
#: normalise stage's own target (`stages.DEFAULTS["normalise"]["target_rel"]`), i.e. an edge the healer could not
#: bring to the tolerance it aims for
_EDGE_REL = 1e-5
# B19 (owner priority 2026-11-24): THE SEARCH LACKED PERMISSION, NOT OPS. A mesher failure and a layer-1 finding on
# the same faces merge into ONE site which takes the MESHER's kind, so a face-defect file can land at a kind that
# FORBIDS the op that fixes it. Lane A measured `split_angle` at 60 and 45 reaching CLEAN IN ONE STEP on 3596803faa,
# a file both arms fail, with an op AND a rung the sweep already has - it was never allowed to propose them there.
# Lane C measured the same defect on the body ops: overlapping bodies on a `self_overlap` site got no cut at all.
# So every list below is now a PREFERENCE (its kind's rows come FIRST) and no longer a permission: every family is
# offered at every kind, aimed at the site's own faces and bodies. `oracle_face_grid.py` and `oracle_grid.py` are
# the measured specification of what each kind used to forbid.
_EDGE_KINDS = ("invalid", "self_overlap", "overlap", "intersection", "proximity")  # = ops.edges._SIDE_KINDS
_MAX_EDGE_REBUILDS = 2  # one candidate per edge (one bad edge must not sink the others), worst first
_REBUILD_FACE_CODES = frozenset({"self_intersection", "brep_invalid"})
_MAX_FACE_REBUILDS = 2  # the op takes exactly one face
#: surfaces ShapeCustom_BSplineRestriction can usefully re-approximate; an analytic face would only be converted
_FREEFORM = frozenset({"bsplinesurface", "beziersurface", "offsetsurface", "surfaceofrevolution",
                       "surfaceofextrusion", "othersurface"})
_REAPPROX_KINDS = ("self_overlap", "pole", "periodic", "proximity")
#: (share of the fidelity bound, max degree), mildest first: a quarter of the bound at full degree, then half the
#: bound at a lower degree. Never the whole bound: the op is deviation-gated and other ops must still fit after it.
_REAPPROX_SWEEP = ((0.25, 9), (0.5, 6))
_MAX_REAPPROX_FACES = 8
_REFIT_KINDS = ("sliver", "overlap", "self_overlap")
_REFIT_SHARES = (0.5, 1.0)  # of the fidelity bound; the op declines above its own tol_mm
_REFIT_MAX_FACES = 6
#: `fix_poles` is ShapeFix_Face, and its kernel half RAISES when a targeted face does not change, so it is swept
#: only where a pole was OBSERVED - a mesher's own `pole` error (`sites._MESHER_KINDS`) - and not on the other kinds
#: `ops.edges.FixPoles.applies_to` allows (`periodic`, `invalid`), where nothing says a wire is broken.
_POLE_KINDS = ("pole",)
_MAX_POLE_FIXES = 2  # one face per candidate: one unchanged face would fail the whole call
#: `to_canonical` declines the whole call on the FIRST target that is already analytic or is not a plane, cylinder,
#: cone or sphere within the tolerance, so it is swept one free-form face at a time. One rung, at half the fidelity
#: bound: the recogniser needs slack to see a near-plane, and the staged healer has already tried every face at
#: once at `stages.DEFAULTS["faces"]["tol_mm"]`, which one unrecognisable face is enough to decline.
_CANONICAL_KINDS = ("proximity", "sliver", "invalid")  # = ops.faces.ToCanonical.site_kinds
_CANONICAL_SHARE = 0.5
_MAX_CANONICAL_FACES = 2


def _clash_pairs(site: Site) -> tuple[list[list[StableId]], list[dict]]:
    """The site's MEASURED face pairs, split into near misses (for the proximity split) and CONTACTS (gap 0, which
    that op cannot size a plan from). Nearest first, as the kernel ordered them."""
    near: list[list[StableId]] = []
    contacts: list[dict] = []
    for c in site.graph.get("clashes") or []:
        a, b = c.get("a"), c.get("b")
        if not (isinstance(a, str) and isinstance(b, str)):
            continue
        if c.get("touching"):
            contacts.append(dict(c))
        elif [a, b] not in near:
            near.append([a, b])
    return near[:_MAX_PAIRS], contacts


def _of_kind(entities: list[StableId], prefix: str) -> list[StableId]:
    return [e for e in entities if e[:1] == prefix]


#: Site kinds where removing the site's faces (or the recognised features among them) is a plausible repair: small or
#: thin faces, degenerate poles, faces too close or crossing, a mesher stuck on them. NOT `open` (faces are missing
#: already), `overlap` (between bodies), `invalid` (topology) or an unknown kind.
#: D18: a face at a located failure is narrow at this aspect (area / min_width^2, long side over short) and a sliver
#: at the second; lane A11's two classes (8-20:1 narrow, 20:1 or worse sliver)
NARROW_ASPECT = 8.0
SLIVER_ASPECT = 20.0
#: faces per site the aimed unify/remove_small rows are made for, most elongated first
MAX_AIMED_SMALL = 2

#: D17, owner direction: the det sweep does NOT propose the whole recognised set (every hole and blend on the part,
#: removed in one call) unless a DetGenerator is built with `whole_part_defeature=True`. Basis: lane D15 on dev_v1r,
#: on the search's calibrated rank, that candidate was worse than its parent 18 of 22 times and made the part
#: INVALID 14 of 22. The aimed rows stay on (`located`: the faces a mesher's own failure named; the recognised holes
#: and blends touching a site's faces; the site's faces), and so does the plain `defeature(faces)` op in the AI's
#: catalogue. The code is kept so a later measurement can turn it back on.
WHOLE_PART_DEFEATURE = False
#: B20: whether a pair row may be built from COMBINATIONS at a site whose pairs the kernel could not measure.
#: OFF by default. Dropping an UNMEASURED guess is the one rule in B20 that is defensible rather than provable - a
#: guessed pair could in principle be the one that wins - so it is a flag, and `notes` says when it fires.
#: CAD_REPAIR_V4_GUESS_PAIRS=1 restores the old behaviour.
GUESS_PAIRS = os.environ.get("CAD_REPAIR_V4_GUESS_PAIRS", "") not in ("", "0", "false", "False")

#: how many of a mesher's named faces one aimed overlap op may take. gmsh names two surfaces; a third would mean
#: two failures merged into one site, and a wider target is a wider edit than the mesher asked for.
_MAX_OVERLAP_FACES = 2


def _located_first(site: Site, faces: list[StableId]) -> list[StableId]:
    """`faces`, with the ones a panel member's located failure NAMED moved to the front (order otherwise kept).

    The site's face list is in topological order, so an op capped at the first few faces was aiming at face 1 of a
    843-face part while the mesher's message named surface 108. Nothing is dropped: an op the named faces do not fix
    can still reach the rest."""
    named = [f for f in (site.graph or {}).get("mesher_faces") or [] if f in faces]
    if not named:
        return list(faces)
    rest = [f for f in faces if f not in set(named)]
    return named + rest


DEFEATURE_SITE_KINDS = frozenset({"sliver", "proximity", "pole", "self_overlap", "intersection", "timeout"})


class DetGenerator:
    """Per site kind, a fixed parameter sweep over the ops that apply to it. Only ops present in
    `REGISTRY` are proposed. Sweeps are ordered mildest first; `k` truncates.

    `unfixable` is the registry shared with the AI proposer: a site the model declared unfixable within the
    fidelity budget is not swept (`ai_generators` builds the pair; `search` refuses a mismatch)."""

    def __init__(
        self,
        unfixable: UnfixableRegistry | None = None,
        *,
        diagonal: Callable[[Workspace], float] | None = None,
        recipes: Any = None,
        recipe_k: int = 3,
        whole_part_defeature: bool = WHOLE_PART_DEFEATURE,
        guess_pairs: bool = GUESS_PAIRS,
    ) -> None:
        self.unfixable = unfixable
        #: a `recipes.RecipeBook` (CAD_REPAIR_V4_RECIPES): steps that took similar dev sites to clean, replayed
        #: on this site's own entities BEFORE the fixed sweep. None: no recipe memory.
        self.recipes = recipes
        self.recipe_k = recipe_k
        #: D17: propose the WHOLE recognised set as one det candidate (off by default; see WHOLE_PART_DEFEATURE)
        self.whole_part_defeature = whole_part_defeature
        #: B20: build pair rows from COMBINATIONS when the kernel could not measure the site's pairs. OFF: the one
        #: pruning rule here that is defensible rather than provable, so it is a flag (see GUESS_PAIRS).
        self.guess_pairs = guess_pairs
        #: workspace -> bounding-box diagonal in mm. `search()` binds the kernel's `ws_diagonal` (`bind_pool`) when
        #: this is None. Without one no size can be made: the ops that take a size (`sew`, `remove_small`) are
        #: not proposed, and `notes` says so.
        self.diagonal = diagonal
        #: B21: state id -> the score it was expanded with, so a child can tell whether the edge that made it was
        #: KEPT (its score held or improved). The parent is always expanded before its child, so it is always here.
        self._score_of: dict[str, Score] = {}
        #: why a sweep row was not made (no diagonal, empty window): the absence is on the record, not silent
        self.notes: list[str] = []
        #: ops not proposed, by the class of decline predicted (a count, so the saving is measurable)
        self.skipped: collections.Counter[str] = collections.Counter()
        self._diag_cache: dict[str, float] = {}
        #: workspace -> Raymond Lee's recognised holes/blends/slot ends (ops.defeature.recognised_features), bound by
        #: `bind_pool`; None: the recogniser is not a candidate source. Asked only when a site wants it.
        self.features: Callable[[Workspace], list[dict]] | None = None
        self._feature_cache: dict[str, list[dict]] = {}

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def bind_pool(self, pool: Any) -> None:
        """Measure diagonals with `pool`'s `ws_diagonal` (the call `h_mm_for` uses), and recognise features with the
        pool's `v4_recognise_features`, unless either was given."""
        if self.features is None:
            from meshpipeline.cad.repair_v4.ops.defeature import recognised_features

            def recognise(ws: Workspace) -> list[dict]:
                return recognised_features(pool, ws)

            self.features = recognise
        if self.diagonal is not None:
            return

        def measure(ws: Workspace) -> float:
            return diagonal_mm(pool, ws)

        self.diagonal = measure

    def _features_of(self, ws: Workspace) -> list[dict]:
        """The recognised features of `ws`, once per state; [] (on the record) when the recogniser cannot run."""
        if self.features is None:
            return []
        if ws.brep_path not in self._feature_cache:
            try:
                self._feature_cache[ws.brep_path] = list(self.features(ws))
            except Exception as exc:  # noqa: BLE001 - a candidate source never stops the search
                self._note(f"no recognised features: {type(exc).__name__}: {exc}")
                self._feature_cache[ws.brep_path] = []
        return self._feature_cache[ws.brep_path]

    def _aimed_small_rows(self, site: Site, has_solid: bool) -> list[tuple[str, list[StableId], dict, str]]:
        """D18: the sliver and narrow faces AT a mesher's located failure, merged into their neighbours
        (`unify_same_domain` among them) or removed (`remove_small`), most elongated first. Never part-wide.

        Anchored on D16's `located` source: only a site where a real mesher (not layer 1) named faces. The candidates
        are the faces of the site's measured neighbourhood (`graph["nodes"]`, hop 0 = the site's faces, hop 1 = their
        neighbours) whose aspect - area / min_width^2, the long side over the short of a strip - is at least
        `NARROW_ASPECT`. Lane A11's caveat: a `low_quality` location is the worst ELEMENT, not necessarily the bad
        geometry (on 263d5e2d5bab both members name face 5 while the 2995:1 sliver is face 9), so where every failure
        at the site is low_quality the faces are ranked by aspect alone and a neighbour may be chosen; otherwise the
        faces the mesher named come first."""
        graph = site.graph or {}
        named = [f for f in graph.get("mesher_faces") or [] if f[:1] == "F"]
        meshers = [m for m in site.meshers_failing if m != "valid"]
        if not named or not meshers or graph.get("coarse") or site.kind in ("open", "unbounded"):
            return []
        kinds = {f.get("error_kind") for f in graph.get("failures") or [] if f.get("mesher") != "valid"}
        worst_element_only = bool(kinds) and kinds <= {"low_quality"}
        faces: dict[StableId, dict] = {}
        for node in graph.get("nodes") or []:
            fid, area, width = node.get("id"), node.get("area"), node.get("min_width")
            if not isinstance(fid, str) or fid[:1] != "F" or (node.get("hop") or 0) > 1:
                continue
            if not area or not width or width <= 0:
                continue
            aspect = float(area) / float(width) ** 2
            if aspect >= NARROW_ASPECT:
                faces[fid] = {"aspect": aspect, "width": float(width), "length": float(area) / float(width)}
        if not faces:
            self._note(f"site {site.id}: no aimed unify/remove_small: no face of aspect >= {NARROW_ASPECT:g} at the site")
            return []
        adjacent: dict[StableId, set[StableId]] = collections.defaultdict(set)
        for edge in graph.get("edges") or []:
            pair = [f for f in edge.get("faces") or [] if isinstance(f, str) and f[:1] == "F"]
            if len(pair) == 2 and pair[0] != pair[1]:
                adjacent[pair[0]].add(pair[1])
                adjacent[pair[1]].add(pair[0])

        def rank(fid: StableId) -> tuple:
            first = 0 if (worst_element_only or fid in named) else 1
            return (first, -faces[fid]["aspect"], fid)

        rows: list[tuple[str, list[StableId], dict, str]] = []
        for fid in sorted(faces, key=rank)[:MAX_AIMED_SMALL]:
            f = faces[fid]
            what = "sliver" if f["aspect"] >= SLIVER_ASPECT else "narrow face"
            where = "named by the mesher" if fid in named else "beside the face the mesher named"
            if adjacent.get(fid):
                rows.append(("unify_same_domain", [fid, *sorted(adjacent[fid])], {"scope": "targets", "keep": []},
                             f"merge the {f['aspect']:.0f}:1 {what} {fid} ({where}) into its same-domain neighbours"))
            if has_solid:
                # remove_small measures a face as the larger of its inertia width and its second bounding-box extent,
                # which for a tilted strip is about its LENGTH; the limit is the strip's own length so the op's guard
                # does not refuse the face it was aimed at. Proposed only with a solid: without one the op falls back
                # to ShapeFix_FixSmallFace at this precision, which would reach far beyond the target.
                rows.append(("remove_small", [fid], {"max_width_mm": 1.05 * (f["length"] + f["width"])},
                             f"remove the {f['aspect']:.0f}:1 {what} {fid} ({where}), {f['width']:.3g} mm wide"))
        return rows

    def _defeature_rows(self, site: Site, features: Callable[[], list[dict]]
                        ) -> list[tuple[str, list[StableId], dict, str]]:
        """Defeature at a site, most targeted first. Never at a coarse whole-part site: it names no place to remove.

        1. D16, LOCATED: the faces a panel member's own located failure named (`graph["mesher_faces"]`), at ANY site
           kind but `open` (faces already missing) and `unbounded`: the owner's direction after snapshot v2, where
           every whole-part removal scored worse. A gmsh "other" failure becomes an `invalid` site and a periodic one a
           `periodic` site, kinds the D14 rule below left out, so before this a mesher could name faces and get no
           removal aimed at them.
        2. D14: at the kinds it suits, the recognised holes and blends touching those faces, then the site's faces as
           they are (when that differs from 1: they include the proximity context)."""
        from meshpipeline.cad.repair_v4.ops.defeature import defeature_rows

        graph = site.graph or {}
        faces = _of_kind(site.entities, "F")
        if not faces or graph.get("coarse"):
            return []
        meshers = [m for m in site.meshers_failing if m != "valid"]   # layer 1 is not a mesher
        named = [f for f in graph.get("mesher_faces") or [] if f in faces]
        rows: list[tuple[str, list[StableId], dict, str]] = []
        if named and meshers and site.kind not in ("open", "unbounded"):
            rows.append(("defeature", named, {"source": "located"},
                         f"remove the {len(named)} face(s) {', '.join(meshers)} failed on"))
        if site.kind in DEFEATURE_SITE_KINDS and (site.kind == "sliver" or meshers):
            rows += defeature_rows(features(), named or faces)
            if sorted(faces) != sorted(named):
                rows.append(("defeature", faces, {"source": "site"},
                             f"remove the site's {len(faces)} face(s) with BRepAlgoAPI_Defeaturing"))
        return rows

    def _chain_opener(self, state: SearchState) -> str | None:
        """The op this state's own parent edge was, when that edge opened a chain: the verdict KEPT it (its score
        held or improved - a step the verdict pushed down opens nothing, there is nothing to build on) and, where
        aiming matters, it was aimed. None otherwise."""
        via = state.via or {}
        op = str(via.get("op") or "")
        if not op or state.parent is None or op not in CHAIN_FOLLOWUPS:
            return None
        before = self._score_of.get(state.parent)
        if before is None or state.score is None or list(state.score) < list(before):
            return None
        params = dict(via.get("params") or {})
        targets = [t for t in (via.get("targets") or []) if isinstance(t, str)]
        if op == "defeature" and str(params.get("source") or "") not in AIMED_DEFEATURE_SOURCES:
            return None          # the whole recognised set opens nothing: its oracle wins were gamed
        if op == "remove_small" and not targets:
            return None          # a part-wide ladder rung is not an aimed first step
        return op

    def _chain_rows(self, state: SearchState) -> list[tuple[str, list[StableId], dict, str]]:
        """The one follow-up the sweep cannot build for itself: `run_stage`. Everything else a chain asks for is
        already a row for this state, and `propose` HOISTS those rather than duplicating them."""
        opener = self._chain_opener(state)
        if opener is None or RUN_STAGE not in CHAIN_FOLLOWUPS.get(opener, ()):
            return []
        return [(RUN_STAGE, [], {"stage": stage, "params": {}},
                 f"the chain lane D's oracle measured: the {opener} held, so re-condition the part with the "
                 f"{stage} stage")
                for stage in CHAIN_AFTER_DEFEATURE]

    def _diag_of(self, ws: Workspace) -> float | None:
        if self.diagonal is None:
            return None
        if ws.brep_path not in self._diag_cache:
            try:
                self._diag_cache[ws.brep_path] = float(self.diagonal(ws))
            except CheckerUnavailable as exc:
                self._note(f"no diagonal, so no sized ops: {exc}")
                return None
        return self._diag_cache[ws.brep_path]

    @staticmethod
    def _why_no_size(diag: float | None) -> str:
        """Why no size can be made from `diag`: unknown, unbounded (the real reason for a 1e100 mm part) or no window."""
        if diag is None:
            return "the part's diagonal is unknown"
        if diag > UNBOUNDED_DIAGONAL_MM:
            return (f"the input is unbounded (diagonal {diag:.3g} mm exceeds {UNBOUNDED_DIAGONAL_MM:.0e} mm, "
                    "layer 1 reports unbounded_geometry): no diagonal-relative op applies")
        return f"no rung fits the kernel window on a {diag:.6g} mm part"

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        out: list[Candidate] = []
        self._score_of[state.id] = state.score       # B21: so this state's children can tell their edge was kept
        sites = self.unfixable.filter(state.sites, state.lineage) if self.unfixable is not None else state.sites
        diag = self._diag_of(state.ws) if any(s.kind in ("sliver", "open") for s in sites) else None
        volume = next((v for s in sites if s.kind == "open" and isinstance(v := s.graph.get("volume"), dict)), None)
        has_solid = any(sid[:1] == "B" for sid in state.ws.ids)
        seen: set[str] = set()

        def sized() -> float | None:
            """The diagonal for the ops that need a size, asked only when one of them is about to be swept, so a
            state with no such site costs no extra kernel call. A failure to measure is a note, never a crash."""
            try:
                return self._diag_of(state.ws)
            except Exception as exc:  # noqa: BLE001 - no size means no sized op, on the record
                self._note(f"no diagonal for the sized sweeps: {type(exc).__name__}: {exc}")
                return None

        if self.recipes is not None and len(self.recipes) and diag is None:
            diag = sized()                    # a replayed step's millimetres are fractions of THIS part's diagonal
        # part-wide rows only when NOTHING is placed: aim where there is evidence first (a placed site beside a coarse
        # one turned Task 47's one-hole repair from 5 ops into 11)
        only_coarse = all((s.graph or {}).get("coarse") for s in sites)
        cached: list[list[dict]] = []

        def features() -> list[dict]:
            """The recogniser, asked at most once per state and only when a site wants a defeature row."""
            if not cached:
                cached.append(self._features_of(state.ws))
            return cached[0]

        part_rows: list[tuple[str, list[StableId], dict, str]] = []
        if self.whole_part_defeature and any(m != "valid" for s in sites for m in s.meshers_failing):
            # the whole recognised set, removed in ONE call (Raymond's attempt), proposed once per state
            from meshpipeline.cad.repair_v4.ops.defeature import defeature_rows
            part_rows = defeature_rows(features())
        # D16 ORDER: every site's OWN rows first (its kind's sweep, coverage, coarse rows), for all sites; then the
        # defeature rows, which are generic (a face removal any site kind may get) and come after every site-specific
        # op; then the whole recognised set, last. `k` and the op budget cut from the end, so defeature never
        # pre-empts an op that suits the site. Snapshot v2 (lane B13): on the one body_overlap dev file seven
        # defeature applications came first and fuse/cut_by were `skipped_budget`.
        def ordered(site: Site, rows: list[tuple[str, list[StableId], dict, str]]
                    ) -> list[tuple[str, list[StableId], dict, str]]:
            """B19: the site's own kind decides the ORDER of the families, never which are allowed. B20: WITHIN a
            family, a row aimed at something the kernel MEASURED goes ahead of one built from the site at large and
            ahead of a guessed combination. A stable sort, so each builder's own order survives inside a tier."""
            rank = _family_order(site.kind)
            return sorted(rows, key=lambda row: (rank.get(_family_of(row[0]), len(rank)),
                                                 _aimedness(site, row[0], list(row[1]), dict(row[2]))))

        # B21: the chain's second step is hoisted AHEAD of the fair share, the way a trim is. It is at most three
        # rows, it only exists when this state's own parent edge opened the chain, and it is the whole point of the
        # change: lane D's oracle reached these states and the arms did not, because the follow-up sat behind a
        # whole sweep under `k`. `rationale` says which chain it is, so the trace attributes the win.
        chain_rows = self._chain_rows(state)
        own = [(site, ordered(site, [*self._aimed_small_rows(site, has_solid), *self._recipe_rows(site, diag),
                                     *self._sweep(site, diag), *self._coverage(site, sized),
                                     *(self._coarse_rows(site, state.ws, sized) if only_coarse else [])]))
               for site in sites]
        generic = [(site, self._defeature_rows(site, features)) for site in sites]
        whole = [(sites[0], part_rows)] if part_rows else []
        chain = [(sites[0], chain_rows)] if chain_rows and sites else []
        for site, rows in [*chain, *own, *generic, *whole]:
            for op, targets, params, why in rows:
                # B21: `run_stage` is a MACRO, not a registry op - the driver routes it (`_run_stage_candidate`).
                # It reaches det only as a chain's second step; everything else must be a real op.
                if op != RUN_STAGE and op not in REGISTRY:
                    continue
                key = json.dumps([op, list(targets), params], sort_keys=True, default=str)
                if key in seen:  # the same call twice (two sites both ask for it): the second is the first again
                    self.skipped["identical_to_an_earlier_candidate"] += 1
                    continue
                seen.add(key)
                reason = self._declines_in_advance(op, targets, volume, has_solid)
                if reason is not None:
                    self.skipped[reason.split(":")[0]] += 1
                    self._note(f"site {site.id}: {op} not proposed: {reason}")
                    continue
                out.append(Candidate(op, targets, params, "det", f"{why} (site {site.id}, {site.kind})"))
        # trim_unbounded FIRST, ahead of every other row and of the `k` cut (lane C9). A cost fact, not a judgement of
        # repair quality: until the part is bounded, every other candidate's state is unbounded too and is judged
        # invalid without being measured; after the trim every later re-export is an ordinary, cheap one.
        out.sort(key=lambda c: c.op != "trim_unbounded")
        # B21: the chain's rows are taken out of the fair share and put first, so `k` cannot drop the one follow-up
        # the oracle measured. The rest of the list is shared across families as before.
        # B21b: the chain's follow-ups go FIRST, in the order the oracle's win counts put them, ONE row per
        # follow-up op so a single op cannot fill the hoist. They are the sweep's own rows (already aimed, already
        # measured-only), except `run_stage`, which only exists here.
        opener = self._chain_opener(state)
        first: list[Candidate] = []
        if opener is not None:
            taken: set[str] = set()
            for want in CHAIN_FOLLOWUPS.get(opener, ()):
                for c in out:
                    if c.op == want and _candidate_key(c) not in taken:
                        first.append(c)
                        taken.add(_candidate_key(c))
                        break
                if len(first) >= CHAIN_MAX_ROWS:
                    break
            if first:
                self._note(f"chain after a kept {opener}: {', '.join(c.op for c in first)}")
        keys = {_candidate_key(c) for c in first}
        rest = [c for c in out if _candidate_key(c) not in keys]
        return [*first[:k], *_fair_share(rest, max(0, k - len(first[:k])))]

    @staticmethod
    def _declines_in_advance(op: str, targets: list[StableId], volume: dict | None, has_solid: bool) -> str | None:
        """Why `op` on `targets` would decline, when that is KNOWN from the state before the op is spent; else None.

        Only what the kernel's own code makes certain. Everything the kernel has to DISCOVER is left to it: that
        `sew` finds no partner for a free edge, that a fill deviates, that a shell the topology cannot rule out
        still has no volume. A guess here loses a repair silently, so the rule is: unknown means propose."""
        if op == "sew":
            if has_solid:
                return "sew_on_a_solid: the state carries a solid and sew returns shells, so it declines"
            if volume is not None and volume.get("free_edges") == 0:
                return "sew_without_free_edges: the state has no free edge, so sew has nothing to join"
        if op == "make_volume" and volume is not None:
            reachable = volume.get("loose") if not targets else volume.get("any")
            if reachable is False:
                return ("make_volume_without_a_closable_shell: no subset of the faces uses every edge twice, "
                        "so MakerVolume finds no closed volume")
        return None

    def _recipe_rows(self, site: Site, diag: float | None) -> list[tuple[str, list[StableId], dict, str]]:
        """The steps the recipe book says worked on sites like `site`, replayed on `site`'s own entities."""
        if self.recipes is None or not len(self.recipes):
            return []
        from meshpipeline.cad.repair_v4.recipes import MAX_SIMILARITY, replay

        rows: list[tuple[str, list[StableId], dict, str]] = []
        for hit in self.recipes.lookup(site, self.recipe_k):
            made = replay(hit.step, site, diagonal_mm=float(site.graph.get("diagonal_mm") or diag or 0.0) or None)
            if made is None:
                self.skipped["recipe_not_replayable_here"] += 1
                continue
            targets, params = made
            rows.append((hit.step.op, targets, params,
                         f"recipe {hit.recipe_id} step {hit.step_index}: worked on {hit.support} similar clean dev "
                         f"file(s), similarity {hit.score / MAX_SIMILARITY:.2f}"))
        return rows

    def _fill_rows(self, site: Site, diag: float | None) -> list[tuple[str, list[StableId], dict, str]]:
        """One `fill_loop` per free loop the site carries (`graph["free_loops"]`, smallest first), WHATEVER ITS SIZE:
        the 5%-of-diagonal fill cap was removed with the other fidelity gates (owner decision 2026-11-24); the span
        is reported, not limited. G0 only: a planar fill ignores G1 and the op reports what it achieved."""
        loops = site.graph.get("free_loops") or []
        part = float(site.graph.get("diagonal_mm") or diag or 0.0)
        rows: list[tuple[str, list[StableId], dict, str]] = []
        if part > UNBOUNDED_DIAGONAL_MM:
            self._note(f"site {site.id}: fill_loop not proposed: {self._why_no_size(part)}")
            return rows
        if not loops:
            self._note(f"site {site.id}: fill_loop not proposed: the site carries no free loop")
        for loop in loops:
            span = float(loop["span_mm"])
            rows.append(("fill_loop", list(loop["edges"]), {"continuity": "G0"},
                         f"close the free loop of {len(loop['edges'])} edges ({span:.3g} mm) with one face"))
        return rows

    def _coverage(self, site: Site, sized: Callable[[], float | None]) -> list[tuple[str, list[StableId], dict, str]]:
        """Rows for `DET_COVERED_OPS`, appended after the kind's own sweep (so `k` cuts them first)."""
        from meshpipeline.cad.repair_v4.ops.faces import fidelity_bound

        faces = _of_kind(site.entities, "F")
        graph = site.graph or {}
        rows: list[tuple[str, list[StableId], dict, str]] = []
        if faces:   # B19: at EVERY kind, not only self_overlap and an invalid carrying a rebuild code
            # AIMED, lane A11: the faces the mesher NAMED come first. A gmsh overlapping_facets message names its
            # surfaces ("on surface 108 surface 109") and D16 resolves them to faces, so on a 843-face part the
            # rebuild starts at the two the mesher failed on instead of at face 1.
            for face in _located_first(site, faces)[:_MAX_FACE_REBUILDS]:
                rows.append(("rebuild_face", [face], {"continuity": "G0"},
                             "re-fill the face after fixing its self-intersecting wires (G0)"))
        if not faces:
            self._note(f"site {site.id}: fix_poles not proposed: the site names no face")
        if faces:   # B19: fix_poles at every kind, aimed at the site's own faces
            # The staged healer ran fix_poles over every face before the search; an op applied since may have left a
            # new broken wire, and only this row lets the det arm try again on the face the mesher is still failing on.
            for face in _located_first(site, faces)[:_MAX_POLE_FIXES]:
                rows.append(("fix_poles", [face], {},
                             "repair the face's wires (degenerate edge at the pole, missing seam)"))
        # B19: what the op NEEDS, not what the kind allows. The kind only decides the order (`_family_order`).
        wants_edges = any(not e.get("kind") for e in graph.get("edges") or [])
        wants_reapprox = bool(faces)
        wants_refit = len(faces) >= 2
        wants_canonical = bool(faces)
        if not (wants_edges or wants_reapprox or wants_refit or wants_canonical):
            return rows
        diag = sized()
        if diag is None or diag > UNBOUNDED_DIAGONAL_MM:
            for op, wanted in (("rebuild_edge_as_intersection", wants_edges), ("reapproximate", wants_reapprox),
                               ("refit_cluster", wants_refit), ("to_canonical", wants_canonical)):
                if wanted:
                    self._note(f"site {site.id}: {op} not proposed: {self._why_no_size(diag)}")
            return rows
        bound = fidelity_bound(diag)
        if wants_edges:
            limit = _EDGE_REL * diag
            bad = sorted(((max(float(e.get("tolerance") or 0.0), float(e.get("gap") or 0.0)), e["edge"])
                          for e in graph["edges"] if not e.get("kind") and len(e.get("faces") or []) == 2),
                         reverse=True)
            bad = [(worst, eid) for worst, eid in bad if worst > limit]
            if not bad:
                self._note(f"site {site.id}: rebuild_edge_as_intersection not proposed: no edge exceeds "
                           f"{_EDGE_REL:g} x diagonal")
            for worst, eid in bad[:_MAX_EDGE_REBUILDS]:
                rows.append(("rebuild_edge_as_intersection", [eid], {},
                             f"rebuild edge {eid} from its faces' intersection ({worst:.3g} mm off)"))
        surface = {n.get("id"): n.get("surface") for n in graph.get("nodes") or []}
        if wants_reapprox:
            freeform = [f for f in faces if surface.get(f) in _FREEFORM][:_MAX_REAPPROX_FACES]
            if not freeform:
                self._note(f"site {site.id}: reapproximate not proposed: no free-form face is known on the site")
            for share, degree in _REAPPROX_SWEEP if freeform else ():
                rows.append(("reapproximate", freeform, {"tol_mm": share * bound, "max_degree": degree},
                             f"re-approximate the free-form faces within {share:g} of the bound, degree <= {degree}"))
        if wants_canonical:
            # An analytic face declines the call outright ("no_change"), so only a MEASURED free-form carrier is a
            # candidate; a free-form face that is not within the tolerance of a canonical surface is left to the
            # kernel to discover (unknown means propose).
            nearly = [f for f in faces if surface.get(f) in _FREEFORM][:_MAX_CANONICAL_FACES]
            if not nearly:
                self._note(f"site {site.id}: to_canonical not proposed: no free-form face is known on the site")
            for face in nearly:
                rows.append(("to_canonical", [face], {"tol_mm": _CANONICAL_SHARE * bound},
                             f"snap face {face} onto the exact plane/cylinder/cone/sphere it matches within "
                             f"{_CANONICAL_SHARE * bound:.3g} mm ({_CANONICAL_SHARE:g} of the bound)"))
        if wants_refit:
            why = self._refit_refused(faces, graph)
            if why:
                self._note(f"site {site.id}: refit_cluster not proposed: {why}")
            else:
                for share in _REFIT_SHARES:
                    rows.append(("refit_cluster", list(faces), {"tol_mm": share * bound},
                                 f"refit the {len(faces)} faces as one within {share:g} of the bound"))
        return rows

    def _coarse_rows(self, site: Site, ws: Workspace,
                     sized: Callable[[], float | None]) -> list[tuple[str, list[StableId], dict, str]]:
        """A COARSE site (a failure no member placed, or a layer-1 finding that names only a body; `sites._coarse_site`)
        gets part-wide conditioning instead of nothing: the angle split at the two settings the staged healer did not
        use (it ran 90 deg), the continuity split at C2 (it ran C1), and `remove_small` on EXACTLY the small faces and
        edges the checker reported (the op refuses any wider target). Lane C5."""
        coarse = (site.graph or {}).get("coarse")
        if not coarse:
            return []
        faces = sorted((sid for sid in ws.ids if sid[:1] == "F"), key=lambda s: int(s[1:]) if s[1:].isdigit() else 0)
        why = f"part-wide, because {coarse.get('reason', 'no member said where')}"
        rows: list[tuple[str, list[StableId], dict, str]] = []
        if faces:
            for deg in _COARSE_ANGLES:
                rows.append(("split_angle", faces, {"max_deg": deg}, f"split every face at {deg} deg ({why})"))
            rows.append(("split_continuity", faces, {"order": 2}, f"split every face at C2 breaks ({why})"))
        small = [x["entity"] for x in (site.graph or {}).get("soft") or []
                 if x.get("code") in ("small_face", "small_edge") and x.get("entity") in ws.ids]
        if small:
            diag = sized()
            if diag is None or diag > UNBOUNDED_DIAGONAL_MM:
                self._note(f"site {site.id}: remove_small not proposed: {self._why_no_size(diag)}")
            else:
                for width in det_sliver_widths(diag):
                    rows.append(("remove_small", list(dict.fromkeys(small)), {"max_width_mm": width},
                                 f"remove the {len(small)} small entities the checker found ({why})"))
        return rows

    @staticmethod
    def _refit_refused(faces: list[StableId], graph: dict) -> str | None:
        """Why the op's documented limits would refuse this cluster, from the site's own facts (None: propose)."""
        if len(faces) > _REFIT_MAX_FACES:
            return f"{len(faces)} faces is more than the {_REFIT_MAX_FACES} a sweep refits at once"
        nodes = {n.get("id"): n for n in graph.get("nodes") or []}
        if not all(f in nodes for f in faces):
            return "the faces' neighbourhood was not measured, so the cluster's shape is unknown"
        if any((nodes[f].get("angular_span_deg") or 0.0) >= 359.0 for f in faces):
            return "a face spans a full turn (a seam face), which the op refuses"
        links: dict[str, set[str]] = {f: set() for f in faces}
        for e in graph.get("edges") or []:
            a = [f for f in e.get("faces") or [] if f in links]
            if len(a) == 2:
                links[a[0]].add(a[1])
                links[a[1]].add(a[0])
        reached, todo = {faces[0]}, [faces[0]]
        while todo:
            for nxt in links[todo.pop()] - reached:
                reached.add(nxt)
                todo.append(nxt)
        if len(reached) != len(faces):
            return "the faces are not one connected patch, so they have no single outline"
        return None

    def _overlap_rows(self, site: Site, faces: list[StableId]) -> list[tuple[str, list[StableId], dict, str]]:
        """What an `overlapping_facets` failure allows, aimed at the surfaces gmsh named (lane A11, 12 of 35 rows).

        Measured on the real members (gmsh_default / gmsh_alt on the dev parts): the message names TWO surfaces and
        D16 resolves both, and the pair falls into two classes.

        ONE named face ("on surface 9 surface 9"): the face overlaps ITSELF, so the repair is on that face -
        `rebuild_face` and `split_angle`, which the sweep now aims at it rather than at face 1 of 843.

        TWO named faces whose boxes overlap ("on surface 108 surface 109", both 2.5 x 5 x 1150 mm and
        box-overlapping; elsewhere a 2e-7 mm-thick face lying on its neighbour): two faces occupy the same space, so
        the repair is to merge or drop one of them - `unify_same_domain` scoped to JUST those faces (never "all",
        which merges across the part), and `remove_duplicate_faces` for the case where they are two copies. The
        removal of the smaller one is already proposed by `_defeature_rows` through D16's `located` source.

        No located face: nothing here is aimed, so nothing is proposed and the note says why."""
        named = [f for f in (site.graph or {}).get("mesher_faces") or [] if f in faces]
        if not named:
            self._note(f"site {site.id}: no aimed overlap op: the site names no face the mesher located")
            return []
        if len(named) == 1:
            return []     # the face's own ops (rebuild_face, split_angle) are aimed at it above
        pair = named[:_MAX_OVERLAP_FACES]
        return [
            ("unify_same_domain", pair, {"scope": "targets", "keep": []},
             f"merge the {len(pair)} same-domain faces the mesher's facets overlap on"),
            ("remove_duplicate_faces", list(pair), {"pairs": [list(pair)]},
             f"the overlap on {', '.join(pair)} may be two copies of one face"),
        ]

    @staticmethod
    def _contact_rows(contacts: list[dict]) -> list[tuple[str, list[StableId], dict, str]]:
        """What a CONTACT allows (lane A11: the commonest mesher cause on a valid part, 18 of 35 located failures).

        Not `split_by_proximity`: it sizes its plan from the gap (`max(gap, 1e-9)`), so a zero gap asks for an
        infinitely fine split. What fits is removing one side of the contact, or separating the two faces:
        `remove_duplicate_faces` when the pair is two copies of one face, and `defeature` of the SMALLER face
        (the one whose loss costs least), through D16's `located` source so the removal is aimed at the mesher's
        own face and never part-wide. With no measured areas neither side is guessed at: both are offered."""
        rows: list[tuple[str, list[StableId], dict, str]] = []
        if not contacts:
            return rows
        pairs = [[str(c.get("a")), str(c.get("b"))] for c in contacts][:_MAX_PAIRS]
        rows.append(("remove_duplicate_faces", sorted({f for p in pairs for f in p}), {"pairs": pairs},
                     f"the {len(pairs)} contact(s) may be two copies of one face"))
        offered: set[str] = set()
        for c in contacts:
            a, b = str(c.get("a")), str(c.get("b"))
            area_a, area_b = c.get("area_a_mm2"), c.get("area_b_mm2")
            if area_a is not None and area_b is not None:
                smaller = [a if float(area_a) <= float(area_b) else b]
            else:
                smaller = [a, b]      # nothing measured says which side is small; offer each on its own
            for face in smaller:
                if face in offered:
                    continue
                offered.add(face)
                rows.append(("defeature", [face], {"source": "located"},
                             f"remove the smaller face of the contact {a}-{b}"))
        return rows

    def _sweep(self, site: Site, diag: float | None) -> list[tuple[str, list[StableId], dict, str]]:
        faces = _of_kind(site.entities, "F")
        solids = _of_kind(site.entities, "B")
        rows: list[tuple[str, list[StableId], dict, str]] = []
        kind = site.kind
        # A CONTACT IS GEOMETRY, NOT A SITE KIND (lane C12's second finding; this builder is mine). Two faces
        # measured at gap 0 need the same ops wherever the clash was measured, so the contact rows are read for
        # EVERY kind. Lane C found a post-cut `self_overlap` site carrying 4 touching clashes with both areas
        # (F127-F123 at 768/620 mm2) while the sweep built 0 contact rows from 14 proposals, because this was
        # reached only for `proximity`. The near-miss split stays proximity's own: its plan is sized from the gap,
        # which only a near miss has.
        near, contacts = _clash_pairs(site)
        if len(faces) >= 2:   # B19: the near-face split at every kind that names two faces
            # AIMED, lane A11: the pairs the kernel MEASURED (`graph["clashes"]`), nearest first, not every
            # combination of the site's faces - a combinatorial list proposes work no mesher complained about and
            # crowds the one pair it did out of the op budget. With no measured pair, fall back to the old sweep.
            # B20(3): the pairs the kernel MEASURED, and no guessed combination unless it could not measure. A
            # site whose pairs WERE measured and showed no near miss has nothing for this op to size a plan from,
            # and lane A's bisect measured three such evaluations standing in front of the op that cleaned the file.
            pairs = near
            if not pairs and not contacts:
                unmeasured = int((site.graph or {}).get("unmeasured_pairs") or 0)
                if unmeasured and self.guess_pairs:
                    pairs = [list(p) for p in itertools.combinations(faces, 2)][:_MAX_PAIRS]
                    self._note(f"site {site.id}: split_by_proximity on GUESSED pairs: the kernel could not measure "
                               f"{unmeasured} pair(s) here")
                else:
                    self.skipped["no_measured_pair"] += 1
                    self._note(f"site {site.id}: split_by_proximity not proposed: the kernel measured no near pair"
                               + (f" and {unmeasured} pair(s) it could not measure are not guessed at"
                                  if unmeasured else ""))
            for ratio in _PROXIMITY_RATIOS if pairs else ():
                rows.append(
                    (
                        "split_by_proximity",
                        [],
                        {"face_pairs": pairs, "target_ratio": ratio},
                        f"split the near faces so chords sag at most {ratio} of the gap",
                    )
                )
        rows += self._contact_rows(contacts)
        rows += self._overlap_rows(site, faces)      # B19: the duplicate/contact removals at every kind
        if faces:
            # B19 + A11's caveat: the faces a member LOCATED are split FIRST and on their own, then the site's whole
            # face set behind them. A `low_quality` location is the worst ELEMENT, so the named face is not
            # necessarily the defect - which is why the aimed call comes first but the wider one is still offered.
            aimed = _located_first(site, faces)[:_MAX_FACE_REBUILDS]
            for deg in _SPLIT_ANGLES:
                rows.append(("split_angle", aimed, {"max_deg": deg},
                             f"split the {len(aimed)} face(s) the mesher located at {deg} deg"))
            if sorted(faces) != sorted(aimed):
                for deg in _SPLIT_ANGLES:
                    rows.append(("split_angle", faces, {"max_deg": deg},
                                 f"split all {len(faces)} of the site's faces at {deg} deg"))
            rows.append(("split_closed", faces, {}, "split closed periodic faces at the seam"))
            for order in (1, 2):
                rows.append(("split_continuity", faces, {"order": order}, f"split faces at C{order} breaks"))
        if solids:
            rows.append(("orient_and_close", solids, {}, "fix shell orientation"))
        if kind == "invalid":
            if "no_solids" in (site.graph.get("hard_codes") or ()):
                # Loose faces and no solid at all: a shell that is closed (a fill just closed it) still has to be
                # made a solid. make_volume declines when the faces enclose nothing.
                rows.append(("make_volume", faces, {}, "no solid exists: build one from the closed shell"))
        if site.entities:   # B19: the sliver ladder and the same-domain merge at every kind
            sized = diag is not None and diag <= UNBOUNDED_DIAGONAL_MM
            if not sized:
                self._note(f"site {site.id}: remove_small not proposed: {self._why_no_size(diag)}")
            for width in det_sliver_widths(diag) if diag is not None and sized else ():
                rows.append(
                    (
                        "remove_small",
                        site.entities,
                        {"max_width_mm": width},
                        f"remove entities narrower than {width} mm",
                    )
                )
            rows.append(("unify_same_domain", [], {"scope": "all", "keep": []}, "merge same-domain faces"))
        if True:   # B19: the shell-closing family at every kind (`_declines_in_advance` still refuses a sew on a solid)
            ladder = scaled_sew_ladder(diag) if diag is not None and diag <= UNBOUNDED_DIAGONAL_MM else []
            if ladder:
                rows.append(
                    (
                        "sew",
                        site.entities if site.entities else [],
                        {"tol_ladder": ladder},
                        "sew at the tightest closing tolerance",
                    )
                )
            else:
                self._note(f"site {site.id}: sew not proposed: {self._why_no_size(diag)}")
            rows.extend(self._fill_rows(site, diag))
            rows.append(("make_volume", faces, {}, "build a solid from the loose faces"))
            rows.append(("orient_and_close", solids, {}, "orient and close the shell"))
        # A WHOLE-PART coarse site (`sites._coarse_site`: a failure nobody placed) lists every body, which is no evidence
        # that any two touch or overlap; body ops there fragmented already-conformal bodies for nothing (lane C10: 11
        # no-op conformalise calls on one v2 file). It gets only its part-wide conditioning rows (`_coarse_rows`).
        whole_part = bool(((site.graph or {}).get("coarse") or {}).get("whole_part"))
        # B19 + lane C: ANY site naming 2+ bodies gets the body rows, whatever its kind. A whole-part coarse site
        # still gets none: it lists every body and is no evidence that any two touch (C10 measured 11 no-op
        # conformalise calls from exactly that).
        if not whole_part:
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
            if len(solids) >= 2:
                # fragment declines every REAL overlap (no keeper: `unresolved_overlap`); cut_by is the mode that
                # resolves one. Lane C4: 10 of the smoke's 30 files carried body_overlap and det never proposed it.
                for a, b in list(itertools.combinations(solids, 2))[:_MAX_CUT_PAIRS]:
                    for keep in (a, b):
                        rows.append(("conformalise", [a, b], {"mode": f"cut_by:{keep}", "fuzzy_mm": 0.0},
                                     f"cut the overlap out of the other body, keeping {keep}"))
                # after both cuts: one body instead of two (a body inside another, two copies of one body), which the
                # owner's 2026-11-24 goal allows (lane C7)
                for a, b in list(itertools.combinations(solids, 2))[:_MAX_CUT_PAIRS]:
                    rows.append(("fuse", [a, b], {"fuzzy_mm": 0.0}, f"fuse {a} and {b} into one body"))
                if len(solids) > 2:
                    # ONE KEEPER, MANY VICTIMS (lane C11): on dev 0051 one body overlapped three others and the cut
                    # that resolved layer 1 was `cut_by:B4` over ALL FOUR bodies, which no pair row can express.
                    group = solids[:_MAX_CUT_GROUP]
                    for keep in group:
                        rows.append(("conformalise", group, {"mode": f"cut_by:{keep}", "fuzzy_mm": 0.0},
                                     f"cut {keep} out of all {len(group) - 1} others at once"))
                    rows.append(("fuse", group, {"fuzzy_mm": 0.0},
                                 f"fuse all {len(group)} of the site's bodies into one"))
            if len(faces) >= 2:
                # B20(3): a duplicate face pair is a CONTACT, which the kernel measures. Only when it could not
                # measure the site's pairs is the combination guessed at, and then only behind everything aimed.
                measured = [[c["a"], c["b"]] for c in contacts if c.get("a") and c.get("b")]
                dup_pairs = measured or ([list(p) for p in itertools.combinations(faces, 2)][:_MAX_PAIRS]
                                         if self.guess_pairs and (site.graph or {}).get("unmeasured_pairs") else [])
                if dup_pairs:
                    rows.append(("remove_duplicate_faces", [], {"pairs": dup_pairs},
                                 "remove the coincident duplicate faces the kernel measured"
                                 if measured else "remove coincident duplicate faces (pairs guessed: unmeasured)"))
                else:
                    self.skipped["no_measured_contact"] += 1
        # trim_unbounded is conditioned on a layer-1 FINDING, not on a kind: on a bounded part it declines
        # `nothing_unbounded` AND would take the hoisted first slot below, so it is offered wherever the finding is.
        if kind == "unbounded" or "unbounded_geometry" in ((site.graph or {}).get("hard_codes") or ()):
            for pad in _TRIM_PADS:
                rows.append(("trim_unbounded", [], {"pad_fraction": pad},
                             f"bound the infinite surfaces to the part's extent plus {pad:g} of its diagonal"))
        return rows



#: WHICH FAMILY an op belongs to, for B19's ordering and for the fair share under `k`. A family is a kind of repair,
#: not a kind of site: the point of B19 is that the site's kind decides the ORDER of the families, never which of
#: them is allowed.
_OP_FAMILY: dict[str, str] = {
    "split_by_proximity": "near_faces", "split_angle": "face_split", "split_closed": "face_split",
    "split_continuity": "face_split", "rebuild_face": "face_rebuild", "fix_poles": "face_rebuild",
    "reapproximate": "surface_refit", "to_canonical": "surface_refit", "refit_cluster": "surface_refit",
    "rebuild_edge_as_intersection": "edge", "remove_small": "small", "unify_same_domain": "small",
    "defeature": "remove", "remove_duplicate_faces": "remove",
    "sew": "shell", "make_volume": "shell", "orient_and_close": "shell", "fill_loop": "shell",
    "conformalise": "body", "fuse": "body", "trim_unbounded": "trim", "run_stage": "stage",
    "tighten_tolerances": "condition",
}
#: what each site kind PREFERS, first; every other family follows in `_FAMILIES` order. Read off what each kind used
#: to be the only one allowed (`oracle_face_grid.py` / `oracle_grid.py` list those gates), so a kind keeps exactly
#: the rows it had at the front of its list and gains the rest behind them.
_PREFERRED_FAMILIES: dict[str, tuple[str, ...]] = {
    "proximity": ("near_faces", "face_split", "surface_refit", "edge"),
    "self_overlap": ("face_rebuild", "face_split", "remove", "surface_refit", "edge", "body"),
    "periodic": ("face_split", "surface_refit", "face_rebuild"),
    "invalid": ("face_split", "shell", "face_rebuild", "edge", "surface_refit"),
    "sliver": ("small", "remove", "surface_refit"),
    "open": ("shell", "small"),
    "overlap": ("body", "remove", "edge", "surface_refit"),
    "intersection": ("body", "remove", "edge"),
    "pole": ("face_rebuild", "surface_refit", "face_split"),
    "unbounded": ("trim",),
    "timeout": ("small", "remove", "face_split"),
}
#: the order a family is offered in when the kind does not prefer it
_FAMILIES: tuple[str, ...] = ("near_faces", "face_split", "face_rebuild", "small", "shell", "body", "remove",
                              "surface_refit", "edge", "condition", "trim", "stage")



#: HOW WELL AIMED a row is, lowest first. B20 (owner priority 2026-11-24): every verdict spent on a candidate that
#: cannot win is wall-clock taken from one that can, and lane A's bisect measured a file at 1230 s of search - past
#: its 900 s wall - with three COMBINATORIAL `split_by_proximity` evaluations sitting in front of the op that
#: cleaned it. Aimedness is read off the row and the site, so no builder has to declare it.
AIMED_MEASURED = 0   # the entities the kernel itself measured: a located face, a measured clash, a named body
AIMED_SITE = 1       # the site's own entities, but not the ones a measurement singled out
AIMED_GUESSED = 2    # a combination the kernel never measured, or a part-wide sweep



#: B21b: THE CHAINS, from lane D's oracle pass 1 (`lane-d21/pass1/winners_by_defect.jsonl`, 658 winning paths). Of
#: its 316 UNGAMED chain wins that no v4 arm reached, B21's two hardcoded chains opened 37. This table is the same
#: data, read as "what followed what": opener -> follow-up ops, ORDERED BY MEASURED WINS. The follow-ups are not new
#: rows - they are the sweep's own rows for this state, hoisted ahead of the fair share - so every B20 rule still
#: binds (a guessed pair is still not built, a duplicate is still dropped). `run_stage` is the one exception, since
#: the sweep does not build it; it reaches det only here.
#:   defeature    30 defeature, 23 run_stage, 17 conformalise, 14 fuse, 11 split_angle
#:   remove_small 24 split_angle, 22 defeature, 15 run_stage, 14 split_by_proximity, 6 remove_small, 6 rebuild_face
#:   fuse         26 conformalise, 6 fuse, 4 defeature
#:   conformalise 8 defeature, 6 remove_small, 6 conformalise
#:   rebuild_face 8 split_angle, 8 run_stage
#:   split_angle  8 run_stage, 4 split_continuity
#:   fill_loop    4 remove_small
#:   split_by_proximity 3 remove_small, 3 split_by_proximity
CHAIN_FOLLOWUPS: dict[str, tuple[str, ...]] = {
    "defeature": ("defeature", RUN_STAGE, "conformalise", "fuse", "split_angle"),
    "remove_small": ("split_angle", "defeature", RUN_STAGE, "split_by_proximity", "rebuild_face", "remove_small"),
    "fuse": ("conformalise", "fuse", "defeature"),
    "conformalise": ("defeature", "remove_small", "conformalise"),
    "rebuild_face": ("split_angle", RUN_STAGE),
    "split_angle": (RUN_STAGE, "split_continuity"),
    "fill_loop": ("remove_small",),
    "split_by_proximity": ("remove_small", "split_by_proximity"),
}
#: the stages a kept opener is followed by, when `run_stage` is one of its follow-ups
CHAIN_AFTER_DEFEATURE: tuple[str, ...] = ("bodies", "condition", "shells")
#: at most this many rows are hoisted ahead of the fair share for a chain. The hoist is what makes the follow-up
#: survive `k`; letting a whole table through would undo B20's breadth control, so it is capped and ordered by the
#: measured win count.
CHAIN_MAX_ROWS = 6
#: `defeature` params["source"] values that count as AIMED (D16's located faces, D14's site faces). The whole
#: recognised set ("recognised") is NOT a chain opener: its oracle wins were gamed (x3.98) and it stays off in arms.
AIMED_DEFEATURE_SOURCES: frozenset[str] = frozenset({"located", "site"})

def _candidate_key(c: Candidate) -> str:
    """A candidate's identity for B20's duplicate rule: the op, its targets as a SET, and its params."""
    return json.dumps([c.op, sorted(c.targets), c.params], sort_keys=True, default=str)


def _target_key(c: Candidate) -> str:
    """The op and WHERE it was aimed, without its parameters: what a `no_change` decline is about."""
    return json.dumps([c.op, sorted(c.targets)], sort_keys=True, default=str)


def _aimedness(site: Site, op: str, targets: list[StableId], params: dict) -> int:
    """Where a row's targets came from: a measurement, the site, or a guess."""
    graph = site.graph or {}
    located = {f for f in (graph.get("mesher_faces") or []) if isinstance(f, str)}
    measured_pairs = {frozenset(p) for p in ([[c.get("a"), c.get("b")] for c in graph.get("clashes") or []])
                      if len(set(p)) == 2}
    pairs = params.get("face_pairs") or params.get("pairs") or []
    if pairs:
        got = [frozenset(p) for p in pairs if isinstance(p, (list, tuple)) and len(set(p)) == 2]
        if got and all(p in measured_pairs for p in got):
            return AIMED_MEASURED
        return AIMED_GUESSED          # a combination the kernel never measured
    if not targets:
        # a part-wide row (scope "all", a ladder over the whole part): the last thing to spend a verdict on
        return AIMED_SITE if op in ("sew", "orient_and_close", "make_volume") else AIMED_GUESSED
    on = set(targets)
    if located and on <= located:
        return AIMED_MEASURED
    if on <= set(site.entities):
        return AIMED_SITE
    return AIMED_GUESSED

def _family_of(op: str) -> str:
    return _OP_FAMILY.get(op, op)


def _family_order(kind: str) -> dict[str, int]:
    """Family -> rank for a site of this kind: its preferred families first, then the rest in `_FAMILIES` order."""
    pref = _PREFERRED_FAMILIES.get(kind, ())
    rest = [f for f in _FAMILIES if f not in pref]
    return {fam: i for i, fam in enumerate([*pref, *rest])}


def _fair_share(cands: list[Candidate], k: int) -> list[Candidate]:
    """`k` candidates, taken ROUND ROBIN across families in the order they appear.

    B19's second half. With every family offered at every kind the list is longer than `k`, and a flat `[:k]` let one
    family eat the budget: lane A measured the det sweep PROPOSING `split_angle` at 60 on 3596803faa and then
    dropping it under the cap, on a file where that very candidate reaches clean in one step. A family contributes
    its first row before any family contributes its second, so the right candidate cannot be crowded out by another
    family's rungs - and the per-site order within a family is kept, so the aimed rows still come first.
    """
    if len(cands) <= k:
        return cands
    buckets: dict[str, list[Candidate]] = {}
    for c in cands:
        buckets.setdefault(_family_of(c.op), []).append(c)
    out: list[Candidate] = []
    while len(out) < k and any(buckets.values()):
        for fam in list(buckets):
            if buckets[fam] and len(out) < k:
                out.append(buckets[fam].pop(0))
    return out


# --- the driver ---------------------------------------------------------------------------------


def _neg(score: Score) -> tuple:
    return tuple(-x for x in score)


def _entry(state: SearchState, rank: Score, order: int) -> tuple:
    """A frontier entry; `heapq` pops the smallest, so every key is negated or ascending.

    `(-rank, -depth, order, rank, state)`: the better score first; of EQUAL scores the DEEPER state first; of equal
    score and equal depth the one pushed first (`order` is the search's own monotonic counter, taken in the driver
    thread in proposal order, so it never depends on which worker finished first). A multi-op repair is a chain of ops
    that each keep the score (a `fill_loop` that closes one hole of three improves nothing yet), so ties are exactly
    where the search has to go DEEP; with FIFO alone it went breadth-first over every subset of fills."""
    return (_neg(rank), -state.depth, order, rank, state)


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
    """Two workspaces with the same entity fingerprints AND the same shape signature are the same state, however
    reached. Entity fingerprints are centroid-and-box only, so alone they merged a solid with its inside-out
    twin and discarded `orient_and_close`'s output as a duplicate (Task 45); the signature carries orientation,
    shell layout and closedness."""
    return (tuple(sorted(ws.ids.values())), ws.signature)


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
    #: how many ops the op library ACTUALLY applied for this candidate: 1 for a plain op, and for a
    #: `run_stage` macro whatever its body applied (`StagedResult.applied_ops`), which is 2 or more for a
    #: chain. This is what the op budget is charged (`max(1, applied)`); it is never derived from the op or
    #: stage NAME, so a stage body that changes cannot leave a stale cost behind.
    applied: int = 0
    #: what the caller knows beyond `errors` (a rolled-back stage: which cheap member got worse and by how much).
    #: It reaches the proposer as the structured reason's `detail`; the message is left alone.
    detail: str = ""


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
    # the ops the stage body really applied, however it ended: a rolled-back or half-applied chain did
    # that kernel work too, and the budget is charged for work, not for outcome
    applied = int(result.applied_ops.get(name, 0))
    if name in result.inconclusive:
        return _Outcome(c, gi, "inconclusive",
                        [f"inconclusive: stage {name!r} could not be judged (checker unavailable)"],
                        applied=applied)
    if name in result.rolled_back:
        # the message stays exactly what it was (arms.py and triage.py group on it); WHY, in the cheap tier's own
        # numbers, travels as the structured reason the proposer feeds back (`detail`)
        return _Outcome(c, gi, "declined", [f"stage {name!r} was rolled back: it lowered the cheap score"],
                        applied=applied, detail=result.rollback_why.get(name, ""))
    if name in result.skipped:
        return _Outcome(c, gi, "declined", [f"stage {name!r} changed nothing: {result.skipped[name]}"],
                        applied=applied)
    diffs = result.ws.history[len(base.history) :]
    if result.ws is base or not diffs:
        return _Outcome(c, gi, "declined", [f"stage {name!r} changed nothing"], applied=applied)
    return _Outcome(c, gi, "ok", ws=result.ws, diffs=list(diffs), applied=applied)


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
        message = "; ".join(out.errors)
        reason = reason_of(_STATUS[out.kind], message)
        if out.detail and reason is not None:  # the caller knows more than the message says
            reason = {**reason, "detail": out.detail}
        return Outcome(proposed, _STATUS[out.kind], message=message, reason=reason)
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


def arm_generators(arm: str, *, recipes: Any = None, **ai_kwargs: Any) -> tuple[list[Any], AIProposer | None]:
    """The generators of an eval/engine arm: `search_det` -> the deterministic sweep only (no proposer is built,
    so no model can be reached); `search_ai` -> `ai_generators(**ai_kwargs)`. An unknown arm is a ValueError.
    The entry points that select an arm (`repair_v4._run_search`, Task 20; the eval arms, Task 21) call this.
    `recipes` (a `recipes.RecipeBook`, CAD_REPAIR_V4_RECIPES) seeds both arms' DetGenerator and the AI's observation;
    None leaves both arms exactly as they were."""
    if arm == "search_det":
        return [DetGenerator(recipes=recipes)], None
    if arm == "search_ai":
        return ai_generators(recipes=recipes, **ai_kwargs)
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
    recipes: Any = None,
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
        recipes=recipes, **kwargs,
    )
    return [DetGenerator(unfixable=shared, recipes=recipes), proposer], proposer


#: the least patience the search ever has, however few holes the part presents
PATIENCE_FLOOR = 3
#: rounds of slack beyond one round per hole: one for the op that is not a fill (`make_volume`), one spare
PATIENCE_SLACK = 2


def open_site_count(sites: list[Site]) -> int:
    """How many holes the part presents, as the search sees them: the closed free loops layer 1's open finding
    carries (`graph["free_loops_total"]`, the uncapped count; every open site carries the same part-wide list),
    or at least one per `open` site when the loops could not be read. This is the number of `fill_loop`s the
    deterministic sweep can make, so it is the number of non-improving rounds a repair may need before its volume."""
    # a coarse site (`sites._coarse_site`: a member that said nothing about where) is not a hole of its own
    open_sites = [s for s in sites if s.kind == "open" and not (s.graph or {}).get("coarse")]
    loops = max((int(s.graph.get("free_loops_total") or len(s.graph.get("free_loops") or ())) for s in open_sites),
                default=0)
    return max(loops, len(open_sites))


def scaled_patience(open_sites: int) -> int:
    """`max(3, open_sites + 2)`: patience grows with the work the part presents. Always bounded by the wall clock
    and the op budget, and in fact by the latter alone: a round spends at least one op to add a state, so no more
    than `ops + 1` rounds can run."""
    return max(PATIENCE_FLOOR, open_sites + PATIENCE_SLACK)


def search(
    pool: Any,
    start: Workspace,
    original: str,
    generators: list[Generator],
    budget: Budget,
    *,
    workers: int,
    k_confirm: int = 3,
    patience: int | None = None,
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
    for g in generators:
        if isinstance(g, DetGenerator):
            g.bind_pool(pool)  # the sweep sizes its ops against this part's diagonal
    any_gens: list[Any] = list(generators)  # generators may also carry feedback()/result(), outside the protocol
    trace = Trace(clock)
    t0 = clock()
    state_ids = itertools.count()
    tie = itertools.count()
    spent = {"ops": 0}
    lineages: dict[str, tuple[str, ...]] = {}
    by_id: dict[str, SearchState] = {}

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
        if s["wall_s"] >= propose_wall_s:
            return "wall"
        if s["ops"] >= budget.ops:
            return "ops"
        return None

    def can_reprompt() -> bool:
        """A model generator is still active, has not stopped itself (max turns, its per-file $ ceiling, the wave
        cap, an unreachable model), and the search's model budget has room."""
        live = [gi for gi in model_gens if gi in active and getattr(generators[gi], "stop_reason", None) is None]
        if not live or ai_capped or best.verdict.clean:
            return False
        calls, usd = usage()
        return calls < budget.model_calls and usd < budget.model_usd

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
        by_id[sid] = SearchState(
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
        return by_id[sid]

    def judge_cheap(ws: Workspace) -> tuple[Any, Score]:
        """The cheap verdict and its score. An inconclusive verdict (the pool failed, not the part) is retried;
        what stays inconclusive raises `InconclusiveVerdict` / `CheckerUnavailable` to the caller, which owns the
        branch. Both are the checker's infrastructure failing, not a statement about the part."""
        last: InconclusiveVerdict | CheckerUnavailable | None = None
        for _ in range(JUDGE_ATTEMPTS):
            try:
                v = cheap_fn(pool, ws, original)
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
    # the cheap tier of THIS search also meshes every member its start fails (lane B8 / D7)
    cheap_fn = adaptive_cheap(judge, start_verdict)
    s0 = new_state(start, start_verdict, judge.score(start_verdict), "full", 0, None, None, SiteRegistry())
    try:
        s0.cheap_score = judge.score(cheap_fn(pool, start, original))
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
    #: None asks for the scaled rule (`scaled_patience`), settled from the start state's own sites at its first
    #: expansion; an int is a fixed patience, as before
    fixed_patience = patience
    patience_now = PATIENCE_FLOOR if patience is None else patience
    # THE VERDICT BUDGET (design 5.7, owner ruling 2026-11-24). Proposing stops at `1 - VERDICT_RESERVE` of the
    # wall and the rest is kept to judge the best state with the FULL panel, so a wall stop still delivers a
    # measurement. It comes OUT of the existing wall: no arm gets more wall-clock than it had before, or this
    # wave's numbers stop being comparable with the last one's.
    propose_wall_s = budget.wall_s * (1.0 - VERDICT_RESERVE)
    best = s0
    seen = {_fingerprint(start)}
    #: B20: state id -> the candidate keys already RUN there, and the (op, targets) that declined `no_change`
    #: there. Read over the whole lineage, so a descendant never pays again for its ancestors' answers.
    tried: dict[str, set[str]] = {}
    no_change: dict[str, set[str]] = {}
    #: what the prune saved, by reason. Each is one cheap verdict not spent (`verdicts_saved`).
    pruned: collections.Counter[str] = collections.Counter()
    heap: list[tuple] = [_entry(s0, s0.score, next(tie))]
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
                *_, pushed_score, cand_state = heapq.heappop(heap)
                if not cand_state.expanded and pushed_score == _rank(cand_state):
                    state = cand_state
                    break
            reprompt = False
            if state is None:
                # THE FRONTIER IS EMPTY. A deterministic search is done. A model generator that still has turns and
                # dollars is asked AGAIN, on the best state, with what became of its last candidates: its `feedback`
                # carries every decline, op error and judged delta into the next turn's prompt, and the observation
                # names the failing panel members and their sites. Snapshot v1: on 19 of 27 files search_ai stopped
                # here after one turn with its budget unspent. Each re-prompt counts toward patience, so patience, the
                # per-file $ ceiling (the proposer's own stop), the model budget and the wall still end the search.
                # A re-prompt reuses the state's sites, pictures and patience; it is not a second expansion.
                if not can_reprompt():
                    reason = "exhausted"
                    break
                state, reprompt = best, True
                trace.record("reprompt", state=state.id, spent=spent_now())
            if not reprompt:
                state.expanded = True

                try:
                    state.sites = judge.sites(pool, state.ws, state.verdict)
                except Exception as exc:  # expand blind rather than lose the search
                    state.sites = []
                    trace.record("judge_error", where="sites", state=state.id, error=f"{type(exc).__name__}: {exc}")
                else:
                    state.site_registry = _carry_site_ids(state, trace)
                state.budget = {"spent": spent_now(), "limits": budget.__dict__}
                if fixed_patience is None and state is s0:
                    holes = open_site_count(state.sites)
                    patience_now = scaled_patience(holes)
                    trace.record("patience", open_sites=holes, patience=patience_now, floor=PATIENCE_FLOOR)
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
                        {"id": s.id, "kind": s.kind, "entities": s.entities, "meshers_failing": s.meshers_failing,
                         "coarse": bool((s.graph or {}).get("coarse"))}
                        for s in state.sites
                    ],
                    unfixable=declared_unfixable,
                )

            # gather and validate
            proposals: list[tuple[int, Candidate]] = []
            for gi in ([g for g in active if g in model_gens] if reprompt else active):
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
            # B20(2): DROP BEFORE ANY VERDICT, and only where the drop is PROVABLE.
            #  * an exact duplicate (same op, same targets, same params) already run on this lineage cannot reach a
            #    new state: the ops are deterministic, so the same call on the same geometry gives the same result,
            #    and `_fingerprint` would discard it AFTER paying for the op and a cheap verdict.
            #  * an op that already declined `no_change` on exactly these targets on this lineage declined because
            #    it found nothing to change there; nothing between then and now changed those targets, because a
            #    state that changed them would be a different lineage.
            # Each drop is one cheap verdict NOT spent (and, on a confirmed child, a full one). C10 measured 11
            # no-op conformalise calls on one file: eleven verdicts for nothing.
            kept: list[tuple[int, Candidate]] = []
            for gi, c in valid:
                ckey, tkey = _candidate_key(c), _target_key(c)
                why = None
                if any(ckey in tried.get(sid, ()) for sid in state.lineage):
                    why = "already_run_on_this_lineage"
                elif any(tkey in no_change.get(sid, ()) for sid in state.lineage):
                    why = "no_change_on_these_targets_already"
                if why is not None:
                    pruned[why] += 1
                    _record_candidate(trace, state, c, "pruned", reason=why)
                    continue
                kept.append((gi, c))
            valid = kept
            room = max(0, budget.ops - spent["ops"])
            for gi, c in valid[room:]:
                _record_candidate(trace, state, c, "skipped_budget")
                fates.append((gi, _Outcome(c, gi, "declined", ["not run: the op budget was exhausted"]), None, False))
            to_run = valid[:room]
            # ONE op reserved per candidate that is about to run: the attempt is charged even when the op
            # declines or fails (its kernel work was still done). What a candidate applied BEYOND that one op
            # is charged below, once the outcomes are known.
            spent["ops"] += len(to_run)
            for _gi, c in to_run:
                tried.setdefault(state.id, set()).add(_candidate_key(c))

            # apply on forks and take the cheap verdict, in parallel
            timeout_s = max(1.0, budget.wall_s - (clock() - t0))

            def evaluate(
                item: tuple[int, Candidate], timeout_s: float = timeout_s, state: SearchState = state
            ) -> _Outcome:
                gi, c = item
                try:
                    applied = 1  # a plain op applies exactly one op; a stage says how many it applied
                    if c.op == RUN_STAGE:
                        out = _stage_outcome(pool, state.ws, c, gi, timeout_s)
                        if out.kind != "ok":
                            return out
                        new, diffs, applied = out.ws, out.diffs, out.applied
                    else:
                        new, diff = REGISTRY[c.op].apply(
                            pool, state.ws.fork(), list(c.targets), dict(c.params), timeout_s=timeout_s
                        )
                        declined = diff.params.get("declined") if isinstance(diff.params, dict) else None
                        if declined is not None:
                            # the body ops decline with an UNCHANGED workspace and the reason in the Diff (stages.py
                            # reads it the same way): a decline, not an applied op whose result duplicates the start
                            detail = diff.params.get("detail")
                            return _Outcome(c, gi, "declined", [f"{declined}: {detail}" if detail else str(declined)])
                        diffs = [diff]
                    assert new is not None
                    v, sc = judge_cheap(new)
                    return _Outcome(c, gi, "ok", ws=new, verdict=v, score=sc, diffs=diffs, applied=applied)
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
            evaluated = [f.result() for f in [executor.submit(evaluate, i) for i in to_run]]
            # THE BUDGET IS CHARGED PER APPLIED OP (Task 53). A candidate that applied k ops costs k, for
            # every generator: a `run_stage` macro whose body is a chain (shells = sew + make_volume) must not
            # buy k ops of geometry for the price of one, or `search_ai - search_det` measures routing rather
            # than judgement. One op was reserved per candidate above, so only the excess is added here. k is
            # only known after the body ran, so the total may finish just over `budget.ops`; the overrun is at
            # most one chain, it is reported as spent, and the next expansion stops the search.
            spent["ops"] += sum(max(0, o.applied - 1) for o in evaluated)
            for out in evaluated:
                if out.kind == "ok":
                    n_verdicts += 1
                if out.kind == "inconclusive":
                    n_inconclusive += 1
                    _record_candidate(trace, state, out.candidate, "inconclusive", errors=out.errors,
                                      ops_applied=out.applied)
                    continue  # not a rejection and no fate: the generator is not told its candidate failed
                if out.kind != "ok":
                    if out.kind == "declined" and any("no_change" in str(e) for e in out.errors or ()):
                        # B20(2): this op found nothing to change on exactly these targets. No state on this lineage
                        # can change that, so the call is never worth a second verdict.
                        no_change.setdefault(state.id, set()).add(_target_key(out.candidate))
                    _reject(trace, rejections, state, out.gen, out.candidate, out.kind, out.errors,
                            ops_applied=out.applied)
                    fates.append((out.gen, out, None, False))
                    continue
                assert out.ws is not None and out.score is not None
                fp = _fingerprint(out.ws)
                if fp in seen:
                    _record_candidate(trace, state, out.candidate, "duplicate", score=out.score,
                                      ops_applied=out.applied)
                    fates.append((out.gen, out, None, True))
                    continue
                seen.add(fp)
                child = new_state(
                    out.ws, out.verdict, out.score, "cheap", state.depth + 1, state.id, _via(out.candidate),
                    state.site_registry,
                )
                child.cheap_score = child.score
                child.rank = _estimate(state, child.score)
                child.ops_applied = out.applied
                _record_candidate(
                    trace,
                    state,
                    out.candidate,
                    "evaluated",
                    child=child.id,
                    score=child.score,
                    parent_score=state.score,
                    rank=child.rank,
                    ops_applied=out.applied,
                )
                children.append(child)
                fates.append((out.gen, out, child, False))
                heapq.heappush(heap, _entry(child, _rank(child), next(tie)))

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
            progressed = False
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
                heapq.heappush(heap, _entry(child, child.score, next(tie)))
                if child.score > best.score:
                    trace.record("best", state=child.id, score=child.score, previous=best.id, previous_score=best.score)
                    best, improved = child, True
                if child.score >= best.score:
                    # A new state that ties the best so far (or beats it) is a step of a multi-op repair. Judged
                    # against the BEST, not the parent: a child of a worse state that merely ties its worse parent
                    # is a search wandering at a loss, and must still run out of patience.
                    progressed = True

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
            flat = 0 if (improved or progressed) else flat + 1
            if flat >= patience_now:
                reason = "patience"

    ended_by = reason
    # ANYTIME: whatever stopped the search, the state being delivered is judged by the FULL panel before the row is
    # written. Usually it already is (the confirm step judges the top-k children), and then this costs nothing.
    stopped_on_wall = ended_by in TIME_STOPS
    final_verdict: dict | None = None
    if best.tier != "full":
        left = budget.wall_s - (clock() - t0)
        try:
            full = judge.full(pool, best.ws, original)
            full_score = judge.score(full)
        except Exception as exc:  # noqa: BLE001 - the final verdict is the ONE thing whose failure means unmeasured
            final_verdict = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "reserve_s_left": round(left, 1)}
            trace.record("final_verdict", state=best.id, ok=False, error=final_verdict["error"])
        else:
            best.verdict, best.tier, best.score, best.rank = full, "full", full_score, full_score
            final_verdict = {"ok": True, "score": list(full_score), "clean": bool(full.clean),
                             "reserve_s_left": round(left, 1), "took_s": round(clock() - t0 - (budget.wall_s - left), 1)}
            trace.record("final_verdict", state=best.id, ok=True, tier="full", score=full_score,
                         clean=bool(full.clean), spent=spent_now())
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
        stopped_on_wall=stopped_on_wall,
        best_tier=best.tier,
        final_verdict=final_verdict,
        pruned=dict(pruned),
        verdicts_saved=sum(pruned.values()),
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
        path_steps=_path_steps(by_id, best),
        reason=reason,
        stopped_on_wall=stopped_on_wall,
        best_tier=best.tier,
        pruned=dict(pruned),
        verdicts_saved=sum(pruned.values()),
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
    ops_applied: int = 0,
) -> None:
    rejections.setdefault(gi, []).append(Rejection(c, stage, errors, state.id))  # type: ignore[arg-type]
    _record_candidate(trace, state, c, "rejected", stage=stage, errors=errors, ops_applied=ops_applied)


def _path_steps(by_id: dict[str, SearchState], best: SearchState) -> list[dict]:
    """The ops from the start to `best`, each with the sites of the state it was applied to (oldest first)."""
    steps: list[dict] = []
    node: SearchState | None = best
    while node is not None and node.parent is not None and node.via is not None:
        parent = by_id.get(node.parent)
        steps.append({"via": dict(node.via), "sites": list(parent.sites) if parent is not None else [],
                      "ops": node.ops_applied})
        node = parent
    return steps[::-1]


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

    The delivery rule, "no worse than the input", measured in WHAT CLEAN IS (owner decision 2026-11-24:
    valid + panel): the best state is delivered only if it is no worse than the input in layer 1 AND passes at
    least as many panel members. A clean input is never replaced, and a search that beat nothing delivers
    nothing. This is the gate against shipping a "repair" that made things worse.

    Layer 3 is not consulted: fidelity is reported, never a gate. A delivered output that moved the part a long
    way is flagged for the owner in the report (`gaming_flags`).
    """
    if not result.changed or input_verdict.clean:
        return None
    score_of = score_fn or score
    out, inp = score_of(result.best_verdict), score_of(input_verdict)
    if out[VALID_KEY] < inp[VALID_KEY]:           # layer 1 regressed: never shipped
        return None
    if panel_passes(out) < panel_passes(inp):
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
    "default_judge_for",
    "engine_form",
    "h_mm_for",
    "deliverable",
    "panel_passes",
    "search",
]
