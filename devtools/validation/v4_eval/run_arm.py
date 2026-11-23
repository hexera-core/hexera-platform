#!/usr/bin/env python3
# Responsibility: Run ONE shard of the v4 wave inside the image: the re-probe (`--mode reprobe`) or a
#                 repair_corpus harness run (`--mode harness`), always under the per-file wall kill, and
#                 say exactly what ran in PROVENANCE.json.
# Owns: shard selection from the pool manifest, input resolution, the pool/cache wiring, rows.jsonl,
#       EXCLUDED.json and PROVENANCE.json.
#       The shard's model-spend LEDGER (`WaveLedger.for_shard`) when the launcher names a wave: wave type, wave id,
#       N, a durable log path and the wave bucket's marker store.
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
import platform
import socket
import sys
import time
import traceback
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

#: The arms this runner can dispatch. v3_meshability_pass_ai is run by v3_eval's own run_arm (it is v3 as it ships).
DISPATCHABLE_ARMS = evalspec.DISPATCHABLE_ARMS
SEARCH_ARMS = ("search_det", "search_ai")


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def shard_entries(entries: list[Any], shard: str) -> list[Any]:
    i, n = evalspec.parse_shard(shard)
    return [e for e in entries if evalspec.shard_of(e.sha256, i, n)]


def select_sources(entries: list[Any], sources: str) -> list[Any]:
    """The pool entries whose source the wave may run (a comma list of pool source labels). An EMPTY
    list means the CLEARED sources, never the whole pool: a missing flag or metadata key must not open
    the pool to an uncleared dataset (Fusion 360). A label that is not cleared is refused here as well
    as at launch."""
    wanted = set(evalspec.resolve_sources(sources.split(",") if sources else
                                          list(evalspec.CLEARED_SOURCES)))
    return [e for e in entries if e.source in wanted]


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

    os.environ[CACHE_URI_ENV] = uri
    return default_verdict_cache()


def run_reprobe(args: argparse.Namespace, out: Path, prov: dict) -> int:
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    listed = pool_mod.read_pool(Path(args.pool))
    whole_pool, dropped = reprobe_mod.split_allowed(listed)  # licence hold: F360 never reaches a shard
    prov["pool_listed"], prov["pool_dropped_by_source"] = len(listed), dropped
    print(f"pool: {len(listed)} listed, {len(whole_pool)} allowed, dropped {dropped}", file=sys.stderr)
    reprobe_mod.check_not_quarantined(whole_pool)  # pool-level: any source, before any shard work
    entries = shard_entries(select_sources(whole_pool, args.sources), args.shard)
    prov["shard_files"] = len(entries)
    prov["sources"] = args.sources or "cleared"
    cache = make_cache(args.cache_uri)
    prov["cache_uri"] = args.cache_uri or None
    pool = WorkerPool(args.workers)
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
    shas = [r.sha256 for r in reprobe_mod.read_split(path)]
    by_sha = {e.sha256: e for e in whole_pool}
    missing = [h for h in shas if h not in by_sha]
    if missing:
        raise ValueError(f"split {split!r} ({path}) names {len(missing)} file(s) the pool does not carry, "
                         f"e.g. {missing[0]}")
    return [by_sha[h] for h in shas]


def run_arms(args: argparse.Namespace, out: Path, prov: dict, ledger: Any | None = None, *,
             engine: Any = None) -> int:
    """Every arm in `args.arms` on every file of this shard's slice of the split.

    `ledger` is the shard's WaveLedger and is handed to every model arm; a model arm with no ledger is refused here
    (before a pool is started) rather than built without a cap. Nothing in this function prices or caps by itself:
    the cap is the ledger's `admit`, which the proposer calls before every model request."""
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    names = list(args.arms_resolved)
    model_arms = [a for a in names if evalspec.ARMS[a].uses_model]
    if model_arms and ledger is None:
        raise arms_mod.ModelArmWithoutLedger(
            f"{model_arms} make model calls and this shard has no WaveLedger (pass --wave-type ... --marker-bucket)")
    route = None
    if model_arms:
        route = _prepare_model_route(args, prov)
    engine = engine if engine is not None else arms_mod.RealEngine()
    listed = pool_mod.read_pool(Path(args.pool))
    whole_pool, dropped = reprobe_mod.split_allowed(listed)  # licence hold: F360 never reaches a shard
    prov["pool_listed"], prov["pool_dropped_by_source"] = len(listed), dropped
    reprobe_mod.check_not_quarantined(whole_pool)
    in_split = {e.sha256 for e in split_entries(whole_pool, args.split, args.split_file)}
    entries = shard_entries([e for e in select_sources(whole_pool, args.sources) if e.sha256 in in_split],
                            args.shard)
    prov.update(shard_files=len(entries), arms=names, split=args.split, wave=args.wave, route=route,
                h_mm=args.h_mm, max_turns=args.max_turns, sources=args.sources or "cleared",
                ledger_attached=ledger is not None)
    rows: list[dict] = []
    excluded: list[dict] = []
    pool = WorkerPool(args.workers)
    try:
        with (out / "rows.jsonl").open("w", encoding="utf-8") as handle:
            for entry in entries:
                for name in names:
                    spec = evalspec.ARMS[name]
                    try:
                        source = input_path(Path(args.inputs), entry)
                        with reprobe_mod._WallClock(args.file_timeout_s):
                            outcome = arms_mod.run_file(
                                name, entry, source, pool=pool, engine=engine, ledger=ledger, arm=spec,
                                budget=spec.budget, h_mm=args.h_mm, route=route, max_turns=args.max_turns,
                                job_id=f"{args.wave_id or 'wave'}:{entry.sha256[:12]}:{name}",
                                workdir=str(out / "work" / entry.sha256[:12] / name), workers=args.workers)
                    except FileNotFoundError as exc:
                        outcome = arms_mod.ArmOutcome(excluded=f"input_missing: {exc}")
                    except reprobe_mod.FileTimeout as exc:
                        outcome = arms_mod.ArmOutcome(excluded=f"file_timeout: {exc}")
                    except arms_mod.ModelArmWithoutLedger:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one file's failure is an excluded file, never a verdict
                        outcome = arms_mod.ArmOutcome(excluded=arms_mod.failure_reason(exc))
                    if outcome.row is not None:
                        rows.append(outcome.row)
                        handle.write(json.dumps(outcome.row, sort_keys=True) + "\n")
                        handle.flush()
                    else:
                        excluded.append({"name": entry.name, "sha256": entry.sha256, "source": entry.source,
                                         "arm": name, "reason": outcome.excluded})
    finally:
        pool.close()
    (out / "rows.json").write_text(json.dumps(rows, sort_keys=True))
    (out / "EXCLUDED.json").write_text(json.dumps(excluded, indent=1))
    prov.update(rows=len(rows), excluded=len(excluded),
                unmeasured_rows=sum(1 for r in rows if r["inconclusive"]),
                model_usd=round(sum(r["model_usd"] for r in rows), 6))
    return 0


def _prepare_model_route(args: argparse.Namespace, prov: dict) -> str:
    """Before any file: this process must hold a model key, the real router must be installed under the proposer,
    and a route must pass the tool-and-image probe. Each failure refuses the shard (NoModelKey / NoUsableRoute)
    instead of letting a model arm run as a deterministic one."""
    from meshpipeline.cad.repair_v4 import model_route
    from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_ROUTES

    model_route.require_model_key()
    model_route.install_router()
    routes = (args.route,) if args.route else REPAIR_V4_PROPOSE_ROUTES
    route = model_route.probe_route_chain(routes)
    prov["route_probe"] = {"candidates": list(routes), "chosen": route}
    return route


def run_harness(args: argparse.Namespace, out: Path, prov: dict) -> int:
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    argv = evalspec.harness_argv(inputs=args.inputs, out=str(out), shard=args.shard,
                                 file_timeout_s=args.file_timeout_s)
    prov["repair_corpus_argv"] = argv
    sys.argv = ["repair_corpus.py", *argv]
    return int(rc.main() or 0)


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
    parser.add_argument("--h-mm", type=float, default=None,
                        help="target element size for site finding; NO default (required by the search arms)")
    parser.add_argument("--route", default=None, help="pin the AI proposer's provider:model (default: the first "
                        "of REPAIR_V4_PROPOSE_ROUTES that passes the probe)")
    parser.add_argument("--max-turns", type=int, default=12, help="the AI proposer's model rounds per file")
    parser.add_argument("--shard", default="1/1")
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
    parser.add_argument("--n-shards", type=int, default=None, help="the wave's N; must equal the N in --shard")
    parser.add_argument("--ledger-path", default=None, help="the shard's durable ledger log")
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
    args.arms_resolved = tuple(names)


def _model_refusals() -> tuple[type[BaseException], ...]:
    from meshpipeline.cad.repair_v4.model_route import NoModelKey
    from meshpipeline.cad.repair_v4.proposer import NoUsableRoute

    return (NoModelKey, NoUsableRoute, arms_mod.ModelArmWithoutLedger)


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
    if args.mode in ("arms", "reprobe") and wave_spec_mod.shared_cache_problem(args.cache_uri):
        parser.error(f"--cache-uri: {wave_spec_mod.shared_cache_problem(args.cache_uri)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prov: dict = {"label": f"{args.mode}/{args.shard}", "mode": args.mode, "shard": args.shard,
                  "commit": args.commit, "image": args.image, "machine_type": args.machine_type,
                  "host": socket.gethostname(), "hardware_class": f"{platform.system().lower()}-{platform.machine().lower()}",
                  "file_timeout_s": args.file_timeout_s, "started_at": _now()}
    t0, code = time.monotonic(), 3
    ledger = None
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
            except (LostShardLog, ShardArithmeticError, MarkerBackendMismatch):
                # the shard refuses rather than start at an unknown spend; the reason is in PROVENANCE
                prov["error"] = traceback.format_exc()[-4000:]
                code = 5
                return code
            prov["ledger"] = {"wave_type": args.wave_type, "wave_id": args.wave_id, "n_shards": args.n_shards,
                              "cap_usd": ledger.cap_usd, "path": args.ledger_path,
                              "provenance": ledger.provenance}
        if args.mode in ("arms", "reprobe"):
            make_cache(args.cache_uri)  # before any judge exists; parse_args refused a blank one
        if args.mode == "arms":
            code = run_arms(args, out, prov, ledger)
        else:  # reprobe and harness make no metered model call, so there is nothing for a ledger to gate
            code = run_reprobe(args, out, prov) if args.mode == "reprobe" else run_harness(args, out, prov)
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
        prov.update(ended_at=_now(), wall_seconds=round(time.monotonic() - t0, 1), exit_code=code)
        (out / "PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
