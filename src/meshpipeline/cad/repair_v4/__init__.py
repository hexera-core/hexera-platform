# Responsibility: The one entry point of the CRS v4 search engine - `repair_v4` - and the plain
#                 result it hands back to `repair_step_file`. `_run_search` is the engine's ENTRY and
#                 EXIT: STEP in (XCAF read -> Workspace), STEP out (the delivery gate's answer).
# Owns: `V4Result`, `SearchUnavailable`, `_run_search`. `Budget` is search's own, re-exported.
# Boundaries: no OCCT here: every kernel call goes through `WorkerPool.call`, shapes cross as BREP file
#             paths. The middle (staged healers, generators, the search) is other lanes' code, reached
#             only through `_find_best`. Imports at module level are stdlib-only so that importing
#             the package never pulls a kernel or an op module (`kernel_calls` imports the op modules
#             at its foot, and the registry auto-discovers); `Budget` is therefore a lazy re-export.
"""CRS v4 search-based CAD repair, selected by an env flag that defaults to v3."""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshpipeline.cad.repair_v4.search import Budget as Budget
    from meshpipeline.cad.repair_v4.search import SearchResult
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace


def __getattr__(name: str) -> Any:
    if name == "Budget":  # search's own (wall_s, ops, model_calls, model_usd); lazy, see the header
        from meshpipeline.cad.repair_v4.search import Budget

        return Budget
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class V4Result:
    """The outcome of one v4 run.

    `delivered` True: the repaired STEP is at `dest`. False: the run found nothing it would stand
    behind and `dest` carries the upload's bytes unchanged (a PASSTHROUGH). `trace_path` is the
    search trace on disk (None when none was written); `verdict_summary` is the checker's verdict
    reduced to plain data (see `_summarise`: `status`, `input`, and `delivered` when one shipped)."""

    trace_path: Path | None
    verdict_summary: dict = field(default_factory=dict)
    delivered: bool = False

    @property
    def passthrough(self) -> bool:
        return not self.delivered

    @property
    def file_unmeasured(self) -> bool:
        """True when the engine never measured this file (the SHARED rule, `measurement.file_unmeasured`, applied to
        `verdict_summary["search"]`). A summary with no search block (a deadline passthrough) was not measured."""
        from meshpipeline.cad.repair_v4.measurement import file_unmeasured

        search = self.verdict_summary.get("search")
        if not search:
            return True
        return file_unmeasured(
            reason=search.get("reason"), infrastructure_failed=search.get("infrastructure_failed"),
            inconclusive_candidates=search.get("inconclusive_candidates"),
            verdict_candidates=search.get("verdict_candidates"), out_clean=search.get("clean"))


class SearchUnavailable(RuntimeError):
    """The v4 search cannot run here (no worker pool, unreadable by our STEP reader, or a checker that could not run). OUR limitation, never a statement
    about the customer's file."""


#: The arm. The four the evaluation compares, all selectable here: `input_only` (the do-nothing control: the
#: input's verdict and a passthrough, nothing touched), `staged` (the deterministic staged healers alone),
#: `search_det` and `search_ai` (best-first search without / with the model proposer).
ARM = "search_det"
#: Arms that run no search: they ask the judge for verdicts and hand back what the staged healers (or nothing) made.
VERDICT_ONLY_ARMS = ("input_only", "staged")
#: The `search_ai` route on the engine path: a `provider:model`, or None for the strongest of
#: `contracts.model_inference.REPAIR_V4_PROPOSE_ROUTES`. The engine does NOT probe per file (a probe is a model call,
#: cents each, on every one of the wave's files); a wave probes once per shard (`model_route.probe_route_chain`)
#: and pins the winner here, and the price check inside the router refuses an unpriced route before dialling.
ROUTE: str | None = None
#: Time kept back out of the deadline for the STEP write and the teardown.
WRITE_RESERVE_S = 30.0
READ_TIMEOUT_S = 120.0
WRITE_TIMEOUT_S = 120.0


def _summarise(verdict: Any) -> dict:
    """A checker Verdict as plain data. Layer results by name, never the objects."""
    faithful = getattr(verdict, "faithful", None)
    return {
        "clean": bool(verdict.clean),
        "tier": getattr(verdict, "tier", None),
        "valid": bool(verdict.valid.ok),
        "panel": {str(m.name): str(m.status) for m in getattr(verdict, "panel", ())},
        "faithful": None if faithful is None else bool(faithful.ok),
    }


def _search_summary(result: Any) -> dict:
    """The search's outcome as plain data: everything `measurement.file_unmeasured` reads, so the one shared rule
    applies to a `V4Result` as it does to an evaluation row (`V4Result.file_unmeasured`)."""
    return {"reason": getattr(result, "reason", None), "changed": bool(result.changed),
            "clean": bool(result.clean),
            "verdict_candidates": int(getattr(result, "verdict_candidates", 0)),
            "inconclusive_candidates": int(getattr(result, "inconclusive_candidates", 0)),
            "infrastructure_failed": bool(getattr(result, "infrastructure_failed", False))}


def _workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def _route() -> str:
    from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_ROUTES

    return ROUTE or REPAIR_V4_PROPOSE_ROUTES[0]


def _ai_kwargs(source: str, budget: Budget) -> dict[str, Any]:
    """What `ai_generators` needs on the engine path: the route, one in-memory ledger capped at this file's model
    budget (every request is admitted against it before it is sent, so the file's ceiling holds per call whatever
    `Budget.model_usd` advises), the turn cap, and the file's identity (the STEP's sha256)."""
    import hashlib

    from meshpipeline.cad.repair_v4.model_ledger import WaveLedger

    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    return {"route": _route(), "ledger": WaveLedger.for_file(budget.model_usd), "max_turns": budget.model_calls,
            "usd_ceiling": budget.model_usd, "file_id": digest, "job_id": f"repair_v4:{digest[:12]}"}


def _verdict_only(pool: WorkerPool, ws: Workspace, source: str, *, input_verdict: Any, judge: Any) -> SearchResult:
    """The `input_only` and `staged` arms as a `SearchResult`, so one delivery gate and one summary serve all four.
    `input_only` changes nothing; `staged` runs the staged healers and is judged once more. No candidate goes
    through a search, so there are none to count."""
    from meshpipeline.cad.repair_v4.loss import check_loss
    from meshpipeline.cad.repair_v4.search import SearchResult
    from meshpipeline.cad.repair_v4.stages import confirm_staged, run_staged
    from meshpipeline.cad.repair_v4.trace import Trace
    from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict, adaptive_cheap_members

    trace = Trace()
    trace.record("start")
    if ARM == "input_only":
        return SearchResult(best=ws, best_verdict=input_verdict, clean=bool(input_verdict.clean), trace=trace,
                            changed=False, reason="input_only")
    staged = run_staged(pool, ws, cheap_members=adaptive_cheap_members(input_verdict))
    # the stages were judged by the CHEAP tier; the whole panel confirms the result, falling back stage by stage
    conf = confirm_staged(pool, ws, staged, full=lambda w: judge.full(pool, w, source), input_verdict=input_verdict)
    verdict = conf.verdict if conf.verdict is not None else judge.full(pool, staged.ws, source)
    if getattr(verdict, "inconclusive", False):
        raise InconclusiveVerdict("the staged output could not be measured")
    trace.record("staged_confirmation", fell_back_to=conf.fell_back_to, dropped=conf.dropped, checked=conf.checked)
    # B22: the staged healer can lose the part too (it is where a `defeature` chain runs), so its output carries
    # the same measurement the search's does and meets the same delivery gate. Reference: the input workspace.
    loss = check_loss(pool, ws.brep_path, staged.ws.brep_path) if staged.ws.brep_path != ws.brep_path else None
    if loss is not None:
        trace.record("loss", state="staged", **loss.row())
    return SearchResult(best=staged.ws, best_verdict=verdict, clean=bool(verdict.clean), trace=trace,
                        changed=staged.ws.brep_path != ws.brep_path, reason="staged", best_loss=loss)


def _find_best(pool: WorkerPool, ws: Workspace, source: str, *, budget: Budget, deadline_s: float,
               judge: Any, input_verdict: Any = None) -> SearchResult:
    """The middle: staged healers, then the search. Other lanes own both; this only calls them in the
    frozen order (04-task-22-engine-seam-contract.md). Tests fake THIS function."""
    from meshpipeline.cad.repair_v4.search import Budget as SearchBudget
    from meshpipeline.cad.repair_v4.search import arm_generators, search
    from meshpipeline.cad.repair_v4.stages import confirm_staged, run_staged
    from meshpipeline.cad.repair_v4.verdict.assemble import adaptive_cheap_members

    input_verdict = input_verdict if input_verdict is not None else judge.full(pool, ws, source)
    if getattr(input_verdict, "out_of_scope", False):
        # AN UNBOUNDED INPUT SKIPS THE STAGED HEALER (lane C9) and the search starts from the input itself. The staged
        # healer's `_judge` always judges a RE-EXPORT of the workspace, and on an unbounded shape the re-export's
        # check_valid runs to its timeout: on sha bfc8d0b5 the engine spent 1808 s in the staged baseline and raised
        # SearchUnavailable before anything was searched. The search judges an unbounded input on its original STEP
        # and an unbounded state by its extent alone (`search._judged`), and its det sweep proposes trim_unbounded first.
        start = ws
    else:
        staged = run_staged(pool, ws, cheap_members=adaptive_cheap_members(input_verdict))
        # the search starts from the healer's output only once the FULL panel says it is no worse than the input
        confirm_staged(pool, ws, staged, full=lambda w: judge.full(pool, w, source), input_verdict=input_verdict)
        start = staged.ws
    ai = _ai_kwargs(source, budget) if ARM == "search_ai" else {}
    generators, _proposer = arm_generators(ARM, **ai)
    remaining = deadline_s - time.monotonic() - WRITE_RESERVE_S
    capped = SearchBudget(wall_s=max(0.0, min(budget.wall_s, remaining)), ops=budget.ops,
                          model_calls=budget.model_calls, model_usd=budget.model_usd)
    result = search(pool, start, source, generators, capped, workers=_workers(), judge=judge)
    # `search` judges `changed` against ITS start, which is the staged healer's output. Delivery asks a different
    # question - did the best state improve on the INPUT? - so a healer-only gain must still count as changed.
    # `deliverable` still compares result.best_verdict (the verdict of that very state) with the input verdict,
    # so a regression is refused exactly as before; no extra verdict is needed.
    if not result.changed and result.best.brep_path != ws.brep_path:
        result = dataclasses.replace(result, changed=True)
    return result


def _make_judge(pool: WorkerPool, ws: Workspace) -> Any:
    """The judge, sized from the part: `h_mm` is the production FEA mesh's base size for THIS part's diagonal
    (`search.default_judge_for`), not a constant."""
    from meshpipeline.cad.repair_v4.search import default_judge_for

    return default_judge_for(pool, ws)


def _run_search(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result:
    """STEP in, STEP out. `dest` always ends holding either the repaired STEP (`delivered=True`) or the
    upload's own bytes (a passthrough, `delivered=False`). Anything that stops the engine from
    MEASURING the file raises `SearchUnavailable` (ours, never a verdict on the file) and writes
    nothing: a file that was never measured has not failed."""
    from meshpipeline.cad.repair_v4.search import deliverable
    from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v4.workspace import Workspace

    infra = (CheckerUnavailable, InconclusiveVerdict)

    def passthrough(summary: dict, trace: Path | None = None) -> V4Result:
        shutil.copyfile(source, dest)
        return V4Result(trace_path=trace, verdict_summary=summary, delivered=False)

    if ARM not in VERDICT_ONLY_ARMS:
        from meshpipeline.cad.repair_v4.search import ARMS

        if ARM not in ARMS:
            raise ValueError(f"unknown search arm {ARM!r}; the arms are {VERDICT_ONLY_ARMS + tuple(ARMS)}")
    if deadline_s - time.monotonic() <= 0:
        return passthrough({"status": "not_measured", "reason": "deadline_expired"})
    # A16: ONE place sets the file's wall, and every panel and op reads it from there (`verdict.wall`). Without
    # this the wall module is inert and every call keeps its fixed timeout, which is exactly the old behaviour -
    # so a caller that does not set it loses nothing but the new ceiling.
    from meshpipeline.cad.repair_v4.verdict import wall

    wall.set_file_deadline(deadline_s)
    try:
        pool = WorkerPool(_workers())
    except Exception as exc:  # noqa: BLE001 - no pool is our failure, whatever its type
        # THE ONLY PATH THAT RAISES BEFORE THE `finally` BELOW, and it leaked the wall: the first A16 left the
        # deadline set for the rest of the process, so every later panel saw a shrinking remainder, got clipped,
        # and with any non-pass member came back inconclusive. That made the unit directory fail 28/40/29/28/29/
        # 29/30 times over 7 runs, varying with machine speed. A file that never ran must leave no wall behind.
        wall.set_file_deadline(None)
        raise SearchUnavailable(f"the v4 worker pool could not start: {type(exc).__name__}: {exc}") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="v4-run-") as work:
            read = pool.call(
                "read_step_xcaf",
                {"path": source, "brep_out": str(Path(work) / "input.brep")},
                timeout_s=max(1.0, min(READ_TIMEOUT_S, deadline_s - time.monotonic())))
            if not read.ok or read.value is None:
                raise SearchUnavailable(
                    f"the v4 STEP reader could not read the file ({read.status}: {read.error}); "
                    "that is this engine's limit, not a finding about the part")
            doc = read.value
            try:
                start = Workspace.from_brep(pool, doc["brep"], doc["labels"])
            except RuntimeError as exc:
                raise SearchUnavailable(f"the v4 engine could not index the part: {exc}") from exc
            try:
                judge = _make_judge(pool, start)
                input_verdict = judge.full(pool, start, source)
                if getattr(input_verdict, "inconclusive", False):
                    raise InconclusiveVerdict("the input could not be measured")
                if ARM in VERDICT_ONLY_ARMS:
                    result = _verdict_only(pool, start, source, input_verdict=input_verdict, judge=judge)
                else:
                    result = _find_best(pool, start, source, budget=budget, deadline_s=deadline_s, judge=judge,
                                        input_verdict=input_verdict)
                if getattr(result, "infrastructure_failed", False):
                    raise InconclusiveVerdict("the search measured nothing (checker infrastructure)")
            except infra as exc:
                raise SearchUnavailable(
                    f"the v4 checker could not run ({type(exc).__name__}: {exc}); "
                    "the file was never measured, which is not a verdict on it") from exc

            trace_path: Path | None = None
            if getattr(result, "trace", None) is not None:
                trace_path = Path(f"{dest}.v4-trace.json")
                result.trace.write(trace_path)
            summary: dict = {
                "status": "clean" if input_verdict.clean else "not_clean",
                "input": _summarise(input_verdict),
                "search": _search_summary(result),
            }
            # B22: what the state the gate just judged kept of the input, in the summary either way - a refusal
            # is reported, never silent (the row's `loss` field; `report.py` counts them)
            if getattr(result, "best_loss", None) is not None:
                summary["loss"] = result.best_loss.row()
            best = deliverable(result, input_verdict)
            if best is None:
                return passthrough(summary, trace_path)
            staging = str(Path(work) / "out.step")
            written = pool.call(
                "write_step_xcaf",
                {"brep": best.brep_path, "labels": best.labels, "units": doc.get("units", "mm"),
                 "assembly_ids": doc.get("assembly_ids", []), "path": staging},
                timeout_s=max(1.0, min(WRITE_TIMEOUT_S, deadline_s - time.monotonic())))
            if not written.ok:
                raise SearchUnavailable(
                    f"the v4 engine could not write the repaired STEP ({written.status}: {written.error})")
            shutil.move(staging, dest)
            summary["delivered"] = _summarise(result.best_verdict)
            summary["status"] = "clean" if result.best_verdict.clean else "improved"
            return V4Result(trace_path=trace_path, verdict_summary=summary, delivered=True)
    finally:
        pool.close()
        wall.set_file_deadline(None)   # the next file in this process must not inherit this one's wall


def repair_v4(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result:
    """Repair the STEP at `source` into `dest` with the v4 search engine.

    `deadline_s` is an absolute monotonic-clock deadline for the whole run, the same clock
    `repair_step_file` takes its own deadline from; `budget` bounds what the search may spend
    inside it."""
    return _run_search(str(source), str(dest), budget=budget, deadline_s=deadline_s)
