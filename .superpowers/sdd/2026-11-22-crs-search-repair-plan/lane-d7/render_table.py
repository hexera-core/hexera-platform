"""Render lane-d7/table.json (ai_table.py) as the D7 markdown table."""
import collections
import glob
import json
import sys

R = sys.argv[1]
t = json.load(open(f"{R}/lane-d7/table.json"))
rows = [json.loads(l) for f in glob.glob(f"{R}/snap-v1/results/snap-v1-dev/*/rows.jsonl")
        + glob.glob(f"{R}/snap-v1/results/snap-v1-clean/*/rows.jsonl") for l in open(f)]
by = collections.defaultdict(dict)
for r in rows:
    by[r["sha256"]][r["arm"]] = r


def start_fail(sha):
    st = by[sha].get("staged") or by[sha]["input_only"]
    names = sorted(m["name"].replace("gmsh_", "g_").replace("netgen_occ", "ng")
                   for m in (st.get("triage") or {}).get("panel_failing", []))
    return ",".join(names) or "-"


def layer(sha):
    io = by[sha]["input_only"]
    return "valid" if not io.get("in_valid") else "panel" if (io.get("triage") or {}).get("panel_failing") else "clean"


hdr = ("| file | input fails at | failing members at search start | turns | $ | AI proposals | schema/tool err | "
       "declined by op | dup | judged (cheap) | cheap pass | full checks | full pass | stop | det proposals | "
       "det judged | det stop |")
lines = [hdr, "|" + "---|" * 17]
tot = collections.Counter()
for r in sorted(t, key=lambda r: -r["usd"]):
    sha = next(s for s in by if s.startswith(r["sha"]))
    stop = r["stop"] + (f" ({r['ai_stop']})" if r["ai_stop"] else "")
    lines.append(f"| {r['file']} | {layer(sha)} | {start_fail(sha)} | {r['turns']} | {r['usd']:.4f} | {r['ai_props']} | "
                 f"{r['schema_err']} | {r['ai_declined']} | {r['ai_dup']} | {r['ai_judged']} | {r['cheap_pass']} | "
                 f"{r['cheap_judged_row']} | {r['full_pass']} | {stop} | {r['det_props']} | {r['det_judged']} | {r['det_stop']} |")
    for k in ("turns", "ai_props", "schema_err", "ai_declined", "ai_dup", "ai_judged", "cheap_pass", "cheap_judged_row",
              "full_pass", "det_props", "det_judged"):
        tot[k] += r[k] or 0
    tot["usd"] += r["usd"]
lines.append(f"| **total, 27 files** | | | **{tot['turns']}** | **{tot['usd']:.4f}** | **{tot['ai_props']}** | "
             f"**{tot['schema_err']}** | **{tot['ai_declined']}** | **{tot['ai_dup']}** | **{tot['ai_judged']}** | "
             f"**{tot['cheap_pass']}** | **{tot['cheap_judged_row']}** | **{tot['full_pass']}** | | **{tot['det_props']}** | "
             f"**{tot['det_judged']}** | |")
open(f"{R}/lane-d7/table.md", "w").write("\n".join(lines) + "\n")
print("\n".join(lines[-2:]))
