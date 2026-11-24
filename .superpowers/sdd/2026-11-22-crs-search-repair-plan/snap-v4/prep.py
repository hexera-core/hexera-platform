#!/usr/bin/env python3
"""Snapshot v4 launch prep: PER-ARM shard groups, packed onto several VMs in several regions.

    prep.py --code <extracted tree of the commit> --profile v4 --dev-split F --clean-split F
            --dev-wave W --clean-wave W --total-usd X --inputs-ls <gcloud storage ls -l>
            --prior-rows <dir with */shard-*/rows.json|rows.partial.jsonl> --regions SPEC --target-min M --out DIR

WHY GROUPS (lane B14, measured on v2). A shard that runs all four arms over its files in sequence makes the slowest
shard a SUM of four arms, which is how v2 took 2.7 h and how v1r left one shard 3.5 h behind. The arms are
independent - `arms.run_file` re-derives everything per arm - so each arm becomes its own set of shards, sized to
its own measured work:

    dev search_ai  29.3 core-h   capped at 37 shards by model_throttle (for_shard REFUSES at 38)
    dev search_det 28.0 core-h   free to scale
    dev staged      9.8 core-h
    dev input_only  4.3 core-h
    clean, all arms 0.3 core-h

--regions is `<region>:<vcpu>:<zone,zone,...>` repeated, e.g.
    us-central1:88:us-central1-b,us-central1-a us-east1:96:us-east1-b,us-east1-c
The vCPU figure per region is what the LIVE quota allows (B14 measured C2D_CPUS 100 and the region-wide CPUS 200
with ~92 vCPU of other people's VMs in us-central1, so 88 there leaves them headroom and needs no quota raise).
Every VM is c2d in every region, so `wall_s` stays comparable between files of one benchmark.

Refuses (exit 1) for every reason v1r's prep refuses, plus: a group whose arms are not the wave's arms, a model
group over the throttle ceiling, more shard slots demanded than the regions can hold, and a VM plan that leaves a
group unplaced. Writes plan_<group>.json, caps_<group>.txt, vm_plan.json, launch_manifest.json, inputs.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ARMS4 = ("input_only", "staged", "search_det", "search_ai")
CLEAN_WEIGHT = 0.25
#: (group, its arms, its split). One shard plan, one wave id, one --n-shards and one VM slice EACH.
GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("dev_ai", ("search_ai",), "dev"),
    ("dev_det", ("search_det",), "dev"),
    ("dev_staged", ("staged",), "dev"),
    ("dev_input", ("input_only",), "dev"),
    ("clean", ARMS4, "clean"),
)
#: each arm's measured share of a dev file's four-arm seconds (lane B14, fitted on v2's rows). Used ONLY for a file
#: with no prior row of that arm, so a new file is still packed sensibly instead of as a four-arm average.
ARM_SHARE = {"input_only": 0.06, "staged": 0.14, "search_det": 0.39, "search_ai": 0.41}
#: c2d machine sizes, largest first: a region's vCPU budget is filled with these.
C2D_LADDER = (56, 32, 16, 8, 4)
#: vCPU per shard. Two is one physical core on c2d, which is what v2 measured its walls with; one vCPU halves the
#: core AND the 4 GB/vCPU memory, which lane B14 refused to assume without a pilot.
VCPU_PER_SHARD = 2


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
    out = {}
    pat = re.compile(r"^\s*(\d+)\s+\S+\s+(gs://\S+/inputs/([0-9a-f]{64})/.+)$")
    for line in path.read_text().splitlines():
        m = pat.match(line)
        if m:
            out[m.group(3)] = (int(m.group(1)), m.group(2))
    return out


#: an exclusion with one of these reasons is NOT a cost measurement: the pair never ran (the wave was stopped at its
#: deadline) or its reason was lost at the stop. v3's plan counted 393 of them at the 1800 s wall and projected
#: 549 min against a real ~3 h (the snapshot runner's af59c91f; v2's own EXCLUDED has 393 + 37 of them).
NOT_A_COST = ("unmeasured:", "excluded before the stop")


def prior_costs_per_arm(root: Path, wall: float) -> dict[str, dict[str, float]]:
    """sha256 -> {arm: measured seconds} from an earlier snapshot, PER ARM and per arm INDEPENDENTLY: a file is
    useful for packing search_det even when its search_ai row is missing. `rows.partial.jsonl` is read too, so an
    unfinished wave still informs the next one, and an exclusion that RAN (a file wall, a crash) counts at THIS
    wall - but a deadline-unmeasured or reason-lost pair is skipped, not charged."""
    per: dict[str, dict[str, float]] = defaultdict(dict)
    for f in root.glob("*/shard-*/rows.json"):
        for r in json.loads(f.read_text()):
            per[r["sha256"]][r["arm"]] = float(r["wall_s"])
    for f in root.glob("*/shard-*/rows.partial.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                per[r["sha256"]].setdefault(r["arm"], float(r["wall_s"]))
    for f in root.glob("*/shard-*/EXCLUDED.json"):
        for e in json.loads(f.read_text()):
            if str(e.get("reason", "")).startswith(NOT_A_COST):
                continue
            per[e["sha256"]].setdefault(e["arm"], wall)
    return dict(per)


def require_prior(spec: str) -> Path:
    """The prior-rows tree, or REFUSE. A missing or empty prior used to fall through to k_dev = 1.0, which planned
    one shard per arm group - all 150 search_ai files in ONE shard - and printed a 40-minute projection for a job
    of tens of hours. A plan that cannot be reproduced from an artifact on disk is not a plan."""
    if not spec:
        raise SystemExit("REFUSED: --prior-rows is empty. The shard plan is sized on an earlier wave's MEASURED "
                         "per-arm walls; without them every group would be planned on an uncalibrated size model.")
    root = Path(spec)
    if not root.exists():
        raise SystemExit(f"REFUSED: --prior-rows {root} does not exist. Point it at a committed run's out/ "
                         "directory (one that holds <wave_id>/shard-*/rows.json).")
    found = list(root.glob("*/shard-*/rows.json")) + list(root.glob("*/shard-*/rows.partial.jsonl"))
    if not found:
        raise SystemExit(f"REFUSED: --prior-rows {root} holds no */shard-*/rows.json or rows.partial.jsonl")
    return root


def prior_unclean(root: Path) -> set[str]:
    out: set[str] = set()
    for f in root.glob("*/shard-*/rows.json"):
        for r in json.loads(f.read_text()):
            if r["arm"] == "input_only" and not r["in_clean"]:
                out.add(r["sha256"])
    for f in root.glob("*/shard-*/rows.partial.jsonl"):
        for line in f.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r["arm"] == "input_only" and not r["in_clean"]:
                    out.add(r["sha256"])
    return out


def throttle_ceiling(route: str, code: Path) -> int | None:
    """The most shards a MODEL group may have: `model_throttle.wave_max_in_flight`, whose `for_shard` refuses as
    soon as `ceiling // n_shards` is below one call (lane B11). None when the module cannot be imported, which is
    refused by the caller rather than guessed at."""
    sys.path.insert(0, str(code / "src"))
    try:
        from meshpipeline.cad.repair_v4.model_throttle import wave_max_in_flight

        return int(wave_max_in_flight(route))
    except Exception:  # noqa: BLE001 - an unimportable throttle is the caller's refusal, not a default
        return None


def parse_regions(specs: list[str]) -> list[dict]:
    out = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) != 3 or not parts[2]:
            raise SystemExit(f"REFUSED: --regions wants <region>:<vcpu>:<zone,zone>, not {spec!r}")
        region, vcpu, zones = parts[0], int(parts[1]), [z for z in parts[2].split(",") if z]
        if vcpu < min(C2D_LADDER):
            raise SystemExit(f"REFUSED: region {region} has {vcpu} vCPU, under one {min(C2D_LADDER)}-vCPU VM")
        out.append({"region": region, "vcpu": vcpu, "zones": zones})
    return out


def vms_for(regions: list[dict], label: str, ts: str = "", vcpu_per_shard: float = 0.0) -> list[dict]:
    """A region's vCPU budget filled with c2d VMs, largest first. Every VM is c2d so one benchmark's walls stay
    comparable across regions; the size ladder only decides how the budget is cut up."""
    vms = []
    for r in regions:
        left, n = r["vcpu"], 0
        while left >= min(C2D_LADDER):
            size = next(s for s in C2D_LADDER if s <= left)
            n += 1
            left -= size
            # the run stamp is IN the name: a name is unique per run, so a retried create can never yield two VMs of one
            # name in two zones that nothing keyed on name can tell apart (the B17 pilot's leaked VM)
            vms.append({"name": f"dev-crsv4-snap-{label}-{r['region']}-{n}" + (f"-{ts}" if ts else ""), "region": r["region"],
                        "zones": r["zones"], "machine": f"c2d-standard-{size}", "vcpu": size,
                        "slots": int(size / (vcpu_per_shard or VCPU_PER_SHARD)), "units": []})
    return vms


def pack(vms: list[dict], units: list[dict]) -> list[dict]:
    """Heaviest unit first onto the least-loaded VM that still has a slot (capacity-constrained LPT). Deterministic:
    ties by VM name, so a relaunch packs identically."""
    load = {v["name"]: 0.0 for v in vms}
    for u in sorted(units, key=lambda u: (-float(u["predicted_s"]), u["group"], u["shard"])):
        free = [v for v in vms if len(v["units"]) < v["slots"]]
        if not free:
            capacity = sum(v["slots"] for v in vms)
            work_s = sum(float(x["predicted_s"]) for x in units)
            feasible = work_s / max(capacity, 1) / 60.0
            raise SystemExit(
                f"REFUSED: {len(units)} shard slots do not fit {capacity} on {len(vms)} VMs. The plan asks for "
                f"{work_s / 3600:.1f} core-h; {capacity} slots can only deliver that in about {feasible:.0f} min. "
                f"Either raise --regions vCPU, or pass --target-min {max(1, int(feasible) + 1)} and accept that "
                "wall, or cut the split. I will not quietly pack more work into fewer shards.")
        home = min(free, key=lambda v: (load[v["name"]], v["name"]))
        home["units"].append(u)
        load[home["name"]] += float(u["predicted_s"])
    for v in vms:
        v["predicted_s"] = round(load[v["name"]], 1)
        v["units"].sort(key=lambda u: (u["group"], u["shard"]))
    return [v for v in vms if v["units"]]


def main() -> int:  # noqa: PLR0915 - one linear script: read, plan, refuse, write
    p = argparse.ArgumentParser()
    for k in ("code", "profile", "dev-split", "clean-split", "dev-wave", "clean-wave", "inputs-ls", "out"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--prior-rows", default="")
    p.add_argument("--total-usd", type=float, required=True)
    p.add_argument("--file-usd-cap", type=float, default=1.0)
    p.add_argument("--regions", nargs="+", required=True)
    p.add_argument("--target-min", type=float, default=50.0, help="the wall every group is sized to hit")
    p.add_argument("--route", default="openai:gpt-6-astra")
    p.add_argument("--label", default="v4")
    p.add_argument("--ts", default="", help="the launcher's run stamp; the group wave ids are snap-<label>-<group>-<ts>")
    p.add_argument("--ops", type=int, default=None)
    p.add_argument("--groups", default="", help="override GROUPS: 'name:arm,arm:split ...' (the fast loop runs the "
                   "search arms only); default the four dev arm groups + clean")
    p.add_argument("--file-timeout-s", type=float, default=None, help="the per-file wall the shards run with "
                   "(default the commit's DEFAULT_FILE_TIMEOUT_S); every (file, arm) cost is capped at it")
    p.add_argument("--search-wall-s", type=float, default=None, help="passed through to run_arm (lower only)")
    p.add_argument("--vcpu-per-shard", type=float, default=0.0, help="vCPU per container (default VCPU_PER_SHARD=2); "
                   "1 = one container per core (the owner's packing directive), below 1 oversubscribes")
    p.add_argument("--vm-usd-per-vcpu-h", type=float, default=0.047,
                   help="c2d-standard-56 at the v1r profile's own VM_USD_PER_H=2.63 (lane B14; NOT $0.38)")
    a = p.parse_args()

    code = Path(a.code)
    # BOTH, and before evalspec: every WaveDef checks its cap at import time, which reads
    # meshpipeline.cad.repair_v4.model_ledger, so `src` has to be importable first (prep runs under python3 -I,
    # so PYTHONPATH is not consulted).
    sys.path.insert(0, str(code / "src"))
    sys.path.insert(0, str(code / "devtools/validation/v4_eval"))
    import evalspec as ev

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    wall = float(a.file_timeout_s) if a.file_timeout_s else float(ev.DEFAULT_FILE_TIMEOUT_S)
    global GROUPS
    if a.groups:
        parsed = []
        for spec in a.groups.split():
            g, arms, sp = spec.split(":")
            parsed.append((g, tuple(arms.split(",")), sp))
        GROUPS = tuple(parsed)
    waves = {"dev": ev.wave(a.dev_wave), "clean": ev.wave(a.clean_wave)}
    splits = {"dev": Path(a.dev_split), "clean": Path(a.clean_split)}
    files = {k: load_split(splits[k], k) for k in splits}
    for k, w in waves.items():
        ran = [arm for g, arms, sp in GROUPS if sp == k for arm in arms]
        if w.split != k or w.wave_type != "normal" or sorted(ran) != sorted(w.arms) or len(set(ran)) != len(ran):
            raise SystemExit(f"REFUSED: wave {w.name!r} ({w.wave_type}, {w.split}, {w.arms}) is not exactly what the "
                             f"{k} groups run ({ran}): every arm of the wave once, normal type")
    if {f["sha256"] for f in files["dev"]} & {f["sha256"] for f in files["clean"]}:
        raise SystemExit("REFUSED: dev and clean share a file")
    staged = parse_ls(Path(a.inputs_ls))
    for k in files:
        missing = [f["sha256"][:12] for f in files[k] if f["sha256"] not in staged]
        if missing:
            raise SystemExit(f"REFUSED: {k} files with no staged input: {missing[:5]}")

    prior_root = require_prior(a.prior_rows)
    prior = prior_costs_per_arm(prior_root, wall)
    unclean = prior_unclean(prior_root)
    four = {s: sum(v.values()) for s, v in prior.items() if set(ARMS4) <= set(v)}
    ratios = [four[s] / ev.predicted_cost_s(staged[s][0]) for s in sorted(unclean) if s in four and s in staged]
    if not ratios:
        raise SystemExit(f"REFUSED: {prior_root} has no file with all four arms measured AND unclean at input, so "
                         "the size model has no calibration. Use a prior wave that finished some dev files.")
    k_dev = statistics.mean(ratios)
    measured_arms = Counter(arm for v in prior.values() for arm in v)
    clean_prior = [sum(prior[f["sha256"]].values()) for f in files["clean"] if f["sha256"] in prior]
    clean_default = statistics.mean(clean_prior) if clean_prior else 60.0

    ceiling = throttle_ceiling(a.route, code)
    if ceiling is None:
        raise SystemExit("REFUSED: cannot read model_throttle.wave_max_in_flight from the commit; a model group "
                         "sized past its in-flight ceiling is refused at run time (lane B11), not here")

    manifest: dict = {"profile": a.profile, "label": a.label, "ts": a.ts,
                      "file_timeout_s": wall, "target_min": a.target_min,
                      "throttle_ceiling_shards": ceiling, "route": a.route,
                      "calibration": {"k_dev_four_arms_per_model_s": k_dev, "from_prior_files": len(ratios),
                                      "clean_default_s": clean_default, "arm_share": ARM_SHARE,
                                      "prior_rows": str(prior_root),
                                      "prior_measured_rows_per_arm": dict(sorted(measured_arms.items())),
                                      "prior_exclusions_skipped": "deadline-unmeasured and reason-lost pairs"},
                      "groups": {}, "splits": {}, "waves": {k: w.name for k, w in waves.items()},
                      "wave_arms": {k: list(w.arms) for k, w in waves.items()}}
    target_s = a.target_min * 60.0
    units: list[dict] = []
    caps_model: dict[str, list[float]] = {}

    # PASS 1: the per-group cost vectors, so the shard counts can be checked against the VMs' capacity BEFORE a
    # single plan is written. A refusal that cannot be acted on is no better than a silent overload, so when the
    # slots do not fit, the smallest target that DOES fit is searched for and named.
    cost_of: dict[str, dict[str, float]] = {}
    source_of: dict[str, dict[str, int]] = {}
    for group, arms, k in GROUPS:
        if not set(arms) <= set(waves[k].arms):
            raise SystemExit(f"REFUSED: group {group} wants {arms}, wave {waves[k].name} runs {waves[k].arms}")
        costs: dict[str, float] = {}
        source = {"prior": 0, "size_model": 0, "clean_default": 0}
        for f in files[k]:
            s = f["sha256"]
            have = [min(prior[s][arm], wall) for arm in arms if s in prior and arm in prior[s]]  # capped at THIS wall
            if len(have) == len(arms):
                costs[s] = sum(have)
                source["prior"] += 1
            elif k == "clean":
                costs[s] = clean_default * sum(ARM_SHARE.get(arm, 0.25) for arm in arms)
                source["clean_default"] += 1
            else:
                costs[s] = min(len(arms) * wall,
                               k_dev * sum(ARM_SHARE.get(arm, 0.25) for arm in arms) * ev.predicted_cost_s(staged[s][0]))
                source["size_model"] += 1
        cost_of[group], source_of[group] = costs, source

    regions = parse_regions(a.regions)
    capacity = sum(v["slots"] for v in vms_for(regions, a.label, a.ts, a.vcpu_per_shard))

    def slots_at(target_minutes: float) -> dict[str, int]:
        """The shard count each group needs to finish inside `target_minutes`, with the model ceiling applied."""
        got = {}
        for group, arms, k in GROUPS:
            n = max(1, min(len(files[k]), math.ceil(sum(cost_of[group].values()) / (target_minutes * 60.0))))
            if any(ev.arm(arm).uses_model for arm in arms):
                n = min(n, ceiling)
            got[group] = n
        return got

    want = slots_at(a.target_min)
    if sum(want.values()) > capacity:
        work_h = sum(sum(c.values()) for c in cost_of.values()) / 3600.0
        feasible = next((t for t in range(int(a.target_min), int(a.target_min) + 24 * 60)
                         if sum(slots_at(t).values()) <= capacity), None)
        raise SystemExit(
            f"REFUSED: {sum(want.values())} shard slots ({want}) do not fit {capacity} on "
            f"{len(vms_for(regions, a.label, a.ts, a.vcpu_per_shard))} VMs. The plan asks for {work_h:.1f} core-h. "
            + (f"--target-min {feasible} fits ({sum(slots_at(feasible).values())} slots) and is the smallest that "
               f"does; " if feasible else "no target under 24 h fits, so the regions are too small for this split; ")
            + "or raise --regions vCPU, or cut the split. I will not quietly pack more work into fewer shards.")

    # PASS 2: the plans, the caps and the units
    for group, arms, k in GROUPS:
        costs, source = cost_of[group], source_of[group]
        total = sum(costs.values())
        model_group = any(ev.arm(arm).uses_model for arm in arms)
        n = want[group]
        plan = ev.build_shard_plan(costs, n)
        plan["cost_source"] = {"unit": f"predicted seconds, arms {list(arms)}", **source,
                               "calibration_k": k_dev, "file_timeout_s": wall}
        path = out / f"plan_{group}.json"
        path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
        ev.load_shard_plan(path)
        load: dict[int, float] = defaultdict(float)
        count: dict[int, int] = defaultdict(int)
        for s, i in plan["assign"].items():
            load[i] += costs[s]
            count[i] += 1
        per_shard = [round(load[i], 1) for i in range(1, n + 1)]
        manifest["groups"][group] = {
            "arms": list(arms), "split": k, "wave": waves[k].name, "n_shards": n,
            "wave_id": f"snap-{a.label}-{group}-{a.ts}" if a.ts else None,
            "uses_model": model_group, "files_per_shard": [count[i] for i in range(1, n + 1)],
            "predicted_shard_s": per_shard, "predicted_total_s": round(total, 1),
            "predicted_wall_min": round(max(per_shard) / 60.0, 1), "plan_digest": ev.plan_digest(plan),
            "capped_by_throttle": bool(model_group and math.ceil(total / target_s) > ceiling)}
        manifest["groups"][group]["cost_source"] = dict(source)
        for i in range(1, n + 1):
            units.append({"group": group, "shard": i, "n": n, "split": k, "wave": waves[k].name,
                          "arms": list(arms), "predicted_s": load[i], "uses_model": model_group})

    # THE MODEL CAP. Only a group with a model arm can spend, so only those groups take from --total-usd; the rest
    # carry a nominal cap they cannot spend (their arms make no calls) and it is excluded from the total.
    model_units = [u for u in units if u["uses_model"]]
    slice_cap = {u["group"]: ev.wave_cap_usd("normal") / u["n"] for u in model_units}
    demand = {}
    for u in model_units:
        files_here = manifest["groups"][u["group"]]["files_per_shard"][u["shard"] - 1]
        weight = 1.0 if u["split"] == "dev" else CLEAN_WEIGHT
        demand[(u["group"], u["shard"])] = files_here * a.file_usd_cap * weight
    floor_ = {key: min(d, a.file_usd_cap) for key, d in demand.items()}
    left = a.total_usd - sum(floor_.values())
    if left < 0:
        raise SystemExit(f"REFUSED: {len(floor_)} model shards x one file's ${a.file_usd_cap} exceeds "
                         f"${a.total_usd}: fewer model shards or a larger total")
    rest = sum(demand[key] - floor_[key] for key in demand)
    scale = min(1.0, left / rest) if rest > 0 else 0.0
    for u in units:
        key = (u["group"], u["shard"])
        if u["uses_model"]:
            u["cap_usd"] = min(slice_cap[u["group"]],
                               math.floor(100 * (floor_[key] + (demand[key] - floor_[key]) * scale)) / 100)
        else:
            u["cap_usd"] = round(a.file_usd_cap, 2)   # nominal: this group's arms make no model calls
    total_caps = round(sum(u["cap_usd"] for u in units if u["uses_model"]), 2)
    if total_caps > a.total_usd + 1e-9:
        raise SystemExit(f"REFUSED: model shard caps sum to ${total_caps} > ${a.total_usd}")
    if any(u["cap_usd"] <= 0 for u in units):
        raise SystemExit("REFUSED: a shard cap rounded to $0: fewer shards or a larger total")

    vms = pack(vms_for(regions, a.label, a.ts, a.vcpu_per_shard), units)
    placed = sum(len(v["units"]) for v in vms)
    if placed != len(units):
        raise SystemExit(f"REFUSED: {placed} of {len(units)} shard slots placed")
    for group in manifest["groups"]:
        caps_model[group] = [u["cap_usd"] for u in sorted((x for v in vms for x in v["units"]
                                                           if x["group"] == group), key=lambda x: x["shard"])]
        (out / f"caps_{group}.txt").write_text(" ".join(f"{c:.2f}" for c in caps_model[group]) + "\n")
        manifest["groups"][group]["caps_usd"] = caps_model[group]

    vcpu = sum(v["vcpu"] for v in vms)
    wall_min = max(g["predicted_wall_min"] for g in manifest["groups"].values())
    boot_min = 9.0 if len({v["region"] for v in vms}) > 1 else 5.0   # a cross-region image pull is the long pole
    vm_h = (wall_min + boot_min + 6.0) / 60.0
    manifest["splits"] = {k: {"path": str(splits[k]), "sha256": sha_of(splits[k]), "files": len(files[k]),
                              "input_bytes": sum(staged[f["sha256"]][0] for f in files[k])} for k in splits}
    manifest["inputs"] = {f["sha256"]: staged[f["sha256"]][1] for k in files for f in files[k]}
    manifest["vms"] = [{k: v[k] for k in ("name", "region", "zones", "machine", "vcpu", "slots", "predicted_s")}
                       for v in vms]
    manifest["projection"] = {
        "shard_slots": len(units), "vcpu": vcpu, "vms": len(vms), "regions": sorted({v["region"] for v in vms}),
        "group_wall_min": {g: manifest["groups"][g]["predicted_wall_min"] for g in manifest["groups"]},
        "run_wall_min": wall_min, "boot_and_pull_min": boot_min,
        "total_wall_min": round(wall_min + boot_min + 6.0, 1),
        "vm_hours": round(len(vms) * vm_h, 2), "vcpu_hours": round(vcpu * vm_h, 1),
        "vm_usd": round(vcpu * vm_h * a.vm_usd_per_vcpu_h, 2),
        "expected_model_usd": round(0.04 * len(files["dev"]) + 0.02 * len(model_units), 2),
        "ops": a.ops if a.ops is not None else int(ev.SHARED_BUDGET.ops), "total_caps_usd": total_caps}
    (out / "vm_plan.json").write_text(json.dumps({"label": a.label, "vms": vms}, indent=1, sort_keys=True) + "\n")
    (out / "launch_manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (out / "inputs.txt").write_text("".join(f"{s} {u}\n" for s, u in sorted(manifest["inputs"].items())))
    pr = manifest["projection"]
    print(f"OK profile={a.profile} groups=" + " ".join(
        f"{g}:{manifest['groups'][g]['n_shards']}s/{manifest['groups'][g]['predicted_wall_min']}min"
        for g in manifest["groups"]))
    print(f"   {pr['shard_slots']} shard slots on {pr['vms']} VMs / {pr['vcpu']} vCPU in {pr['regions']}; "
          f"run {pr['run_wall_min']} min + boot {pr['boot_and_pull_min']} min -> {pr['total_wall_min']} min total")
    print(f"   {pr['vcpu_hours']} vCPU-h ~ ${pr['vm_usd']}; model caps ${total_caps} of ${a.total_usd}; "
          f"ai shards {manifest['groups']['dev_ai']['n_shards']} (ceiling {ceiling})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
