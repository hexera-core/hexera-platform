#!/usr/bin/env python3
# Responsibility: Run ONE shard of the v4 wave inside the image: the re-probe (`--mode reprobe`) or a
#                 repair_corpus harness run (`--mode harness`), always under the per-file wall kill, and
#                 say exactly what ran in PROVENANCE.json.
# Owns: shard selection from the pool manifest, input resolution, the pool/cache wiring, rows.jsonl,
#       EXCLUDED.json and PROVENANCE.json.
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

    whole_pool = pool_mod.read_pool(Path(args.pool))
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


def run_harness(args: argparse.Namespace, out: Path, prov: dict) -> int:
    rc = _load("repair_corpus", REPO / "devtools/validation/repair_corpus.py")
    argv = evalspec.harness_argv(inputs=args.inputs, out=str(out), shard=args.shard,
                                 file_timeout_s=args.file_timeout_s)
    prov["repair_corpus_argv"] = argv
    sys.argv = ["repair_corpus.py", *argv]
    return int(rc.main() or 0)


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    if args.file_timeout_s <= 0:
        parser.error("--file-timeout-s must be positive: a shard without the wall kill can hang")
    evalspec.parse_shard(args.shard)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prov: dict = {"label": f"{args.mode}/{args.shard}", "mode": args.mode, "shard": args.shard,
                  "commit": args.commit, "image": args.image, "machine_type": args.machine_type,
                  "host": socket.gethostname(), "hardware_class": f"{platform.system().lower()}-{platform.machine().lower()}",
                  "file_timeout_s": args.file_timeout_s, "started_at": _now()}
    t0, code = time.monotonic(), 3
    try:
        code = run_reprobe(args, out, prov) if args.mode == "reprobe" else run_harness(args, out, prov)
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
