"""B15 step 2: what does the wave's ONE verdict cache actually save between the arms of a file?

A shard runs every arm of a file in ONE process against `default_verdict_cache()`, so the arms CAN share layer 2
(the cached thing is the panel, and only the panel: layer 1, layer 3 and the STEP export are never cached). This
probe runs the arms twice over the same files and prints both walls and the cache's own counters:

  SHARED  one cache for every arm of a file - the wave's real configuration
  COLD    a fresh cache per arm - what the wave would cost if nothing were shared

SHARED minus COLD is what sharing already saves; what is left inside SHARED is the re-paid work no cache covers.

  python cache_share_probe.py --inputs <dir of <sha>/<name>.step> --out <dir> [--arms input_only,staged,search_det]

No model: `search_ai` needs a ledger and a key, and its input verdict goes through the same judge as `search_det`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/work/src")
sys.path.insert(0, "/work/devtools/validation/v4_eval")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--inputs", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--arms", default="input_only,staged,search_det")
    p.add_argument("--ops", type=int, default=24)
    p.add_argument("--workers", type=int, default=1)
    a = p.parse_args()

    import arms as arms_mod
    import evalspec as ev

    from meshpipeline.cad.repair_v4.verdict import cache as cache_mod
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    names = [ev.arm(x.strip()).name for x in a.arms.split(",") if x.strip()]
    files = sorted(q for d in sorted(Path(a.inputs).iterdir()) if d.is_dir() for q in d.glob("*.step"))
    report: dict = {"arms": names, "ops": a.ops, "files": [], "note": __doc__.splitlines()[0]}

    for step in files:
        sha = step.parent.name
        entry = SimpleNamespace(name=step.name, sha256=sha, source="probe")
        record: dict = {"file": step.name, "sha256": sha[:12], "bytes": step.stat().st_size, "modes": {}}
        for mode in ("shared", "cold"):
            per_arm = []
            cache_mod.reset_default_verdict_cache()           # a cold start for this mode
            cache_dir = out / f"cache_{mode}_{sha[:8]}"
            import os

            os.environ[cache_mod.CACHE_URI_ENV] = str(cache_dir)
            for i, name in enumerate(names):
                if mode == "cold":   # a fresh cache per arm: nothing is shared between arms
                    cache_mod.reset_default_verdict_cache()
                    os.environ[cache_mod.CACHE_URI_ENV] = str(cache_dir) + f"_{i}"
                before = cache_mod.default_verdict_cache().stats()
                pool = WorkerPool(a.workers)
                t0 = time.monotonic()
                try:
                    spec = ev.ARMS[name]
                    budget = ev.Budget(wall_s=spec.budget.wall_s, ops=a.ops, model_usd=spec.budget.model_usd)
                    outcome = arms_mod.run_file(
                        name, entry, str(step), pool=pool, engine=arms_mod.RealEngine(), ledger=None, arm=spec,
                        budget=budget, h_mm=arms_mod.H_MM_AUTO, route=None,
                        max_turns=0, job_id=f"b15:{sha[:12]}:{name}",
                        workdir=str(out / "work" / sha[:12] / mode / name), workers=a.workers)
                finally:
                    pool.close()
                wall = round(time.monotonic() - t0, 2)
                after = cache_mod.default_verdict_cache().stats()
                delta = {k: after[k] - before.get(k, 0) for k in after}
                row = outcome.row or {}
                per_arm.append({"arm": name, "wall_s": wall, "cache_delta": delta,
                                "excluded": outcome.excluded,
                                "out_clean": row.get("out_clean"), "out_panel_passes": row.get("out_panel_passes")})
                print(json.dumps({"file": step.name, "mode": mode, **per_arm[-1]}, sort_keys=True), flush=True)
            record["modes"][mode] = {"arms": per_arm, "total_wall_s": round(sum(x["wall_s"] for x in per_arm), 2),
                                     "hits": sum(x["cache_delta"].get("hits", 0) for x in per_arm),
                                     "misses": sum(x["cache_delta"].get("misses", 0) for x in per_arm)}
        sh, cold = record["modes"]["shared"], record["modes"]["cold"]
        record["sharing_saves_s"] = round(cold["total_wall_s"] - sh["total_wall_s"], 2)
        record["sharing_saves_pct"] = round(100.0 * record["sharing_saves_s"] / max(cold["total_wall_s"], 1e-9), 1)
        report["files"].append(record)
        (out / "b15_cache_share.json").write_text(json.dumps(report, indent=1, sort_keys=True))
        print(json.dumps({"file": step.name, "SUMMARY": {k: record[k] for k in
                                                         ("sharing_saves_s", "sharing_saves_pct")},
                          "shared": sh, "cold": cold}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
