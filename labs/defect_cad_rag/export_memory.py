"""Bake sweep records into the fix-memory snapshot the engine's shadow posture reads.

One defect row per sampled defect: code, hand-v2 vector, and how each judged operation ended when
tried alone (sweep.py). Standardisation and (code, op) priors are baked at export time so the
engine never recomputes statistics at run time. Precapped files are excluded: their verdicts say
nothing about operations (precap.py).

Usage:
    .venv/bin/python -m labs.defect_cad_rag.export_memory \
        /tmp/dcr-sweep/records.jsonl /tmp/dcr-sweep-dg/records.jsonl \
        --precapped /tmp/dcr-sweep/precapped.json \
        --out src/meshpipeline/cad/repair/data/fixmemory-hand-v2.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

from labs.defect_cad_rag.knn import JUDGED

from meshpipeline.cad.repair.patchdesc import DESCRIPTOR_VERSION


def build(paths: list[Path], exclude: frozenset[str]) -> dict:
    defects = []
    counts: dict = defaultdict(lambda: [0, 0])
    families = set()
    for path in paths:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") != "defect" or not r.get("vector") or r["file"] in exclude:
                continue
            ops = {a["op"]: JUDGED[a["status"]] for a in r["attempts"] if a["status"] in JUDGED}
            if not ops:
                continue
            families.add(r["family"])
            defects.append({"c": r["code"], "v": [round(x, 4) for x in r["vector"]], "ops": ops})
            for op, y in ops.items():
                counts[(r["code"], op)][0] += y
                counts[(r["code"], op)][1] += 1
    n = len(defects[0]["v"])
    mean = [sum(d["v"][i] for d in defects) / len(defects) for i in range(n)]
    sd = [math.sqrt(sum((d["v"][i] - mean[i]) ** 2 for d in defects) / len(defects)) or 1.0
          for i in range(n)]
    return {
        "descriptor_version": DESCRIPTOR_VERSION,
        "defects": defects,
        "mean": [round(x, 6) for x in mean],
        "sd": [round(x, 6) for x in sd],
        "priors": {f"{c}|{o}": round((k + 1) / (m + 2), 6)
                   for (c, o), (k, m) in sorted(counts.items())},
        "provenance": {"records": [str(p) for p in paths], "families": len(families),
                       "excluded_files": sorted(exclude)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="+", type=Path)
    ap.add_argument("--precapped", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    exclude = frozenset(json.loads(args.precapped.read_text())) if args.precapped else frozenset()
    snapshot = build(args.records, exclude)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(gzip.compress(
        json.dumps(snapshot, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(snapshot['defects'])} defects, {len(snapshot['priors'])} (code, op) priors "
          f"-> {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
