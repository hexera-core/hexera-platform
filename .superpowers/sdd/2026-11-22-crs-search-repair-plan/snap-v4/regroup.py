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
  * `arms` is the union, which must equal the wave's four, or this refuses;
  * `exit_code` is the WORST of the contributing shards (a merged shard is finished only if all of them are);
  * `commit`, `image`, `wave`, `wave_type`, `split`, `file_timeout_s` must AGREE across groups, or this refuses;
  * `shard_plan` is the merged plan's digest, and the merged plan is written beside it;
  * `groups` records which (group, shard) each merged shard came from, so the regrouping is auditable;
  * `model_throttle` and `ledger` are kept per group under `groups`, because they legitimately differ (the AI group
    runs 37 shards, the others do not).

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
MUST_AGREE = ("commit", "image", "machine_type", "wave", "wave_type", "split", "file_timeout_s", "h_mm", "ops",
              "route", "cache_uri", "wave_cap_usd")
#: provenance keys that legitimately differ per group and are kept per group instead of merged
PER_GROUP = ("arms", "n_shards", "shard", "shard_plan", "model_throttle", "ledger_path", "wave_id", "exit_code",
             "wall_seconds", "verdict_cache", "started_at", "ended_at")


class RegroupRefused(RuntimeError):
    pass


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


def merge_split(out: Path, splits: Path, split: str, groups: dict, prefix: str) -> dict:
    """One split's groups into one wave directory. The merged partition is the file's FIRST appearance, so a merged
    shard holds every arm of the files it owns and no file is in two merged shards."""
    loaded = {g: read_group(out, meta["wave_id"]) for g, meta in groups.items()}
    agreed: dict[str, object] = {}
    for key in MUST_AGREE:
        values = {json.dumps(s["prov"].get(key), sort_keys=True) for g in loaded for s in loaded[g]
                  if key in s["prov"]}
        if len(values) > 1:
            raise RegroupRefused(f"{split}: the groups disagree on {key}: {sorted(values)}")
        if values:
            agreed[key] = json.loads(values.pop())
    arms = sorted({a for g in loaded for s in loaded[g] for a in (s["prov"].get("arms") or [])})
    if arms and arms != sorted(ARMS4):
        raise RegroupRefused(f"{split}: the groups ran arms {arms}, not the wave's {sorted(ARMS4)}: a merged shard "
                             "would claim an arm that never ran")

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
    n = max(len(loaded[g]) for g in loaded)
    shas = sorted(set(rows_by_sha) | set(exc_by_sha))
    assign = {sha: 1 + (i % n) for i, sha in enumerate(shas)}
    plan = {"version": 1, "n": n, "model": [0.0, 0.0], "assign": dict(sorted(assign.items())),
            "cost_source": {"unit": "merged presentation of per-arm groups", "groups": sorted(loaded)}}
    plan_path = splits / f"plan_{prefix}_{split}.json"
    plan_path.write_text(json.dumps(plan, indent=1, sort_keys=True) + "\n")
    import hashlib

    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

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
        prov = {**agreed, "arms": list(ARMS4), "n_shards": n, "shard": f"{i}/{n}", "shard_plan": digest,
                "exit_code": worst, "regrouped": True,
                "groups": {g: {"wave_id": groups[g]["wave_id"], "n_shards": len(loaded[g]),
                               "shards": source[g],
                               "per_shard": [{k: s["prov"].get(k) for k in PER_GROUP} for s in loaded[g]]}
                           for g in sorted(loaded)}}
        d.joinpath("PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str) + "\n")
    return {"split": split, "merged_dir": dest_root.name, "shards": n, "files": len(shas),
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
        summary[split] = merge_split(out, splits, split, by_split[split], a.merged_prefix)
        print(json.dumps(summary[split], sort_keys=True))
    (out / "regroup_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    return 0 if all(s["exit_code"] == 0 for s in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
