#!/usr/bin/env python3
# Responsibility: Gather the re-probe shards, refuse anything not comparable (a missing or failed
#                 shard, mixed commits or images), merge their rows, report cheap/full agreement and
#                 counts, and cut the splits.
# Owns: shard discovery, the comparability gate, the merge, `report`, and the corpora/v4 write.
# Boundaries: no CAD, no mesher. Rows come from `reprobe`; an EXCLUDED entry (infrastructure failure)
#             is reported as such and is never added to the failing, unclean or clean counts.
"""Collect the re-probe.

    collect.py --from <dir> [--shards N]            # shards in <dir>/shard-I-of-N/, then the splits
    collect.py --from <dir> --report-only           # numbers only; write no split
    collect.py --pull gs://bucket/eval/reprobe-<c12> --to <dir>

`--shards N` is the planned shard count: a wave with fewer shards present is REFUSED, because merging
a partial wave would give every stratum the wrong size.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_arm as _ra  # noqa: E402  (loads evalspec, reprobe, pool under their module names)

evalspec = _ra.evalspec
rp = _ra.reprobe_mod

SHARD_RE = re.compile(r"^shard-(\d+)-of-(\d+)$")


class CollectRefused(RuntimeError):
    pass


def load_shards(root: Path, expected: int | None = None) -> list[dict]:
    """Every shard under `root`, with its provenance and rows. Refuses missing, failed or mixed shards."""
    shards: list[dict] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and SHARD_RE.match(p.name)):
        i, n = (int(x) for x in SHARD_RE.match(d.name).groups())  # type: ignore[union-attr]
        prov_path = d / "PROVENANCE.json"
        if not prov_path.is_file():
            raise CollectRefused(f"{d.name}: no PROVENANCE.json (shard unfinished)")
        prov = json.loads(prov_path.read_text())
        if prov.get("exit_code") != 0:
            raise CollectRefused(f"{d.name}: exit_code {prov.get('exit_code')!r}: {str(prov.get('error', ''))[-200:]}")
        shards.append({"i": i, "n": n, "prov": prov, "dir": d,
                       "rows": [rp.ProbeRow(**r) for r in json.loads((d / "rows.json").read_text())],
                       "cheap": rp.read_rows(d / "cheap.jsonl"),
                       "excluded": json.loads((d / "EXCLUDED.json").read_text())})
    if not shards:
        raise CollectRefused(f"no shard-I-of-N directories under {root}")
    counts = {s["n"] for s in shards}
    if len(counts) != 1:
        raise CollectRefused(f"shards disagree on the shard count: {sorted(counts)}")
    n = counts.pop()
    if expected is not None and expected != n:
        raise CollectRefused(f"the plan has {expected} shards but the shards say {n}")
    missing = sorted(set(range(1, n + 1)) - {s["i"] for s in shards})
    if missing:
        raise CollectRefused(f"missing shard(s) {missing[:10]} of {n}: a partial wave would mis-size every stratum")
    for key in ("commit", "image"):
        values = {str(s["prov"].get(key) or "") for s in shards}
        if len(values) != 1 or "" in values:
            raise CollectRefused(f"shards do not share one {key}: {sorted(values)}")
    return shards


def merge(shards: list[dict]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    cheap: dict[str, Any] = {}
    excluded: list[dict] = []
    for s in shards:
        rows.update({r.sha256: r for r in s["rows"]})
        cheap.update({r.sha256: r for r in s["cheap"]})
        excluded += s["excluded"]
    final = [r for r in rows.values() if rp.is_counted(r)]  # an uncounted row never reaches a count
    full = [r for r in final if r.verdict_summary.get("tier") == "full"]
    return {"rows": final, "cheap": list(cheap.values()), "full": full, "excluded": excluded}


def report(merged: dict[str, Any], splits: Any | None) -> dict:
    rows = merged["rows"]
    failing = [r for r in rows if r.first_failing_layer is not None]
    no_solids = [r for r in failing if rp.is_no_solids(r)]
    return {
        "probed_conclusive": len(rows),
        "excluded_inconclusive": len(merged["excluded"]),
        "excluded_by_reason": dict(Counter(x["reason"].split(":")[0] for x in merged["excluded"])),
        "failing": len(failing) - len(no_solids),
        "no_solids_not_repairable": len(no_solids),
        "by_layer": dict(Counter(str(r.first_failing_layer) for r in rows)),
        "by_source": dict(Counter(r.source for r in rows)),
        "failing_by_stratum": dict(Counter("/".join(rp._stratum(r)) for r in failing if not rp.is_no_solids(r))),
        "agreement": rp.agreement(merged["cheap"], merged["full"]),
        "splits": None if splits is None else {
            "dev": len(splits.dev), "heldout": len(splits.heldout), "clean": len(splits.clean),
            "shortfall": splits.shortfall, "strata": splits.strata},
    }


def pull_command(src: str, dest: Path) -> list[str]:
    return ["gcloud", "storage", "rsync", "-r", src, str(dest)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="src", help="directory holding shard-I-of-N/")
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--out-dir", default=str(rp.SPLITS_DIR))
    parser.add_argument("--pull", default="")
    parser.add_argument("--to", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.pull:
        cmd = pull_command(args.pull, Path(args.to or "."))
        print(" ".join(shlex.quote(c) for c in cmd))
        return 0 if args.dry_run else subprocess.run(cmd).returncode
    if not args.src:
        parser.error("--from is required")
    try:
        merged = merge(load_shards(Path(args.src), args.shards))
    except CollectRefused as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 1
    splits = None if args.report_only else rp.build_splits(merged["rows"], out_dir=args.out_dir)
    print(json.dumps(report(merged, splits), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
