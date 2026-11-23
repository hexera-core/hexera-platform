# Responsibility: The v4 repair search - best-first over Workspace states ranked by the verdict
#                 score, the deterministic sweep that proposes ops, the budget, and the delivery rule.
# Owns: `Candidate`, `Generator`, `DetGenerator`, `Budget`, `Judge`, `SearchState`, `Rejection`,
#       `SearchResult`, `search`, `deliverable`.
# Boundaries: the verdict (Task 5), site finding (Task 17) and the AI proposer (Task 19) are NOT
#             here: the first two arrive through `Judge`, the proposer is just another `Generator`.
#             Ops come from `ops.REGISTRY` and are checked by `validate_call` before they run.
"""Best-first search over Workspace states. The reward IS the metric.

    expand the best state -> every generator proposes `Candidate`s -> `validate_call` each ->
    apply the valid ones in parallel on forks of the state -> `cheap_verdict` each child ->
    push the children by `score` -> confirm the best `k_confirm` new children with the full panel.

The deterministic verifier decides; a generator (rules or a model) only proposes. A proposal that is
wrong - an unknown op, an out-of-bounds parameter, a target a previous op removed, an op that
declines or fails - is never a crash and never silently dropped: it becomes a `Rejection` handed
back to the generator that made it (`on_rejected`, if it has one) and an event in the `Trace`.

Ranking uses the CHEAP score of an unconfirmed state and the FULL score of a confirmed one. The
cheap panel is a subset, so the two are not strictly comparable; that is why only fully confirmed
states can become `best` (and so be delivered), while cheap scores merely order the frontier.

Budget. `ops` counts op applications (one per validated candidate that is run); `wall_s` is
elapsed time; `model_calls` / `model_usd` are read from generators that expose
`model_usage() -> (calls, usd)` (cumulative). Each limit stops the search independently, checked
before every expansion; `ops` also truncates a batch so it is never overrun. Patience is the
number of consecutive expansions that did not improve the best confirmed score.
"""

from __future__ import annotations

import heapq
import importlib
import itertools
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined, OpFailed, validate_call
from meshpipeline.cad.repair_v4.trace import Trace
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.workspace import UnknownTarget, Workspace

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


def default_judge(h_mm: float = 1.0) -> Judge:
    """The real verdict (Task 5) and sites (Task 17), imported late so this module loads without
    them. `declared` is the union over the workspace's whole lineage, since faithfulness compares
    against the ORIGINAL."""
    vmod = importlib.import_module("meshpipeline.cad.repair_v4.verdict")
    smod = importlib.import_module("meshpipeline.cad.repair_v4.sites")

    def declared(ws: Workspace) -> Any:
        return vmod.Declared(
            added=[i for d in ws.history for i in d.declared_added],
            removed=[i for d in ws.history for i in d.declared_removed],
        )

    return Judge(
        cheap=lambda pool, ws, original: vmod.cheap_verdict(pool, ws.brep_path, original, declared=declared(ws)),
        full=lambda pool, ws, original: vmod.verdict(pool, ws.brep_path, original, declared=declared(ws)),
        score=vmod.score,
        sites=lambda pool, ws, verdict: smod.find_sites(pool, ws, verdict, h_mm=h_mm),
    )


@dataclass
class SearchResult:
    best: Workspace
    best_verdict: Any
    clean: bool
    trace: Trace
    #: False when nothing beat the start (best is the start workspace)
    changed: bool = False


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
    `REGISTRY` are proposed. Sweeps are ordered mildest first; `k` truncates."""

    def propose(self, state: SearchState, k: int) -> list[Candidate]:
        out: list[Candidate] = []
        for site in state.sites:
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


def _fingerprint(ws: Workspace) -> tuple:
    """Two workspaces with the same entity fingerprints are the same geometry, however reached."""
    return tuple(sorted(ws.ids.values()))


@dataclass
class _Outcome:
    candidate: Candidate
    gen: int
    kind: Literal["ok", "validate", "declined", "failed", "error"]
    errors: list[str] = field(default_factory=list)
    ws: Workspace | None = None
    verdict: Any = None


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
    clock: Callable[[], float] = time.monotonic,
) -> SearchResult:
    judge = judge or default_judge()
    trace = Trace(clock)
    t0 = clock()
    state_ids = itertools.count()
    tie = itertools.count()
    spent = {"ops": 0}

    def usage() -> tuple[int, float]:
        calls, usd = 0, 0.0
        for g in generators:
            fn = getattr(g, "model_usage", None)
            if fn is not None:
                c, u = fn()
                calls, usd = calls + c, usd + u
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
        if s["model_calls"] >= budget.model_calls:
            return "model_calls"
        if s["model_usd"] >= budget.model_usd:
            return "model_usd"
        return None

    def new_state(
        ws: Workspace, verdict: Any, tier: str, depth: int, parent: str | None, via: dict | None
    ) -> SearchState:
        return SearchState(
            id=f"S{next(state_ids)}",
            ws=ws,
            verdict=verdict,
            score=judge.score(verdict),
            sites=[],
            depth=depth,
            parent=parent,
            via=via,
            tier=tier,
        )

    start_verdict = judge.full(pool, start, original)
    s0 = new_state(start, start_verdict, "full", 0, None, None)
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

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        while reason is None:
            reason = over_budget()
            if reason:
                break
            state = None
            while heap:
                _, _, pushed_score, cand_state = heapq.heappop(heap)
                if not cand_state.expanded and pushed_score == cand_state.score:
                    state = cand_state
                    break
            if state is None:
                reason = "exhausted"
                break
            state.expanded = True

            state.sites = judge.sites(pool, state.ws, state.verdict)
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
            )

            # gather and validate
            proposals: list[tuple[int, Candidate]] = []
            for gi, gen in enumerate(generators):
                proposals += [(gi, c) for c in gen.propose(state, k_propose)]
            rejections: dict[int, list[Rejection]] = {}
            valid: list[tuple[int, Candidate]] = []
            for gi, c in proposals:
                errors = validate_call(c.op, c.targets, c.params, state.ws)
                if errors:
                    _reject(trace, rejections, state, gi, c, "validate", errors)
                else:
                    valid.append((gi, c))
            room = max(0, budget.ops - spent["ops"])
            for _gi, c in valid[room:]:
                _record_candidate(trace, state, c, "skipped_budget")
            to_run = valid[:room]
            spent["ops"] += len(to_run)

            # apply on forks and take the cheap verdict, in parallel
            timeout_s = max(1.0, budget.wall_s - (clock() - t0))

            def evaluate(
                item: tuple[int, Candidate], timeout_s: float = timeout_s, state: SearchState = state
            ) -> _Outcome:
                gi, c = item
                try:
                    new, _diff = REGISTRY[c.op].apply(
                        pool, state.ws.fork(), list(c.targets), dict(c.params), timeout_s=timeout_s
                    )
                    return _Outcome(c, gi, "ok", ws=new, verdict=judge.cheap(pool, new, original))
                except OpDeclined as exc:
                    return _Outcome(c, gi, "declined", [exc.reason])
                except OpFailed as exc:
                    return _Outcome(c, gi, "failed", [f"{exc.status}: {exc.message}"])
                except (UnknownTarget, ValueError) as exc:
                    return _Outcome(c, gi, "validate", [str(exc)])
                except Exception as exc:  # one broken branch must not end the search
                    return _Outcome(c, gi, "error", [f"{type(exc).__name__}: {exc}"])

            children: list[SearchState] = []
            for out in [f.result() for f in [executor.submit(evaluate, i) for i in to_run]]:
                if out.kind != "ok":
                    _reject(trace, rejections, state, out.gen, out.candidate, out.kind, out.errors)
                    continue
                assert out.ws is not None
                fp = _fingerprint(out.ws)
                if fp in seen:
                    _record_candidate(trace, state, out.candidate, "duplicate", score=judge.score(out.verdict))
                    continue
                seen.add(fp)
                child = new_state(out.ws, out.verdict, "cheap", state.depth + 1, state.id, _via(out.candidate))
                _record_candidate(
                    trace,
                    state,
                    out.candidate,
                    "evaluated",
                    child=child.id,
                    score=child.score,
                    parent_score=state.score,
                )
                children.append(child)
                heapq.heappush(heap, (_neg(child.score), next(tie), child.score, child))

            for gi, items in rejections.items():
                told = getattr(generators[gi], "on_rejected", None)
                if told is not None:
                    told(items)

            # confirm the best new states with the full panel
            improved = False
            top = sorted(children, key=lambda s: s.score, reverse=True)[:k_confirm]
            fulls = [f.result() for f in [executor.submit(judge.full, pool, c.ws, original) for c in top]]
            for child, full in zip(top, fulls, strict=True):
                cheap_score = child.score
                child.verdict, child.tier, child.score = full, "full", judge.score(full)
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
            if best.verdict.clean:
                reason = "clean"
                break
            flat = 0 if improved else flat + 1
            if flat >= patience:
                reason = "patience"

    trace.record(
        "stop",
        reason=reason,
        best=best.id,
        best_score=best.score,
        clean=bool(best.verdict.clean),
        best_path=_path(trace, best),
        spent=spent_now(),
        budget=budget.__dict__,
    )
    return SearchResult(
        best=best.ws, best_verdict=best.verdict, clean=bool(best.verdict.clean), trace=trace, changed=best.id != s0.id
    )


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
    score = score_fn or default_judge().score
    if score(result.best_verdict)[1] < score(input_verdict)[1]:
        return None
    return result.best


__all__ = [
    "Budget",
    "Candidate",
    "DetGenerator",
    "Generator",
    "Judge",
    "Rejection",
    "SearchResult",
    "SearchState",
    "default_judge",
    "deliverable",
    "search",
]
