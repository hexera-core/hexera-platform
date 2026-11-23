# Responsibility: Run ONE file through ONE evaluation arm (input_only, staged, search_det, search_ai) and say, in the
#                 row, whether the file was MEASURED.
# Owns: the per-file arm dispatch, the arm -> generators wiring (`search.arm_generators`), the row each arm writes,
#       and `Engine`, the seam to the entry/exit and staged lanes.
# Boundaries: no OCCT here: every kernel call goes through the WorkerPool handed in. It makes no model call itself;
#             the AI arm's only way to a model is `AIProposer`, which asks the WaveLedger before every call, and the
#             ledger is HANDED IN (a model arm with no ledger is refused, never built without one). Infrastructure
#             (a dead checker, a timeout, an unreadable file) is returned as an `Excluded` entry, never a row, so a
#             file that was not measured cannot read as one that failed.
"""One file, one arm.

    run_file("search_ai", entry, source, pool=pool, engine=engine, ledger=ledger, ...) -> ArmOutcome

`ArmOutcome.row` is a dict of `report.ArmRow` fields (or None) and `ArmOutcome.excluded` the reason the file produced
no row (or None); exactly one is set. A search that ended `no_verdict` writes a row with `inconclusive=True` and
SearchResult's own `reason`/`verdict_candidates`/`inconclusive_candidates`/`infrastructure_failed`, which the report
checks against the engine's own rule (`repair_v4.measurement.file_unmeasured`).
"""
from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


#: `--h-mm auto`: derive the element size from each part (`search.h_mm_for`) instead of one size for every file.
H_MM_AUTO = float("inf")


class Engine(Protocol):
    """What the arms need from the lanes that do not belong to this module. `RealEngine` is the production one;
    tests fake it (22a owns `open_workspace`, 22b owns `run_staged` and `judge`)."""

    def open_workspace(self, pool: Any, source: str, workdir: str) -> Any: ...

    def judge(self, h_mm: float | None) -> Any: ...

    def run_staged(self, pool: Any, ws: Any) -> Any: ...


class RealEngine:
    """The shipped functions, composed the way the contract's call order says (spec 04)."""

    def open_workspace(self, pool: Any, source: str, workdir: str) -> Any:
        from meshpipeline.cad.repair_v4.workspace import Workspace

        Path(workdir).mkdir(parents=True, exist_ok=True)
        read = pool.call("read_step_xcaf", {"path": source, "brep_out": os.path.join(workdir, "input.brep")},
                         timeout_s=300)
        if not read.ok or read.value is None:
            raise EngineUnavailable(f"read_step_xcaf failed: {read.status}: {read.error}")
        return Workspace.from_brep(pool, read.value["brep"], read.value.get("labels") or {})

    def judge(self, h_mm: float | None) -> Any:
        import dataclasses

        from meshpipeline.cad.repair_v4.search import default_judge

        if h_mm is not None:
            return default_judge(h_mm=h_mm)

        def no_sites(pool: Any, ws: Any, verdict: Any) -> list:
            raise ValueError("this judge was built without --h-mm: site finding has no default scale")

        # input_only and staged only ask for verdicts; a search arm without h_mm is refused at the command line
        return dataclasses.replace(default_judge(h_mm=float("nan")), sites=no_sites)

    def run_staged(self, pool: Any, ws: Any) -> Any:
        from meshpipeline.cad.repair_v4.stages import run_staged

        return run_staged(pool, ws)


class EngineUnavailable(RuntimeError):
    """A lane's work could not be done here (pool died, STEP unreadable). Infrastructure, never a verdict."""


class ModelArmWithoutLedger(RuntimeError):
    """A model arm was dispatched with no WaveLedger: a cap nothing consults is not a cap."""


@dataclass
class ArmOutcome:
    row: dict | None = None
    excluded: str | None = None


def _layers(v: Any, prefix: str) -> dict:
    faithful_ok = bool(v.faithful is not None and v.faithful.ok)
    return {f"{prefix}_valid": bool(v.valid.ok), f"{prefix}_faithful_ok": faithful_ok,
            f"{prefix}_panel_passes": sum(1 for r in v.panel if r.status == "pass"),
            f"{prefix}_clean": bool(v.clean)}


def _verdict_fields(v_in: Any, v_out: Any) -> dict:
    out = _layers(v_out, "out")
    fields = {"in_clean": bool(v_in.clean), "in_panel_passes": _layers(v_in, "in")["in_panel_passes"],
              "in_valid": bool(v_in.valid.ok), "in_faithful_ok": bool(v_in.faithful is not None and v_in.faithful.ok),
              "panel_size": len(v_out.panel), "out_tier": v_out.tier, **out}
    return fields


def _usable(v: Any, what: str) -> str | None:
    """None if `v` can be a row's verdict, else why it cannot (it says nothing about the file)."""
    if not v.counts_for_scoreboard:
        return f"inconclusive: {what} had a panel member fail for pool reasons"
    if v.out_of_scope:  # measured and outside the definition: the splits keep these out, so a wave never reaches here
        return f"out_of_scope: {what} is unbounded (layer 1 unbounded_geometry); it is not a failure and not unmeasured"
    if not v.panel:
        return f"empty_panel: {what} produced no member result"
    return None


def run_file(arm_name: str, entry: Any, source: str, *, pool: Any, engine: Engine, ledger: Any | None,
             arm: Any, budget: Any, h_mm: float | None, route: str | None, max_turns: int, job_id: str,
             workdir: str, workers: int = 1, clock: Any = time.monotonic) -> ArmOutcome:
    """Run one file through one arm. `arm` is the `evalspec.Arm`, `budget` the arm's `evalspec.Budget`."""
    from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    if arm.uses_model and ledger is None:
        raise ModelArmWithoutLedger(f"arm {arm_name!r} makes model calls and needs the shard's WaveLedger")
    t0 = clock()
    base = {"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": arm_name}

    def excluded(why: str) -> ArmOutcome:
        return ArmOutcome(excluded=why)

    try:
        ws = engine.open_workspace(pool, source, workdir)
        if h_mm is not None and h_mm == H_MM_AUTO:
            from meshpipeline.cad.repair_v4.search import h_mm_for

            h_mm = h_mm_for(pool, ws)  # CheckerUnavailable (unmeasurable part) is caught below: infrastructure
        judge = engine.judge(h_mm)
        full = judge.full
        v_in = full(pool, ws, source)
        bad = _usable(v_in, "the input")
        if bad:
            return excluded(bad)
        out_v, extra, usd = v_in, {}, 0.0
        if arm_name == "input_only":
            pass
        elif arm_name == "staged":
            staged = engine.run_staged(pool, ws)
            out_v = full(pool, staged.ws, source)
        elif arm_name in ("search_det", "search_ai"):
            out_v, extra, usd = _search_arm(arm_name, pool=pool, ws=ws, source=source, judge=judge, v_in=v_in,
                                            ledger=ledger, arm=arm, budget=budget, h_mm=h_mm, route=route,
                                            max_turns=max_turns, job_id=job_id, entry=entry, workers=workers,
                                            clock=clock, full=full)
        else:
            raise ValueError(f"arm {arm_name!r} is not dispatchable here")
    except (CheckerUnavailable, InconclusiveVerdict, EngineUnavailable) as exc:
        return excluded(f"checker_unavailable: {type(exc).__name__}: {exc}")
    wall = round(clock() - t0, 3)
    unmeasured = bool(extra.get("_unmeasured"))
    extra = {k: v for k, v in extra.items() if k != "_unmeasured"}
    if unmeasured:
        return ArmOutcome(row={**base, "inconclusive": True, "model_usd": usd, "wall_s": wall, **extra})
    bad = _usable(out_v, "the output")
    if bad:
        return excluded(bad)
    return ArmOutcome(row={**base, "inconclusive": False, "model_usd": usd, "wall_s": wall,
                           **_verdict_fields(v_in, out_v), **extra})


def _search_arm(arm_name: str, *, pool: Any, ws: Any, source: str, judge: Any, v_in: Any, ledger: Any, arm: Any,
                budget: Any, h_mm: float | None, route: str | None, max_turns: int, job_id: str, entry: Any,
                workers: int, clock: Any, full: Any) -> tuple[Any, dict, float]:
    """(output verdict, the SearchResult's own measured-ness fields, model USD) for a search arm."""
    from meshpipeline.cad.repair_v4 import search as S

    ai_kwargs: dict[str, Any] = {}
    if arm_name == "search_ai":
        if not route:
            raise ValueError("search_ai needs a route (provider:model)")
        ai_kwargs = {"route": route, "ledger": ledger, "max_turns": max_turns, "usd_ceiling": arm.budget.model_usd,
                     "file_id": entry.sha256, "job_id": job_id}
    generators, proposer = S.arm_generators(arm_name, **ai_kwargs)
    sbudget = S.Budget(wall_s=budget.wall_s, ops=budget.ops, model_calls=max_turns if proposer else 0,
                       model_usd=budget.model_usd)
    result = S.search(pool, ws, source, generators, sbudget, workers=workers, judge=judge, h_mm=h_mm, clock=clock)
    usd = float(proposer.result()["spent_usd"]) if proposer is not None else 0.0
    fields = {"reason": result.reason, "verdict_candidates": result.verdict_candidates,
              "inconclusive_candidates": result.inconclusive_candidates,
              "infrastructure_failed": result.infrastructure_failed, "_unmeasured": result.unmeasured}
    delivered = S.deliverable(result, v_in)
    out_v = v_in if delivered is None else full(pool, delivered, source)
    return out_v, fields, usd


def failure_reason(exc: BaseException) -> str:
    return f"error: {type(exc).__name__}: {''.join(traceback.format_exception_only(type(exc), exc)).strip()[-300:]}"
