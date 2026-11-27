#!/usr/bin/env python3
# Responsibility: a PARTIAL re-probe of the cleared pool: draw a seeded, stratified sample (source x file-size band)
#                 that never touches the held-out files, write it as a subset pool the existing wave tooling runs
#                 unchanged, and cut PROVISIONAL dev / clean splits (v2) from what the wave measured.
# Owns: the sample design (per-source quotas, size bands, held-out and v1 exclusions), the subset pool manifest, the
#       v2 split files and the per-stratum summary.
# Boundaries: no gcloud, no CAD, no model. Stratified allocation and the within-stratum shuffle are reprobe.py's own
#             (`_apportion`, `_shuffled`); the splits are `reprobe.build_splits` with heldout=0. The probe itself is
#             wave.py / launch_fleet.py / run_arm.py --mode reprobe over the subset pool, at a separate prefix.
"""Partial re-probe (reprobe-800).

    partial_reprobe.py sample --sizes inputs_ls.txt --out-dir DIR [--n 800]
        -> DIR/sample_manifest.json, DIR/pool_subset.json (version-3 pool manifest: only the sampled files)
    partial_reprobe.py splits --from <dir with shard-I-of-N/> --manifest DIR/sample_manifest.json --out-dir DIR
        -> DIR/PROVISIONAL_dev_v2.json, DIR/PROVISIONAL_clean_v2.json, DIR/probe_records.jsonl,
           DIR/excluded.jsonl, DIR/SUMMARY.json

THE SAMPLE. The pool is ~91% abc_fail, which Task 43 and Lane D measured as ~never clean (0 of 82 measured), while
abc_pass certified clean 12 of 29 times. A pool-proportional 800 would hold ~70 abc_pass and so ~25 clean files; the
clean control needs 30-40 plus margin, so abc_pass is OVERSAMPLED (stated weight below) and nist / occt are taken
whole (they are tiny and each file is a distinct kind of part). No source is oversampled toward FAILING: there is no
per-stratum prior that would justify it, and abc_fail is already failing at ~100% among measured files. Within a
source, files are allocated over file-size bands in proportion to the band's population (largest remainder), then
drawn by a seeded shuffle of sha256-sorted entries. Size is the pre-probe stratifier because it is the one feature
known for every pool file without reading CAD (evalspec's cost model uses it for the same reason); the diagonal
bands Task 43 / measure_reprobe used need a CAD read of every candidate.

HELD-OUT. Task 43's PROVISIONAL_heldout.json files, and every file sharing a part group with one of them, are never
sampled. There is no hash rule for the full corpus's held-out (build_splits draws it from the full re-probe's
failing set), so the FULL wave's split cut must exclude this sample's v2 dev/clean files from its held-out; the
sample manifest lists them for that purpose.

V1. The 38 files of PROVISIONAL_dev.json + PROVISIONAL_clean.json are INCLUDED in the probe and marked `v1`, so the
re-probe re-measures them (a run-to-run / commit-to-commit flip check), and EXCLUDED from v2's draw, so v2 is
additive to v1.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_arm as _ra  # noqa: E402

reprobe_mod = _ra.reprobe_mod
pool_mod = _ra.pool_mod
evalspec = _ra.evalspec

SEED = 20261124
V1_DIR = HERE.parent / "corpora/v4/provisional_sample_task43"
HELDOUT_FILES = (V1_DIR / "PROVISIONAL_heldout.json",)
V1_FILES = (V1_DIR / "PROVISIONAL_dev.json", V1_DIR / "PROVISIONAL_clean.json")
#: File-size bands, bytes (lower bound inclusive).
SIZE_BANDS: tuple[tuple[float, float], ...] = ((0, 1e5), (1e5, 1e6), (1e6, 1e7), (1e7, math.inf))
#: "all" takes every eligible file of the source; an int is the number drawn at random (v1 files excluded from the
#: draw and added on top); abc_fail takes whatever is left of --n.
SOURCE_QUOTA: dict[str, int | str] = {"nist": "all", "occt": "all", "abc_pass": 240, "abc_fail": "rest"}
#: A panel member whose min SICN lies within this factor of the floor is NEAR THRESHOLD: a re-run can flip it (the
#: measured case: 0.0042 against the 0.01 floor flipping gmsh_alt).
NEAR_FACTOR = 3.0
MIN_SICN = 0.01
PROVISIONAL = ("PROVISIONAL v2, DRAWN FROM A PARTIAL (reprobe-800) RE-PROBE OF THE CLEARED POOL. For dev-wave "
               "tuning and the clean control only. The real splits come from the full re-probe; do not report "
               "these as the benchmark.")


def size_band(size: int) -> str:
    for lo, hi in SIZE_BANDS:
        if lo <= size < hi:
            return f"{int(lo)}-{'inf' if math.isinf(hi) else int(hi)}"
    raise ValueError(size)


def read_sizes(path: Path) -> dict[str, int]:
    """`gcloud storage ls -l <prefix>/inputs/**` output -> {sha256: bytes} (the object under <sha256>/)."""
    out: dict[str, int] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[-1].startswith("gs://") and "/inputs/" in parts[-1]:
            sha = parts[-1].split("/inputs/", 1)[1].split("/", 1)[0]
            out[sha] = int(parts[0])
    return out


def split_files(paths: tuple[Path, ...]) -> list[dict]:
    return [f for p in paths for f in json.loads(p.read_text())["files"]]


def draw_sample(entries: list[Any], sizes: dict[str, int], *, n: int, heldout: list[dict], v1: list[dict],
                seed: int = SEED, quota: dict[str, int | str] | None = None) -> dict:
    """The sample manifest: every chosen file with its source, size band and marks, plus the design."""
    quota = dict(SOURCE_QUOTA if quota is None else quota)
    held_sha = {f["sha256"] for f in heldout}
    held_groups = {f["group"] for f in heldout if f.get("group")}
    v1_sha = {f["sha256"] for f in v1}
    if held_sha & v1_sha:
        raise ValueError("a v1 dev/clean file is also held-out")
    eligible = [e for e in entries if e.sha256 not in held_sha and not (e.group and e.group in held_groups)]
    unstaged = [e.sha256 for e in eligible if e.sha256 not in sizes]
    if unstaged:
        raise ValueError(f"{len(unstaged)} cleared file(s) are not staged (e.g. {unstaged[0][:12]})")
    by_source: dict[str, list[Any]] = defaultdict(list)
    for e in eligible:
        by_source[e.source].append(e)

    chosen: dict[str, tuple[Any, str]] = {}  # sha -> (entry, how)
    for e in eligible:
        if e.sha256 in v1_sha:
            chosen[e.sha256] = (e, "v1")
    missing_v1 = v1_sha - set(chosen)
    if missing_v1:
        raise ValueError(f"{len(missing_v1)} v1 file(s) are not in the cleared pool")
    for source, rule in quota.items():
        if rule == "all":
            for e in by_source.get(source, []):
                chosen.setdefault(e.sha256, (e, "all"))
    fixed = {s: q for s, q in quota.items() if isinstance(q, int)}
    rest = [s for s, q in quota.items() if q == "rest"]
    if len(rest) > 1:
        raise ValueError("at most one source takes the rest")
    left = n - len(chosen) - sum(fixed.values())
    if rest:
        fixed[rest[0]] = left
    elif left < 0:
        raise ValueError(f"the quotas ask for more than --n {n}")

    design: dict[str, dict] = {}
    for source, want in sorted(fixed.items()):
        pop = [e for e in by_source.get(source, []) if e.sha256 not in chosen]
        bands: dict[str, list[Any]] = defaultdict(list)
        for e in pop:
            bands[size_band(sizes[e.sha256])].append(e)
        alloc = reprobe_mod._apportion({b: len(v) for b, v in bands.items()}, want)
        for b in sorted(bands):
            for e in reprobe_mod._shuffled(bands[b], f"reprobe-800/{source}/{b}", seed)[: alloc[b]]:
                chosen[e.sha256] = (e, "drawn")
        design[source] = {"drawn": want, "population_after_exclusions": len(pop),
                          "band_population": {b: len(v) for b, v in sorted(bands.items())},
                          "band_drawn": {b: alloc[b] for b in sorted(alloc)}}
    pool_counts = Counter(e.source for e in entries)
    files = []
    for sha, (e, how) in sorted(chosen.items()):
        files.append({"sha256": sha, "name": e.name, "source": e.source, "group": e.group, "size": sizes[sha],
                      "size_band": size_band(sizes[sha]), "how": how, "v1": sha in v1_sha})
    by_src = Counter(f["source"] for f in files)
    return {
        "seed": seed, "n": len(files), "quota": dict(quota), "size_bands": [list(b) for b in SIZE_BANDS],
        "pool_cleared_by_source": dict(sorted(pool_counts.items())),
        "sampled_by_source": dict(sorted(by_src.items())),
        "sampling_fraction": {s: by_src[s] / pool_counts[s] for s in sorted(by_src)},
        "strata": dict(sorted(Counter(f"{f['source']}/{f['size_band']}" for f in files).items())),
        "design": design,
        "excluded_heldout": {"sha256": sorted(held_sha), "groups": sorted(held_groups),
                             "pool_files_removed": len(entries) - len(eligible)},
        "v1_included": len(v1_sha),
        "files": files,
    }


def subset_pool(pool_path: Path, shas: set[str]) -> dict:
    manifest = json.loads(pool_path.read_text())
    manifest["files"] = [f for f in manifest["files"] if f["sha256"] in shas]
    if len(manifest["files"]) != len(shas):
        raise ValueError("a sampled sha is missing from the pool manifest")
    return manifest


def cmd_sample(a: argparse.Namespace) -> int:
    entries, _ = reprobe_mod.split_allowed(pool_mod.read_pool(Path(a.pool)))
    entries = [e for e in entries if e.source in evalspec.CLEARED_SOURCES]
    manifest = draw_sample(entries, read_sizes(Path(a.sizes)), n=a.n, heldout=split_files(HELDOUT_FILES),
                           v1=split_files(V1_FILES), seed=a.seed)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "sample_manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    pool = subset_pool(Path(a.pool), {f["sha256"] for f in manifest["files"]})
    (out / "pool_subset.json").write_text(json.dumps(pool, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: manifest[k] for k in ("n", "sampled_by_source", "strata")}, indent=1))
    return 0


# --------------------------------------------------------------------------- splits

def near_threshold(row: Any) -> bool:
    for m in row.verdict_summary.get("panel", []):
        s = m.get("min_sicn")
        if s is not None and MIN_SICN / NEAR_FACTOR <= float(s) <= MIN_SICN * NEAR_FACTOR:
            return True
    return False


def outcome(row: Any) -> str:
    if reprobe_mod.is_unbounded(row):
        return "unbounded"
    if reprobe_mod.is_no_solids(row):
        return "no_solids"
    if not reprobe_mod.is_counted(row):
        return "uncounted"
    return "clean" if row.first_failing_layer is None else "failing"


def kind_of(row: Any) -> str:
    o = outcome(row)
    return o if o != "failing" else f"{row.first_failing_layer}/{reprobe_mod.coarse_kind(row)}"


def load_collected(root: Path) -> tuple[list[Any], list[dict], list[dict]]:
    rows, excluded, prov = [], [], []
    for shard in sorted(root.glob("shard-*-of-*")):
        rows += [reprobe_mod.ProbeRow(**r) for r in json.loads((shard / "rows.json").read_text())]
        excluded += json.loads((shard / "EXCLUDED.json").read_text())
        prov.append(json.loads((shard / "PROVENANCE.json").read_text()))
    return rows, excluded, prov


def write_split(path: Path, name: str, rows: list[Any], meta: dict) -> None:
    body = {"PROVISIONAL": PROVISIONAL, "split": name, **meta, "files": [asdict(r) for r in rows]}
    path.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")


def label_of(row: dict | Any) -> str:
    """One comparable label for a row (dict or ProbeRow): clean, failing <layer>/<coarse kind>, or the outcome."""
    r = row if not isinstance(row, dict) else reprobe_mod.ProbeRow(**row)
    return kind_of(r)


def write_v1r(out: Path, rows: list[Any], excluded: list[dict], v1_paths: tuple[Path, ...] = V1_FILES) -> dict:
    """The v1 splits (Task 43) with every file's row REPLACED by this wave's engine-form row. Membership is v1's own
    (the same files, so snapshot v1 reruns on the same set); a file whose label changed is listed in the header, and
    a file this wave could not judge (excluded) is dropped and listed, never kept on its old original-form row."""
    new = {r.sha256: r for r in rows}
    gone = {x["sha256"]: x["reason"] for x in excluded}
    report: dict[str, Any] = {}
    for path in v1_paths:
        body = json.loads(path.read_text())
        changes, dropped, kept = [], [], []
        for f in body["files"]:
            sha = f["sha256"]
            if sha not in new:
                dropped.append({"sha256": sha, "name": f["name"], "reason": gone.get(sha, "not probed")})
                continue
            before, after = label_of(f), label_of(new[sha])
            pb = [m.get("status") for m in f["verdict_summary"].get("panel", [])]
            pa = [m.get("status") for m in new[sha].verdict_summary.get("panel", [])]
            if before != after or pb != pa:
                changes.append({"sha256": sha, "name": f["name"], "v1_label": before, "v1r_label": after,
                                "v1_panel": pb, "v1r_panel": pa, "label_changed": before != after})
            kept.append(new[sha])
        name = body["split"]
        target = out / path.name.replace(".json", "_v1r.json")
        meta = {k: v for k, v in body.items() if k not in ("files", "PROVISIONAL", "split")}
        meta.update({"relabel_of": path.name, "relabel_note": "engine-form rows (reprobe.engine_form) from this wave; "
                     "membership is v1's", "label_changes": changes, "dropped_no_engine_form_label": dropped})
        write_split(target, name, kept, meta)
        report[name] = {"files": len(kept), "label_changed": sum(c["label_changed"] for c in changes),
                        "panel_changed_only": sum(not c["label_changed"] for c in changes), "dropped": len(dropped),
                        "changes": changes, "dropped_files": dropped}
    return report


def cmd_splits(a: argparse.Namespace) -> int:
    manifest = json.loads(Path(a.manifest).read_text())
    by_sha = {f["sha256"]: f for f in manifest["files"]}
    rows, excluded, prov = load_collected(Path(a.from_dir))
    seen = {r.sha256 for r in rows} | {x["sha256"] for x in excluded}
    if seen != set(by_sha):
        raise SystemExit(f"REFUSED: collected {len(seen)} files, the manifest has {len(by_sha)} "
                         f"(missing {len(set(by_sha) - seen)}, extra {len(seen - set(by_sha))})")
    held = set(manifest["excluded_heldout"]["sha256"])
    if held & seen:
        raise SystemExit("REFUSED: a held-out file was probed")
    eligible = [r for r in rows if not by_sha[r.sha256]["v1"]]
    splits = reprobe_mod.build_splits(eligible, dev=a.dev, heldout=0, clean=a.clean, seed=a.seed)
    out = Path(a.out_dir)
    meta = {"seed": splits.seed, "strata": splits.strata, "shortfall": splits.shortfall,
            "excluded_no_solids": splits.excluded_no_solids, "excluded_uncounted": splits.excluded_uncounted,
            "excluded_unbounded": splits.excluded_unbounded, "source_run": a.wave_id}
    write_split(out / "PROVISIONAL_dev_v2.json", "dev", splits.dev, meta)
    write_split(out / "PROVISIONAL_clean_v2.json", "clean", splits.clean, meta)

    in_dev = {r.sha256 for r in splits.dev}
    in_clean = {r.sha256 for r in splits.clean}
    with (out / "probe_records.jsonl").open("w") as h:
        for r in sorted(rows, key=lambda r: r.sha256):
            f = by_sha[r.sha256]
            h.write(json.dumps({**asdict(r), "size": f["size"], "size_band": f["size_band"], "v1": f["v1"],
                                "outcome": outcome(r), "kind": kind_of(r),
                                "failing_members": reprobe_mod.failing_members(r),
                                "near_threshold": near_threshold(r),
                                "split_v2": "dev" if r.sha256 in in_dev else "clean" if r.sha256 in in_clean else None},
                               sort_keys=True) + "\n")
    with (out / "excluded.jsonl").open("w") as h:
        for x in sorted(excluded, key=lambda x: x["sha256"]):
            f = by_sha[x["sha256"]]
            h.write(json.dumps({**x, "size": f["size"], "size_band": f["size_band"], "v1": f["v1"]},
                               sort_keys=True) + "\n")

    def per(key: Any) -> dict:
        table: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            table[key(by_sha[r.sha256])][outcome(r)] += 1
        for x in excluded:
            table[key(by_sha[x["sha256"]])]["excluded"] += 1
        return {k: dict(sorted(v.items())) for k, v in sorted(table.items())}

    summary = {
        "PROVISIONAL": PROVISIONAL, "wave_id": a.wave_id, "seed_split": a.seed, "seed_sample": manifest["seed"],
        "files": len(by_sha), "rows": len(rows), "excluded": len(excluded),
        "outcomes": dict(Counter(outcome(r) for r in rows)),
        "by_source": per(lambda f: f["source"]),
        "by_stratum": per(lambda f: f"{f['source']}/{f['size_band']}"),
        "failure_kinds": dict(Counter(kind_of(r) for r in rows if outcome(r) == "failing").most_common()),
        "failure_family_fine": dict(Counter(r.family for r in rows if outcome(r) == "failing").most_common()),
        "failing_members_panel": dict(sorted(Counter(reprobe_mod.failing_members(r) for r in rows
                                                     if r.first_failing_layer == "panel").items())),
        "excluded_by_reason": dict(Counter(x["reason"].split(":")[0] for x in excluded).most_common()),
        "excluded_by_source": dict(Counter(x["source"] for x in excluded)),
        "near_threshold": {"all": sum(near_threshold(r) for r in rows),
                           "dev_v2": sum(near_threshold(r) for r in splits.dev),
                           "clean_v2": sum(near_threshold(r) for r in splits.clean),
                           "rule": f"any panel member min SICN within x{NEAR_FACTOR:g} of the {MIN_SICN} floor"},
        "single_member_failures": {"all": sum(1 for r in rows if r.first_failing_layer == "panel"
                                              and reprobe_mod.failing_members(r) == 1),
                                   "dev_v2": sum(1 for r in splits.dev if r.first_failing_layer == "panel"
                                                 and reprobe_mod.failing_members(r) == 1)},
        "splits": {"dev": len(splits.dev), "clean": len(splits.clean), "shortfall": splits.shortfall,
                   "dev_by_source": dict(Counter(r.source for r in splits.dev)),
                   "clean_by_source": dict(Counter(r.source for r in splits.clean))},
        "commits": sorted({p.get("commit", "") for p in prov}), "images": sorted({p.get("image", "") for p in prov}),
        "shards": len(prov),
    }
    summary["v1r"] = write_v1r(out, rows, excluded, tuple(Path(p) for p in a.v1_split)) if a.v1_split else None
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in ("files", "rows", "excluded", "outcomes", "splits")}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--sizes", required=True, help="gcloud storage ls -l output of the staged inputs")
    s.add_argument("--pool", default=str(pool_mod.OUT))
    s.add_argument("--n", type=int, default=800)
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument("--out-dir", required=True)
    s.set_defaults(fn=cmd_sample)
    p = sub.add_parser("splits")
    p.add_argument("--from", dest="from_dir", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--dev", type=int, default=150)
    p.add_argument("--clean", type=int, default=40)
    p.add_argument("--seed", type=int, default=reprobe_mod.SEED)
    p.add_argument("--wave-id", default="reprobe-800")
    p.add_argument("--v1-split", action="append", default=None,
                   help="a v1 split json to relabel with this wave's rows (-> <name>_v1r.json); default: Task 43's dev "
                        "and clean")
    p.set_defaults(fn=cmd_splits)
    a = ap.parse_args(argv)
    if a.cmd == "splits" and a.v1_split is None:
        a.v1_split = [str(p) for p in V1_FILES]
    return int(a.fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
