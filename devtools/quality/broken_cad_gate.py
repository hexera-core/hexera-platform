#!/usr/bin/env python3
# Responsibility: MESH-3, the zero-false-stop gate: run every broken-CAD check the FEA stage runs over known-good files and count the stops (must be zero) and the warnings (reported).
# Boundaries: reads files, writes one JSON line per file; never meshes and never heals. Holdout files are counted, never listed.
"""THE ZERO-FALSE-STOP GATE (docs/RULES.md MESH-3).

For each file, exactly what the FEA stage computes: the file canonicalised and read as the geometry
check reads it, the clean check (cad/validity), the solid count, the FEA measurement with its folds,
the suspected defects (cad/suspect_defects), then application/fea_check.blocker (the stop) and
warnings(). A known-good file must never stop.

usage:
  python devtools/quality/broken_cad_gate.py --list FILES.txt --out results.jsonl [--workers N]
      FILES.txt: one path per line, optionally "path<TAB>group" (group "holdout" is never named)
  python devtools/quality/broken_cad_gate.py --summary results.jsonl

The full-corpus run for any change to a check (MESH-3), on a lab VM, never a laptop:
  the clean CFD corpus's gate tier (~/overnight/e2e/index_verify5.jsonl, tier "gate"), the FEA corpus
  (~/fea_corpus/corpus/index.jsonl, dev and holdout), Areen's files and the assembly corpus A set
  (~/asm_corpus/corpus_table.tsv, group A). Zero stops is the bar; warnings are reported.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def check_file(path: str, group: str = "") -> dict:
    """The stage's verdict on one file: {"stop": kind or "", "warnings": n, ...}."""
    t0 = time.time()
    out: dict = {"file": path, "group": group}
    try:
        from meshpipeline.application import fea_check
        from meshpipeline.cad.fea_measure import FACT_KEY, measure_cad, measure_triangles
        from meshpipeline.cad.ingest import canonicalise
        from meshpipeline.cad.validity import check_canonical
        from meshpipeline.contracts.intake_formats import GeometryKind

        with tempfile.TemporaryDirectory(prefix="gate_") as tmp:
            src = Path(path)
            canonical = canonicalise(src, Path(tmp), stem="geometry", ecxml_form="placed")
            validity = dict(check_canonical(src, canonical).as_dict())
            facts: dict = {"validity": validity, **canonical.facts()}
            if canonical.kind is GeometryKind.cad:
                from meshpipeline.cad import suspect_defects as SD
                from meshpipeline.cad.scout import read_cad
                shape = read_cad(canonical.path)
                record = measure_cad(shape, mm_per_unit=1.0)
                # the solids as the scout counts them (cad/scout: TopAbs_SOLID), what the stage reads
                from OCP.TopAbs import TopAbs_SOLID
                from OCP.TopExp import TopExp_Explorer
                ex, n = TopExp_Explorer(shape, TopAbs_SOLID), 0
                while ex.More():
                    n += 1
                    ex.Next()
                facts["solids"] = n
                if n > 1:
                    from meshpipeline.cad.fea_assembly import measure_assembly
                    record = measure_assembly(shape, record, source=canonical.path, mm_per_unit=1.0)
                record["suspects"] = SD.find(shape, record, validity=validity)
            else:
                from meshpipeline.cad.scout_mesh import read_triangles
                record = measure_triangles(read_triangles(canonical.path))
                facts["solids"] = int((validity.get("measured") or {}).get("bodies") or 1)
            facts[FACT_KEY] = record
            stop, kind, _way = fea_check.blocker(facts)
            warns = fea_check.warnings(facts)
            out.update(stop=kind if stop else "", stop_words=stop[:300], warnings=len(warns),
                       warning_words=warns[:4], solids=facts.get("solids"),
                       suspects=[{k: s.get(k) for k in ("kind", "faces", "confirmed")}
                                 for s in record.get("suspects") or []][:6])
    except Exception as exc:  # noqa: BLE001 - a file the gate cannot read is reported, never a pass
        out.update(stop="", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    out["seconds"] = round(time.time() - t0, 1)
    return out


def summary(rows: list[dict]) -> dict:
    stops = [r for r in rows if r.get("stop")]
    return {"files": len(rows), "stops": len(stops), "warned": sum(1 for r in rows if r.get("warnings")),
            "errors": sum(1 for r in rows if r.get("error")),
            "stopped_files": [r["file"] for r in stops if r.get("group") != "holdout"],
            "stopped_holdout": sum(1 for r in stops if r.get("group") == "holdout"),
            "warned_files": [(r["file"], r.get("warning_words")) for r in rows
                             if r.get("warnings") and r.get("group") != "holdout"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--summary")
    a = ap.parse_args()
    if a.summary:
        rows = [json.loads(x) for x in Path(a.summary).read_text().splitlines() if x.strip()]
        print(json.dumps(summary(rows), indent=1))
        return 1 if any(r.get("stop") for r in rows) else 0
    items = []
    for line in Path(a.list).read_text().splitlines():
        if line.strip():
            path, _, group = line.partition("\t")
            items.append((path.strip(), group.strip()))
    done = set()
    outp = Path(a.out)
    if outp.exists():
        done = {json.loads(x)["file"] for x in outp.read_text().splitlines() if x.strip()}
    todo = [it for it in items if it[0] not in done]
    with ProcessPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = [pool.submit(check_file, p, g) for p, g in todo]
        for f in as_completed(futs):
            r = f.result()
            with outp.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            shown = r["file"] if r.get("group") != "holdout" else "(holdout)"
            print(f"{r.get('stop') or '-':12} w={r.get('warnings', 0)} {r['seconds']:6}s {shown}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
