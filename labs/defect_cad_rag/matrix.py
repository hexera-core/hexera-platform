"""The action-outcome matrix: per (defect code, operation), how attempts ended and why.

Also the harness numbers the design asks for: file outcomes (swept / crashed / timeout / error),
file and patch timing distributions, and how many defects each family contributes.

Usage:
    .venv/bin/python -m labs.defect_cad_rag.matrix /tmp/dcr-sweep/records.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _dist(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    v = sorted(values)

    def q(p: float) -> float:
        return round(v[min(len(v) - 1, int(p * len(v)))], 5)
    return {"n": len(v), "p50": q(0.5), "p90": q(0.9), "max": round(v[-1], 5),
            "mean": round(sum(v) / len(v), 5)}


def summarise(path: Path, *, exclude: frozenset[str] = frozenset()) -> dict:
    files, defects = [], []
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r.get("kind") == "file":
            files.append(r)
        elif r["file"] not in exclude:
            defects.append(r)

    matrix: dict = defaultdict(Counter)
    reasons: dict = defaultdict(Counter)
    kept_files: dict = defaultdict(set)
    kept_families: dict = defaultdict(set)
    for d in defects:
        for a in d["attempts"]:
            key = f'{d["code"]} -> {a["op"]}'
            matrix[key][a["status"]] += 1
            if a["status"] != "kept":
                reasons[key][(a.get("reason") or a["status"]).split(" (")[0][:60]] += 1
            else:
                kept_files[key].add(d["file"])
                kept_families[key].add(d["family"])
    defect_families = defaultdict(set)
    for d in defects:
        defect_families[d["code"]].add(d["family"])

    return {
        "files": dict(Counter(f.get("outcome") for f in files)),
        "crashes": [{k: f.get(k) for k in ("file", "outcome", "returncode", "error")}
                    for f in files if f.get("outcome") not in ("swept",)],
        "defects_sampled": len(defects),
        "defects_located": sum(int(f.get("defects_located", 0) or 0) for f in files),
        "defects_by_code": dict(Counter(d["code"] for d in defects)),
        "families_by_code": {c: len(s) for c, s in sorted(defect_families.items())},
        "families": len({d["family"] for d in defects}),
        "patch_errors": sum(1 for d in defects if d.get("patch_error")),
        "file_seconds": _dist([f["seconds"] for f in files if f.get("outcome") == "swept"]),
        "isolate_seconds": _dist([d["isolate_s"] for d in defects]),
        "describe_seconds": _dist([d["describe_s"] for d in defects]),
        "attempt_seconds": _dist([a["seconds"] for d in defects for a in d["attempts"]
                                  if "seconds" in a]),
        "matrix": {k: dict(v) for k, v in sorted(matrix.items())},
        "kept_spread": {k: {"files": len(kept_files[k]), "families": len(kept_families[k])}
                        for k in sorted(kept_files)},
        "top_refusals": {k: dict(v.most_common(4)) for k, v in sorted(reasons.items())},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--precapped", type=Path, help="precap.py output; their defects are dropped")
    args = ap.parse_args(argv)
    exclude = frozenset(json.loads(args.precapped.read_text())) if args.precapped else frozenset()
    text = json.dumps(summarise(args.records, exclude=exclude), indent=2)
    if args.json:
        args.json.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
