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


#: The per-file arm dispatch `--mode harness` calls. None until Task 22c's `arms.run_file` is adapted onto it; a
#: harness run with none is refused (ArmDispatchMissing). Tests and the infrastructure check set it.
ARM_RUNNER: ArmRunner | None = None


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
    from meshpipeline.cad.repair_v4.verdict.cache import DirStore, GcsStore, VerdictCache

    return VerdictCache(GcsStore(uri) if uri.startswith("gs://") else DirStore(uri))


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
    cache = make_cache(args.cache_uri) if args.cache_uri else None
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


def run_repair_corpus(args: argparse.Namespace, out: Path, prov: dict) -> int:
    """`--mode harness` with NO plan: repair_corpus's own harness run (its rows are not ArmRows and no report reads
    them). A wave never takes this path: the launcher always hands its shards --plan-provenance."""
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    argv = evalspec.harness_argv(inputs=args.inputs, out=str(out), shard=args.shard,
                                 file_timeout_s=args.file_timeout_s)
    prov["repair_corpus_argv"] = argv
    sys.argv = ["repair_corpus.py", *argv]
    return int(rc.main() or 0)


class ArmDispatchMissing(RuntimeError):
    """`--mode harness` has no arm runner: the per-file arm dispatch (Task 22c's `arms.run_file`) is not wired in."""


class PlanProvenanceMismatch(RuntimeError):
    """The `--plan-provenance` the launcher handed this shard disagrees with the flags the shard was started with."""


#: One file through one arm: `(entry, arm, source path) -> (row dict | None, why it was excluded | None)`. Exactly
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


def run_harness(args: argparse.Namespace, out: Path, prov: dict, *, runner: ArmRunner | None = None,
                plan: dict | None = None) -> int:
    """`--mode harness`: every arm of the plan's wave on every file of this shard's slice of the plan's split, written
    as the `ArmRow`s the report reads. The wave is the PLAN's (its arms, split and slot), never inferred from flags.

    THE ARMS ARE NOT DISPATCHED HERE. `runner` is the per-file arm dispatch (Task 22c); with none, this refuses
    (ArmDispatchMissing) rather than write rows it did not measure. What is owned here is that a row is only written
    after the report's contract accepts it, that an unreadable or timed-out file is EXCLUDED (never a row), and that
    the shard's provenance carries the plan the launcher claimed."""
    if plan is None:
        return run_repair_corpus(args, out, prov)  # no wave: the plain repair_corpus run, which writes no ArmRows
    if runner is None:
        raise ArmDispatchMissing("--mode harness has no arm runner: pass one (Task 22c's arms.run_file adapter)")
    # held-out files are reported, never tuned on: a harness run is a REPORT purpose; a tuning purpose is refused
    # for the held-out split here, not left to the caller to remember
    if getattr(args, "purpose", "report") == "tuning":
        evalspec.refuse_heldout_for_tuning(plan["split"], *( [args.split_file] if getattr(args, "split_file", None) else []))
    arm_names = [a for a in plan["arms"] if a in getattr(evalspec, "DISPATCHABLE_ARMS", tuple(plan["arms"]))]
    path = Path(getattr(args, "split_file", None) or reprobe_mod.SPLITS_DIR / f"{plan['split']}.json")
    split = reprobe_mod.read_split(path)
    reprobe_mod.check_sources(split)  # Fusion 360 is in no split, row or report
    by_sha = {e.sha256: e for e in pool_mod.read_pool(Path(args.pool))}
    missing = [r.sha256 for r in split if r.sha256 not in by_sha]
    if missing:
        raise ValueError(f"split {plan['split']!r} names {len(missing)} file(s) the pool does not carry, "
                         f"e.g. {missing[0]}")
    whole, _ = reprobe_mod.split_allowed([by_sha[r.sha256] for r in split])
    entries = shard_entries(select_sources(whole, args.sources), args.shard)
    # `arms` / `split` are the PLAN's (copied above) and are not overwritten: the report compares them with the wave
    prov.update(shard_files=len(entries), arms_run=arm_names, sources=args.sources or "cleared")
    rows: list[dict] = []
    excluded: list[dict] = []
    for entry in entries:
        for name in arm_names:
            try:
                with reprobe_mod._WallClock(args.file_timeout_s):
                    row, why = runner(entry, name, input_path(Path(args.inputs), entry))
            except FileNotFoundError as exc:
                row, why = None, f"input_missing: {exc}"
            except reprobe_mod.FileTimeout as exc:
                row, why = None, f"file_timeout: {exc}"
            except Exception as exc:  # noqa: BLE001 - one file's failure is an excluded file, never a verdict
                row, why = None, f"error: {type(exc).__name__}: {str(exc)[-300:]}"
            if row is not None:
                rows.append(row)
            else:
                excluded.append({"name": entry.name, "sha256": entry.sha256, "source": entry.source, "arm": name,
                                 "reason": why or "no row and no reason"})
    write_arm_rows(out, rows, excluded)
    prov.update(rows=len(rows), excluded=len(excluded), unmeasured_rows=sum(1 for r in rows if r.get("inconclusive")),
                model_usd=round(sum(float(r.get("model_usd") or 0.0) for r in rows), 6))
    return 0


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
    parser.add_argument("--mode", choices=("reprobe", "harness"), default="reprobe")
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
    parser.add_argument("--plan-provenance", default=None,
                        help="plan_wave(...).provenance() as JSON, from the launcher that claimed the wave's slot; "
                             "copied into PROVENANCE.json (a held-out report refuses a shard without it)")
    parser.add_argument("--purpose", default="report", choices=("report", "tuning"),
                        help="what this run is FOR; `tuning` is refused for the held-out split")
    parser.add_argument("--n-shards", type=int, default=None, help="the wave's N; must equal the N in --shard")
    parser.add_argument("--ledger-path", default=None, help="the shard's durable ledger log")
    parser.add_argument("--marker-bucket", default=None,
                        help="the wave bucket holding the ledger's markers (native GCS, VM service account)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.file_timeout_s <= 0:
        parser.error("--file-timeout-s must be positive: a shard without the wall kill can hang")
    i, n = evalspec.parse_shard(args.shard)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prov: dict = {"label": f"{args.mode}/{args.shard}", "mode": args.mode, "shard": args.shard,
                  "commit": args.commit, "image": args.image, "machine_type": args.machine_type,
                  "host": socket.gethostname(), "hardware_class": f"{platform.system().lower()}-{platform.machine().lower()}",
                  "file_timeout_s": args.file_timeout_s, "started_at": _now()}
    t0, code = time.monotonic(), 3
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
            # `ledger` is the string the report reads ("shard"); the detail is its own key. A dict here was refused
            # by every report ("ran without a shard ledger") whatever the shard did.
            prov["ledger"] = ledger.provenance
            prov["ledger_detail"] = {"wave_type": args.wave_type, "wave_id": args.wave_id,
                                     "n_shards": args.n_shards, "cap_usd": ledger.cap_usd, "path": args.ledger_path}
        plan = load_plan_provenance(args.plan_provenance)
        if plan is not None:
            wrong = plan_provenance_problems(args, plan)
            if wrong:
                raise PlanProvenanceMismatch("; ".join(wrong))
            prov.update(plan)  # wave, wave_id, wave_type, split, arms, n_shards, wave_cap_usd, shard_cap_usd, slot
        code = (run_reprobe(args, out, prov) if args.mode == "reprobe"
                else run_harness(args, out, prov, plan=plan, runner=ARM_RUNNER))
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
