#!/usr/bin/env python3
"""Snapshot launch prep: everything that can be checked without a VM and without the model.

    prep.py --code <extracted tree of the commit> --profile <name> --dev-split F --clean-split F
            --dev-wave W --clean-wave W --dev-shards N --clean-shards N --total-usd X
            --inputs-ls <gcloud storage ls -l of the inputs prefix> --prior-rows <dir with */shard-*/rows.json>
            --out DIR

Refuses (exit 1) when: a wave is not in the commit's evalspec, is not a normal-type wave with all four v4 arms, or
does not run the split it is given; a split file is not the split it claims; a split file has a sha256 that has no
staged input under the inputs prefix; dev and clean share a file; the caps would sum past --total-usd or past any
shard's ledger slice (WAVE_CAPS_USD[normal] / N). Writes DIR/plan_{dev,clean}.json (evalspec shard plans),
DIR/launch_manifest.json (caps, inputs, projection) and prints a summary. stdlib + the commit's own evalspec.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ARMS4 = ("input_only", "staged", "search_det", "search_ai")
CLEAN_WEIGHT = 0.25


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_split(path: Path, want: str) -> list[dict]:
    body = json.loads(path.read_text())
    if body.get("split") not in (None, want):
        raise SystemExit(f"REFUSED: {path} says split {body.get('split')!r}, expected {want!r}")
    files = body["files"]
    if not files:
        raise SystemExit(f"REFUSED: {path} has no files")
    shas = [f["sha256"] for f in files]
    if len(set(shas)) != len(shas):
        raise SystemExit(f"REFUSED: {path} lists a file twice")
    return files


def parse_ls(path: Path) -> dict[str, tuple[int, str]]:
    """sha256 -> (bytes, object url) from `gcloud storage ls -l "<prefix>/**"`."""
    out = {}
    pat = re.compile(r"^\s*(\d+)\s+\S+\s+(gs://\S+/inputs/([0-9a-f]{64})/.+)$")
    for line in path.read_text().splitlines():
        m = pat.match(line)
        if m:
            out[m.group(3)] = (int(m.group(1)), m.group(2))
    return out


def prior_costs(root: Path | None, wall: float) -> dict[str, float]:
    """sha256 -> all-four-arm seconds measured in an earlier snapshot (an excluded arm counted at THIS wall)."""
    if root is None or not root.exists():
        return {}
    per: dict[str, dict[str, float]] = defaultdict(dict)
    for f in root.glob("*/shard-*/rows.json"):
        for r in json.loads(f.read_text()):
            per[r["sha256"]][r["arm"]] = float(r["wall_s"])
    for f in root.glob("*/shard-*/EXCLUDED.json"):
        for e in json.loads(f.read_text()):
            per[e["sha256"]][e["arm"]] = wall
    return {s: sum(a.values()) for s, a in per.items() if set(ARMS4) <= set(a)}


def prior_unclean(root: Path | None) -> set[str]:
    """sha256 of the prior snapshot's files that were NOT clean at input (the dev-like ones)."""
    if root is None or not root.exists():
        return set()
    return {r["sha256"] for f in root.glob("*/shard-*/rows.json") for r in json.loads(f.read_text())
            if r["arm"] == "input_only" and not r["in_clean"]}


def main() -> int:
    p = argparse.ArgumentParser()
    for k in ("code", "profile", "dev-split", "clean-split", "dev-wave", "clean-wave", "inputs-ls", "out"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--prior-rows", default="")
    p.add_argument("--dev-shards", type=int, required=True)
    p.add_argument("--clean-shards", type=int, required=True)
    p.add_argument("--total-usd", type=float, required=True)
    p.add_argument("--file-usd-cap", type=float, default=0.5)
    p.add_argument("--vm-usd-per-h", type=float, required=True)
    p.add_argument("--ops", type=int, default=None, help="the op budget this wave runs with (applied ops)")
    p.add_argument("--prior-ops", type=int, default=24, help="the op budget the prior rows were measured at")
    p.add_argument("--max-run", default="6h", help="the VM's server-side reaper, e.g. 5h")
    p.add_argument("--usd-per-dev-file", type=float, default=0.04,
                   help="expected model $ per dev file (snapshot v1: $1.00 over 26 called files of 28)")
    a = p.parse_args()

    code = Path(a.code)
    sys.path.insert(0, str(code / "devtools/validation/v4_eval"))
    import evalspec as ev  # the COMMIT's evalspec: the waves, the wall and the plan builder it will run with

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    waves = {}
    for split, name in (("dev", a.dev_wave), ("clean", a.clean_wave)):
        if name not in ev.WAVES:
            problems.append(f"wave {name!r} is not in this commit's evalspec.WAVES (merge the branch that adds it, "
                            f"or pass a commit that has it); waves here: {sorted(ev.WAVES)}")
            continue
        w = ev.WAVES[name]
        if w.wave_type != "normal" or tuple(w.arms) != ARMS4 or w.split != split:
            problems.append(f"wave {name!r} is ({w.wave_type}, {w.split}, {w.arms}); need (normal, {split}, {ARMS4})")
        waves[split] = w
    if problems:
        print("\n".join("REFUSED: " + x for x in problems))
        return 1

    splits = {"dev": Path(a.dev_split), "clean": Path(a.clean_split)}
    files = {k: load_split(v, k) for k, v in splits.items()}
    overlap = {f["sha256"] for f in files["dev"]} & {f["sha256"] for f in files["clean"]}
    if overlap:
        problems.append(f"dev and clean share {len(overlap)} file(s), e.g. {sorted(overlap)[0][:12]}")
    staged = parse_ls(Path(a.inputs_ls))
    missing = [f["name"] for k in files for f in files[k] if f["sha256"] not in staged]
    if missing:
        problems.append(f"{len(missing)} split file(s) have no staged input, e.g. {missing[:3]}")
    misnamed = [f["name"] for k in files for f in files[k]
                if f["sha256"] in staged and not staged[f["sha256"]][1].endswith("/" + Path(f["name"]).name)]
    if misnamed:
        problems.append(f"{len(misnamed)} staged input(s) have another basename than the split, e.g. {misnamed[:2]}")
    if problems:
        print("\n".join("REFUSED: " + x for x in problems))
        return 1

    wall = float(ev.DEFAULT_FILE_TIMEOUT_S)
    prior = prior_costs(Path(a.prior_rows) if a.prior_rows else None, wall)
    # calibration: all-four-arm seconds per size-model second, from the prior snapshot's dev files
    # calibrated on EVERY prior file that was unclean at input (not only those in this split), so a split of new
    # files still gets a measured scale
    unclean = prior_unclean(Path(a.prior_rows) if a.prior_rows else None)
    ratios = [prior[s] / ev.predicted_cost_s(staged[s][0]) for s in sorted(unclean) if s in prior and s in staged]
    # the MEAN ratio: the projection is a sum of costs, and the heavy tail (file-wall hits) is in the mean, not the median
    k_dev = statistics.mean(ratios) if ratios else 4.0 * 2.0
    clean_prior = [prior[f["sha256"]] for f in files["clean"] if f["sha256"] in prior]
    clean_default = statistics.mean(clean_prior) if clean_prior else 60.0

    manifest: dict = {"profile": a.profile, "file_timeout_s": wall, "budget": {
        "wall_s": ev.SHARED_BUDGET.wall_s if hasattr(ev, "SHARED_BUDGET") else None},
        "calibration": {"k_dev_four_arms_per_model_s": k_dev, "from_prior_files": len(ratios),
                        "clean_default_s": clean_default}, "waves": {}, "splits": {}}
    shards = {"dev": a.dev_shards, "clean": a.clean_shards}
    slice_cap = {k: ev.wave_cap_usd("normal") / shards[k] for k in shards}
    caps: dict[str, list[float]] = {}
    proj: dict[str, list[float]] = {}
    counts: dict[str, list[int]] = {}
    costs_all: dict[str, dict[str, float]] = {}
    for k in ("dev", "clean"):
        costs, source = {}, {"prior": 0, "size_model": 0}
        for f in files[k]:
            s = f["sha256"]
            if s in prior:
                costs[s] = prior[s]
                source["prior"] += 1
            else:
                costs[s] = (k_dev * ev.predicted_cost_s(staged[s][0])) if k == "dev" else clean_default
                source["size_model" if k == "dev" else "clean_default"] = source.get(
                    "size_model" if k == "dev" else "clean_default", 0) + 1
        costs_all[k] = costs
        n = min(shards[k], len(files[k]))
        shards[k] = n
        slice_cap[k] = ev.wave_cap_usd("normal") / n
        plan = ev.build_shard_plan(costs, n)
        plan["cost_source"] = {"unit": "predicted seconds, all four arms", **source,
                               "calibration_k": k_dev, "file_timeout_s": wall}
        path = out / f"plan_{k}.json"
        path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
        ev.load_shard_plan(path)
        load, count = defaultdict(float), defaultdict(int)
        for s, i in plan["assign"].items():
            load[i] += costs[s]
            count[i] += 1
        counts[k] = [count[i] for i in range(1, n + 1)]
        proj[k] = [load[i] for i in range(1, n + 1)]
        manifest["waves"][k] = {"wave": waves[k].name, "n_shards": n, "files_per_shard": counts[k],
                                "predicted_shard_s": [round(x) for x in proj[k]],
                                "plan_digest": ev.plan_digest(plan), "slice_usd": slice_cap[k]}
        manifest["splits"][k] = {"path": str(splits[k]), "sha256": sha_of(splits[k]), "files": len(files[k]),
                                 "input_bytes": sum(staged[f["sha256"]][0] for f in files[k])}
    # A shard's cap is its DEMAND (files x the per-file cap; a clean file weighs CLEAN_WEIGHT, it is clean at input and
    # the model is only asked when the healer damages it), scaled down so every cap sums to at most --total-usd,
    # never above the shard's ledger slice (cap_to only lowers), rounded DOWN to the cent.
    # Two passes, so no shard's cap is below what ONE of its files may spend (a shard cap under the per-file ceiling
    # would bind first and silently shorten that file's model turns): (1) every shard gets min(its demand, one file's
    # ceiling); (2) what is left of the total goes out in proportion to the demand still unmet.
    demand = {k: [c * a.file_usd_cap * (1.0 if k == "dev" else CLEAN_WEIGHT) for c in counts[k]] for k in counts}
    floor_ = {k: [min(d, a.file_usd_cap) for d in demand[k]] for k in demand}
    left = a.total_usd - sum(sum(v) for v in floor_.values())
    if left < 0:
        print(f"REFUSED: {sum(len(v) for v in floor_.values())} shards x one file's ${a.file_usd_cap} exceeds "
              f"${a.total_usd}: fewer shards or a larger total")
        return 1
    rest = sum(d - f for k in demand for d, f in zip(demand[k], floor_[k]))
    scale = min(1.0, left / rest) if rest > 0 else 0.0
    for k in demand:
        caps[k] = [min(slice_cap[k], math.floor(100 * (f + (d - f) * scale)) / 100)
                   for d, f in zip(demand[k], floor_[k])]
        manifest["waves"][k]["caps_usd"] = caps[k]
    manifest["cap_rule"] = {"clean_weight": CLEAN_WEIGHT, "file_usd_cap": a.file_usd_cap,
                            "floor_per_shard": "min(demand, file_usd_cap)", "scale_of_the_rest": scale}
    total_caps = round(sum(caps["dev"]) + sum(caps["clean"]), 2)
    if total_caps > a.total_usd + 1e-9:
        print(f"REFUSED: shard caps sum to ${total_caps} > ${a.total_usd}")
        return 1
    if any(c <= 0 for k in caps for c in caps[k]):
        print("REFUSED: a shard cap rounded to $0: fewer shards or a larger total")
        return 1
    crit = max(max(proj["dev"]), max(proj["clean"]))
    # THE OP BUDGET. More ops lengthen a search only where the search stopped on `ops`; a search that EXHAUSTED its
    # candidates is unchanged. Either way each search arm is bounded by its own Budget.wall_s (the search's wall),
    # so the upper bound puts BOTH search arms at that wall on every dev file. The lower bound is the plan above.
    sb_wall = float(ev.SHARED_BUDGET.wall_s)
    ops = a.ops if a.ops is not None else int(ev.SHARED_BUDGET.ops)
    upper_file = {f["sha256"]: min(4 * wall, (costs_all["dev"][f["sha256"]] if f["sha256"] in costs_all["dev"] else 0.0)
                                   + 2 * sb_wall) for f in files["dev"]}
    up_load = defaultdict(float)
    plan_dev = json.loads((out / "plan_dev.json").read_text())
    for s_, i in plan_dev["assign"].items():
        up_load[i] += upper_file[s_]
    crit_up = max(max(up_load.values()), max(proj["clean"]))
    reaper_s = float(a.max_run.rstrip("h")) * 3600 if a.max_run.endswith("h") else float(a.max_run)
    setup_s = 0.35 * 3600
    model_usd = a.usd_per_dev_file * len(files["dev"]) + 0.02 * (shards["dev"] + shards["clean"])
    vm_h = crit / 3600 + 0.35  # + create, image pull, staging, collect, delete
    manifest.update(total_caps_usd=total_caps, inputs={f["sha256"]: staged[f["sha256"]][1]
                                                        for k in files for f in files[k]},
                    projection={"critical_shard_min": round(crit / 60, 1),
                                "ops": ops, "prior_ops": a.prior_ops, "search_wall_s": sb_wall,
                                "critical_shard_min_upper": round(crit_up / 60, 1),
                                "fits_reaper_lower": crit + setup_s < reaper_s,
                                "fits_reaper_upper": crit_up + setup_s < reaper_s,
                                "worst_single_file_min": round(max(4 * wall, 0) / 60, 1),
                                "expected_model_usd": round(model_usd, 2), "vm_hours": round(vm_h, 2),
                                "vm_usd": round(vm_h * a.vm_usd_per_h, 2)})
    (out / "launch_manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (out / "caps_dev.txt").write_text(" ".join(f"{c:.2f}" for c in caps["dev"]) + "\n")
    (out / "caps_clean.txt").write_text(" ".join(f"{c:.2f}" for c in caps["clean"]) + "\n")
    (out / "inputs.txt").write_text("".join(f"{s} {u}\n" for s, u in sorted(manifest["inputs"].items())))
    w = manifest["waves"]
    print(f"OK profile={a.profile} file_wall={wall:.0f}s dev={len(files['dev'])} files/{w['dev']['n_shards']} shards "
          f"{w['dev']['files_per_shard']} clean={len(files['clean'])} files/{w['clean']['n_shards']} shards "
          f"{w['clean']['files_per_shard']}")
    print(f"   caps dev {caps['dev']} clean {caps['clean']} total ${total_caps} (limit ${a.total_usd})")
    print(f"   predicted shard minutes dev {[round(x / 60) for x in proj['dev']]} clean "
          f"{[round(x / 60) for x in proj['clean']]}; cost source dev k={k_dev:.2f} from {len(ratios)} prior files")
    print(f"   projection: wave ~{crit / 60:.0f} min (critical shard), VM ~{vm_h:.2f} h ~${vm_h * a.vm_usd_per_h:.2f}, "
          f"model ~${model_usd:.2f} expected (cap ${total_caps}); a file that walls in all 4 arms costs "
          f"{4 * wall / 60:.0f} min on its shard")
    print(f"   op budget {ops} (prior rows at {a.prior_ops}): critical shard {crit / 60:.0f} min if searches end by "
          f"exhaustion (ops do not bind), up to {crit_up / 60:.0f} min if both search arms run to their "
          f"{sb_wall:.0f} s search wall on every dev file; reaper {reaper_s / 3600:.1f} h -> "
          f"{'fits' if crit + setup_s < reaper_s else 'DOES NOT FIT'} (lower), "
          f"{'fits' if crit_up + setup_s < reaper_s else 'DOES NOT FIT'} (upper)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
