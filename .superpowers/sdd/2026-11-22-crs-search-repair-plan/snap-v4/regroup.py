#!/usr/bin/env python3
"""Snapshot v4: present a GROUPED wave as the one wave per split that report.py and analyze.py already read.

    regroup.py --out <the run's out dir> --splits <dir with plan_*.json> --manifest launch_manifest.json
               [--merged-prefix merged]

WHY THIS EXISTS, and why report.py is NOT changed. v4 runs one shard set per ARM GROUP, so a dev file has its four
arms in four different group directories. `collect.check_partition` refuses a file that appears in two shard
directories of one wave, and `report.load_wave` requires every shard's provenance to name the wave's four arms. Both
checks are RIGHT - they catch a wave cut from two plans, or an arm that silently did not run - so this tool does not
weaken them. It rewrites the groups into ONE directory per split whose partition IS one plan and whose shards carry
all four arms, and then every gate in report.py applies to the merged view unchanged.

Nothing is invented: every row is copied byte for byte, and the merged provenance is built from the groups' own:
  * `arms_run` is the union of what the groups ACTUALLY ran, which must equal the wave's four, or this refuses
    (`arms` is the plan's and is always four, so a gate on it proves nothing);
  * `wave_id` is ONE id derived from the groups' own `snap-<label>-<group>-<ts>`, because report.load_wave refuses
    shards that do not share one;
  * `ledger` is `"shard"` only when every group that ran a model arm carried it, which report.load_wave requires;
  * `exit_code` is the WORST of the contributing shards (a merged shard is finished only if all of them are);
  * `commit`, `image`, `wave`, `wave_type`, `split`, `file_timeout_s` must AGREE across groups, or this refuses;
  * `shard_plan` is the merged plan's digest, and the merged plan is written beside it;
  * `groups` records which (group, shard) each merged shard came from, so the regrouping is auditable;
  * `wave_backoff` is each model-arm shard's real account, carried to exactly ONE merged shard and summed where
    several land together, so the report's throttle totals are the real ones redistributed - a merged shard with no
    account at all would make the whole report UNRELIABLE, and rightly so;
  * `wall_seconds` is the slowest contributing shard's, so "slowest shard" means a shard that really ran that long;
  * `model_throttle` and the per-group ledger paths are kept under `groups`, because they legitimately differ (the
    AI group runs 35 shards, the others do not).

Writes <out>/<merged-prefix>-<split>/shard-I-of-N/{rows.json,rows.jsonl,EXCLUDED.json,PROVENANCE.json} and
<splits>/plan_<merged-prefix>_<split>.json. Prints one line per split. stdlib only.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ARMS4 = ("input_only", "staged", "search_det", "search_ai")
#: provenance keys a merged shard inherits ONLY if every group agrees on them
#: `collect.WALL_KEYS` are BOTH here: the report prints the walls the shards ran with, and a missing
#: `check_valid_timeout_s` showed up as "unrecorded" in the merged report's excluded-stratum line.
MUST_AGREE = ("commit", "image", "machine_type", "wave", "wave_type", "split", "file_timeout_s",
              "check_valid_timeout_s", "h_mm", "ops", "route", "cache_uri", "wave_cap_usd", "slot")
#: provenance keys that legitimately differ per group and are kept under `groups` instead of merged
PER_GROUP = ("arms", "arms_run", "n_shards", "shard", "shard_plan", "model_throttle", "wave_backoff", "ledger",
             "ledger_path", "wave_id", "exit_code", "wall_seconds", "verdict_cache", "started_at", "ended_at")
#: the arms that make paid model calls. report.load_wave REFUSES a wave with one of these whose shards do not
#: carry `ledger: "shard"`, so the merged shard must carry it, and only when every model group really did.
MODEL_ARMS = ("search_ai",)


class RegroupRefused(RuntimeError):
    pass


def combine_guards(accounts: list[dict]) -> dict | None:
    """Several real shards' `wave_backoff` accounts as ONE, for the merged shard that carries their rows. Counters
    are summed and trips unioned, and each real account is used exactly ONCE across the merged shards, so
    report.backoff_summary's totals over the merged wave equal the totals over the real one. Without this the
    merged wave reports "back-off NOT RECORDED" and the whole report is marked UNRELIABLE - the absence of an
    account is not the absence of a back-off, which is exactly what that warning is for."""
    live = [a for a in accounts if isinstance(a, dict) and a.get("guard") == "wave_backoff"]
    if not live:
        return None
    trips: dict[tuple, dict] = {}
    for a in live:
        for t in a.get("trips_seen") or []:
            trips[(t.get("bucket"), t.get("by_shard"))] = t
    return {"guard": "wave_backoff", "threshold": live[0].get("threshold"), "window_s": live[0].get("window_s"),
            "reported_429": sum(int(a.get("reported_429") or 0) for a in live),
            "tripped_here": any(bool(a.get("tripped_here")) for a in live),
            "trips_seen": sorted(trips.values(), key=lambda t: t.get("at", 0.0)),
            "held_s": round(sum(float(a.get("held_s") or 0.0) for a in live), 3),
            "holds": sum(int(a.get("holds") or 0) for a in live),
            "store_errors": sum(int(a.get("store_errors") or 0) for a in live),
            "malformed_markers": sum(int(a.get("malformed_markers") or 0) for a in live),
            "regrouped_from": len(live)}


def merged_id(split: str, by_group: dict[str, str]) -> str:
    """One wave id for the merged wave, derived from the groups' own `snap-<label>-<group>-<ts>` ids: every group
    must share the label and the stamp, and the group name is replaced by the split. A group id that does not parse
    is refused rather than guessed at - the merged id ends up in the report and in the ledger's provenance."""
    heads, tails = set(), set()
    for group, wid in by_group.items():
        parts = str(wid).split("-")
        if len(parts) < 3 or not parts[0].startswith("snap"):
            raise RegroupRefused(f"{split}: group {group}'s wave id {wid!r} is not snap-<label>-<group>-<ts>")
        heads.add("-".join(parts[:-2]))     # snap-<label>
        tails.add(parts[-1])                # <ts>
    if len(heads) != 1 or len(tails) != 1:
        raise RegroupRefused(f"{split}: the groups' wave ids disagree on the label or the stamp: "
                             f"{sorted(by_group.values())}")
    return f"{heads.pop()}-{split}-{tails.pop()}"


def shard_dirs(root: Path) -> list[Path]:
    return sorted((p for p in root.iterdir() if p.is_dir() and p.name.startswith("shard-")),
                  key=lambda p: int(p.name.split("-")[1]))


def read_group(out: Path, wave_id: str) -> list[dict]:
    """Every shard of one group: its provenance, rows and exclusions. A shard with no PROVENANCE.json never
    finished; that is carried as exit_code None so the merge refuses rather than reporting a short wave as whole."""
    root = out / wave_id
    if not root.is_dir():
        raise RegroupRefused(f"group directory {root} is missing: the group did not run")
    got = []
    for d in shard_dirs(root):
        prov_path = d / "PROVENANCE.json"
        prov = json.loads(prov_path.read_text()) if prov_path.is_file() else {"exit_code": None}
        rows = json.loads((d / "rows.json").read_text()) if (d / "rows.json").is_file() else []
        exc = json.loads((d / "EXCLUDED.json").read_text()) if (d / "EXCLUDED.json").is_file() else []
        got.append({"dir": d, "prov": prov, "rows": rows, "excluded": exc})
    if not got:
        raise RegroupRefused(f"group {wave_id} has no shard directories")
    return got


def merge_split(out: Path, splits: Path, split: str, groups: dict, prefix: str,
                wave_arms: tuple[str, ...] = ARMS4) -> dict:
    """One split's groups into one wave directory. The merged partition is the file's FIRST appearance, so a merged
    shard holds every arm of the files it owns and no file is in two merged shards."""
    loaded = {g: read_group(out, meta["wave_id"]) for g, meta in groups.items()}

    # A LOST SHARD used to pass: a group directory with one shard deleted merged happily at exit_code 0. The
    # manifest says how many each group was planned with; anything else is refused.
    for g, meta in sorted(groups.items()):
        want = int(meta.get("n_shards") or 0)
        if want and len(loaded[g]) != want:
            raise RegroupRefused(f"{split}: group {g} has {len(loaded[g])} shard directories, the plan says {want}: "
                                 "a shard is missing or an extra one is present")
    agreed: dict[str, object] = {}
    for key in MUST_AGREE:
        values = {json.dumps(s["prov"].get(key), sort_keys=True) for g in loaded for s in loaded[g]
                  if key in s["prov"]}
        if len(values) > 1:
            raise RegroupRefused(f"{split}: the groups disagree on {key}: {sorted(values)}")
        if values:
            agreed[key] = json.loads(values.pop())
    # `arms` is the PLAN's (always the wave's four, even in a one-arm group), so a gate on it is vacuous. What a
    # shard actually ran is `arms_run`.
    arms_run = sorted({a for g in loaded for s in loaded[g] for a in (s["prov"].get("arms_run") or [])})
    if arms_run != sorted(wave_arms):
        raise RegroupRefused(f"{split}: the groups RAN arms {arms_run} (prov['arms_run']), not the wave's "
                             f"{sorted(wave_arms)}: a merged shard would claim an arm that never ran")

    # report.load_wave refuses a model-arm wave whose shards do not carry ledger: "shard". Carry it only when
    # every group that ran a model arm really did.
    model_groups = [g for g in loaded if set(groups[g].get("arms") or []) & set(MODEL_ARMS)
                    or set(MODEL_ARMS) & {a for s in loaded[g] for a in (s["prov"].get("arms_run") or [])}]
    ledger_value = None
    if model_groups:
        bad = [f"{g}/{s['dir'].name}={s['prov'].get('ledger')!r}" for g in sorted(model_groups)
               for s in loaded[g] if s["prov"].get("ledger") != "shard"]
        if bad:
            raise RegroupRefused(f"{split}: a model arm ran without a shard ledger: {bad[:4]}")
        ledger_value = "shard"

    # ONE wave id for the merged wave: report.load_wave refuses shards that do not share one, and the per-group
    # ids legitimately differ. Built from the groups' own ids (snap-<label>-<group>-<ts>), so it is derived, not
    # invented, and every group must agree on the label and the stamp.
    merged_wave_id = merged_id(split, {g: groups[g]["wave_id"] for g in sorted(loaded)})
    if agreed.get("wave_type") == "held_out":
        # lane C14: a held_out wave is ONE group (prep refuses otherwise), and its once-only slot in the bucket holds
        # THAT group's wave id. A renamed id here would make the report find the slot "held by another wave" and
        # refuse the one measurement that cannot be re-run, so the group's own id is kept.
        if len(loaded) != 1:
            raise RegroupRefused(f"{split}: a held_out wave must be ONE group, found {sorted(loaded)}")
        merged_wave_id = groups[next(iter(loaded))]["wave_id"]

    rows_by_sha: dict[str, list[dict]] = defaultdict(list)
    exc_by_sha: dict[str, list[dict]] = defaultdict(list)
    source: dict[str, list[str]] = defaultdict(list)
    for g in sorted(loaded):
        for s in loaded[g]:
            for r in s["rows"]:
                rows_by_sha[r["sha256"]].append(r)
            for e in s["excluded"]:
                exc_by_sha[e["sha256"]].append(e)
            source[g].append(s["dir"].name)

    # the merged plan: files in file order, round-robin onto the merged shards, so each merged shard holds a
    # similar number of files and the partition is one plan again
    # The merged shard count is the MODEL group's, when there is one: every merged shard then carries exactly one
    # model-arm shard's back-off account and `wall_seconds`, so the report's per-shard throttle and wall numbers
    # are the real ones redistributed, never invented and never dropped.
    n = max((len(loaded[g]) for g in model_groups), default=0) or max(len(loaded[g]) for g in loaded)
    shas = sorted(set(rows_by_sha) | set(exc_by_sha))
    assign = {sha: 1 + (i % n) for i, sha in enumerate(shas)}
    plan = {"version": 1, "n": n, "model": [0.0, 0.0], "assign": dict(sorted(assign.items())),
            "cost_source": {"unit": "merged presentation of per-arm groups", "groups": sorted(loaded)}}
    plan_path = splits / f"plan_{prefix}_{split}.json"
    plan_path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
    import hashlib

    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # each model-group shard to exactly one merged shard (round robin over its own order)
    buckets: dict[int, list[dict]] = {i: [] for i in range(1, n + 1)}
    for g in sorted(model_groups):
        for j, sh in enumerate(loaded[g]):
            buckets[1 + (j % n)].append(sh)
    codes = [s["prov"].get("exit_code") for g in loaded for s in loaded[g]]
    worst = 0
    for c in codes:
        if c is None:
            worst = 99 if worst == 0 else worst   # unfinished: never reported as a whole wave
        elif int(c) != 0:
            worst = int(c)
    dest_root = out / f"{prefix}-{split}"
    for i in range(1, n + 1):
        mine = [sha for sha, where in assign.items() if where == i]
        d = dest_root / f"shard-{i}-of-{n}"
        d.mkdir(parents=True, exist_ok=True)
        rows = [r for sha in sorted(mine) for r in rows_by_sha.get(sha, [])]
        exc = [e for sha in sorted(mine) for e in exc_by_sha.get(sha, [])]
        d.joinpath("rows.json").write_text(json.dumps(rows, sort_keys=True))
        d.joinpath("rows.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        d.joinpath("EXCLUDED.json").write_text(json.dumps(exc, indent=1))
        guard = combine_guards([sh["prov"].get("wave_backoff") for sh in buckets.get(i, [])])
        walls = [float(sh["prov"].get("wall_seconds") or 0.0) for g in loaded for sh in loaded[g]
                 if sh["dir"].name == f"shard-{i}-of-{len(loaded[g])}"]
        prov = {**agreed, "arms": list(wave_arms), "arms_run": list(wave_arms), "wave_id": merged_wave_id,
                "n_shards": n, "shard": f"{i}/{n}", "shard_plan": digest, "exit_code": worst, "regrouped": True,
                **({"ledger": ledger_value} if ledger_value else {}),
                **({"wave_backoff": guard} if guard else {}),
                **({"wall_seconds": round(max(walls), 1)} if walls else {}),
                "groups": {g: {"wave_id": groups[g]["wave_id"], "n_shards": len(loaded[g]),
                               "shards": source[g],
                               "per_shard": [{k: s["prov"].get(k) for k in PER_GROUP} for s in loaded[g]]}
                           for g in sorted(loaded)}}
        d.joinpath("PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str) + "\n")
    return {"split": split, "merged_dir": dest_root.name, "wave_id": merged_wave_id, "ledger": ledger_value,
            "arms_run": arms_run, "shards": n, "files": len(shas),
            "rows": sum(len(v) for v in rows_by_sha.values()), "excluded": sum(len(v) for v in exc_by_sha.values()),
            "exit_code": worst, "plan": plan_path.name, "plan_digest": digest,
            "groups": {g: len(loaded[g]) for g in sorted(loaded)}}


def main() -> int:
    p = argparse.ArgumentParser()
    for k in ("out", "splits", "manifest"):
        p.add_argument(f"--{k}", required=True)
    p.add_argument("--merged-prefix", default="merged")
    a = p.parse_args()
    out, splits = Path(a.out), Path(a.splits)
    manifest = json.loads(Path(a.manifest).read_text())
    by_split: dict[str, dict] = defaultdict(dict)
    for group, meta in manifest["groups"].items():
        by_split[meta["split"]][group] = meta
    summary = {}
    for split in sorted(by_split):
        arms = tuple((manifest.get("wave_arms") or {}).get(split) or ARMS4)  # the WAVE's arms (fast loop: 2)
        summary[split] = merge_split(out, splits, split, by_split[split], a.merged_prefix, arms)
        print(json.dumps(summary[split], sort_keys=True))
    (out / "regroup_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    return 0 if all(s["exit_code"] == 0 for s in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
