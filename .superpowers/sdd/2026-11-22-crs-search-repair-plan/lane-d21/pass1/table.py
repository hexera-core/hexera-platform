"""D21 table from the collected oracle rows: per file, what made it clean or added passes, and what nothing touched."""
import collections
import glob
import json
import sys

rows = [json.loads(l) for l in open(sys.argv[1])]
census = {}
for p in glob.glob("/tmp/laned-d19/res/[0-9][0-9][0-9]_*.json"):
    r = json.load(open(p))
    census[r["sha256"]] = r["engine_form"]["nb_roots"]
meta = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else {}

# ---- gate vs rung (lanes A13/C): from the candidate evidence, not from rationale text
cands_dir = sys.argv[3] if len(sys.argv) > 3 else None
GRID_PREFIX = ("oracle_grid:", "oracle_face_grid:")


def origin(c):
    why = c.get("why") or ""
    if c["op"] == "run_stage":
        return "stage"
    return "grid" if why.startswith(GRID_PREFIX) else "det"


def rung_key(c):
    return (c["op"], json.dumps({k: v for k, v in (c.get("params") or {}).items() if k not in ("source", "groups")},
                                sort_keys=True))


def state_key(c):
    return (c["depth"], json.dumps(c.get("path") or [], sort_keys=True))


per_file = {}
det_rungs_anywhere = set()
if cands_dir:
    for p in glob.glob(f"{cands_dir}/*.jsonl"):
        recs = []
        for line in open(p):
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
        per_file[p.split("/")[-1][:16]] = recs
        det_rungs_anywhere |= {rung_key(c) for c in recs if origin(c) == "det"}


def ceiling(r, w):
    """Why the search did not have this winner: det had it / stage / taxonomy (gate) / parameter range (rung) /
    both / aim. Needs the file's candidates; '?' without them."""
    recs = per_file.get(r["sha256"][:16])
    o = origin(w)
    if o == "det" and w["op"] == "defeature" and "on the part" in (w.get("why") or ""):
        # the oracle sweeps with whole_part_defeature ON; the arms have it OFF by default (D17, 14 of 22 invalidated)
        return "whole-part defeature (OFF in the arms)"
    if o != "grid":
        return "det had it" if o == "det" else "stage"
    if recs is None:
        return "?"
    here = [c for c in recs if state_key(c) == state_key(w) and origin(c) == "det"]
    gate = not any(c["op"] == w["op"] for c in here)
    rung = rung_key(w) not in det_rungs_anywhere
    if gate and rung:
        return "both: taxonomy + range"
    if gate:
        return "taxonomy (gate)"
    if rung:
        return "parameter range (rung)"
    return "aim (det had op+rung, other faces)"


# ---- the arms' own rows (v4): which files an arm now cleans, and where search_det stopped on its wall
v4_dir = sys.argv[4] if len(sys.argv) > 4 else None
v4_solved, v4_wall = set(), set()
if v4_dir:
    for p in glob.glob(f"{v4_dir}/**/rows*.jsonl", recursive=True):
        for line in open(p):
            if not line.strip():
                continue
            a = json.loads(line)
            if a.get("arm") in ("search_det", "search_ai", "staged") and not a.get("inconclusive") and a.get("out_clean"):
                v4_solved.add(a["sha256"])
            if a.get("arm") == "search_det" and a.get("reason") == "wall" and not a.get("out_clean"):
                v4_wall.add(a["sha256"])
# the wall is read from the wave BEFORE the aiming fixes (v3c): that is where lane A13 measured the search holding the
# answer and running out of time
for p in glob.glob(f"{sys.argv[5]}/**/rows*.jsonl", recursive=True) if len(sys.argv) > 5 else []:
    for line in open(p):
        if line.strip():
            a = json.loads(line)
            if a.get("arm") == "search_det" and a.get("reason") == "wall" and not a.get("out_clean"):
                v4_wall.add(a["sha256"])


def arm_ceiling(r, label):
    """Refine 'det had it' with the arms' rows: WALL = det proposed the winner AND v4's search_det stopped on the wall
    (lane A13: the search held the answer and ran out of time); SOLVED = a v4 arm cleans the file now."""
    if r["sha256"] in v4_solved:
        return "SOLVED by a v4 arm"
    if label == "det had it" and r["sha256"] in v4_wall:
        return "WALL (det had it; search_det stopped on the wall)"
    return label


status = collections.Counter(r["status"] for r in rows)
print(f"# D21 oracle table - {len(rows)} rows for {len(rows)} inputs")
print(f"commit {meta.get('commit')}; netgen: {meta.get('netgen')}")
print("CLEAN below is under FIRST-ROOT netgen (the sweep's commit). The tip (.8) meshes every root: a multi-root "
      "winner (flagged MR) may not be clean there. D20: 0 of 92 multi-root INPUTS went clean->unclean; outputs unmeasured.")
print("VMs: us-east1, ephemeral external IPs (no NAT there), self-deleting.\n")
print("status:", dict(status))

fixed, improved, nothing, other = [], [], [], []
for r in rows:
    mr = census.get(r["sha256"], 1) > 1
    w = r.get("winners") or []
    for x in w:
        x["_ceiling"] = arm_ceiling(r, ceiling(r, x))
    clean_w = [x for x in w if x["full"]["clean"]]
    ungamed = [x for x in clean_w if not x.get("gaming")]
    entry = (r, mr, w, clean_w, ungamed)
    if r["status"] in ("staged_clean", "clean_at_input"):
        other.append(entry)
    elif ungamed or clean_w:
        fixed.append(entry)
    elif w:
        improved.append(entry)
    else:
        nothing.append(entry)

def fmt(x):
    path = "+".join([p["op"] for p in x.get("path") or []] + [x["op"]])
    params = json.dumps(x["params"], sort_keys=True)[:70]
    own = x.get("own") or {}
    g = f" GAMING {x['gaming']}" if x.get("gaming") else ""
    faces = x.get("faces") or {}
    fc = f" faces={ {k: (len(v) if isinstance(v, list) else v) for k, v in faces.items()} }" if faces else ""
    d2 = " [d2 face row: aimed at faces the SPLIT made, not mesher_faces]" if (x.get("why") or "").startswith("oracle_face_grid: depth2") else ""
    return (f"[{x.get('_ceiling', '?')}]{d2} d{x['depth']} {path} {params} -> clean={x['full']['clean']} passes={x['full']['passes']} "
            f"vol x{own.get('volume_ratio', float('nan')):.3g} bbox-axis x{own.get('bbox_min_axis_ratio', float('nan')):.3g}{g}{fc}")

print(f"\n## CLEAN reached by an op: {len(fixed)} files ({sum(1 for e in fixed if e[4])} with an ungamed clean)")
for r, mr, w, cw, ug in fixed:
    print(f"- {r['sha256'][:10]}{' MR' if mr else ''} {r['name'][-38:]} | staged {r.get('staged', {}).get('failing')}")
    for x in (ug or cw)[:4]:
        print(f"    {fmt(x)}")
print(f"\n## PASSES added, not clean: {len(improved)} files")
for r, mr, w, cw, ug in improved:
    best = max(w, key=lambda x: x["full"]["passes"])
    print(f"- {r['sha256'][:10]}{' MR' if mr else ''} staged {r.get('staged', {}).get('passes')} -> best {fmt(best)}")
print(f"\n## NOTHING TOUCHED: {len(nothing)} files ({sum(1 for e in nothing if e[0]['sha256'] in v4_solved)} of them SOLVED by a v4 arm)")
for r, mr, w, cw, ug in nothing:
    s = r.get("staged") or r.get("input") or {}
    print(f"- {r['sha256'][:10]}{' MR' if mr else ''} {r['status']} valid={s.get('valid')} failing={s.get('failing')} "
          f"tried={r.get('recorded')} outcomes={r.get('outcomes')} stopped_at={(r.get('stopped_at') or {}).get('stage')}")
print(f"\n## staged alone clean / clean at input: {len(other)}")
print(" ", dict(collections.Counter(e[0]['status'] for e in other)))
ops = collections.Counter()
for r, mr, w, cw, ug in fixed:
    for x in (ug or cw):
        ops["+".join([p["op"] for p in x.get("path") or []] + [x["op"]])] += 1
print("\n## ops that reached clean (per winning candidate):", dict(ops.most_common()))
print("layer-1 losses across all candidates:", sum(r.get("lost_layer1", 0) for r in rows))
ceil = collections.Counter()
for r, mr, w, cw, ug in fixed:
    best = (ug or cw)
    kinds = {x["_ceiling"] for x in best}
    # a file counts under the CHEAPEST explanation: if det already had a clean winner, the ceiling was the search
    for label in ("SOLVED by a v4 arm", "WALL (det had it; search_det stopped on the wall)", "det had it", "stage",
                  "whole-part defeature (OFF in the arms)",
                  "aim (det had op+rung, other faces)", "parameter range (rung)",
                  "taxonomy (gate)", "both: taxonomy + range", "?"):
        if label in kinds:
            ceil[label] += 1
            break
print("\n## WHAT HELD THE SEARCH BACK, per file reaching clean (cheapest explanation):", dict(ceil))
