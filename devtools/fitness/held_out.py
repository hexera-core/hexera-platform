#!/usr/bin/env python3
# Responsibility: Check the engine recommendation on shapes the fitness table never saw.
# Boundaries: offline evaluation over the committed table's rows; it changes no data and no code.
"""THE HELD-OUT CHECK - does the recommendation pick an engine that passes shapes it never saw?

For every group of shapes (one shape at a time, or a whole family at a time - the harder test: no
shape of that kind is left in the table), the table is rebuilt WITHOUT them and each held-out
geometry (one case in one file format, run on two or more engines) is recommended an engine from
the engines the lab ran on it. The recommendation counts as right when that engine passed there.
It is compared with what the system did before - the first engine in the declared ladder order -
and with the best any choice could do (some engine passed).

usage: PYTHONPATH=src python devtools/fitness/held_out.py [--table PATH] [--by shape|family] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "src" / "meshpipeline" / "engines" / "fitness_table.json"


def evaluate(data: dict, *, by: str = "family") -> dict:
    """{'cases': n, 'recommended_passed': n, 'ladder_passed': n, 'any_passed': n, 'rows': [...]}
    over every held-out geometry with runs on at least two engines."""
    from meshpipeline.cad.shape_traits import ShapeTraits
    from meshpipeline.engines.capability import ladder_order
    from meshpipeline.engines.fitness import recommend, table_from

    rows = [r for r in data.get("rows") or () if isinstance(r, dict)]
    # every shape belongs to ONE group: its own, or the family of its first row (a shape the
    # workstreams filed under two family spellings is still held out once)
    family: dict[str, str] = {}
    for r in rows:
        family.setdefault(r["shape"], str(r.get("family") or r["shape"]))
    groups: dict[str, set] = defaultdict(set)
    for shape, fam in family.items():
        groups[fam if by == "family" else shape].add(shape)
    out_rows = []
    for g, shapes in sorted(groups.items()):
        table = table_from(data, exclude_shapes=shapes)
        held: dict[tuple, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            if r["shape"] in shapes:
                held[(r["case"], r["format"])][r["engine"]] = r
        for (case, fmt), by_engine in sorted(held.items()):
            if len(by_engine) < 2:
                continue
            any_row = next(iter(by_engine.values()))
            traits = ShapeTraits.from_dict(any_row.get("traits"))
            engines = sorted(by_engine)
            rec = recommend(traits, engines, table=table, brief=any_row.get("brief") or {})
            ladder = [e for e in ladder_order(traits.flow) if e in by_engine] or engines
            passed = {e for e, r in by_engine.items() if r.get("status") == "pass"}
            out_rows.append({
                "group": g, "case": case, "format": fmt, "flow": traits.flow,
                "engines": engines, "passed": sorted(passed),
                "recommended": rec.engine, "recommended_passed": rec.engine in passed,
                "ladder_first": ladder[0], "ladder_passed": ladder[0] in passed,
                "any_passed": bool(passed), "shape": rec.shape})
    n = len(out_rows)
    return {"by": by, "cases": n,
            "recommended_passed": sum(r["recommended_passed"] for r in out_rows),
            "ladder_passed": sum(r["ladder_passed"] for r in out_rows),
            "any_passed": sum(r["any_passed"] for r in out_rows),
            "rows": out_rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", default=str(TABLE))
    ap.add_argument("--by", choices=("shape", "family"), default="family")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = evaluate(json.loads(Path(args.table).read_text()), by=args.by)
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    n = res["cases"] or 1
    print(f"held out by {res['by']}: {res['cases']} geometries run on 2+ engines")
    print(f"  recommended engine passed: {res['recommended_passed']} ({100 * res['recommended_passed'] / n:.0f}%)")
    print(f"  declared ladder's first passed: {res['ladder_passed']} ({100 * res['ladder_passed'] / n:.0f}%)")
    print(f"  some engine passed: {res['any_passed']} ({100 * res['any_passed'] / n:.0f}%)")
    misses = [r for r in res["rows"] if r["any_passed"] and not r["recommended_passed"]]
    for r in misses:
        print(f"  miss: {r['case']} [{r['format']}] recommended {r['recommended']}, passed {r['passed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
