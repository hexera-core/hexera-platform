"""D24 (2): for every dev file still NOT clean after pass 1 + D24, the BLOCKER on its best state, ranked by file count.

Best state = the full-judged candidate with the highest (valid, passes) across both sweeps; the staged root when no
candidate was full-judged better. Blocker = what fails LAST on it: layer-1 hard codes when invalid; otherwise the
failing panel members with their error kind. Plus what was tried: op families, deepest depth, killed or exhausted.

usage: python blockers.py PASS1_TABLE.jsonl D24_ROWS_DIR D24_CANDS_DIR PASS1_CANDS_DIR
"""
import collections
import glob
import json
import os
import sys

p1 = {json.loads(l)["sha256"]: json.loads(l) for l in open(sys.argv[1])}
d24 = {}
for p in glob.glob(f"{sys.argv[2]}/*.row.json"):
    r = json.load(open(p))
    d24[r["sha256"]] = r


def cands(dirs, sha):
    out = []
    for d in dirs:
        p = f"{d}/{sha[:16]}.jsonl"
        if os.path.exists(p):
            for line in open(p):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def key(s):
    return (bool(s.get("valid")), int(s.get("passes") or 0))


LABELS = {f["sha256"]: f.get("verdict_summary") for f in json.load(open(
    "/Users/kitts/archductor/sdd-worktrees/lane-d/devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json"))["files"]}
rows = []
for sha, r1 in p1.items():
    r2 = d24.get(sha)
    rs = [r for r in (r1, r2) if r]
    if any(r["status"] in ("staged_clean", "clean_at_input") for r in rs):
        continue
    if any(w["full"]["clean"] for r in rs for w in r.get("winners") or []):
        continue
    staged = ((r2 or {}).get("staged") or (r2 or {}).get("input") or r1.get("staged") or r1.get("input"))
    best_src = "staged root"
    if not staged:   # a row killed before it summarised anything: the input's own label (the dev split)
        vs = LABELS.get(sha) or {}
        staged = {"valid": vs.get("valid"), "passes": sum(m["status"] == "pass" for m in vs.get("panel", [])),
                  "failing": [f"{m['name']}:{m['error_kind'] or m['status']}" for m in vs.get("panel", [])
                              if m["status"] != "pass"], "hard": list(vs.get("hard") or [])}
        best_src = "input label (no state summarised before the kill)"
    best = staged
    cs = cands([sys.argv[3], sys.argv[4]], sha)
    for c in cs:
        f = c.get("full")
        if f and key(f) > key(best):
            best, best_src = f, "+".join([p["op"] for p in c.get("path") or []] + [c["op"]])
    if not best.get("valid", True) and best.get("hard"):
        fam = "L1 invalid: " + ", ".join(sorted(set(best["hard"])))
    elif best.get("failing"):
        fam = "mesh: " + ", ".join(sorted(set(best["failing"])))
    else:
        fam = "unknown (no verdict recorded)"
    ops = collections.Counter(c["op"] for c in cs if c.get("outcome") == "applied")
    depth = max([c.get("depth", 0) for c in cs] or [0])
    status = (r2 or r1)["status"]
    rows.append({"sha": sha, "name": r1["name"], "family": fam, "best": best_src, "best_passes": best.get("passes"),
                 "best_valid": best.get("valid"), "status": status, "depth": depth, "candidates": len(cs),
                 "op_families": dict(ops.most_common(6))})

print(f"# D24 blocker table: {len(rows)} dev files still NOT clean (of {len(p1)})\n")
fams = collections.Counter(r["family"] for r in rows)
print("| rank | blocker on the best state reached | files | killed | exhausted | example |")
print("|---|---|---|---|---|---|")
for i, (fam, n) in enumerate(fams.most_common(), 1):
    rs = [r for r in rows if r["family"] == fam]
    ex = max(rs, key=lambda r: r["candidates"])
    print(f"| {i} | {fam} | {n} | {sum(r['status'] == 'killed' for r in rs)} | "
          f"{sum(r['status'] != 'killed' for r in rs)} | {ex['sha'][:10]} ({ex['candidates']} cands, d{ex['depth']}) |")
# coarser view: by the single failing member / layer-1 code (a file counts under each)
coarse = collections.Counter()
for r in rows:
    if r["family"].startswith("L1"):
        for c in r["family"][len("L1 invalid: "):].split(", "):
            coarse["L1 " + c] += 1
    elif r["family"].startswith("mesh"):
        for c in r["family"][len("mesh: "):].split(", "):
            coarse[c] += 1
print("\n## by single check (a file counts under each check that fails on its best state)")
for c, n in coarse.most_common():
    print(f"- {c}: {n}")
json.dump(rows, open("blockers.json", "w"), indent=1)
