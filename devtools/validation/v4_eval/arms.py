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

import dataclasses
import json
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

    def run_staged(self, pool: Any, ws: Any, cheap_members: Any = None) -> Any:
        from meshpipeline.cad.repair_v4.stages import run_staged

        return run_staged(pool, ws, cheap_members=cheap_members)


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
    f = v.faithful
    fields: dict[str, Any] = {f"{prefix}_valid": bool(v.valid.ok), f"{prefix}_faithful_ok": faithful_ok,
                              f"{prefix}_panel_passes": sum(1 for r in v.panel if r.status == "pass"),
                              f"{prefix}_clean": bool(v.clean)}
    if prefix == "out":
        # layer 3 gates nothing since the owner's 2026-11-24 decision, so the ROW carries what it measured and
        # report.py flags a clean output that moved the part (`gaming_flags`). None = not measured, never a 1.0.
        fields.update({"out_volume_ratio": None if f is None else _opt_float(f.volume_ratio),
                       "out_area_ratio": None if f is None else _opt_float(f.area_ratio),
                       "out_bbox_ratio": None if f is None else _opt_float(getattr(f, "bbox_ratio", None)),
                       "out_max_dev_mm": None if f is None else _opt_float(f.max_dev_mm),
                       "out_bodies_before": None if f is None else f.bodies_before,
                       "out_bodies_after": None if f is None else f.bodies_after})
    return fields


def _opt_float(x: Any) -> float | None:
    return None if x is None else float(x)


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


def _run_staged(engine: Any, pool: Any, ws: Any, v_in: Any) -> Any:
    """The staged healer as the engine runs it: its transactions' cheap tier also meshes every member the input
    fails (`assemble.adaptive_cheap_members`). A test engine without that parameter is called as before."""
    if isinstance(engine, RealEngine):
        from meshpipeline.cad.repair_v4.verdict.assemble import adaptive_cheap_members

        return engine.run_staged(pool, ws, cheap_members=adaptive_cheap_members(v_in))
    return engine.run_staged(pool, ws)


def confirm_staged(pool: Any, ws: Any, staged: Any, *, full: Any, input_verdict: Any) -> Any:
    from meshpipeline.cad.repair_v4.stages import confirm_staged as _confirm

    return _confirm(pool, ws, staged, full=full, input_verdict=input_verdict)


def run_file(arm_name: str, entry: Any, source: str, *, pool: Any, engine: Engine, ledger: Any | None,
             arm: Any, budget: Any, h_mm: float | None, route: str | None, max_turns: int, job_id: str,
             workdir: str, workers: int = 1, clock: Any = time.monotonic, throttle: Any = None,
             recipes: Any = None) -> ArmOutcome:
    """Run one file through one arm. `arm` is the `evalspec.Arm`, `budget` the arm's `evalspec.Budget`. `throttle`
    is the shard's `model_throttle.WaveThrottle`, handed to the AI proposer (None: unthrottled). `recipes` is the
    shard's `recipes.RecipeMemory` (CAD_REPAIR_V4_RECIPES; None: off): the search arms read it, and a clean file of
    the dev split writes to it."""
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
        try:
            overlap = overlap_triage(pool, ws, v_in)
        except Exception as exc:  # noqa: BLE001 - a measurement for the report never loses the row
            overlap = {"max_depth_mm": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
        out_v, usd = v_in, 0.0
        extra: dict[str, Any] = {}
        if arm_name == "input_only":
            keep_delivered(ws.brep_path, workdir, is_input=True)
        elif arm_name == "staged":
            staged = _run_staged(engine, pool, ws, v_in)
            out_v = _confirmed_or_input(pool, ws, staged, source, full=full, v_in=v_in, workdir=workdir)
            extra["_triage"] = {"stages": stages_triage(staged)}
        elif arm_name in ("search_det", "search_ai"):
            # the seam contract's call order (spec 04; `repair_v4._find_best`): the search starts from the staged
            # healer's output, never the raw input, or the arm would measure a pipeline the engine does not ship
            if getattr(v_in, "out_of_scope", False):
                # as `repair_v4._find_best` (lane C9): an unbounded input skips the staged healer, whose re-export runs
                # check_valid to its wall. Dormant today - `_usable` excludes an out_of_scope input above - and kept so
                # engine and eval stay one pipeline if the owner moves the unbounded stratum into scope.
                start = ws
            else:
                staged = _run_staged(engine, pool, ws, v_in)
                confirm_staged(pool, ws, staged, full=lambda w: full(pool, w, source), input_verdict=v_in)
                start = staged.ws
            out_v, extra, usd = _search_arm(arm_name, pool=pool, ws=ws, start=start, source=source, judge=judge,
                                            v_in=v_in, ledger=ledger, arm=arm, budget=budget, h_mm=h_mm, route=route,
                                            max_turns=max_turns, job_id=job_id, entry=entry, workers=workers,
                                            clock=clock, full=full, throttle=throttle, recipes=recipes,
                                            workdir=workdir)
        elif arm_name == "search_seq":
            # B24: the SAME start as the search arms (the staged healer's confirmed output), so the comparison is
            # of method - frontier against one-defect-at-a-time - and not of where the two began.
            staged = _run_staged(engine, pool, ws, v_in)
            confirm_staged(pool, ws, staged, full=lambda w: full(pool, w, source), input_verdict=v_in)
            out_v, extra = _seq_arm(pool=pool, ws=ws, start=staged.ws, source=source, judge=judge, v_in=v_in,
                                    budget=budget, clock=clock, full=full, workdir=workdir)
        else:
            raise ValueError(f"arm {arm_name!r} is not dispatchable here")
    except (CheckerUnavailable, InconclusiveVerdict, EngineUnavailable) as exc:
        return excluded(f"checker_unavailable: {type(exc).__name__}: {exc}")
    wall = round(clock() - t0, 3)
    unmeasured = bool(extra.get("_unmeasured"))
    triage = {k: v for k, v in (extra.get("_triage") or {}).items() if v is not None}
    extra = {k: v for k, v in extra.items() if k not in ("_unmeasured", "_triage")}
    if unmeasured:
        return ArmOutcome(row={**base, "inconclusive": True, "model_usd": usd, "wall_s": wall, **extra,
                               "triage": {**triage, "overlap": overlap}})
    bad = _usable(out_v, "the output")
    if bad:
        return excluded(bad)
    return ArmOutcome(row={**base, "inconclusive": False, "model_usd": usd, "wall_s": wall,
                           **_verdict_fields(v_in, out_v), **extra,
                           "triage": {**verdict_triage(out_v), **triage, "overlap": overlap}})


def _search_arm(arm_name: str, *, pool: Any, ws: Any, start: Any, source: str, judge: Any, v_in: Any, ledger: Any,
                arm: Any, budget: Any, h_mm: float | None, route: str | None, max_turns: int, job_id: str, entry: Any,
                workers: int, clock: Any, full: Any, throttle: Any = None,
                recipes: Any = None, workdir: str | None = None) -> tuple[Any, dict, float]:
    """(output verdict, the SearchResult's own measured-ness fields, model USD) for a search arm."""
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.proposer import MODEL_UNAVAILABLE
    from meshpipeline.cad.repair_v4.verdict.assemble import reset_verdict_phases, verdict_phases

    ai_kwargs: dict[str, Any] = {}
    if arm_name == "search_ai":
        if not route:
            raise ValueError("search_ai needs a route (provider:model)")
        ai_kwargs = {"route": route, "ledger": ledger, "max_turns": max_turns, "usd_ceiling": arm.budget.model_usd,
                     "file_id": entry.sha256, "job_id": job_id, "throttle": throttle}
    generators, proposer = S.arm_generators(arm_name, recipes=recipes.book if recipes is not None else None,
                                            **ai_kwargs)
    sbudget = S.Budget(wall_s=budget.wall_s, ops=budget.ops, model_calls=max_turns if proposer else 0,
                       model_usd=budget.model_usd)
    # B23: the verdict's own phases (layer 1, the panel, cache hits, layer 3) for THIS search. Reset here, read
    # the moment it returns, so the delivery verdict that follows is not counted into it. The totals are a sum of
    # concurrent work across the search's threads, never a wall-clock breakdown: `wall_s` is the wall.
    reset_verdict_phases()
    result = S.search(pool, start, source, generators, sbudget, workers=workers, judge=judge, h_mm=h_mm, clock=clock)
    phases = verdict_phases()
    usd = float(proposer.result()["spent_usd"]) if proposer is not None else 0.0
    if workdir is not None:
        write_trace(result.trace, workdir)
    # A search_ai file whose model could not be reached ran deterministic-only: its outcome measures search_det, not
    # the AI arm. It is INCONCLUSIVE for search_ai (never a failed repair, never a success credited to the AI), so an
    # outage that takes the model away from many files makes the arm NOT MEASURED instead of quietly search_det.
    # `model_unavailable` also covers a file whose every model round failed but whose search ended before the third
    # failure could stop the AI: the model was never there, whatever the stop rule had time to record.
    ai_unavailable = proposer is not None and proposer.model_unavailable
    fields = {"reason": result.reason, "verdict_candidates": result.verdict_candidates,
              "inconclusive_candidates": result.inconclusive_candidates,
              "infrastructure_failed": result.infrastructure_failed,
              # B18 (owner ruling 2026-11-24): a wall stop no longer deletes the row, so the row has to SAY it was
              # wall-stopped - a reader keeping those out of a clean-rate denominator needs to see which they are.
              "stopped_on_wall": bool(getattr(result, "stopped_on_wall", False)),
              "_unmeasured": bool(result.unmeasured or ai_unavailable),
              "cheap_full": cheap_full_cells(result.trace.of_kind("confirm")),
              # B22 (owner ruling 2026-11-24): what the DELIVERED state kept of the input part, and how many
              # states the search refused as best for keeping too little. A refusal is reported, never silent.
              "loss": (result.best_loss.row() if getattr(result, "best_loss", None) is not None else None),
              "refused_catastrophic": int(getattr(result, "refused_catastrophic", 0) or 0),
              "_triage": {"search": {**search_triage(result, generators), "verdict_phases": phases}}}
    if proposer is not None:
        fields["ai_stop_reason"] = MODEL_UNAVAILABLE if ai_unavailable else result.ai_stop_reason
    # `result.changed` says whether anything beat the search's START, which is the staged output, not the input: a
    # file the healer improved and the search could not improve further must still ship the healer's state. The
    # engine has this bug today (`_find_best`; strict xfail in test_engine_arms.py); the eval applies the rule as the
    # delivery gate states it - against the INPUT - rather than measuring the bug.
    delivered = S.deliverable(dataclasses.replace(result, changed=result.changed or start.brep_path != ws.brep_path), v_in)
    out_v = v_in if delivered is None else full(pool, delivered, source)
    if workdir is not None:
        keep_delivered(ws.brep_path if delivered is None else delivered.brep_path, workdir, is_input=delivered is None)
    if recipes is not None and bool(getattr(out_v, "clean", False)) and not fields["_unmeasured"]:
        recipes.learn(result, file_sha256=entry.sha256, diagonal_mm=_diagonal_or_none(pool, start))
    return out_v, fields, usd


def _seq_arm(*, pool: Any, ws: Any, start: Any, source: str, judge: Any, v_in: Any, budget: Any, clock: Any,
             full: Any, workdir: str | None = None) -> tuple[Any, dict]:
    """(output verdict, the row's own fields) for `search_seq` (B24).

    The arm's claim is a RATIO - local checks against full panels - so the row carries both, and the headline
    number is `full_checks`. The delivery gate is the one every arm meets, B22's catastrophic-loss refusal
    included; a state that keeps under a tenth of the part is refused here exactly as it is in the search arms.
    """
    from types import SimpleNamespace

    from meshpipeline.cad.repair_v4 import seq as Q
    from meshpipeline.cad.repair_v4.loss import check_loss
    from meshpipeline.cad.repair_v4.search import deliverable
    from meshpipeline.cad.repair_v4.trace import Trace

    trace = Trace()
    trace.record("start", arm="search_seq")
    outcome = Q.repair_sequentially(pool, start, source, judge=judge, sites_of=judge.sites,
                                    run_stage=Q.stage_runner(), wall_s=budget.wall_s, clock=clock, trace=trace)
    if workdir is not None:
        write_trace(trace, workdir)
    # the delivery gate, as the staged arm meets it: `changed` is measured against the INPUT, not against the
    # arm's own start, so a healer-only gain is still delivered (the same rule `_search_arm` applies)
    stand_in: Any = SimpleNamespace(changed=outcome.best.brep_path != ws.brep_path, best=outcome.best,
                                    best_verdict=outcome.verdict)
    loss = (check_loss(pool, ws.brep_path, outcome.best.brep_path)
            if outcome.best.brep_path != ws.brep_path else None)
    delivered = deliverable(stand_in, v_in, loss=loss)
    out_v = v_in if delivered is None else full(pool, delivered, source)
    if workdir is not None:
        keep_delivered(ws.brep_path if delivered is None else delivered.brep_path, workdir,
                       is_input=delivered is None)
    fields: dict[str, Any] = {
        **outcome.counters.row(),
        "loss": (loss.row() if loss is not None else None),
        # the owner's readable trace, one line per step and one per round, on the row as it is written
        "_triage": {"seq": {"lines": outcome.rounds and outcome.lines() or [],
                            "rounds": [{"index": r.index, "kept": r.kept, "full_check": r.full_check,
                                        "steps": r.steps} for r in outcome.rounds]}},
    }
    return out_v, fields


def _diagonal_or_none(pool: Any, ws: Any) -> float | None:
    from meshpipeline.cad.repair_v4.stages import diagonal_mm
    try:
        return float(diagonal_mm(pool, ws))
    except Exception:  # noqa: BLE001 - the recipe falls back to the sites' own diagonal, or keeps raw mm
        return None


#: The four cells of the cheap-vs-full table, named as the re-probe's `reprobe.agreement` names them.
CHEAP_FULL_CELLS = ("cheap_pass_full_pass", "cheap_pass_full_fail", "cheap_fail_full_pass", "cheap_fail_full_fail")


def cheap_full_cells(confirms: list[dict]) -> dict[str, int]:
    """Cheap tier vs full panel over the states this search CONFIRMED (the search's `confirm` trace events carry the
    state's cheap score and the full verdict's `clean`). Since the re-probe runs the full verdict only (plan amendment
    2026-11-23 19:15), this is where the cheap tier's agreement is measured: the tier now exists only to rank search
    states. A cheap PASS is `score[0] == 1` (layer 1 VALID since the owner's 2026-11-24 decision; it was "valid AND
    faithful") and `score[1]` = EVERY cheap member passing (the cheap panel is
    `verdict.assemble.default_members("cheap")`); a full pass is `clean`. The code reads key 0 rather than the
    layers, so it followed the definition without a change - only this sentence was stale."""
    from meshpipeline.cad.repair_v4.verdict.assemble import default_members

    n_cheap = len(default_members("cheap"))
    cells = dict.fromkeys(CHEAP_FULL_CELLS, 0)
    for e in confirms:
        cheap = e.get("cheap_score")
        if not cheap or len(cheap) < 2 or e.get("clean") is None:
            continue
        cheap_pass = int(cheap[0]) == 1 and int(cheap[1]) >= n_cheap
        cells[f"cheap_{'pass' if cheap_pass else 'fail'}_full_{'pass' if e['clean'] else 'fail'}"] += 1
    return cells


# ------------------------------------------------------------------ failure triage (read by triage.py)
#
# Every row carries `triage`: WHY its output is what it is, in a compact, bounded form a wave-level report can
# aggregate. From the OUTPUT verdict (every arm): the panel members still failing and their error kinds, the deviation
# budget used, the hard layer-1 findings. From the search (search arms): the stop reason, the open site kinds at the
# stop, each op's fate (proposed by det/ai, evaluated, declined / failed / invalid with their reasons), what the
# deterministic sweep declined to propose, and the proposer's turns, tool errors and dollars. From the staged healer
# (staged arm): which stages were kept and which rolled back.

#: reasons are free text; each is cut to this and each fate keeps only its most common few, so a row stays small
TRIAGE_REASON_CHARS = 120
TRIAGE_TOP_REASONS = 5
#: evaluated candidates whose (parent score -> cheap score) is kept, in proposal order; the total is kept beside it
TRIAGE_MAX_DELTAS = 8
_REJECTED_AS = {"validate": "invalid", "declined": "declined", "failed": "failed", "error": "failed"}
_COUNTED = ("evaluated", "duplicate", "inconclusive", "skipped_budget")


def _short(text: Any) -> str:
    return " ".join(str(text).split())[:TRIAGE_REASON_CHARS]


def _top(counter: Any) -> dict[str, int]:
    """The most common reasons; the rest folded into `(other)` so the count still adds up."""
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    top = dict(items[:TRIAGE_TOP_REASONS])
    rest = sum(n for _, n in items[TRIAGE_TOP_REASONS:])
    if rest:
        top["(other)"] = rest
    return top


#: located entries kept per failing member in a row (a mesher can name hundreds of faces; the total is kept beside them)
LOCATED_CAP = 20


def _located(m: Any) -> dict[str, Any]:
    """Where the member said it went wrong (`panel.Located`): the face fingerprint, the point in mm and the mesher's own
    tag, the first LOCATED_CAP of them, and how many there were."""
    loc = list(getattr(m, "located", None) or [])
    return {"located": [{"entity": getattr(x, "entity", None),
                         "xyz": None if getattr(x, "xyz", None) is None else [float(c) for c in x.xyz],
                         "mesher_tag": getattr(x, "mesher_tag", None)} for x in loc[:LOCATED_CAP]],
            "located_total": len(loc)}


def verdict_triage(v: Any) -> dict[str, Any]:
    f = v.faithful
    deviation = None if f is None else {
        "ok": bool(f.ok), "max_dev_mm": f.max_dev_mm, "bound_mm": f.bound_mm,
        "used": (f.max_dev_mm / f.bound_mm) if f.bound_mm else None, "reasons": list(getattr(f, "reasons", []))}
    return {"panel_failing": [{"name": m.name, "status": m.status, "error_kind": getattr(m, "error_kind", None),
                               "cause": getattr(m, "cause", None),  # report.py counts repeated timeouts by cause
                               **_located(m)}                        # the face / point the mesher named (C5)
                              for m in v.panel if m.status != "pass"],
            "deviation": deviation,
            "valid": {"ok": bool(v.valid.ok), "hard": [x.code for x in v.valid.hard]}}


def stages_triage(staged: Any) -> dict[str, Any] | None:
    kept, rolled = getattr(staged, "kept", None), getattr(staged, "rolled_back", None)
    conf = getattr(staged, "confirmation", None)
    if kept is None and rolled is None and conf is None:
        return None
    out: dict[str, Any] = {"kept": list(kept or []), "rolled_back": list(rolled or [])}
    if conf is not None:      # what the FULL panel undid of what the cheap tier kept
        out["full_panel"] = {"fell_back_to": conf.fell_back_to, "dropped": list(conf.dropped)}
    return out


def _confirmed_or_input(pool: Any, ws: Any, staged: Any, source: str, *, full: Any, v_in: Any,
                        workdir: str | None = None) -> Any:
    """The staged arm's output verdict, measured as the ENGINE ships it (`repair_v4._verdict_only` + `deliverable`):
    the healer's result confirmed by the full panel, falling back stage by stage (`stages.confirm_staged`), and then
    the delivery gate - a clean input is never replaced, and nothing that passes fewer members is delivered."""
    from types import SimpleNamespace

    from meshpipeline.cad.repair_v4.loss import check_loss
    from meshpipeline.cad.repair_v4.search import deliverable

    conf = confirm_staged(pool, ws, staged, full=lambda w: full(pool, w, source), input_verdict=v_in)
    out_v = conf.verdict if conf.verdict is not None else v_in
    # `deliverable` reads only these three fields of a result; a SearchResult cannot be built here (there was no
    # search), so the stand-in is deliberate
    stand_in: Any = SimpleNamespace(changed=staged.ws.brep_path != ws.brep_path, best=staged.ws, best_verdict=out_v)
    # B22: the staged arm meets the same catastrophic-loss gate as the search arms, measured against the INPUT
    loss = check_loss(pool, ws.brep_path, staged.ws.brep_path) if staged.ws.brep_path != ws.brep_path else None
    shipped = deliverable(stand_in, v_in, loss=loss)
    if workdir is not None:
        keep_delivered(ws.brep_path if shipped is None else shipped.brep_path, workdir, is_input=shipped is None)
    return v_in if shipped is None else out_v


TRACE_NAME = "search-trace.json"
DELIVERED_BREP = "delivered.brep"
DELIVERED_JSON = "delivered.json"


def keep_delivered(source_brep: str | None, workdir: str, *, is_input: bool) -> None:
    """Which state this (file, arm) SHIPPED, beside its work states: `delivered.json` always, and a copy of a non-input
    output as `delivered.brep` (the input is already `input.brep` there), so a repaired output can be measured after
    the wave. A failed copy is recorded in the json and never costs the row."""
    import shutil

    rec: dict[str, Any] = {"is_input": is_input, "source_brep": source_brep, "kept": None}
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        if not is_input and source_brep:
            shutil.copyfile(source_brep, Path(workdir) / DELIVERED_BREP)
            rec["kept"] = True
    except Exception as exc:  # noqa: BLE001 - evidence lost, not a failed measurement
        rec.update(kept=False, error=f"{type(exc).__name__}: {exc}"[:300])
    try:
        (Path(workdir) / DELIVERED_JSON).write_text(json.dumps(rec))
    except OSError:
        pass


def write_trace(trace: Any, workdir: str) -> None:
    """The WHOLE search trace beside the file's work states (`<workdir>/search-trace.json`): the row keeps a summary,
    triage needs every candidate (kept vs abandoned, each op's mode, every verdict delta, a row's origin). A failed
    write is recorded in place of the trace and never costs the row."""
    path = Path(workdir) / TRACE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        trace.write(path)
    except Exception as exc:  # noqa: BLE001 - a lost trace is evidence lost, not a failed measurement
        try:
            path.write_text(json.dumps({"events": [], "write_error": f"{type(exc).__name__}: {exc}"[:300]}))
        except OSError:
            pass


def search_triage(result: Any, generators: Any) -> dict[str, Any]:
    from collections import Counter

    trace = result.trace
    stops = trace.of_kind("stop")
    stop = stops[-1] if stops else {}
    expands = trace.of_kind("expand")
    at = next((e for e in reversed(expands) if e.get("state") == stop.get("best")), None)
    where = "best" if at is not None else ("last_expanded" if expands else None)
    at = at if at is not None else (expands[-1] if expands else {"sites": []})
    ops: dict[str, dict[str, Any]] = {}
    for e in trace.of_kind("candidate"):
        op = str(e.get("op"))
        if op == "run_stage":
            op = f"run_stage:{(e.get('params') or {}).get('stage')}"
        d = ops.setdefault(op, {"det": 0, "ai": 0, "ops_applied": 0, **dict.fromkeys(_COUNTED, 0),
                                "declined": Counter(), "failed": Counter(), "invalid": Counter()})
        d[e.get("source") if e.get("source") in ("det", "ai") else "det"] += 1
        # what this op cost the op budget, not how many candidates named it: a `run_stage` macro applies a
        # CHAIN, and since Task 53 the budget is charged per applied op (search records `ops_applied`).
        d["ops_applied"] += int(e.get("ops_applied") or 0)
        outcome = e.get("outcome")
        if outcome in _COUNTED:
            d[outcome] += 1
        elif outcome == "rejected":
            errors = e.get("errors") or ["(no reason recorded)"]
            d[_REJECTED_AS.get(e.get("stage"), "failed")][_short(errors[0])] += 1
    for d in ops.values():
        for k in ("declined", "failed", "invalid"):
            d[k] = _top(d[k])
    skipped: Counter = Counter()
    for g in generators or ():
        skipped.update(getattr(g, "skipped", None) or {})
    ai = None
    if result.ai:
        keys = ("turns", "tool_errors", "candidate_errors", "declared_unfixable", "truncated_rounds", "spent_usd")
        ai = {k: sum(float(r.get(k) or 0) if k == "spent_usd" else int(r.get(k) or 0) for r in result.ai) for k in keys}
        ai.update(stop_reason=result.ai_stop_reason, ai_capped=bool(result.ai_capped))
    evaluated = [e for e in trace.of_kind("candidate") if e.get("outcome") == "evaluated"]
    deltas = [{"op": e.get("op"), "source": e.get("source"), "parent": e.get("parent_score"), "score": e.get("score")}
              for e in evaluated[:TRIAGE_MAX_DELTAS]]
    return {"pruned": dict(getattr(result, "pruned", {}) or {}),
            "verdicts_saved": int(getattr(result, "verdicts_saved", 0) or 0),
            "stop": {"reason": stop.get("reason"), "ended_by": stop.get("ended_by"), "spent": stop.get("spent"),
                     # D9: how often the model was asked again on an empty frontier (0 for search_det, always)
                     "reprompts": len(trace.of_kind("reprompt"))},
            "open_sites": dict(Counter(str(s.get("kind")) for s in at.get("sites") or [])), "open_sites_at": where,
            "ops": ops, "not_proposed": _top(skipped), "ai": ai, "deltas": deltas, "deltas_total": len(evaluated),
            "sites_by_placement": _placement(at.get("sites") or [])}


#: body pairs whose overlap is measured per input, at most (each pair is two `_cut_depth` calls on the kernel)
MAX_OVERLAP_PAIRS = 10
OVERLAP_TIMEOUT_S = 240.0


def overlap_triage(pool: Any, ws: Any, v_in: Any) -> dict | None:
    """The INPUT's body overlap as the owner's ruling needs it ("reported both ways": with and without a stratum for
    overlaps beyond fidelity), so either cut is computed from rows without a rerun. Per overlapping body pair (layer 1's
    `body_overlap` findings), both cuts measured by the kernel's own `body_kernel._cut_depth` (the measure `cut_by`
    declines on), with its method; per pair the cheaper cut is what a repair would need, and the FILE needs every pair,
    so `max_depth_mm` is the worst pair's cheaper cut, `within_bound` compares it with the fidelity bound. None when the
    input has no overlap. A failed measurement is recorded, never a reason to lose the row."""
    pairs: list[list[int]] = []
    for finding in getattr(v_in.valid, "hard", []) or []:
        if finding.code != "body_overlap" or len(finding.entities) < 2:
            continue
        try:
            pair = sorted(int(str(e)[1:]) for e in finding.entities[:2])
        except ValueError:
            continue
        if pair not in pairs and len(pairs) < MAX_OVERLAP_PAIRS:
            pairs.append(pair)
    if not pairs:
        return None
    result = pool.call("v4_overlap_depths", {"brep_path": ws.brep_path, "pairs": pairs}, timeout_s=OVERLAP_TIMEOUT_S)
    if not result.ok or result.value is None:
        return {"max_depth_mm": None, "method": None, "bound_mm": None, "within_bound": None, "pairs": [],
                "error": f"{result.status}: {result.error}"[:200]}
    rec = result.value
    out_pairs, worst = [], None
    for p in rec.get("pairs") or []:
        cuts = [c for c in (p.get("a_by_b"), p.get("b_by_a")) if c and c.get("depth_mm") is not None]
        need = min(cuts, key=lambda c: c["depth_mm"]) if cuts else None
        out_pairs.append({**p, "required_mm": None if need is None else need["depth_mm"],
                          "required_method": None if need is None else need["method"]})
        if need is not None and (worst is None or need["depth_mm"] > worst["depth_mm"]):
            worst = need
    bound = rec.get("bound_mm")
    return {"max_depth_mm": None if worst is None else worst["depth_mm"],
            "method": None if worst is None else worst["method"], "bound_mm": bound,
            "within_bound": None if worst is None or bound is None else worst["depth_mm"] <= bound,
            "diag_mm": rec.get("diag_mm"), "pairs": out_pairs, "measured_on": "input"}


def _placement(sites: list[dict]) -> dict[str, int]:
    """located = names an entity and is not coarse; coarse = the whole part (`sites._coarse_site`, or a finding that
    names only a body); unlocated = names nothing at all (an `unlocated` site, or a whole-part one with no entity)."""
    out = {"located": 0, "coarse": 0, "unlocated": 0}
    for s in sites:
        if s.get("coarse"):
            out["coarse"] += 1
        elif s.get("entities") and s.get("kind") != "unlocated":
            out["located"] += 1
        else:
            out["unlocated"] += 1
    return out


def failure_reason(exc: BaseException) -> str:
    return f"error: {type(exc).__name__}: {''.join(traceback.format_exception_only(type(exc), exc)).strip()[-300:]}"
