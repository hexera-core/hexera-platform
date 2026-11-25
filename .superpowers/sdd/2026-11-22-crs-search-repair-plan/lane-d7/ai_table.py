"""Lane D7: per-file table of what search_ai did in snapshot v1, against search_det on the same file."""
import collections
import glob
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows = [json.loads(l) for f in sorted(glob.glob(str(root / "snap-v1-dev/*/rows.jsonl")))
        + sorted(glob.glob(str(root / "snap-v1-clean/*/rows.jsonl"))) for l in open(f)]
ledger = collections.defaultdict(lambda: {"admits": 0, "usd": 0.0})
for f in glob.glob(str(root / "ledger/*.jsonl")):
    for l in open(f):
        e = json.loads(l)
        if e.get("t") == "spend":
            ledger[e["file_id"]]["admits"] += 1
            ledger[e["file_id"]]["usd"] += float(e.get("usd") or 0)
by = collections.defaultdict(dict)
for r in rows:
    split = "clean" if "/snap-v1-clean/" in "" else None
    by[r["sha256"]][r["arm"]] = r


def search_counts(r):
    s = ((r.get("triage") or {}).get("search") or {})
    ops = s.get("ops") or {}
    out = collections.Counter()
    for key, d in ops.items():
        ai, det = int(d.get("ai") or 0), int(d.get("det") or 0)
        decl = sum((d.get("declined") or {}).values())
        fail = sum((d.get("failed") or {}).values())
        inval = sum((d.get("invalid") or {}).values())
        out["ai_props"] += ai
        out["det_props"] += det
        who = "ai" if ai and not det else "det" if det and not ai else "mixed"
        out[f"{who}_keys"] += 1
        for name, n in (("invalid", inval), ("declined", decl), ("failed", fail), ("duplicate", d.get("duplicate") or 0),
                        ("evaluated", d.get("evaluated") or 0), ("applied", d.get("ops_applied") or 0),
                        ("inconclusive", d.get("inconclusive") or 0), ("skipped_budget", d.get("skipped_budget") or 0)):
            out[f"{who}_{name}"] += int(n)
    return s, out


table = []
for sha, arms in by.items():
    a = arms.get("search_ai")
    if not a or not a.get("model_usd"):
        continue
    s, c = search_counts(a)
    ai = s.get("ai") or {}
    cf = a.get("cheap_full") or {}
    d = arms.get("search_det") or {}
    ds, dc = search_counts(d) if d else ({}, collections.Counter())
    dcf = d.get("cheap_full") or {}
    table.append({
        "file": a["name"].rsplit("/", 1)[-1][:34], "sha": sha[:8], "turns": ai.get("turns"), "usd": a["model_usd"],
        "ledger_usd": round(ledger[sha]["usd"], 6), "ledger_admits": ledger[sha]["admits"],
        "ai_props": c["ai_props"], "schema_err": int(ai.get("candidate_errors") or 0) + int(ai.get("tool_errors") or 0),
        "ai_invalid": c["ai_invalid"], "ai_declined": c["ai_declined"] + c["ai_failed"], "ai_dup": c["ai_duplicate"],
        "ai_judged": c["ai_evaluated"], "ai_applied": c["ai_applied"], "mixed_keys": c["mixed_keys"],
        "mixed_judged": c["mixed_evaluated"],
        "cheap_judged_row": sum(cf.values()), "cheap_pass": cf.get("cheap_pass_full_pass", 0) + cf.get("cheap_pass_full_fail", 0),
        "full_pass": cf.get("cheap_pass_full_pass", 0) + cf.get("cheap_fail_full_pass", 0),
        "cheap_pass_full_fail": cf.get("cheap_pass_full_fail", 0),
        "stop": (s.get("stop") or {}).get("reason"), "ai_stop": ai.get("stop_reason"), "declared_unfixable": ai.get("declared_unfixable"),
        "out_clean": a.get("out_clean"), "in_pass": a.get("in_panel_passes"), "out_pass": a.get("out_panel_passes"),
        "det_props": dc["det_props"], "det_judged": dc["det_evaluated"], "det_full_checks": sum(dcf.values()), "det_cheap_pass": dcf.get("cheap_pass_full_pass", 0) + dcf.get("cheap_pass_full_fail", 0),
        "det_full_pass": dcf.get("cheap_pass_full_pass", 0) + dcf.get("cheap_fail_full_pass", 0),
        "det_stop": ((ds.get("stop") or {}).get("reason")), "det_out_pass": d.get("out_panel_passes"), "det_out_clean": d.get("out_clean"),
        "ai_open_sites": s.get("open_sites"),
    })
json.dump(table, open(sys.argv[2], "w"), indent=1)
print(len(table), "files")
