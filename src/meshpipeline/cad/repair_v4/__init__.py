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


class SearchUnavailable(RuntimeError):
    """The v4 search cannot run here (no worker pool, unreadable by our STEP reader, or a checker that could not run). OUR limitation, never a statement
    about the customer's file."""


#: Mesh size the judge clusters sites with. UNOWNED DECISION: nothing in the tree derives it from a part.
H_MM = 1.0
#: The generator arm. `search_ai` needs a ledger, a route and a model key the v4 VM does not have (22c).
ARM = "search_det"
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


def _workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def _find_best(pool: WorkerPool, ws: Workspace, source: str, *, budget: Budget, deadline_s: float,
               judge: Any) -> SearchResult:
    """The middle: staged healers, then the search. Other lanes own both; this only calls them in the
    frozen order (04-task-22-engine-seam-contract.md). Tests fake THIS function."""
    from meshpipeline.cad.repair_v4.search import Budget as SearchBudget
    from meshpipeline.cad.repair_v4.search import arm_generators, search
    from meshpipeline.cad.repair_v4.stages import run_staged

    staged = run_staged(pool, ws)
    generators, _proposer = arm_generators(ARM)
    remaining = deadline_s - time.monotonic() - WRITE_RESERVE_S
    capped = SearchBudget(wall_s=max(0.0, min(budget.wall_s, remaining)), ops=budget.ops,
                          model_calls=budget.model_calls, model_usd=budget.model_usd)
    return search(pool, staged.ws, source, generators, capped, workers=_workers(), judge=judge)


def _make_judge() -> Any:
    from meshpipeline.cad.repair_v4.search import default_judge

    return default_judge(h_mm=H_MM)


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

    if deadline_s - time.monotonic() <= 0:
        return passthrough({"status": "not_measured", "reason": "deadline_expired"})
    try:
        pool = WorkerPool(_workers())
    except Exception as exc:  # noqa: BLE001 - no pool is our failure, whatever its type
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
                judge = _make_judge()
                input_verdict = judge.full(pool, start, source)
                if getattr(input_verdict, "inconclusive", False):
                    raise InconclusiveVerdict("the input could not be measured")
                result = _find_best(pool, start, source, budget=budget, deadline_s=deadline_s, judge=judge)
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
                "search": {"reason": getattr(result, "reason", None), "changed": bool(result.changed),
                           "clean": bool(result.clean)},
            }
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


def repair_v4(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result:
    """Repair the STEP at `source` into `dest` with the v4 search engine.

    `deadline_s` is an absolute monotonic-clock deadline for the whole run, the same clock
    `repair_step_file` takes its own deadline from; `budget` bounds what the search may spend
    inside it."""
    return _run_search(str(source), str(dest), budget=budget, deadline_s=deadline_s)
