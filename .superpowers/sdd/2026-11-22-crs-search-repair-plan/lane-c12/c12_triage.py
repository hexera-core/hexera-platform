"""Lane C12: v3b's 42 OVERLAP files, per file and arm. Did a body op apply, did the output reach VALID, CLEAN, and if
valid but not clean, which member fails on which faces: NEW (made or changed by an op: not in the input's own face
fingerprints, on the workspace grid) vs OLD (the input's face, untouched) vs unlocated.
Usage: python c12_triage.py <rows.jsonl> <fp.json> <ovl.json> [traces dir] > out.md"""
from __future__ import annotations

import collections
import glob
import json
import os
import sys

BODY_OPS = ("conformalise", "fuse")


def load(path):
    return [json.loads(x) for x in open(path) if x.strip()]


def faces_of(member, fps):
    new, old, unloc = [], [], 0
    for loc in member.get("located") or []:
        e = loc.get("entity")
        if e is None:
            unloc += 1
        elif e in fps:
            old.append(fps[e])
        else:
            new.append(e[:14])
    return new, old, unloc


def trace_path(traces, sha):
    if not traces:
        return None
    hits = glob.glob(os.path.join(traces, "**", sha[:12] + ".json"), recursive=True)
    return hits


def main(rows_p, fp_p, ovl_p, traces=None):
    rows = load(rows_p)
    fp = json.load(open(fp_p))
    ovl = {o["sha256"]: o for o in json.load(open(ovl_p))}
    by = collections.defaultdict(dict)
    for r in rows:
        if r["sha256"] in ovl:
            by[r["sha256"]][r["arm"]] = r
    out = ["# C12: v3b overlap files", "",
           f"rows read {len(rows)}; overlap files with any row {len(by)} of {len(ovl)}", "",
           "| file | bodies | arm | body op applied | valid in->out | clean | panel in->out | failing: member kind NEW/OLD/unlocated |",
           "|---|---|---|---|---|---|---|---|"]
    tally = collections.Counter()
    seam = collections.Counter()
    for sha, arms in sorted(by.items(), key=lambda kv: ovl[kv[0]]["name"]):
        fps = (fp.get(sha) or {}).get("faces") or {}
        nb = (fp.get(sha) or {}).get("bodies")
        for arm in ("input_only", "staged", "search_det", "search_ai"):
            r = arms.get(arm)
            if r is None:
                continue
            if r.get("inconclusive"):
                out.append(f"| {ovl[sha]['name'][-40:]} | {nb} | {arm} | - | inconclusive ({r.get('reason')}) | | | |")
                tally[(arm, "inconclusive")] += 1
                continue
            t = r.get("triage") or {}
            ops = ((t.get("search") or {}).get("ops") or {})
            applied = {k: v.get("evaluated", 0) for k, v in ops.items() if k in BODY_OPS and v.get("evaluated")}
            fails = []
            for m in t.get("panel_failing") or []:
                new, old, un = faces_of(m, fps)
                fails.append(f"{m['name']} {m.get('error_kind')} N{len(new)}/O{len(old)}/U{un}"
                             + (f" old={old[:3]}" if old else ""))
                if r.get("out_valid") and not r.get("out_clean") and arm.startswith("search") and applied:
                    seam[(m["name"], "new" if new and not old else "old" if old and not new else
                          "both" if new else "unlocated")] += 1
            vin, vout = r.get("in_valid"), r.get("out_valid")
            tally[(arm, "clean" if r.get("out_clean") else "valid" if vout else "invalid")] += 1
            if applied:
                tally[(arm, "body_op_applied")] += 1
                if vout and not vin:
                    tally[(arm, "body_op_and_made_valid")] += 1
            out.append(f"| {ovl[sha]['name'][-40:]} | {nb} | {arm} | {applied or '-'} | {vin}->{vout} | "
                       f"{r.get('out_clean')} | {r.get('in_panel_passes')}->{r.get('out_panel_passes')} | "
                       f"{'; '.join(fails) or '-'} |")
    # INSTRUMENT VALIDATION, always printed: on input_only rows no op ran, so every located face must be the input's
    # own (OLD). Any NEW there means the fingerprints and the rows are not on the same grid and the split is void.
    v_old = v_new = 0
    for sha, arms in by.items():
        fps = (fp.get(sha) or {}).get("faces") or {}
        r = arms.get("input_only")
        for m in ((r or {}).get("triage") or {}).get("panel_failing") or []:
            new, old, _ = faces_of(m, fps)
            v_new += len(new)
            v_old += len(old)
    out += ["", f"**Instrument validation (input_only rows, no op ran): located faces OLD {v_old}, NEW {v_new}** - "
            + ("PASS: NEW must be 0." if v_new == 0 else "FAIL: the NEW/OLD split below is NOT trustworthy.")]
    out += ["", "## tallies (files)", ""] + [f"- {a} {k}: {n}" for (a, k), n in sorted(tally.items())]
    out += ["", "## valid-not-clean search outputs where a body op applied: failing member by face class", ""]
    out += [f"- {m} {c}: {n}" for (m, c), n in sorted(seam.items())]
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
