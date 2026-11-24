#!/usr/bin/env python3
# Responsibility: a LOCAL, small, free re-probe of a sample of the cleared pool: it produces real ProbeRows,
#                 per-file wall-clock, verdict-cache statistics and a PROVISIONAL dev/heldout/clean split.
# Owns: candidate discovery over local copies of the corpus, a diagonal-stratified sample, the per-file probe
#       loop (cheap then full tier, timed), the cache/mesher-invocation counters, and the provisional split.
# Boundaries: it calls no model and no cloud. It changes no verdict logic: it drives reprobe.reprobe and
#             reprobe.build_splits exactly as run_arm does. The splits it writes are NOT the benchmark's:
#             the real splits come from the full re-probe (Task: the wave), never from this sample.
"""Run inside the native image (OCP, gmsh, netgen), with PYTHONPATH=/w/src:

    local_reprobe_sample.py candidates --root DIR [--root DIR ...] --out candidates.json
    local_reprobe_sample.py sample     --candidates candidates.json --out sample.json
    local_reprobe_sample.py probe      --sample sample.json --out DIR --shard I/N [--cache DIR] [--pass2]
    local_reprobe_sample.py splits     --probe-dir DIR [--probe-dir DIR ...] --out DIR [--dev N --heldout N --clean N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
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
POOL = HERE.parent / "corpora/v4/pool.json"
SOURCES = ("abc_fail", "abc_pass", "nist", "occt")  # the cleared ones; f360 is never read
BANDS = [(0, 10), (10, 100), (100, 1e3), (1e3, 1e4), (1e4, math.inf)]
TARGETS = {"abc_fail": 50, "abc_pass": 20, "nist": 8, "occt": 8}
SIZE_CAP = 20_000_000  # bytes: candidates larger than this are not sampled (emulated amd64 would take hours)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def band_of(diag: float) -> int:
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= diag < hi:
            return i
    return len(BANDS) - 1


# --------------------------------------------------------------------------- candidates

def diagonal_mm(path: str) -> float | None:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        return None
    reader.TransferRoots()
    shape = reader.OneShape()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 0.0
    x0, y0, z0, x1, y1, z1 = box.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def cmd_candidates(args: argparse.Namespace) -> int:
    entries = [e for e in pool_mod.read_pool(POOL) if e.source in SOURCES]
    index: dict[str, list[str]] = defaultdict(list)
    for root in args.root:
        for p in Path(root).rglob("*"):
            if p.is_file():
                index[p.name].append(str(p))
    rng = random.Random(args.seed)
    by_source: dict[str, list] = defaultdict(list)
    for e in entries:
        hits = index.get(Path(e.name).name, [])
        if hits:
            by_source[e.source].append((e, hits))
    out = []
    for source, items in sorted(by_source.items()):
        rng.shuffle(items)
        cap = args.per_source
        taken = 0
        for e, hits in items:
            if taken >= cap:
                break
            path = next((h for h in hits if Path(h).stat().st_size <= SIZE_CAP), None)
            if path is None:
                continue
            if sha256_of(path) != e.sha256:  # a local file that is not the pool's file is not this entry
                continue
            diag = diagonal_mm(path)
            taken += 1
            out.append({"name": e.name, "sha256": e.sha256, "source": e.source, "group": e.group, "path": path,
                        "size": Path(path).stat().st_size, "diag_mm": diag})
            print(f"{source} {taken}/{cap} {Path(path).name} diag={diag}", flush=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(f"candidates: {len(out)} {dict(Counter(c['source'] for c in out))}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    cands = json.loads(Path(args.candidates).read_text())
    rng = random.Random(args.seed)
    chosen: list[dict] = []
    for source, want in TARGETS.items():
        pop = [c for c in cands if c["source"] == source and c["diag_mm"] is not None]
        rng.shuffle(pop)
        bands: dict[int, list] = defaultdict(list)
        for c in pop:
            bands[band_of(c["diag_mm"])].append(c)
        picked: list[dict] = []
        # round robin over the diagonal bands: the range is covered, the 10-100 mm band is not the whole sample
        while len(picked) < want and any(bands.values()):
            for b in sorted(bands):
                if bands[b] and len(picked) < want:
                    picked.append(bands[b].pop())
        chosen += picked
    Path(args.out).write_text(json.dumps(chosen, indent=1))
    print(f"sample: {len(chosen)} {dict(Counter(c['source'] for c in chosen))} "
          f"bands={dict(sorted(Counter(band_of(c['diag_mm']) for c in chosen).items()))}")
    return 0


# --------------------------------------------------------------------------- probe

class Counters:
    def __init__(self) -> None:
        self.mesher_runs: Counter[str] = Counter()
        self.mesher_seconds: dict[str, list[float]] = defaultdict(list)
        self.cache_get = 0
        self.cache_hit = 0
        self.cache_put = 0


def install_counters(c: Counters) -> None:
    """Count mesher invocations by wrapping the members' `run` and the cache's get/put. Observation only."""
    from meshpipeline.cad.repair_v4.verdict import gmsh_members, netgen_member
    from meshpipeline.cad.repair_v4.verdict.cache import VerdictCache

    def wrap(cls) -> None:
        orig = cls.run

        def run(self, pool, step_path, *, timeout_s):  # noqa: ANN001, ANN202
            t = time.monotonic()
            r = orig(self, pool, step_path, timeout_s=timeout_s)
            c.mesher_runs[self.name] += 1
            c.mesher_seconds[self.name].append(time.monotonic() - t)
            return r

        cls.run = run

    wrap(gmsh_members.GmshMember)
    wrap(netgen_member.NetgenOCC)
    og, op = VerdictCache.get, VerdictCache.put

    def get(self, *a, **k):  # noqa: ANN001, ANN202
        r = og(self, *a, **k)
        c.cache_get += 1
        c.cache_hit += r is not None
        return r

    def put(self, *a, **k):  # noqa: ANN001, ANN202
        c.cache_put += 1
        return op(self, *a, **k)

    VerdictCache.get, VerdictCache.put = get, put


def cmd_probe(args: argparse.Namespace) -> int:
    from meshpipeline.cad.repair_v4.verdict.cache import DirStore, VerdictCache
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    sample = json.loads(Path(args.sample).read_text())
    i, n = (int(x) for x in args.shard.split("/"))
    mine = [s for s in sorted(sample, key=lambda s: s["sha256"]) if int(s["sha256"][:8], 16) % n == i - 1]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    counters = Counters()
    install_counters(counters)
    cache = VerdictCache(DirStore(args.cache))
    pool = WorkerPool(args.workers)
    by_sha = {s["sha256"]: s for s in sample}

    class Entry:  # the pool entry's shape the probe reads
        def __init__(self, s: dict) -> None:
            self.name, self.sha256, self.source, self.group = s["name"], s["sha256"], s["source"], s["group"]

    results: list[dict] = []
    done_path = out / "files.jsonl"
    done = {json.loads(line)["sha256"]: json.loads(line) for line in done_path.read_text().splitlines()
            if line.strip()} if done_path.exists() else {}  # resume: a restarted shard skips what it already wrote
    results += done.values()
    try:
        with done_path.open("a") as handle:
            for s in mine:
                if s["sha256"] in done:
                    continue
                e = Entry(s)
                rec = {"name": s["name"], "sha256": s["sha256"], "source": s["source"], "size": s["size"],
                       "diag_mm": s["diag_mm"], "group": s["group"]}
                kw = {"pool": pool, "resolve": lambda _e, p=s["path"]: p, "cache": cache,
                      "file_timeout_s": args.file_timeout_s}
                before = dict(counters.mesher_runs)
                for tier in ("cheap", "full"):
                    if tier == "full":
                        crow = rec.get("cheap_row")
                        if crow is None:
                            break
                        if crow["first_failing_layer"] is not None and (
                                crow["family"] in (reprobe_mod.NO_SOLIDS, reprobe_mod.UNBOUNDED)):
                            break  # the real flow does not pay for these twice
                    exc: list = []
                    t = time.monotonic()
                    try:
                        rows = reprobe_mod.reprobe([e], tier=tier, excluded=exc, **kw)
                    except Exception as err:  # noqa: BLE001 - recorded, never swallowed: reported as a driver_error
                        rows = []
                        rec[f"{tier}_driver_error"] = f"{type(err).__name__}: {err}"
                        pool.close()  # a pool that was interrupted mid-call may hold a stale reply: start a clean one
                        pool = WorkerPool(args.workers)
                        kw["pool"] = pool
                    rec[f"{tier}_s"] = time.monotonic() - t
                    rec[f"{tier}_row"] = asdict(rows[0]) if rows else None
                    rec[f"{tier}_excluded"] = asdict(exc[0]) if exc else None
                rec["mesher_runs"] = {k: v - before.get(k, 0) for k, v in counters.mesher_runs.items()
                                      if v - before.get(k, 0)}
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
                handle.flush()
                results.append(rec)
                print(f"[{len(results)}/{len(mine)}] {s['source']} {Path(s['name']).name} "
                      f"cheap={rec.get('cheap_s', 0):.0f}s full={rec.get('full_s', 0):.0f}s", flush=True)
        stats = {"pass": 1, "mesher_runs": dict(counters.mesher_runs), "cache_get": counters.cache_get,
                 "cache_hit": counters.cache_hit, "cache_put": counters.cache_put,
                 "member_seconds": dict(counters.mesher_seconds)}
        (out / "stats_pass1.json").write_text(json.dumps(stats, indent=1))
        if args.pass2:  # replay: same files, same cache. Every verdict that was stored must now be a hit.
            base = (counters.cache_get, counters.cache_hit, dict(counters.mesher_runs))
            t = time.monotonic()
            for s in mine:
                e = Entry(s)
                for tier in ("cheap", "full"):
                    if s["sha256"] and any(r["sha256"] == s["sha256"] and r.get(f"{tier}_row") is not None
                                           for r in results):
                        reprobe_mod.reprobe([e], tier=tier, pool=pool, resolve=lambda _e, p=s["path"]: p,
                                            cache=cache, file_timeout_s=args.file_timeout_s)
            stats2 = {"pass": 2, "seconds": time.monotonic() - t,
                      "cache_get": counters.cache_get - base[0], "cache_hit": counters.cache_hit - base[1],
                      "mesher_runs": {k: v - base[2].get(k, 0) for k, v in counters.mesher_runs.items()
                                      if v - base[2].get(k, 0)}}
            (out / "stats_pass2.json").write_text(json.dumps(stats2, indent=1))
            print("pass2", stats2)
    finally:
        pool.close()
    del by_sha
    return 0


# --------------------------------------------------------------------------- splits

def load_records(dirs: list[str]) -> list[dict]:
    recs = []
    for d in dirs:
        for line in (Path(d) / "files.jsonl").read_text().splitlines():
            if line.strip():
                recs.append(json.loads(line))
    return recs


def final_rows(recs: list[dict]) -> list:
    """The full-tier row where one exists, else the cheap one: `ProbeResult.final_rows`' rule."""
    rows = []
    for r in recs:
        row = r.get("full_row") or r.get("cheap_row")
        if row is not None:
            rows.append(reprobe_mod.ProbeRow(**row))
    return rows


def cmd_splits(args: argparse.Namespace) -> int:
    recs = load_records(args.probe_dir)
    rows = final_rows(recs)
    cheap_failed = {r["sha256"] for r in recs if r.get("cheap_row") and r["cheap_row"]["first_failing_layer"]}
    out = Path(args.out)
    result: dict = {"files": len(recs), "rows": len(rows)}
    try:
        splits = reprobe_mod.build_splits(rows, dev=args.dev, heldout=args.heldout, clean=args.clean,
                                          cheap_failed=cheap_failed)
        result["refused"] = None
    except ValueError as exc:
        result["refused"] = str(exc)
        failing = [r for r in rows if r.first_failing_layer and r.family not in (reprobe_mod.NO_SOLIDS, reprobe_mod.UNBOUNDED)]
        result["failing_set"] = len(failing)
        (out / "SUMMARY.json").parent.mkdir(parents=True, exist_ok=True)
        (out / "SUMMARY.json").write_text(json.dumps(result, indent=1))
        print("build_splits REFUSED:", exc)
        return 0
    reprobe_mod.write_splits(splits, out)
    banner = ("PROVISIONAL, DRAWN FROM A LOCAL SAMPLE OF THE CLEARED POOL. For the T2 smoke only. The real splits "
              "must come from the full re-probe; do not use these for any reported number.")
    for name in ("dev", "heldout", "clean"):
        p = out / f"{name}.json"
        body = json.loads(p.read_text())
        body["PROVISIONAL"] = banner
        p.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    result.update(PROVISIONAL=banner, dev=len(splits.dev), heldout=len(splits.heldout), clean=len(splits.clean),
                  excluded_no_solids=splits.excluded_no_solids, excluded_uncounted=splits.excluded_uncounted,
                  excluded_unbounded=splits.excluded_unbounded, shortfall=splits.shortfall, strata=splits.strata)
    (out / "SUMMARY.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    print(json.dumps({k: v for k, v in result.items() if k != "strata"}, indent=1))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--root", action="append", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--per-source", type=int, default=150)
    c.add_argument("--seed", type=int, default=evalspec.SEED)
    s = sub.add_parser("sample")
    s.add_argument("--candidates", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--seed", type=int, default=evalspec.SEED)
    r = sub.add_parser("probe")
    r.add_argument("--sample", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--shard", default="1/1")
    r.add_argument("--cache", required=True)
    r.add_argument("--workers", type=int, default=1)
    r.add_argument("--file-timeout-s", type=float, default=evalspec.DEFAULT_FILE_TIMEOUT_S)
    r.add_argument("--pass2", action="store_true")
    q = sub.add_parser("splits")
    q.add_argument("--probe-dir", action="append", required=True)
    q.add_argument("--out", required=True)
    q.add_argument("--dev", type=int, default=300)
    q.add_argument("--heldout", type=int, default=200)
    q.add_argument("--clean", type=int, default=150)
    args = p.parse_args()
    return {"candidates": cmd_candidates, "sample": cmd_sample, "probe": cmd_probe, "splits": cmd_splits}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
