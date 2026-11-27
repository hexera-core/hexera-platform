#!/usr/bin/env python3
# Responsibility: MEASURE what one re-probe file costs on the fleet's shape, so the wave's projection is a
#                 measurement on the current code and not a carried-over number.
# Owns: a diagonal-stratified sample of the cleared pool, the per-file timed FULL verdict (the same
#       `reprobe.reprobe(tier="full")` call run_arm makes, one WorkerPool worker, a cold verdict cache), and the
#       per-file cgroup CPU accounting that tests the one-vCPU-per-file assumption.
# Boundaries: calls no model and no cloud. It changes no verdict logic. The cache is a fresh local directory,
#             so every file pays its full cost (a warm cache would under-measure).
"""Run inside the mesh image with PYTHONPATH=<checkout>/src, one container per shard, `--cpus=1` as the fleet runs:

    measure_reprobe.py sample --inputs DIR --out sample.json [--n 60] [--candidates 360]
    measure_reprobe.py probe  --sample sample.json --inputs DIR --shard I/N --out DIR
    measure_reprobe.py form   --sample sample.json --inputs DIR --shard I/N --out DIR   # engine_form cost only

`probe` writes one JSON line per file: wall seconds, the container's cgroup CPU seconds and THROTTLED seconds
over that file, and the outcome (a row's first failing layer, or the exclusion reason). Throttled time is the
direct test of "one vCPU per file": it is time the file wanted more CPU than the container's one vCPU.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_arm as _ra  # noqa: E402

reprobe_mod = _ra.reprobe_mod
pool_mod = _ra.pool_mod
evalspec = _ra.evalspec
SEED = 20261123
#: Diagonal bands, mm: the sample takes round-robin over them so the range is covered, not just the mode.
BANDS = [(0, 10), (10, 100), (100, 1e3), (1e3, 1e4), (1e4, math.inf)]
#: Per-source share of the sample, roughly the cleared pool's mix with a floor for the small sources.
QUOTA = {"abc_fail": 0.70, "abc_pass": 0.15, "nist": 0.075, "occt": 0.075}
_DIAG = """
import sys
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
r = STEPControl_Reader()
if r.ReadFile(sys.argv[1]) != IFSelect_RetDone:
    print("nan"); raise SystemExit
r.TransferRoots(); b = Bnd_Box(); BRepBndLib.Add_s(r.OneShape(), b)
if b.IsVoid():
    print(0.0); raise SystemExit
x0, y0, z0, x1, y1, z1 = b.Get(); print(((x1-x0)**2 + (y1-y0)**2 + (z1-z0)**2) ** 0.5)
"""


def band_of(diag: float) -> int:
    return next((i for i, (lo, hi) in enumerate(BANDS) if lo <= diag < hi), len(BANDS) - 1)


def diagonal_mm(path: Path, timeout_s: float = 120.0) -> float | None:
    """The bounding-box diagonal, in its own process so a pathological file cannot hang the sampler."""
    try:
        out = subprocess.run([sys.executable, "-c", _DIAG, str(path)], capture_output=True, text=True,
                             timeout=timeout_s)
        v = float(out.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return None
    return None if math.isnan(v) else v


def cmd_sample(args: argparse.Namespace) -> int:
    from concurrent.futures import ThreadPoolExecutor

    allowed, _ = reprobe_mod.split_allowed(pool_mod.read_pool(Path(args.pool)))
    rng = random.Random(SEED)
    by_source: dict[str, list] = defaultdict(list)
    for e in sorted(allowed, key=lambda e: e.sha256):
        by_source[e.source].append(e)
    want = {s: max(1, round(args.n * q)) for s, q in QUOTA.items() if by_source.get(s)}
    cands = []
    for s, n in want.items():  # candidates: a seeded draw, ~6x the quota, whose diagonals are then measured
        pop = by_source[s][:]
        rng.shuffle(pop)
        cands += pop[: max(n, round(args.candidates * QUOTA[s]))]
    paths = {e.sha256: _ra.input_path(Path(args.inputs), e) for e in cands}
    with ThreadPoolExecutor(args.jobs) as ex:
        diags = dict(zip(paths, ex.map(lambda h: diagonal_mm(Path(paths[h])), paths), strict=True))
    chosen = []
    for s, n in want.items():
        bands: dict[int, list] = defaultdict(list)
        for e in cands:
            if e.source == s and diags[e.sha256] is not None:
                bands[band_of(diags[e.sha256])].append(e)
        picked: list = []
        while len(picked) < n and any(bands.values()):
            for b in sorted(bands):
                if bands[b] and len(picked) < n:
                    picked.append(bands[b].pop())
        chosen += [{"name": e.name, "sha256": e.sha256, "source": e.source, "diag_mm": diags[e.sha256],
                    "size": Path(paths[e.sha256]).stat().st_size} for e in picked]
    unreadable = [h for h, d in diags.items() if d is None]
    Path(args.out).write_text(json.dumps({"files": chosen, "candidates": len(cands),
                                          "candidates_unreadable": len(unreadable)}, indent=1))
    print(f"sample {len(chosen)} {dict(Counter(c['source'] for c in chosen))} bands "
          f"{dict(sorted(Counter(band_of(c['diag_mm']) for c in chosen).items()))}; {len(unreadable)} of "
          f"{len(cands)} candidates had no readable diagonal")
    return 0


def _cgroup() -> dict[str, float]:
    """The container's own cgroup v2 counters: CPU used and throttled, seconds; peak memory, bytes."""
    stat = dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())
    peak = Path("/sys/fs/cgroup/memory.peak")
    return {"cpu_s": int(stat["usage_usec"]) / 1e6, "throttled_s": int(stat.get("throttled_usec", 0)) / 1e6,
            "mem_peak": float(peak.read_text()) if peak.exists() else float("nan")}


def cmd_probe(args: argparse.Namespace) -> int:
    from meshpipeline.cad.repair_v4.verdict.cache import DirStore, VerdictCache
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    sample = json.loads(Path(args.sample).read_text())["files"]
    i, n = evalspec.parse_shard(args.shard)
    by_sha = {e.sha256: e for e in pool_mod.read_pool(Path(args.pool))}
    mine = [s for k, s in enumerate(sorted(sample, key=lambda s: s["sha256"])) if k % n == i - 1]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = VerdictCache(DirStore(str(out / "cold-cache")))  # fresh per shard: every file pays full price
    pool = WorkerPool(1)
    try:
        with (out / "files.jsonl").open("w") as handle:
            for s in mine:
                e = by_sha[s["sha256"]]
                excluded: list = []
                c0, t0 = _cgroup(), time.monotonic()
                rows = reprobe_mod.reprobe([e], tier="full", pool=pool,
                                           resolve=lambda x: _ra.input_path(Path(args.inputs), x), cache=cache,
                                           file_timeout_s=args.file_timeout_s, excluded=excluded)
                wall, c1 = time.monotonic() - t0, _cgroup()
                rec = {**s, "wall_s": wall, "cpu_s": c1["cpu_s"] - c0["cpu_s"],
                       "throttled_s": c1["throttled_s"] - c0["throttled_s"], "mem_peak": c1["mem_peak"],
                       "layer": rows[0].first_failing_layer if rows else None,
                       "family": rows[0].family if rows else None,
                       "excluded": asdict(excluded[0])["reason"] if excluded else None}
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
                handle.flush()
                print(f"{s['source']} {Path(s['name']).name} {wall:.1f}s cpu {rec['cpu_s']:.1f}s "
                      f"throttled {rec['throttled_s']:.1f}s", flush=True)
    finally:
        pool.close()
    return 0


def cmd_form(args: argparse.Namespace) -> int:
    """What judging the engine's form adds per file: `search.engine_form` (XCAF read -> BREP -> STEP export), timed
    alone, no meshing. The re-probe pays this once per file on top of the verdict."""
    from meshpipeline.cad.repair_v4.search import engine_form
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    sample = json.loads(Path(args.sample).read_text())["files"]
    i, n = evalspec.parse_shard(args.shard)
    by_sha = {e.sha256: e for e in pool_mod.read_pool(Path(args.pool))}
    mine = [s for k, s in enumerate(sorted(sample, key=lambda s: s["sha256"])) if k % n == i - 1]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pool = WorkerPool(1)
    try:
        with (out / "form.jsonl").open("w") as handle:
            for s in mine:
                step = _ra.input_path(Path(args.inputs), by_sha[s["sha256"]])
                c0, t0 = _cgroup(), time.monotonic()
                error = None
                try:
                    engine_form(pool, step)
                except Exception as exc:  # noqa: BLE001 - recorded: an unreadable file is excluded by the re-probe
                    error = f"{type(exc).__name__}: {exc}"[:300]
                wall, c1 = time.monotonic() - t0, _cgroup()
                rec = {**s, "form_s": wall, "cpu_s": c1["cpu_s"] - c0["cpu_s"], "error": error}
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
                handle.flush()
    finally:
        pool.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--inputs", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--n", type=int, default=60)
    s.add_argument("--candidates", type=int, default=360)
    s.add_argument("--jobs", type=int, default=8)
    f = sub.add_parser("form")
    f.add_argument("--sample", required=True)
    f.add_argument("--inputs", required=True)
    f.add_argument("--shard", required=True)
    f.add_argument("--out", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--sample", required=True)
    p.add_argument("--inputs", required=True)
    p.add_argument("--shard", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--file-timeout-s", type=float, default=evalspec.DEFAULT_FILE_TIMEOUT_S)
    for x in (s, p, f):
        x.add_argument("--pool", default=str(pool_mod.OUT))
    args = ap.parse_args(argv)
    return {"sample": cmd_sample, "probe": cmd_probe, "form": cmd_form}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
