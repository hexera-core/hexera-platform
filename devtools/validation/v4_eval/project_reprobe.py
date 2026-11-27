#!/usr/bin/env python3
# Responsibility: Turn a measured re-probe sample (measure_reprobe.py) into a projection for the whole cleared pool:
#                 per-file stats, then VM-hours / wall / $ under the launcher's hash sharding and under largest-first
#                 bin packing by predicted cost, by bootstrap simulation.
# Owns: the size-based cost model, the two shard schemes and the simulation. PURE: no gcloud, no CAD.
# Boundaries: it projects; it launches nothing. Its predictor is file size (the only per-file feature known for every
#             pool file before any CAD is read); the sample's residual spread is resampled so the timeouts it cannot
#             predict are carried into every simulated wave.
"""Project the re-probe wave from a measured sample.

    project_reprobe.py --measured files.jsonl [...] --sizes sizes.json --shards 96 80 [--reps 400]

`--sizes` maps sha256 -> bytes for every cleared pool file. `$` is c2d-standard-8 STANDARD in us-central1 at
`--usd-per-vm-hour` (list price + the 100 GB pd-balanced boot disk) and `--overhead-h` per VM (boot, docker install,
image pull, inputs copy): both are stated inputs, not measurements.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import importlib.util
import json
import math
import random
import statistics
import sys
from pathlib import Path


def _load_evalspec():
    """v4's evalspec, by PATH under a unique module name. Not `import evalspec`: v3_eval's modules register
    their own evalspec under exactly that bare name (v3_eval/run_arm.py and friends), so a plain import picks up
    whichever evalspec reached sys.modules first - and in a test session that is sometimes v3's."""
    spec = importlib.util.spec_from_file_location("v4_evalspec_for_projection",
                                                  Path(__file__).resolve().parent / "evalspec.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` looks its own module up in sys.modules by __module__.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_evalspec = _load_evalspec()  # pure constants; no CAD, no gcloud

SEED = 20261123
EVAL_SEED = 20261122  # evalspec.SEED: the launcher's hash partition


def shard_of(sha256: str, n: int) -> int:
    """evalspec.shard_of, 0-based: the partition every v4 shard uses today."""
    return int(hashlib.sha256(f"{EVAL_SEED}:{sha256}".encode()).hexdigest()[:12], 16) % n


def pack_vms(n_shards: int, slots: int = 8) -> list[list[int]]:
    """evalspec.pack: shard idx -> VM idx % vms."""
    vms = max(1, -(-n_shards // slots))
    out: list[list[int]] = [[] for _ in range(vms)]
    for idx in range(n_shards):
        out[idx % vms].append(idx)
    return out


def fit(rows: list[dict]) -> tuple[float, float, list[float]]:
    """log(wall) = a + b log(size), least squares; returns a, b and the multiplicative residuals."""
    xs = [math.log(max(r["size"], 1)) for r in rows]
    ys = [math.log(max(r["wall_s"], 0.1)) for r in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else 0.0
    a = my - b * mx
    return a, b, [y - (a + b * x) for x, y in zip(xs, ys, strict=True)]


def lpt(costs: dict[str, float], n: int) -> dict[str, int]:
    """Largest-first: each file, by descending predicted cost, onto the currently lightest shard."""
    heap = [(0.0, i) for i in range(n)]
    out: dict[str, int] = {}
    for sha, c in sorted(costs.items(), key=lambda kv: (-kv[1], kv[0])):
        load, i = heapq.heappop(heap)
        out[sha] = i
        heapq.heappush(heap, (load + c, i))
    return out


def simulate(sizes: dict[str, int], a: float, b: float, resid: list[float], n: int, cap_s: float,
             overhead_h: float, reps: int, scheme: str) -> list[dict]:
    rng = random.Random(f"{SEED}:{n}:{scheme}")
    pred = {h: math.exp(a + b * math.log(max(s, 1))) for h, s in sizes.items()}
    assign = lpt(pred, n) if scheme == "lpt" else {h: shard_of(h, n) for h in sizes}
    vms = pack_vms(n)
    out = []
    for _ in range(reps):
        shard_s = [0.0] * n
        for h, p in pred.items():
            shard_s[assign[h]] += min(cap_s, p * math.exp(rng.choice(resid)))
        vm_h = [max(shard_s[i] for i in v) / 3600 + overhead_h for v in vms]
        out.append({"vm_hours": sum(vm_h), "wall_h": max(vm_h), "cpu_h": sum(shard_s) / 3600})
    return out


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measured", nargs="+", required=True)
    ap.add_argument("--sizes", required=True)
    ap.add_argument("--shards", type=int, nargs="+", default=[96, 80])
    ap.add_argument("--reps", type=int, default=400)
    # The wall is NOT retyped here: the projection must cap simulated files at the same wall the wave will run.
    ap.add_argument("--file-timeout-s", type=float, default=_evalspec.DEFAULT_FILE_TIMEOUT_S,
                    help="the per-file wall simulated files are capped at (default: evalspec.DEFAULT_FILE_TIMEOUT_S, "
                         f"{_evalspec.DEFAULT_FILE_TIMEOUT_S:g} s); pass another value to price a candidate wall")
    ap.add_argument("--overhead-h", type=float, default=0.25)
    ap.add_argument("--usd-per-vm-hour", type=float, default=0.377)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    rows = [json.loads(line) for f in args.measured for line in Path(f).read_text().splitlines() if line.strip()]
    walls = [r["wall_s"] for r in rows]
    util = [r["cpu_s"] / r["wall_s"] for r in rows if r["wall_s"] > 0]
    timeouts = [r for r in rows if (r.get("excluded") or "").startswith("file_timeout")]
    a, b, resid = fit(rows)
    sizes = {h: int(s) for h, s in json.loads(Path(args.sizes).read_text()).items()}
    doc: dict = {
        "sample": {"n": len(rows), "median_s": statistics.median(walls), "mean_s": statistics.fmean(walls),
                   "p90_s": pct(walls, 0.9), "max_s": max(walls),
                   "wall_timeout_rate": len(timeouts) / len(rows),
                   "excluded": sorted({r["excluded"].split(":")[0] for r in rows if r.get("excluded")}),
                   "cpu_per_wall_median": statistics.median(util), "cpu_per_wall_max": max(util),
                   "throttled_s_total": sum(r["throttled_s"] for r in rows),
                   "throttled_frac_of_wall": sum(r["throttled_s"] for r in rows) / sum(walls),
                   "mem_peak_gb_max": max(r["mem_peak"] for r in rows) / 2**30},
        "model": {"log_wall = a + b log_size": [a, b],
                  "r2": 1 - statistics.pvariance(resid) / statistics.pvariance(
                      [math.log(max(w, 0.1)) for w in walls])},
        "pool_files": len(sizes), "naive_vcpu_h": len(sizes) * statistics.fmean(walls) / 3600, "plans": {}}
    for n in args.shards:
        for scheme in ("hash", "lpt"):
            sims = simulate(sizes, a, b, resid, n, args.file_timeout_s, args.overhead_h, args.reps, scheme)
            vmh = [s["vm_hours"] for s in sims]
            wall = [s["wall_h"] for s in sims]
            doc["plans"][f"{n}/{scheme}"] = {
                "vms": len(pack_vms(n)), "vcpu_h_median": statistics.median(s["cpu_h"] for s in sims),
                "vm_hours_median": statistics.median(vmh), "vm_hours_p90": pct(vmh, 0.9),
                "wall_h_median": statistics.median(wall), "wall_h_p90": pct(wall, 0.9),
                "usd_median": statistics.median(vmh) * args.usd_per_vm_hour,
                "usd_p90": pct(vmh, 0.9) * args.usd_per_vm_hour}
    text = json.dumps(doc, indent=1, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
