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
import re
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


def outcomes(rows: list[dict]) -> dict[str, dict]:
    """Per (defect kind, repair): how often it was TRIED, how often it APPLIED, and the causes of every
    non-application, counted. THIS IS MEASUREMENT ONLY - the demotion is a policy that reads it, and it is not
    decided here. One place, so the policy cannot be argued from a different count than the report shows.

    A cause is the op's own words with the numbers stripped, so `nothing at or under 1211.9 mm` and `nothing at
    or under 974.0 mm` are one cause and not two.
    """
    out: dict[str, dict] = {}
    for r in rows:
        for line in ((r.get("triage") or {}).get("seq") or {}).get("lines") or []:
            t = line.strip()
            if " -> " not in t or t.startswith(("round", "full check")):
                continue
            kind = t.split(" on ")[0]
            parts = t.split(" -> ")
            if len(parts) < 3:
                continue                      # "no repair [...]" and the cluster summary carry no repair
            # the key is the OP SEQUENCE, with the derived parameters stripped: the table ranks ops, and keying
            # on the params made every call its own row - which hid the three repairs that did apply under
            # param-laden keys while the bare op read applied 0.
            # THE KEY IS THE OP SEQUENCE. Each op's own name is the text before its first brace, so the derived
            # parameters drop out whatever they contain - and they contain both " [" (a face-pair list) and
            # nested braces (`run_stage`'s `params={}`), each of which defeated a cheaper strip.
            repair = " + ".join(op.split("{")[0].split(" [")[0].strip() for op in parts[1].split(" + ")) or "?"
            tail = parts[-1]
            entry = out.setdefault(kind, {}).setdefault(repair, {"tried": 0, "applied": 0, "causes": {}})
            entry["tried"] += 1
            if tail.startswith("not applied"):
                cause = re.sub(r"[-+]?\d*\.?\d+(?:e[-+]?\d+)?", "N", tail.split("not applied: ", 1)[-1])
                cause = re.sub(r"\s+", " ", cause).strip()[:120]
                entry["causes"][cause] = entry["causes"].get(cause, 0) + 1
            else:
                entry["applied"] += 1
    return out


def main(dirs: list[str]) -> int:
    emit = None
    if "--json" in dirs:
        at = dirs.index("--json")
        emit, dirs = dirs[at + 1], dirs[:at] + dirs[at + 2:]
    rows = [r for d in dirs for r in rows_of(Path(d))]
    if not rows:
        print("no rows found")
        return 1
    by_arm: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_arm[str(r.get("arm"))].append(r)

    # LEAD-3's ruling: `inconclusive verdicts` sits BESIDE the headline, so a file whose round-end panel was
    # never measured cannot make the arm look cheap. A row with one is a row whose full-check count is short by
    # however many panels said nothing, and the column is printed whether it is zero or not.
    print(f"{'arm':12} {'rows':>4} {'incl':>4} {'clean':>5} {'wall_s mean':>11} {'full/file':>9} "
          f"{'incv':>4} {'local/file':>10}")
    for arm, rs in sorted(by_arm.items()):
        con = [r for r in rs if not r.get("inconclusive")]
        full = [r for r in rs if r.get("full_checks") is not None]
        fpf = f"{sum(r['full_checks'] for r in full) / len(full):.2f}" if full else "-"
        lpf = f"{sum(r.get('local_checks') or 0 for r in full) / len(full):.2f}" if full else "-"
        incv = str(sum(int(r.get("inconclusive_verdicts") or 0) for r in full)) if full else "-"
        wall = sum(float(r.get("wall_s") or 0) for r in rs) / max(len(rs), 1)
        print(f"{arm:12} {len(rs):>4} {len(rs) - len(con):>4} {sum(clean(r) for r in con):>5} "
              f"{wall:>11.1f} {fpf:>9} {incv:>4} {lpf:>10}")

    # B25: there are TWO sequential arms now. Their blocks are printed apart, because the whole question is
    # whether letting the model pick where the table cannot decide buys anything - and a merged block would
    # hide it.
    seq_arms = [a for a in sorted(by_arm) if any(r.get("full_checks") is not None for r in by_arm[a])]
    if not seq_arms:
        print("\nno sequential-arm rows")
        return 0
    for arm in seq_arms:
        _seq_block(arm, [r for r in by_arm[arm] if r.get("full_checks") is not None])
    if emit:
        measured = {arm: outcomes([r for r in by_arm[arm] if r.get("full_checks") is not None])
                    for arm in seq_arms}
        Path(emit).write_text(json.dumps(measured, indent=1, sort_keys=True) + "\n")
        print(f"\nper (defect kind, repair) tried/applied/causes written to {emit} - the demotion's evidence, "
              f"as measurement and not yet as policy")
    return 0


def _seq_block(arm: str, seq: list[dict]) -> None:
    print(f"\n--- {arm}, {len(seq)} rows ---")
    tot: collections.Counter = collections.Counter()
    defects: dict[str, collections.Counter] = {k: collections.Counter() for k in ("found", "fixed", "skipped")}
    why: collections.Counter = collections.Counter()
    rollbacks: collections.Counter = collections.Counter()
    tried: collections.Counter = collections.Counter()
    applied: collections.Counter = collections.Counter()
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
            kind = line.strip().split(" on ")[0]
            repair = line.split(" -> ")[1].split(" [")[0] if " -> " in line else "?"
            outcome = line.rsplit(" -> ", 1)[-1]
            tried[(kind, repair)] += 1
            if "rolled back: it lowered the cheap score" in line:
                # the table's entry refused by the STAGE's cheap-tier veto, not by the part
                rollbacks[(kind, repair)] += 1
            elif not outcome.startswith("not applied"):
                applied[(kind, repair)] += 1
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
        print("\n  THE CHEAP-VS-FULL COUNT, per DEFECT KIND and repair - the table's entry refused by the STAGE "
              "for lowering the CHEAP score, while the oracle that put it there judged on the FULL verdict.")
        print("  This is the evidence for the demotion LEAD-3 approved: an entry rolled back here is demoted "
              "below the alternatives that APPLIED, for that kind.")
        print(f"    {'rolled back':>11} {'of tried':>8}  {'applied':>7}  defect kind -> repair")
        for (kind, repair), n in rollbacks.most_common():
            print(f"    {n:>11} {tried[(kind, repair)]:>8}  {applied[(kind, repair)]:>7}  {kind} -> {repair}")
        print(f"  affected: {len({kind for kind, _repair in rollbacks})} defect kind(s), "
              f"{sum(rollbacks.values())} of {sum(tried.values())} steps")
    else:
        print("\n  no stage rollbacks on the cheap score in these traces (nothing to demote)")
    ai = [r for r in seq if (((r.get("triage") or {}).get("seq") or {}).get("ai"))]
    if ai:
        # B25: was the model ever REACHED on this table, and did its answers stand?
        asked = sum(int(((r["triage"]["seq"]["ai"]) or {}).get("asked") or 0) for r in ai)
        answered = sum(int(((r["triage"]["seq"]["ai"]) or {}).get("answered") or 0) for r in ai)
        ignored = sum(int(((r["triage"]["seq"]["ai"]) or {}).get("ignored_not_offered") or 0) for r in ai)
        failed = sum(int(((r["triage"]["seq"]["ai"]) or {}).get("failed") or 0) for r in ai)
        spent = sum(float(r.get("model_usd") or 0.0) for r in seq)
        stops = collections.Counter(str(((r["triage"]["seq"]["ai"]) or {}).get("stop_reason")) for r in ai)
        print(f"  THE AI PICK: asked {asked} on {len(ai)} rows, answered {answered}, ignored-not-offered "
              f"{ignored}, failed rounds {failed}, ${spent:.2f}; stops {dict(stops)}")
        if asked == 0:
            print("    THE MODEL WAS NEVER REACHED: on this table only a declared TIE asks it, so this arm and "
                  "the det one ran the same repairs. Any difference between their rows is noise, not the AI.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["."]))
