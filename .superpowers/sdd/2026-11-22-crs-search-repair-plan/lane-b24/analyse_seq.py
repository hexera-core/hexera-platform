#!/usr/bin/env python3
"""Read a fast-loop wave's rows and say what search_seq cost and did, against the other arms.

    analyse_seq.py <merged-dev dir> [<merged-clean dir> ...]

The headline (owner, 2026-11-24) is FULL CHECKS PER FILE. Everything else here exists to say whether the
arm bought its clean files more cheaply than the frontier did, and what stopped it when it failed.

The cheap-vs-full count is the one I flagged before the wave: the rule table is ranked by what the ORACLE won
with, and the oracle judged on the FULL verdict, while `stages` keeps or rolls back on the CHEAP one. A step
line that reads "was rolled back: it lowered the cheap score" is the table's top rule being refused by the
stage, not by the part.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def rows_of(d: Path) -> list[dict]:
    out: list[dict] = []
    for shard in sorted(d.glob("shard-*")):
        f = shard / "rows.json"
        if f.is_file():
            out.extend(json.loads(f.read_text()))
    return out


def clean(r: dict) -> bool:
    return bool(r.get("out_valid") and r.get("out_panel_passes") is not None
                and r.get("out_panel_passes") == r.get("panel_size"))


def main(dirs: list[str]) -> int:
    rows = [r for d in dirs for r in rows_of(Path(d))]
    if not rows:
        print("no rows found")
        return 1
    by_arm: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_arm[str(r.get("arm"))].append(r)

    print(f"{'arm':12} {'rows':>4} {'incl':>4} {'clean':>5} {'wall_s mean':>11} {'full/file':>9} {'local/file':>10}")
    for arm, rs in sorted(by_arm.items()):
        con = [r for r in rs if not r.get("inconclusive")]
        full = [r for r in rs if r.get("full_checks") is not None]
        fpf = f"{sum(r['full_checks'] for r in full) / len(full):.2f}" if full else "-"
        lpf = f"{sum(r.get('local_checks') or 0 for r in full) / len(full):.2f}" if full else "-"
        wall = sum(float(r.get("wall_s") or 0) for r in rs) / max(len(rs), 1)
        print(f"{arm:12} {len(rs):>4} {len(rs) - len(con):>4} {sum(clean(r) for r in con):>5} "
              f"{wall:>11.1f} {fpf:>9} {lpf:>10}")

    seq = by_arm.get("search_seq") or []
    if not seq:
        print("\nno search_seq rows")
        return 0
    print(f"\n--- search_seq, {len(seq)} rows ---")
    tot: collections.Counter = collections.Counter()
    defects: dict[str, collections.Counter] = {k: collections.Counter() for k in ("found", "fixed", "skipped")}
    why: collections.Counter = collections.Counter()
    rollbacks: collections.Counter = collections.Counter()
    first_choice: collections.Counter = collections.Counter()
    for r in seq:
        for k in ("full_checks", "local_checks", "rounds", "ai_picks", "overlap_pairs_fixed",
                  "overlap_pairs_skipped", "resolved_by_earlier_repair", "inconclusive_verdicts"):
            tot[k] += int(r.get(k) or 0)
        for k in ("local_s", "full_s"):
            tot[k] += float(r.get(k) or 0.0)
        for name, key in (("found", "defects_found"), ("fixed", "defects_fixed"), ("skipped", "defects_skipped")):
            defects[name].update(r.get(key) or {})
        why.update(r.get("skipped_why") or {})
        # the trace tells what the FIRST rule tried was, and whether a stage refused it on the cheap score
        for line in ((r.get("triage") or {}).get("seq") or {}).get("lines") or []:
            if "->" not in line or line.strip().startswith(("round", "full check")):
                continue
            if "rolled back: it lowered the cheap score" in line:
                rollbacks[line.split(" -> ")[1].split(" [")[0]] += 1
            outcome = line.rsplit(" -> ", 1)[-1]
            first_choice[outcome.split(" (")[0].split(":")[0].strip()] += 1
    print(f"  full checks {tot['full_checks']} ({tot['full_checks'] / len(seq):.2f} per file)  "
          f"local checks {tot['local_checks']} ({tot['local_checks'] / len(seq):.2f} per file)")
    print(f"  seconds: full {tot['full_s']:.0f}  local {tot['local_s']:.0f}  "
          f"rounds {tot['rounds']} ({tot['rounds'] / len(seq):.2f} per file)")
    print(f"  defects found {dict(defects['found'])}")
    print(f"  defects fixed {dict(defects['fixed'])}")
    print(f"  defects skipped {dict(defects['skipped'])}")
    print(f"  why skipped {dict(why)}")
    print(f"  overlap pairs fixed {tot['overlap_pairs_fixed']} / skipped {tot['overlap_pairs_skipped']}; "
          f"resolved by an earlier repair {tot['resolved_by_earlier_repair']}; "
          f"inconclusive verdicts {tot['inconclusive_verdicts']}; AI picks {tot['ai_picks']}")
    print(f"  step outcomes {dict(first_choice)}")
    if rollbacks:
        print("\n  THE CHEAP-VS-FULL COUNT - repairs the STAGE rolled back for lowering the CHEAP score, while the "
              "oracle that put them in the table judged on the FULL verdict:")
        for repair, n in rollbacks.most_common():
            print(f"    {n:>3}  {repair}")
    else:
        print("\n  no stage rollbacks on the cheap score in these traces")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["."]))
