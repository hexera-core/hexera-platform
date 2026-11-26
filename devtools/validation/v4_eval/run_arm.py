#!/usr/bin/env python3
# Responsibility: Run ONE shard of the v4 wave inside the image: the re-probe (`--mode reprobe`) or a
#                 repair_corpus harness run (`--mode harness`), always under the per-file wall kill, and
#                 say exactly what ran in PROVENANCE.json.
# Owns: shard selection from the pool manifest, input resolution, the pool/cache wiring, rows.jsonl,
#       EXCLUDED.json and PROVENANCE.json.
#       The shard's model-spend LEDGER (`WaveLedger.for_shard`) when the launcher names a wave: wave type, wave id,
#       N, a durable log path and the wave bucket's marker store.
#       The harness's ROW WRITER (`write_arm_rows`): every row is checked against the report's own row contract
#       before it is written, and the plan's provenance (`plan_wave(...).provenance()`, incl. the claimed slot) is
#       copied into PROVENANCE.json so the report can tell the once-only slot was claimed.
# Boundaries: it measures nothing itself. `reprobe` runs the checker (through the verdict cache);
#             repair_corpus runs the files in harness mode. Excluded files (infrastructure failures)
#             are written to their own file and never into rows.jsonl.
"""One shard, in the container (launched like the v3 shards).

    python run_arm.py --shard 3/40 --inputs /eval/inputs --out /eval/out \\
        --pool devtools/validation/corpora/v4/pool.json --cache-uri gs://<bucket>/verdict-cache \\
        --commit <40-hex> --image <digest ref> [--file-timeout-s 900]

    python run_arm.py --mode harness --shard 3/40 --inputs /eval/inputs --out /eval/out ...

    python run_arm.py --mode arms --arms input_only,staged,search_det,search_ai --split dev --split-file dev.json \\
        --h-mm 0.5 --shard 3/40 --wave-type normal --wave-id w1 --n-shards 40 --ledger-path ... --marker-bucket ...

`--mode arms` (implied by --arm/--arms) runs each named arm on each file of the shard that is in the split, through
`evalspec.ARMS`; `evalspec.WavePlan.shard_argv` emits exactly these flags (`--wave` names a WAVES entry whose arms,
split and wave type the other flags must agree with). A model arm (`search_ai`) is refused unless the shard has its
WaveLedger, and the ledger is passed into every proposer it builds.

Inputs are staged as `<inputs>/<sha256>/<basename>`. A shard takes the pool entries whose hash falls in
it (evalspec.shard_of), so the partition does not depend on manifest order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import platform
import socket
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evalspec = _load("v4_evalspec", HERE / "evalspec.py")
reprobe_mod = _load("v4_reprobe", HERE / "reprobe.py")
pool_mod = _load("v4_pool_manifest", HERE / "pool.py")
wave_limits = _load("v4_wave_limits", HERE / "wave_limits.py")
marker_probe = _load("v4_marker_probe", HERE / "marker_probe.py")
arms_mod = _load("v4_arms", HERE / "arms.py")
wave_spec_mod = _load("v4_wave_spec", HERE / "wave_spec.py")

#: The arms this runner can dispatch. v3_meshability_pass_ai is run by v3_eval's own run_arm (it is v3 as it ships).
DISPATCHABLE_ARMS = evalspec.DISPATCHABLE_ARMS
SEARCH_ARMS = ("search_det", "search_ai")


#: A per-file runner that replaces `arms.run_file` in `run_arms`. None in production (the real dispatch runs); tests
#: and the infrastructure check set it to run the same dispatch, row contract and provenance over a fake or a probe.
ARM_RUNNER: ArmRunner | None = None


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def shard_entries(entries: list[Any], shard: str, plan: dict | None = None) -> list[Any]:
    """This shard's entries: by the hash, or, given a cost-balanced shard plan, by the plan. An entry the plan does not
    assign is refused, never dropped: a silently smaller shard would make the wave a different set."""
    i, n = evalspec.parse_shard(shard)
    if plan is None:
        return [e for e in entries if evalspec.shard_of(e.sha256, i, n)]
    if int(plan["n"]) != n:
        raise ValueError(f"the shard plan has {plan['n']} shards, this shard is {shard}")
    missing = [e.sha256 for e in entries if e.sha256 not in plan["assign"]]
    if missing:
        raise ValueError(f"{len(missing)} file(s) are not in the shard plan (e.g. {missing[0][:12]})")
    return [e for e in entries if int(plan["assign"][e.sha256]) == i]


def _shard_plan(args: argparse.Namespace, prov: dict) -> dict | None:
    """The cost-balanced shard plan named by --shard-plan, its digest stamped into PROVENANCE (collect and report check
    the partition against the plan with that digest, and refuse any other)."""
    path = getattr(args, "shard_plan", None)
    if not path:
        return None
    plan = evalspec.load_shard_plan(path)
    prov["shard_plan"] = evalspec.plan_digest(plan)
    return plan


def select_sources(entries: list[Any], sources: str) -> list[Any]:
    """The pool entries whose source the wave may run (a comma list of pool source labels). An EMPTY
    list means the CLEARED sources, never the whole pool: a missing flag or metadata key must not open
    the pool to an uncleared dataset (Fusion 360). A label that is not cleared is refused here as well
    as at launch."""
    wanted = set(evalspec.resolve_sources(sources.split(",") if sources else
                                          list(evalspec.CLEARED_SOURCES)))
    return [e for e in entries if e.source in wanted]


#: D22: kernel/mesher workers per file = candidate workers (`--workers`, the search's concurrent candidate
#: evaluation) x panel threads (lane A's parallel panel: CRS_V4_PANEL_THREADS, 1 = the serial panel; unset = the
#: verdict's own default, `assemble.DEFAULT_PANEL_THREADS`, parallel since CRS v5 step 4). Each candidate
#: thread holds one pool worker for its op and one per panel member it meshes at once, so a pool smaller than the
#: product would starve the threads it was sized for. Cores per container should be at least the product
#: (snap-v4 VCPU_PER_SHARD), or the parallelism is oversubscription.
PANEL_THREADS_ENV = "CRS_V4_PANEL_THREADS"


def pool_size(workers: int) -> int:
    from meshpipeline.cad.repair_v4.verdict.assemble import full_panel_threads

    # read through the verdict's OWN function, so the pool is sized for the threads the panel will actually use
    return max(1, int(workers)) * full_panel_threads()


def input_path(inputs: Path, entry: Any) -> str:
    """Where a staged input lives; a missing file raises FileNotFoundError (the entry is EXCLUDED)."""
    path = inputs / entry.sha256 / Path(entry.name).name
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return str(path)


def make_cache(uri: str) -> Any:
    """THE wave's one verdict cache. `--cache-uri` is exported as the engine's own setting, so the search and
    staged-healer judges (`default_judge`, `stages`, which read `default_verdict_cache()` and take no flag) share
    the cache the re-probe uses: one cache per wave, one source of truth. Called once from `main`."""
    import os

    from meshpipeline.cad.repair_v4.verdict.cache import CACHE_URI_ENV, default_verdict_cache

    _PRIOR_ENV.setdefault(CACHE_URI_ENV, os.environ.get(CACHE_URI_ENV))
    os.environ[CACHE_URI_ENV] = uri
    return default_verdict_cache()


_PRIOR_ENV: dict[str, str | None] = {}


def restore_cache_env() -> None:
    """Undo `make_cache`'s export (a shard process just exits; an in-process caller, a test, must not leak it)."""
    import os

    while _PRIOR_ENV:
        name, prior = _PRIOR_ENV.popitem()
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def run_reprobe(args: argparse.Namespace, out: Path, prov: dict) -> int:
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    listed = pool_mod.read_pool(Path(args.pool))
    whole_pool, dropped = reprobe_mod.split_allowed(listed)  # licence hold: F360 never reaches a shard
    prov["pool_listed"], prov["pool_dropped_by_source"] = len(listed), dropped
    print(f"pool: {len(listed)} listed, {len(whole_pool)} allowed, dropped {dropped}", file=sys.stderr)
    reprobe_mod.check_not_quarantined(whole_pool)  # pool-level: any source, before any shard work
    entries = shard_entries(select_sources(whole_pool, args.sources), args.shard, _shard_plan(args, prov))
    prov["shard_files"] = len(entries)
    prov["sources"] = args.sources or "cleared"
    cache = make_cache(args.cache_uri)
    prov["cache_uri"] = args.cache_uri or None
    pool = WorkerPool(pool_size(args.workers))
    rows_path = out / "rows.jsonl"
    try:
        with rows_path.open("w", encoding="utf-8") as handle:
            def emit(row: Any) -> None:  # streamed per file, so a killed VM loses little
                handle.write(json.dumps(reprobe_mod.asdict(row), sort_keys=True) + "\n")
                handle.flush()

            result = reprobe_mod.probe_pool(
                entries, pool=pool, resolve=lambda e: input_path(Path(args.inputs), e), cache=cache,
                file_timeout_s=args.file_timeout_s, seed=evalspec.SEED, on_row=emit)
    finally:
        pool.close()
    (out / "rows.json").write_text(json.dumps(
        [reprobe_mod.asdict(r) for r in result.final_rows()], sort_keys=True))
    (out / "cheap.jsonl").write_text("".join(json.dumps(reprobe_mod.asdict(r), sort_keys=True) + "\n" for r in result.cheap))
    (out / "EXCLUDED.json").write_text(json.dumps([reprobe_mod.asdict(x) for x in result.excluded], indent=1))
    prov.update(rows=len(result.final_rows()), cheap_rows=len(result.cheap), full_rows=len(result.full),
                excluded=len(result.excluded))
    return 0


def split_entries(whole_pool: list[Any], split: str, split_file: str | None) -> list[Any]:
    """The pool entries of a split, by sha256. A split that names a file the pool does not carry is refused: a
    silently smaller split would make an arm's clean rate a rate over a different set."""
    path = Path(split_file) if split_file else reprobe_mod.SPLITS_DIR / f"{split}.json"
    split_rows = reprobe_mod.read_split(path)
    reprobe_mod.check_sources(split_rows)  # Fusion 360 is in no split, row or report: refused by name, not by omission
    shas = [r.sha256 for r in split_rows]
    by_sha = {e.sha256: e for e in whole_pool}
    missing = [h for h in shas if h not in by_sha]
    if missing:
        raise ValueError(f"split {split!r} ({path}) names {len(missing)} file(s) the pool does not carry, "
                         f"e.g. {missing[0]}")
    return [by_sha[h] for h in shas]


def run_arms(args: argparse.Namespace, out: Path, prov: dict, ledger: Any | None = None, *,
             engine: Any = None, plan: dict | None = None, runner: ArmRunner | None = None) -> int:
    """Every arm in `args.arms_resolved` on every file of this shard's slice of the split: THE one dispatch, whether
    it was reached by `--mode arms` or by `--mode harness` with a plan.

    `ledger` is the shard's WaveLedger and is handed to every model arm; a model arm with no ledger is refused here
    (before a pool is started) rather than built without a cap. Nothing in this function prices or caps by itself:
    the cap is the ledger's `admit`, which the proposer calls before every model request.

    Rows are written ONLY through `write_arm_rows`, after the report's own row contract has accepted every one: a row
    the report would refuse fails the shard (exit 4) and leaves no rows file. A file that could not be run is
    EXCLUDED and gets no row. `runner`, when given, replaces `arms.run_file` (tests and the infrastructure check);
    `plan` is the launcher's claimed plan, whose `arms`/`split`/`wave` stay as the report's comparison values."""
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    names = list(args.arms_resolved)
    model_arms = [a for a in names if evalspec.ARMS[a].uses_model]
    if model_arms and ledger is None:
        raise arms_mod.ModelArmWithoutLedger(
            f"{model_arms} make model calls and this shard has no WaveLedger (pass --wave-type ... --marker-bucket)")
    split_file = getattr(args, "split_file", None)
    # held-out files are reported, never tuned on: a tuning purpose is refused for the held-out split here
    if getattr(args, "purpose", "report") == "tuning":
        evalspec.refuse_heldout_for_tuning(args.split, *([split_file] if split_file else []))
    if any(a in SEARCH_ARMS for a in names) and not (args.h_mm and args.h_mm > 0):
        raise ValueError("the search arms need --h-mm > 0: there is no default element size")
    listed = pool_mod.read_pool(Path(args.pool))
    whole_pool, dropped = reprobe_mod.split_allowed(listed)  # licence hold: F360 never reaches a shard
    prov["pool_listed"], prov["pool_dropped_by_source"] = len(listed), dropped
    reprobe_mod.check_not_quarantined(whole_pool)
    # every refusal about WHICH files run (Fusion 360 in the split, a file the pool lacks) comes before the model
    # route is probed, so a bad split costs nothing
    in_split = {e.sha256 for e in split_entries(whole_pool, args.split, split_file)}
    entries = shard_entries([e for e in select_sources(whole_pool, args.sources) if e.sha256 in in_split],
                            args.shard, _shard_plan(args, prov))
    route = None
    throttle = None
    recipes = build_recipes(args, prov, plan) if any(a in SEARCH_ARMS for a in names) else None
    if model_arms and runner is None:  # an injected runner (a test, the infra check) replaces the dispatch AND its route
        route = _prepare_model_route(args, prov, ledger)
        throttle = build_throttle(args, route, prov, ledger=ledger)
    engine = engine if engine is not None else arms_mod.RealEngine()
    # `arms` / `split` / `wave` are the PLAN's when there is one (copied before this runs) and the report compares
    # them with the wave definition, so they are not overwritten; what this shard ran is `arms_run`
    prov.update(shard_files=len(entries), arms_run=names, h_mm=args.h_mm, route=route, max_turns=args.max_turns,
                sources=args.sources or "cleared", ledger_attached=ledger is not None)
    if plan is None:
        prov.update(arms=names, split=args.split, wave=args.wave)
    rows: list[dict] = []
    excluded: list[dict] = []
    partial = out / "rows.partial.jsonl"  # streamed per file so a killed VM loses little; not the rows file
    pool = WorkerPool(pool_size(args.workers))
    try:
        with partial.open("w", encoding="utf-8") as handle:
            for entry in entries:
                for name in names:
                    spec = effective_arm(evalspec.ARMS[name], args)
                    fb = None
                    try:
                        source = input_path(Path(args.inputs), entry)
                        started = time.monotonic()
                        with reprobe_mod._WallClock(args.file_timeout_s) as alarm:
                            # CRS v5 step 4: the file's size-scaled budget, started with the alarm it may re-arm
                            fb = arms_mod.FileBudget(base_wall_s=args.file_timeout_s, wall_clock=alarm,
                                                     started=started)
                            if runner is not None:
                                row, why = runner(entry, name, source)
                                outcome = arms_mod.ArmOutcome(row=row, excluded=None if row is not None
                                                              else (why or "no row and no reason"))
                            else:
                                outcome = arms_mod.run_file(
                                    name, entry, source, pool=pool, engine=engine, ledger=ledger, arm=spec,
                                    budget=spec.budget, h_mm=args.h_mm, route=route, max_turns=args.max_turns,
                                    job_id=f"{args.wave_id or 'wave'}:{entry.sha256[:12]}:{name}",
                                    workdir=str(out / "work" / entry.sha256[:12] / name), workers=args.workers,
                                    throttle=throttle, recipes=recipes, file_budget=fb)
                    except FileNotFoundError as exc:
                        outcome = arms_mod.ArmOutcome(excluded=f"input_missing: {exc}")
                    except reprobe_mod.FileTimeout as exc:
                        outcome = arms_mod.ArmOutcome(excluded=f"file_timeout: {exc}",
                                                      info={"finish": arms_mod.FILE_WALL, "finish_cause": "alarm"})
                    except arms_mod.ModelArmWithoutLedger:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one file's failure is an excluded file, never a verdict
                        outcome = arms_mod.ArmOutcome(excluded=arms_mod.failure_reason(exc))
                    wall_s = fb.wall_s if fb is not None and fb.base_wall_s > 0 else args.file_timeout_s
                    late = reprobe_mod.overran(started, wall_s) if outcome.row is not None else None
                    if late is not None:  # the alarm was lost; a row past its wall is not a measurement
                        outcome = arms_mod.ArmOutcome(excluded=late, info={"finish": arms_mod.FILE_WALL,
                                                                          "finish_cause": "overran"})
                    if outcome.row is not None:
                        rows.append(outcome.row)
                        handle.write(json.dumps(outcome.row, sort_keys=True) + "\n")
                        handle.flush()
                    else:
                        _events().emit("excluded", severity="WARNING", sync=True, file_sha256=entry.sha256,
                                       file_name=entry.name, arm=name, reason=outcome.excluded, info=outcome.info)
                        excluded.append({"name": entry.name, "sha256": entry.sha256, "source": entry.source,
                                         "arm": name, "reason": outcome.excluded,
                                         **(fb.fields() if fb is not None else {}), **outcome.info})
    finally:
        pool.close()
        if throttle is not None and "model_throttle" in prov:
            # rounds that gave up waiting for a slot after wait_s (each a failed model round for the proposer)
            prov["model_throttle"]["waits_timed_out"] = getattr(throttle, "waits_timed_out", 0)
        if throttle is not None and getattr(throttle, "guard", None) is not None:
            prov["wave_backoff"] = throttle.guard.describe()   # what this shard held for, and the trips it saw
            prov["model_throttle"]["rate_limited_calls"] = throttle.rate_limited
        if recipes is not None:
            prov["recipes"] = recipes.describe()   # with what this shard learned, for the A/B
    # the partial file is deleted only once the rows file is written: a refused write (a row the schema rejects)
    # keeps the per-file partial rows, so a shard's measurements are never lost to a bookkeeping error (step-13 wave:
    # every v5 shard lost its rows this way and they had to be recovered by hand)
    write_arm_rows(out, rows, excluded)
    partial.unlink(missing_ok=True)
    prov.update(rows=len(rows), excluded=len(excluded),
                unmeasured_rows=sum(1 for r in rows if r.get("inconclusive")),
                model_usd=round(sum(float(r.get("model_usd") or 0.0) for r in rows), 6))
    return 0


def _prepare_model_route(args: argparse.Namespace, prov: dict, ledger: Any) -> str:
    """Before any file: this process must hold a model key, the real router must be installed under the proposer,
    and a route must pass the tool-and-image probe. Each failure refuses the shard (NoModelKey / NoUsableRoute)
    instead of letting a model arm run as a deterministic one."""
    from meshpipeline.cad.repair_v4 import model_route
    from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_ROUTES

    model_route.require_model_key()
    model_route.install_router()
    routes = (args.route,) if args.route else REPAIR_V4_PROPOSE_ROUTES
    # the probe calls are paid: they are admitted and settled in THIS shard's ledger (Task 49's sixth gap)
    route = model_route.probe_route_chain(routes, ledger=ledger)
    prov["route_probe"] = {"candidates": list(routes), "chosen": route}
    return route


def build_recipes(args: argparse.Namespace, prov: dict, plan: dict | None) -> Any:
    """The shard's recipe memory when CAD_REPAIR_V4_RECIPES is on (None, and `prov["recipes"]` says off, when it is
    not). It READS a snapshot taken now; it WRITES only on the dev split and never on the held-out wave."""
    from meshpipeline.settings.cad_repair import recipes_enabled, recipes_uri

    if not recipes_enabled():
        prov["recipes"] = {"enabled": False}
        return None
    from meshpipeline.cad.repair_v4.recipes import RecipeMemory

    uri = recipes_uri()
    if not uri:
        raise ValueError("CAD_REPAIR_V4_RECIPES is on but CAD_REPAIR_V4_RECIPES_URI is unset: no recipe location")
    split = (plan or {}).get("split") or args.split
    wave_type = (plan or {}).get("wave_type") or getattr(args, "wave_type", None)
    memory = RecipeMemory.for_shard(uri, split=split, wave_type=wave_type,
                                    provenance={"wave_id": getattr(args, "wave_id", None),
                                                "commit": getattr(args, "commit", None)})
    prov["recipes"] = memory.describe()
    return memory


def build_throttle(args: argparse.Namespace, route: str, prov: dict, ledger: Any | None = None) -> Any:
    """This shard's slice of the wave's model-call allowance on `route` (`model_throttle.WaveThrottle.for_shard`):
    1/N of the wave's in-flight ceiling (`--model-max-in-flight`, default derived from the route's measured limits),
    requests and tokens per minute. A wave with more shards than in-flight calls is REFUSED here, before any file,
    unless the launch passes `--allow-in-flight-floor`: each slice is then floored to one call (so the wave may exceed
    the ceiling) and `prov["model_throttle"]["in_flight_floor_override"]` records that the override was given."""
    from meshpipeline.cad.repair_v4.model_throttle import WaveBackoffGuard, WaveThrottle

    n_shards = getattr(args, "n_shards", None) or evalspec.parse_shard(args.shard)[1]
    # the wave-level 429 guard shares the ledger's wave-bucket marker store; without a wave id and markers (a test,
    # a single-file run) the shard has only its own cool-down, and the provenance says so
    markers = getattr(ledger, "markers", None)
    wave_id = getattr(args, "wave_id", None)
    guard = (WaveBackoffGuard(markers, wave_id=str(wave_id), shard_id=str(args.shard))
             if markers is not None and wave_id else None)
    throttle = WaveThrottle.for_shard(route, n_shards=n_shards,
                                      max_in_flight=getattr(args, "model_max_in_flight", None), guard=guard,
                                      allow_floor=bool(getattr(args, "allow_in_flight_floor", False)))
    s = throttle.slice
    prov["model_throttle"] = {"n_shards": n_shards, "max_in_flight": s.max_in_flight, "rpm": round(s.rpm, 2),
                              "tpm": round(s.tpm), "in_flight_floored": s.floored,
                              "in_flight_floor_override": bool(getattr(args, "allow_in_flight_floor", False))}
    if guard is None:
        prov["wave_backoff"] = {"guard": "absent", "reason": "no wave id or marker store on this shard"}
    return throttle


def run_repair_corpus(args: argparse.Namespace, out: Path, prov: dict) -> int:
    """`--mode harness` with NO plan: repair_corpus's own harness run (its rows are not ArmRows and no report reads
    them). A wave never takes this path: the launcher always hands its shards --plan-provenance."""
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    argv = evalspec.harness_argv(inputs=args.inputs, out=str(out), shard=args.shard,
                                 file_timeout_s=args.file_timeout_s)
    prov["repair_corpus_argv"] = argv
    sys.argv = ["repair_corpus.py", *argv]
    return int(rc.main() or 0)


class PlanProvenanceMismatch(RuntimeError):
    """The `--plan-provenance` the launcher handed this shard disagrees with the flags the shard was started with."""


#: One file through one arm, as a test or check supplies it in place of `arms.run_file`: `(entry, arm, source path) -> (row dict | None, why it was excluded | None)`. Exactly
#: one is set. A file the checker could not judge is EXCLUDED or a row with `inconclusive: true`, never a row that
#: says `inconclusive: false` (a never-measured file must not read as a conclusive failure).
ArmRunner = Callable[[Any, str, str], tuple["dict | None", "str | None"]]


def _report() -> Any:
    """The report module, loaded on first use (never at import: report -> collect -> this file)."""
    return _load("v4_report", HERE / "report.py")


def plan_provenance_problems(args: argparse.Namespace, plan: dict) -> list[str]:
    """Why a plan's provenance is not THIS shard's wave, empty if it is. Compares only what the shard was told on its
    own command line, so a launcher that claimed the slot for one wave cannot stamp it on another's shards."""
    problems: list[str] = []
    wave_id, n_shards = getattr(args, "wave_id", None), getattr(args, "n_shards", None)
    for label, mine, theirs in (("wave_id", wave_id, plan.get("wave_id")),
                                ("wave_type", getattr(args, "wave_type", None), plan.get("wave_type")),
                                ("n_shards", n_shards, plan.get("n_shards")),
                                ("wave", getattr(args, "wave", None), plan.get("wave")),
                                ("split", getattr(args, "split", None), plan.get("split"))):
        if mine is not None and mine != theirs:
            problems.append(f"{label}: this shard was started with {mine!r}, the plan says {theirs!r}")
    if getattr(args, "shard", None) and plan.get("n_shards") is not None:
        if evalspec.parse_shard(args.shard)[1] != plan["n_shards"]:
            problems.append(f"shard {args.shard} is not one of the plan's {plan['n_shards']} shards")
    ran = [a for a in getattr(args, "arms_resolved", ()) if a not in (plan.get("arms") or ())]
    if ran:
        problems.append(f"arms: this shard was started with {ran}, which are not arms of the plan {plan.get('arms')}")
    try:
        expected = evalspec.wave_cap_usd(str(plan.get("wave_type")))
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if plan.get("wave_cap_usd") != expected:
            problems.append(f"wave_cap_usd: the plan says {plan.get('wave_cap_usd')!r}, the ledger's table says "
                            f"{expected!r} for {plan.get('wave_type')!r}")
    if plan.get("slot") not in (evalspec.slot_keys(str(plan.get("wave_type"))) or [None]):
        problems.append(f"slot {plan.get('slot')!r} is not one of the {plan.get('wave_type')!r} wave type's slots")
    return problems


def load_plan_provenance(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        plan = json.loads(raw)
    except ValueError as exc:
        raise PlanProvenanceMismatch(f"--plan-provenance is not JSON: {exc}") from exc
    need = {"wave", "wave_id", "wave_type", "split", "arms", "n_shards", "wave_cap_usd", "slot"}
    if not isinstance(plan, dict) or need - set(plan):
        raise PlanProvenanceMismatch(f"--plan-provenance must be plan_wave(...).provenance(); it lacks "
                                     f"{sorted(need - set(plan if isinstance(plan, dict) else {}))}")
    return plan


def write_arm_rows(out: Path, rows: list[dict], excluded: list[dict]) -> list[Any]:
    """Write rows.jsonl / rows.json / EXCLUDED.json, after the report's own row contract has accepted EVERY row.
    A row the report would refuse (a missing verdict, `inconclusive: false` beside a search that measured nothing,
    a stored clean the parts do not support) fails HERE, on the shard, where the fault is, not at report time on the
    collected wave. Returns the parsed rows."""
    parsed = _report().parse_arm_rows(rows)
    (out / "rows.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    (out / "rows.json").write_text(json.dumps(rows, sort_keys=True))
    (out / "EXCLUDED.json").write_text(json.dumps(excluded, indent=1))
    return parsed


def adopt_plan(args: argparse.Namespace, plan: dict) -> None:
    """`--mode harness` with a plan: the wave is the PLAN's (its dispatchable arms, split and wave), never inferred from
    flags. The arms the plan lists that this runner cannot dispatch (v3_meshability_pass_ai) are left to v3_eval's own
    run_arm; the report needs a row for them from there (see the merge step in the task-32 report)."""
    args.arms_resolved = tuple(a for a in plan["arms"] if a in DISPATCHABLE_ARMS)
    if not args.arms_resolved:
        raise PlanProvenanceMismatch(f"the plan's arms {plan['arms']} include none this runner can dispatch")
    args.split = args.split or plan["split"]
    args.wave = args.wave or plan["wave"]


def run_harness(args: argparse.Namespace, out: Path, prov: dict, ledger: Any | None = None, *,
                plan: dict | None = None, runner: ArmRunner | None = None) -> int:
    """`--mode harness`. With NO plan: repair_corpus's own run (its rows are not ArmRows and no report reads them).
    With a plan: the SAME dispatch as `--mode arms` (`run_arms`), after `adopt_plan` has taken the arms and split from
    the plan; there is no second implementation of the arms. `runner` replaces `arms.run_file` per file."""
    if plan is None:
        return run_repair_corpus(args, out, prov)
    return run_arms(args, out, prov, ledger, plan=plan, runner=runner)


class MarkerBackendMismatch(RuntimeError):
    """The shard's marker store is not the backend the probe proved: it refuses rather than spend."""


def make_marker_store(bucket: str) -> tuple[Any, str]:
    """(marker store, backend id) for the wave bucket, over the native GCS client and this VM's service-account
    ADC. The backend id is `gcs/<bucket>`, the id the launcher's probe record is keyed by. Tests replace this;
    nothing else in this file reaches a network."""
    return marker_probe.open_markers(f"gcs:{bucket}")


def check_marker_binding(markers: Any, backend: str, expected_bucket: str) -> str | None:
    """None only if this shard's marker store is the bucket the launcher named AND that bucket carries a fresh,
    passing probe record. The shard reads the record through its own store, so a store pointing anywhere else
    finds no record for its backend and refuses (a different backend is as good as unproven)."""
    expected = f"gcs/{expected_bucket}"
    # NOT a binding: `backend` comes from `make_marker_store(expected_bucket)`, so comparing it with a value
    # built from the same bucket can never fail. The binding is `probe_problem`, which refuses unless the probe
    # record read through this store names exactly `expected`.
    if backend != expected:  # a consistency check only (see above); it cannot fail on the real path
        return f"this shard's marker store is {backend!r}, not the wave bucket {expected!r}"
    return marker_probe.probe_problem(markers, expected_backend=expected)


def build_ledger(args: argparse.Namespace, markers: Any) -> Any:
    """This shard's ledger: the cap is WAVE_CAPS_USD[wave type] / N, the index is this shard's `I-1` (unique per
    wave because the plan's units are), the log is the durable path the launcher mounted. Raises LostShardLog /
    ShardArithmeticError when the shard cannot be started at a known spend."""
    from meshpipeline.cad.repair_v4.model_ledger import WaveLedger

    i, n = evalspec.parse_shard(args.shard)
    return WaveLedger.for_shard(args.wave_type, wave_id=args.wave_id, n_shards=n, shard_index=i - 1,
                                path=args.ledger_path, markers=markers)


def _h_mm(text: str) -> float:
    """`--h-mm`: a size in mm, or `auto` (derive it per part; the sentinel is +inf, see arms.H_MM_AUTO)."""
    if text.strip().lower() == "auto":
        return arms_mod.H_MM_AUTO
    return float(text)


def effective_arm(spec: Any, args: argparse.Namespace) -> Any:
    """`spec` with the command line's op budget, search wall and per-file model ceiling applied. The wall and the
    ceiling can only LOWER what the
    arm declares (a ceiling flag that could raise the money would be a way round the owner's limit); a model arm
    keeps a positive ceiling, and a non-model arm never gains one."""
    import dataclasses

    budget = spec.budget
    ops = budget.ops if getattr(args, "ops", None) is None else args.ops
    wall = budget.wall_s
    sw = getattr(args, "search_wall_s", None)
    if sw is not None:
        wall = min(wall, sw)  # lower only: the fast loop stops a search inside its short file wall
    usd = budget.model_usd
    cap = getattr(args, "file_usd_cap", None)
    if cap is not None and spec.uses_model:
        usd = min(usd, cap)
    if (ops, usd, wall) == (budget.ops, budget.model_usd, budget.wall_s):
        return spec
    return dataclasses.replace(spec, budget=evalspec.Budget(wall_s=wall, ops=ops, model_usd=usd))


def build_parser() -> argparse.ArgumentParser:
    """The one argument contract. Tests build their `args` from it, never from a hand-written Namespace,
    so a flag added here cannot leave a test calling run_reprobe with an attribute missing."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("reprobe", "harness", "arms"), default=None,
                        help="default: arms when --arm/--arms/--wave is given, else reprobe")
    parser.add_argument("--arm", default=None, help="one arm to run (see --arms)")
    parser.add_argument("--arms", default=None,
                        help="comma list of arms to run: " + ", ".join(DISPATCHABLE_ARMS))
    parser.add_argument("--wave", default=None, help="a name from evalspec.WAVES; its arms/split/type must agree "
                        "with the other flags")
    parser.add_argument("--split", default=None, choices=evalspec.SPLITS,
                        help="the split the arms run over (default: the wave's)")
    parser.add_argument("--split-file", default=None, help="the split's json (default: the corpus's <split>.json)")
    parser.add_argument("--h-mm", type=_h_mm, default=None,
                        help="target element size for site finding; NO default (required by the search arms). "
                             "`auto` derives it per part (search.h_mm_for: a tenth of the diagonal, the production "
                             "base size): one fixed size is wrong on a panel spanning 10 mm to 10 m")
    parser.add_argument("--search-wall-s", type=float, default=None,
                        help="LOWER every arm's search wall (Budget.wall_s) to this many seconds, never raises it: a "
                             "search must stop itself inside a short --file-timeout-s, or the file is killed unmeasured")
    parser.add_argument("--ops", type=int, default=None,
                        help="budget of APPLIED ops per file for every arm; a run_stage candidate costs every op "
                             "its stage applied (default: the arm's own, evalspec.Budget.ops)")
    parser.add_argument("--model-usd-cap", type=float, default=None,
                        help="LOWER this shard's ledger cap to this many dollars (never raises it)")
    parser.add_argument("--file-usd-cap", type=float, default=None,
                        help="LOWER the model arm's per-file ceiling to this many dollars (never raises it)")
    parser.add_argument("--route", default=None, help="pin the AI proposer's provider:model (default: the first "
                        "of REPAIR_V4_PROPOSE_ROUTES that passes the probe)")
    parser.add_argument("--max-turns", type=int, default=12, help="the AI proposer's model rounds per file")
    parser.add_argument("--shard", default="1/1")
    parser.add_argument("--shard-plan", default=None,
                        help="a cost-balanced shard plan (evalspec.build_shard_plan); default: the hash partition")
    parser.add_argument("--inputs", default="/eval/inputs")
    parser.add_argument("--out", default="/eval/out")
    parser.add_argument("--pool", default=str(pool_mod.OUT))
    parser.add_argument("--sources", default="",
                        help="comma list of pool source labels to run (default: the cleared ones); "
                             "uncleared sources are refused")
    parser.add_argument("--cache-uri", default="", help="gs://... or a directory: the verdict cache")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--file-timeout-s", type=float, default=evalspec.DEFAULT_FILE_TIMEOUT_S,
                        help="wall-clock kill per file; must be positive (0 is refused)")
    parser.add_argument("--commit", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--machine-type", default="")
    parser.add_argument("--wave-type", default=None, choices=sorted(wave_limits.WAVE_CAPS_USD),
                        help="the wave's type: the model-spend cap comes from the ledger's table")
    parser.add_argument("--wave-id", default=None)
    parser.add_argument("--plan-provenance", default=None,
                        help="plan_wave(...).provenance() as JSON, from the launcher that claimed the wave's slot; "
                             "copied into PROVENANCE.json (a held-out report refuses a shard without it)")
    parser.add_argument("--purpose", default="report", choices=("report", "tuning"),
                        help="what this run is FOR; `tuning` is refused for the held-out split")
    parser.add_argument("--n-shards", type=int, default=None, help="the wave's N; must equal the N in --shard")
    parser.add_argument("--ledger-path", default=None, help="the shard's durable ledger log")
    parser.add_argument("--allow-in-flight-floor", action="store_true",
                        help="allow a wave with more shards than in-flight model calls: each shard's slice is floored "
                             "to one call, so the wave may exceed its in-flight ceiling (recorded in provenance); "
                             "without it such a wave is refused")
    parser.add_argument("--model-max-in-flight", type=int, default=None,
                        help="the WAVE's ceiling on in-flight model calls, split 1/N per shard (default: derived from "
                             "the route's measured rate limit, model_throttle.wave_max_in_flight)")
    parser.add_argument("--marker-bucket", default=None,
                        help="the wave bucket holding the ledger's markers (native GCS, VM service account)")
    # derived by parse_args (_resolve_arms); declared so the one argument contract names every attribute read
    parser.set_defaults(arms_resolved=())
    return parser


def _resolve_arms(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Settle --mode / --arm(s) / --wave / --split into `args.arms_resolved`, refusing a contradiction."""
    args.arms_resolved = ()
    selected = args.arm is not None or args.arms is not None or args.wave is not None
    if args.mode is None:
        args.mode = "arms" if selected else "reprobe"
    if args.mode != "arms":
        if selected:
            parser.error(f"--arm/--arms/--wave select arms and need --mode arms, not --mode {args.mode}")
        return
    if args.arm is not None and args.arms is not None:
        parser.error("give --arm or --arms, not both")
    wave = None
    if args.wave is not None:
        try:
            wave = evalspec.wave(args.wave)
        except ValueError as exc:
            parser.error(str(exc))
    raw = args.arm if args.arm is not None else args.arms
    try:
        names = [evalspec.arm(a.strip()).name for a in raw.split(",") if a.strip()] if raw is not None else \
            ([a for a in wave.arms if a in DISPATCHABLE_ARMS] if wave is not None else [])
    except ValueError as exc:
        parser.error(str(exc))
    if not names or len(set(names)) != len(names):
        parser.error("--mode arms needs at least one arm (--arm, --arms or --wave), each named once")
    not_here = [a for a in names if a not in DISPATCHABLE_ARMS]
    if not_here:
        parser.error(f"{not_here} cannot run here: the dispatchable arms are {list(DISPATCHABLE_ARMS)} "
                     "(v3_meshability_pass_ai runs in v3_eval)")
    if wave is not None:
        extra = [a for a in names if a not in wave.arms]
        if extra:
            parser.error(f"--arms {extra} are not arms of wave {wave.name!r} {list(wave.arms)}")
        if args.split is None:
            args.split = wave.split
        elif args.split != wave.split:
            parser.error(f"--split {args.split} disagrees with wave {wave.name!r}, which runs {wave.split!r}")
        if args.wave_type is not None and args.wave_type != wave.wave_type:
            parser.error(f"--wave-type {args.wave_type} disagrees with wave {wave.name!r} ({wave.wave_type})")
    if args.split is None:
        parser.error("--mode arms needs --split (or --wave, which names one)")
    if (args.split == evalspec.HELDOUT) != (args.wave_type == evalspec.HELDOUT_WAVE_TYPE):
        parser.error(f"the held-out split runs in the {evalspec.HELDOUT_WAVE_TYPE} wave type and nothing else does "
                     f"(split {args.split!r}, wave type {args.wave_type!r})")
    if any(a in SEARCH_ARMS for a in names) and (args.h_mm is None or not args.h_mm > 0):
        parser.error("the search arms need --h-mm > 0: there is no default element size")
    if any(evalspec.ARMS[a].uses_model for a in names) and args.wave_type is None:
        parser.error("a model arm needs the shard's ledger (--wave-type --wave-id --n-shards --ledger-path "
                     "--marker-bucket): a cap nothing consults is not a cap")
    if args.max_turns < 1:
        parser.error("--max-turns must be at least 1")
    if args.ops is not None and args.ops < 1:
        parser.error("--ops must be at least 1")
    for flag, value in (("--model-usd-cap", args.model_usd_cap), ("--file-usd-cap", args.file_usd_cap),
                        ("--search-wall-s", args.search_wall_s)):
        if value is not None and not (math.isfinite(value) and value > 0):
            parser.error(f"{flag} must be a finite positive number")
    args.arms_resolved = tuple(names)


def _model_refusals() -> tuple[type[BaseException], ...]:
    from meshpipeline.cad.repair_v4.model_route import NoModelKey
    from meshpipeline.cad.repair_v4.proposer import NoUsableRoute

    return (NoModelKey, NoUsableRoute, arms_mod.ModelArmWithoutLedger)


def _checker_call_wall() -> float:
    """The checker's one-call wall as THIS shard's code defines it (`repair_v4.limits.CHECKER_CALL_TIMEOUT_S`);
    it is not a flag, so provenance is the only record of what a shard ran with."""
    from meshpipeline.cad.repair_v4.limits import CHECKER_CALL_TIMEOUT_S

    return float(CHECKER_CALL_TIMEOUT_S)


def _budget_scale_rule() -> dict:
    """CRS v5 step 4: the size-scaling rule THIS shard ran with (`repair_v4.limits.budget_scale`) and its panel
    threads. Each row carries its own `budget_scale`; this says what rule produced it."""
    from meshpipeline.cad.repair_v4 import limits
    from meshpipeline.cad.repair_v4.verdict.assemble import MEMBER_TIMEOUT_S, full_panel_threads

    return {"floor_faces": limits.BUDGET_SCALE_FLOOR_FACES, "ceil_faces": limits.BUDGET_SCALE_CEIL_FACES,
            "max": limits.BUDGET_SCALE_MAX, "base_member_timeout_s": MEMBER_TIMEOUT_S,
            "panel_threads": full_panel_threads()}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.file_timeout_s <= 0:
        parser.error("--file-timeout-s must be positive: a shard without the wall kill can hang")
    i, n = evalspec.parse_shard(args.shard)
    _resolve_arms(parser, args)
    wave = {"--wave-type": args.wave_type, "--wave-id": args.wave_id, "--n-shards": args.n_shards,
            "--ledger-path": args.ledger_path, "--marker-bucket": args.marker_bucket}
    if any(v is not None for v in wave.values()):
        missing = [k for k, v in wave.items() if v is None]
        if missing:
            parser.error(f"a ledgered shard needs all of --wave-type --wave-id --n-shards --ledger-path --marker-bucket "
                         f"(missing {', '.join(missing)})")
        if args.n_shards != n:
            parser.error(f"--n-shards {args.n_shards} disagrees with --shard {args.shard}: every shard of a "
                         "wave must share the launcher's N")
    return args


def _events() -> Any:
    from meshpipeline.cad.repair_v5 import events

    return events


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prov: dict = {"label": f"{args.mode}/{args.shard}", "mode": args.mode, "shard": args.shard,
                  "commit": args.commit, "image": args.image, "machine_type": args.machine_type,
                  "host": socket.gethostname(), "hardware_class": f"{platform.system().lower()}-{platform.machine().lower()}",
                  # the walls THIS shard ran with, so the excluded stratum can report the walls that produced it
                  # instead of whatever the constants say when it is collected (collect.wall_values)
                  "file_timeout_s": args.file_timeout_s,
                  "check_valid_timeout_s": float(_checker_call_wall()), "started_at": _now(),
                  # file_timeout_s and every arm's search wall are BASE values: a large part's are multiplied by
                  # its row's `budget_scale`, made by this rule
                  "budget_scale_rule": _budget_scale_rule()}
    t0, code = time.monotonic(), 3
    ledger = None
    _events().startup(mode=args.mode, shard_arg=args.shard, arms=getattr(args, "arms", None), out=str(out))
    try:
        if args.wave_type is not None:
            from meshpipeline.cad.repair_v4.model_ledger import LostShardLog, ShardArithmeticError

            try:
                markers, backend = make_marker_store(args.marker_bucket)
                prov["marker_backend"] = backend
                unbound = check_marker_binding(markers, backend, args.marker_bucket)
                if unbound:
                    raise MarkerBackendMismatch(unbound)
                ledger = build_ledger(args, markers)
                if args.model_usd_cap is not None:
                    ledger.cap_to(args.model_usd_cap)  # only ever lowers; the slice stays the ceiling
            except (LostShardLog, ShardArithmeticError, MarkerBackendMismatch):
                # the shard refuses rather than start at an unknown spend; the reason is in PROVENANCE
                prov["error"] = traceback.format_exc()[-4000:]
                code = 5
                return code
            # `ledger` is the string the report reads ("shard"); the detail is its own key. A dict here was refused
            # by every report ("ran without a shard ledger") whatever the shard did.
            prov["ledger"] = ledger.provenance
            prov["ledger_detail"] = {"wave_type": args.wave_type, "wave_id": args.wave_id,
                                     "n_shards": args.n_shards, "cap_usd": ledger.cap_usd, "path": args.ledger_path,
                                     "model_usd_cap_flag": args.model_usd_cap, "file_usd_cap_flag": args.file_usd_cap}
        plan = load_plan_provenance(args.plan_provenance)
        if plan is not None:
            if args.mode == "harness":
                adopt_plan(args, plan)  # a harness run runs the PLAN's wave; arms mode already has its own flags
            wrong = plan_provenance_problems(args, plan)
            if wrong:
                raise PlanProvenanceMismatch("; ".join(wrong))
            prov.update(plan)  # wave, wave_id, wave_type, split, arms, n_shards, wave_cap_usd, shard_cap_usd, slot
        if args.mode in ("arms", "reprobe"):
            # The WAVE boundary: a shard with no SHARED cache refuses (exit 7). NOT harness mode - I added
            # that on my own initiative resolving a conflict and it broke 12 tests: a harness shard legitimately
            # runs without a shared cache (single-file runs, injected runners). The library keeps its
            # in-memory fallback for unit tests, single-file runs and the healer-to-search reuse; a wave
            # must not reach it. make_cache also exports CRS_V4_VERDICT_CACHE_URI, which is what the
            # engine's judges read - without it the reprobe path had a cache and the judges silently did
            # not, and a blank uri gave reprobe no cache at all.
            problem = wave_spec_mod.shared_cache_problem(args.cache_uri)
            if problem:
                prov["error"] = f"--cache-uri: {problem}"
                return 7
            make_cache(args.cache_uri)  # before any judge exists
        if args.mode == "reprobe":
            code = run_reprobe(args, out, prov)
        elif args.mode == "harness":
            code = run_harness(args, out, prov, ledger, plan=plan, runner=ARM_RUNNER)
        else:
            code = run_arms(args, out, prov, ledger, plan=plan, runner=ARM_RUNNER)
        return code
    except _model_refusals() as exc:
        # the shard refuses rather than run a model arm without a key, a route or a ledger; the reason is in PROVENANCE
        prov["error"] = f"{type(exc).__name__}: {exc}"
        code = 6
        return code
    except Exception:  # noqa: BLE001 - the provenance must still be written
        prov["error"] = traceback.format_exc()[-4000:]
        code = 4
        return code
    finally:
        # lane B15: what the wave's ONE cache answered. Without it, a wave whose arms each re-meshed the same input
        # looks exactly like a wave whose files never repeated, and the next wave cannot be sized honestly.
        try:
            from meshpipeline.cad.repair_v4.verdict.cache import default_verdict_cache

            prov["verdict_cache"] = default_verdict_cache().stats()
        except Exception as exc:  # noqa: BLE001 - provenance is never worth failing a shard for
            prov["verdict_cache"] = {"error": f"{type(exc).__name__}: {exc}"}
        restore_cache_env()
        prov.update(ended_at=_now(), wall_seconds=round(time.monotonic() - t0, 1), exit_code=code)
        (out / "PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str) + "\n")
        # the shard's end with WHY it refused (a row-contract refusal is exit 4 with the traceback in prov["error"]):
        # the regroup/report step that later refuses the wave only sees the exit code
        _events().emit("shard_end", severity="INFO" if code == 0 else "ERROR", sync=True, exit_code=code,
                       error=prov.get("error"), rows=prov.get("rows"), excluded=prov.get("excluded"),
                       wall_s=prov.get("wall_seconds"))


if __name__ == "__main__":
    raise SystemExit(main())
