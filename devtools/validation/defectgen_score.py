#!/usr/bin/env python3
# Responsibility: Score a finished corpus run against hexera-cad-defect-generation ground truth:
#                 detection, taxonomy-separated misses, triage reachability and repair outcome,
#                 per generator operator and in aggregate.
# Owns: the operator->inspector-code taxonomy table, the row/answer join, and the score JSON. It
#       runs nothing and repairs nothing.
# Boundaries: stdlib only. It reads the rows JSON repair_corpus.py --json wrote and an answers
#             campaign directory from the generator repo; it writes one JSON file when asked.

"""Score a generated-defect campaign after the engine has run.

INTEGRITY RULE. This scorer runs only after the engine run; nothing in it may be imported by the
engine or the corpus runner. The answers directory is ground truth the engine must never see, so
the join between a row and its answer.json happens here, post hoc, and nowhere else. Keeping the
module stdlib-only (no meshpipeline import) makes an accidental reverse dependency loud in review.

WHY THE TAXONOMY TABLE. The generator injects design-intent geometric damage (thin walls,
ligaments, debris, voids); our inspector reports B-rep validity defects (small faces, open shells).
Most generator operators have no inspector code that claims to see them, so an undetected thin_wall
is "a defect class we do not claim", while an undetected short_edge_sliver is "a missed detectable
defect". Collapsing those into one detection rate would flatter nobody and mislead everybody; the
table below is the declared, reviewable boundary between the two.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -- the declared taxonomy -----------------------------------------------------------------------
# Generator operator -> inspector DefectCode values we expect the inspector to raise for it.
# An empty set means: valid-B-rep damage we do not claim to detect; detection there is incidental.
# Keys ending in "_" are prefix families (fluid_*, interface_*) for operators we have not seen yet.
TAXONOMY: dict[str, frozenset[str]] = {
    # Sliver/flash geometry leaves tiny faces and edges the inspector measures directly.
    "short_edge_sliver": frozenset({"small_edge", "small_face"}),
    "residual_flash": frozenset({"small_face", "small_edge"}),
    "micro_slot": frozenset({"small_face", "small_edge"}),
    "pin_hole": frozenset({"small_face", "small_edge"}),
    # Topology/tolerance pilots map onto the codes the repair loop already heals.
    "missing_face": frozenset({"open_shell"}),
    "duplicate_face": frozenset({"duplicate_surface_data", "non_manifold_surface"}),
    "tolerance_inflation": frozenset({"tolerance_outlier"}),
    # Design-intent damage on a still-valid B-rep: not claimed. The generator proves it with probe
    # flips and volume deltas; the B-rep inspector has no code for "the wall is too thin".
    "thin_wall": frozenset(),
    "extreme_thin_ligament": frozenset(),
    "residual_boss": frozenset(),
    "internal_void": frozenset(),
    "isolated_debris": frozenset(),
    "fluid_obstruction": frozenset(),
    "fluid_": frozenset(),
    "interface_": frozenset(),
}

# Triage routes bucketed the way the benchmark doc reports reachability.
_ROUTE_BUCKETS = {"conservative_repair": "repair", "ask_customer": "review", "abstain": "review",
                  "manual_cleanup": "manual_cleanup", "mesh_as_is": "no_op"}

# Corpus-runner outcome -> repair result, for rows triage actually routed to repair. "refused" and
# "no_change" both mean every attempt rolled back or was discarded; "changed_without_fixing" means
# the written file differs without fixing anything, which is damage until proven otherwise.
_REPAIR_OUTCOMES = {"repaired": "repaired", "refused": "rolled_back", "no_change": "rolled_back",
                    "changed_without_fixing": "damaged"}


def expected_codes(operator: str) -> frozenset[str] | None:
    """Inspector codes we claim for a generator operator; None when the operator is unknown."""
    if operator in TAXONOMY:
        return TAXONOMY[operator]
    for prefix, codes in TAXONOMY.items():
        if prefix.endswith("_") and operator.startswith(prefix):
            return codes
    return None


# -- loading -------------------------------------------------------------------------------------

def load_rows(path: Path) -> dict[str, dict]:
    """Rows from repair_corpus.py --json, keyed by file stem (the generator's case_id)."""
    rows = json.loads(Path(path).read_text())
    return {Path(row["file"]).stem: row for row in rows}


def load_answers(campaign_dir: Path) -> dict[str, dict]:
    """Every answers/<campaign>/<case>/answer.json, keyed by case directory name."""
    answers = {}
    for answer_path in sorted(Path(campaign_dir).glob("*/answer.json")):
        answers[answer_path.parent.name] = json.loads(answer_path.read_text())
    return answers


# -- scoring -------------------------------------------------------------------------------------

def _score_case(case_id: str, row: dict, answer: dict) -> dict:
    detected = sorted(set(row.get("defects_before") or []))
    defects = []
    for labelled in answer.get("defects", []):
        operator = (labelled.get("causal_edit") or {}).get("operator", "?")
        codes = expected_codes(operator) or frozenset()
        expected = bool(codes)
        defects.append({
            "id": labelled.get("id"),
            "operator": operator,
            "category": labelled.get("category"),
            "expected_detectable": expected,
            "detected_expected": expected and bool(codes & set(detected)),
            "expected_repair": labelled.get("expected_repair"),
        })
    route = row.get("route", "")
    return {
        "case_id": case_id,
        "damaged": bool(defects),
        "detected": bool(detected),
        "detected_codes": detected,
        "route": route,
        "route_bucket": _ROUTE_BUCKETS.get(route, "other"),
        "outcome": row.get("outcome", ""),
        "attempted": route == "conservative_repair",
        "defects": defects,
    }


def score(rows_by_stem: dict[str, dict], answers: dict[str, dict]) -> dict:
    """Join rows to answers and aggregate. Unmatched entries on either side are reported, never
    scored: a score over a partial join must say it is one."""
    joined = sorted(set(rows_by_stem) & set(answers))
    cases = {cid: _score_case(cid, rows_by_stem[cid], answers[cid]) for cid in joined}

    damaged = [c for c in cases.values() if c["damaged"]]
    clean = [c for c in cases.values() if not c["damaged"]]
    false_positives = sum(1 for c in clean if c["detected"])

    operators: dict[str, dict] = {}
    unknown: set[str] = set()
    for case in cases.values():
        seen_here: set[str] = set()
        for defect in case["defects"]:
            op = defect["operator"]
            if expected_codes(op) is None:
                unknown.add(op)
            entry = operators.setdefault(op, {
                "expected_detectable": defect["expected_detectable"],
                "expected_codes": sorted(expected_codes(op) or ()),
                "defects": 0, "cases": 0, "detected_any": 0, "detected_expected": 0,
                "missed_detectable": 0,
                "routes": {"repair": 0, "review": 0, "manual_cleanup": 0, "no_op": 0},
                "repair": {"attempted": 0, "repaired": 0, "rolled_back": 0,
                           "damaged": 0, "failed": 0},
            })
            entry["defects"] += 1
            if defect["expected_detectable"]:
                if defect["detected_expected"]:
                    entry["detected_expected"] += 1
                else:
                    entry["missed_detectable"] += 1
            if op in seen_here:
                continue  # case-level tallies count each case once per operator
            seen_here.add(op)
            entry["cases"] += 1
            entry["detected_any"] += int(case["detected"])
            bucket = case["route_bucket"]
            if bucket in entry["routes"]:
                entry["routes"][bucket] += 1
            else:
                entry["routes"][bucket] = 1
            if case["attempted"]:
                entry["repair"]["attempted"] += 1
                result = _REPAIR_OUTCOMES.get(case["outcome"], "failed")
                entry["repair"][result] += 1

    aggregate = {
        "cases_scored": len(cases),
        "rows_without_answers": sorted(set(rows_by_stem) - set(answers)),
        "answers_without_rows": sorted(set(answers) - set(rows_by_stem)),
        "detection": {
            "damaged_cases": len(damaged),
            "detected": sum(1 for c in damaged if c["detected"]),
            "detection_rate": (round(sum(1 for c in damaged if c["detected"]) / len(damaged), 4)
                               if damaged else None),
            "clean_cases": len(clean),
            "false_positives": false_positives,
            "false_positive_rate": (round(false_positives / len(clean), 4) if clean else None),
        },
        "operators": {op: operators[op] for op in sorted(operators)},
        "unknown_operators": sorted(unknown),
        "taxonomy": {op: sorted(codes) for op, codes in sorted(TAXONOMY.items())},
    }
    return {"cases": cases, "aggregate": aggregate}


# -- reporting -----------------------------------------------------------------------------------

def render_table(result: dict) -> str:
    agg = result["aggregate"]
    det = agg["detection"]
    lines = [
        f"cases scored: {agg['cases_scored']}"
        f" (rows without answers: {len(agg['rows_without_answers'])},"
        f" answers without rows: {len(agg['answers_without_rows'])})",
        f"detection: {det['detected']}/{det['damaged_cases']} damaged cases"
        f" (rate {det['detection_rate']}); false positives:"
        f" {det['false_positives']}/{det['clean_cases']}"
        f" (rate {'n/a' if det['false_positive_rate'] is None else det['false_positive_rate']})",
        "",
        f"{'operator':<24}{'claim':<14}{'defects':>8}{'cases':>6}{'det_any':>8}{'det_exp':>8}"
        f"{'missed':>7} | {'repair':>7}{'review':>7}{'manual':>7}{'no_op':>6}"
        f" | {'att':>4}{'rep':>4}{'rb':>4}{'dmg':>4}",
    ]
    for op, e in agg["operators"].items():
        claim = "expected" if e["expected_detectable"] else "not-claimed"
        det_exp = str(e["detected_expected"]) if e["expected_detectable"] else "-"
        missed = str(e["missed_detectable"]) if e["expected_detectable"] else "-"
        r, rep = e["routes"], e["repair"]
        lines.append(
            f"{op:<24}{claim:<14}{e['defects']:>8}{e['cases']:>6}{e['detected_any']:>8}"
            f"{det_exp:>8}{missed:>7} | {r.get('repair', 0):>7}{r.get('review', 0):>7}"
            f"{r.get('manual_cleanup', 0):>7}{r.get('no_op', 0):>6}"
            f" | {rep['attempted']:>4}{rep['repaired']:>4}{rep['rolled_back']:>4}"
            f"{rep['damaged']:>4}")
    if agg["unknown_operators"]:
        lines.append(f"\nunknown operators (not in the taxonomy table):"
                     f" {', '.join(agg['unknown_operators'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a corpus run's rows JSON against a generated-defect answers campaign.")
    parser.add_argument("rows", type=Path, help="rows JSON from repair_corpus.py --json")
    parser.add_argument("answers", type=Path, help="answers/<campaign> directory")
    parser.add_argument("--json", type=Path, default=None,
                        help="write the full score (cases + aggregate) to this file")
    args = parser.parse_args(argv)

    result = score(load_rows(args.rows), load_answers(args.answers))
    print(render_table(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
