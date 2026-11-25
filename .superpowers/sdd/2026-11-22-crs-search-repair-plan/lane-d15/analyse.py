"""D15 analysis: per site kind rows per expansion, the k cap, the op budget, and how far the new ops got."""
from __future__ import annotations

import collections
import glob
import json
import statistics
import sys

K = 12
NEW = ("defeature", "fuse", "cut_by", "trim_unbounded")


def opname(op: str, params: dict | None) -> str:
    if op == "conformalise" and str((params or {}).get("mode", "")).startswith("cut_by"):
        return "cut_by"
    if op == "conformalise":
        return f"conformalise:{(params or {}).get('mode')}"
    if op == "defeature":
        return f"defeature:{(params or {}).get('source')}"
    return op


def main(d: str) -> None:
    recs = [json.load(open(p)) for p in sorted(glob.glob(f"{d}/[0-9][0-9]_*.json"))]
    print(f"files: {len(recs)}")
    per_kind_rows: dict[str, list[int]] = collections.defaultdict(list)   # rows per site per expansion
    per_kind_cut: dict[str, list[int]] = collections.defaultdict(list)    # of those, rows past the k cap
    op_full = collections.Counter()
    op_in_k = collections.Counter()
    exp_full, exp_cut = [], []
    files = []
    new_evald = collections.Counter()
    new_out = collections.defaultdict(collections.Counter)
    applied_hist = collections.Counter()
    for r in recs:
        tr = (r.get("trace") or {}).get("events") or []
        stop = next((e for e in tr if e["kind"] == "stop"), {})
        cands = [e for e in tr if e["kind"] == "candidate"]
        outc = collections.Counter(e["outcome"] for e in cands)
        for e in cands:
            applied_hist[e.get("ops_applied", "n/a")] += 1
            n = opname(e["op"], e.get("params"))
            base = n.split(":")[0] if n.startswith("defeature") else n
            if base in NEW:
                new_evald[n] += 1
                new_out[n][e["outcome"] + (":" + e.get("stage", "") if e["outcome"] == "rejected" else "")] += 1
        new_prop_full = collections.Counter()
        new_prop_k = collections.Counter()
        for p in r.get("proposes") or []:
            exp_full.append(p["n_full"])
            exp_cut.append(max(0, p["n_full"] - p["k"]))
            by_site = collections.defaultdict(list)
            for x in p["rows"]:
                by_site[(x["site"], x["kind"])].append(x)
            for s in p["sites"]:
                rows = [x for x in p["rows"] if x["site"] == s["id"] and x["kind"] != "part"]
                per_kind_rows[s["kind"]].append(len(rows))
                per_kind_cut[s["kind"]].append(sum(1 for x in rows if x["rank"] >= p["k"]))
            for x in p["rows"]:
                n = opname(x["op"], {"mode": x.get("mode"), "source": x.get("source")})
                op_full[n] += 1
                if x["rank"] < p["k"]:
                    op_in_k[n] += 1
                if n.split(":")[0] in NEW:
                    new_prop_full[n] += 1
                    new_prop_k[n] += x["rank"] < p["k"]
        files.append({
            "i": r["index"], "sha": r["sha256"][:10], "label": r.get("label"),
            "excluded": (r.get("excluded") or "")[:50] or None, "err": (r.get("error") or "")[:80] or None,
            "wall": r["wall_s"], "reason": r.get("search_reason"),
            "ops": (stop.get("spent") or {}).get("ops"), "search_wall": (stop.get("spent") or {}).get("wall_s"),
            "exp": len(r.get("proposes") or []), "skipped_budget": outc.get("skipped_budget", 0),
            "outcomes": dict(outc), "clean": (r.get("row") or {}).get("out_clean"),
            "in_clean": (r.get("row") or {}).get("in_clean"),
            "max_full": max([p["n_full"] for p in r.get("proposes") or []] or [0]),
            "new_full": dict(new_prop_full), "new_in_k": dict(new_prop_k),
            "site_kinds": sorted({s["kind"] for p in (r.get("proposes") or [])[:1] for s in p["sites"]}),
        })
    print("\nper file:")
    for f in files:
        print(json.dumps(f))
    print("\nexpansions:", len(exp_full), "rows/expansion median", statistics.median(exp_full) if exp_full else None,
          "max", max(exp_full or [0]), "expansions over k:", sum(1 for c in exp_cut if c), "rows cut:", sum(exp_cut))
    print("\nper site kind (rows per site per expansion): n, mean, max, share of those rows past k")
    for k, v in sorted(per_kind_rows.items()):
        cut = sum(per_kind_cut[k])
        print(f"  {k:14s} n={len(v):4d} mean={statistics.mean(v):5.2f} max={max(v):3d} past_k={cut}/{sum(v)}")
    print("\nper op: rows made, rows within k")
    for op, n in op_full.most_common():
        print(f"  {op:32s} {n:5d} {op_in_k[op]:5d}")
    print("\nnew ops in the trace (evaluated or rejected or skipped):")
    for n, c in sorted(new_out.items()):
        print(f"  {n:24s} {dict(c)}")
    print("\nops_applied per candidate event:", dict(applied_hist))
    reasons = collections.Counter(f["reason"] for f in files)
    print("stop reasons:", dict(reasons))
    print("files ending on ops:", [f["i"] for f in files if f["reason"] == "ops"],
          "on wall:", [f["i"] for f in files if f["reason"] == "wall"])
    print("skipped_budget total:", sum(f["skipped_budget"] for f in files))


if __name__ == "__main__":
    main(sys.argv[1])
