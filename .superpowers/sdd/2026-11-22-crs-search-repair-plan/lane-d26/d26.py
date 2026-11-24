"""D26: did the v4 arms reach clean by the SAME path as the oracle, or a longer one? Per file, both arms."""
import collections
import glob
import json

rows = {json.loads(l)["sha256"][:12]: json.loads(l) for l in open("final/table.jsonl")}


def oracle_paths(r):
    """The oracle's clean, ungamed winning op sequences, shortest first (ops only, and ops with params)."""
    ws = [w for w in r.get("winners") or [] if w["full"]["clean"] and not w.get("gaming")]
    ws = ws or [w for w in r.get("winners") or [] if w["full"]["clean"]]
    seqs = sorted({tuple([p["op"] for p in w.get("path") or []] + [w["op"]]) for w in ws}, key=len)
    return seqs


def arm_path(trace):
    ev = trace.get("events", trace) if isinstance(trace, dict) else trace
    stop = [e for e in ev if e.get("kind") == "stop"][-1]
    made = {e.get("child"): e for e in ev if e.get("kind") == "candidate" and e.get("child")}
    ops = []
    for sid in (stop.get("best_path") or [])[1:]:
        e = made.get(sid)
        ops.append(e["op"] if e else "?")
    return tuple(ops), stop


def subseq(small, big):
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


out = []
for p in sorted(glob.glob("d26tr/*.json")):
    sha12, arm = p.split("/")[-1][:-5].split("_", 1)
    r = rows.get(sha12)
    if not r:
        continue
    o = oracle_paths(r)
    ops, stop = arm_path(json.load(open(p)))
    clean = bool(stop.get("clean"))
    omin = o[0] if o else ()
    if not clean:
        cls = "arm not clean in this trace"
    elif len(ops) == len(omin) and ops in o:
        cls = "SAME length, an oracle path"
    elif len(ops) == len(omin):
        cls = "SAME length, other route"
    elif len(ops) > len(omin):
        cls = f"LONGER by {len(ops) - len(omin)}" + (" (oracle path inside)" if any(subseq(x, ops) for x in o) else "")
    else:
        cls = "SHORTER than the oracle's"
    sp = stop.get("spent") or {}
    out.append({"sha": sha12, "arm": arm, "class": cls, "oracle_min": "+".join(omin), "oracle_n": len(omin),
                "arm_path": "+".join(ops), "arm_n": len(ops), "arm_ops": sp.get("ops"), "arm_wall_s": sp.get("wall_s"),
                "reason": stop.get("reason")})

print(f"# D26 path-length join: {len({x['sha'] for x in out})} files x 2 arms\n")
print("| file | arm | class | oracle shortest | arm best_path | arm ops / wall s to clean |")
print("|---|---|---|---|---|---|")
for x in out:
    print(f"| {x['sha']} | {x['arm']} | {x['class']} | {x['oracle_min']} ({x['oracle_n']}) | {x['arm_path']} ({x['arm_n']}) "
          f"| {x['arm_ops']} / {x['arm_wall_s'] and round(x['arm_wall_s'])} |")
c = collections.Counter((x["arm"], x["class"]) for x in out)
print("\n## summary")
for k, v in sorted(c.items()):
    print(f"- {k[0]} {k[1]}: {v}")
by = collections.defaultdict(list)
for x in out:
    by[(x["arm"], x["oracle_min"].split("+")[0] if x["oracle_min"] else "-")].append(x)
print("\n## by the oracle's OPENER (arm, opener): files, mean arm ops / wall to clean, classes")
for k, xs in sorted(by.items()):
    cl = collections.Counter(x["class"] for x in xs)
    ok = [x for x in xs if x["reason"] == "clean"]
    if ok:
        print(f"- {k[0]} opener={k[1]}: {len(xs)} files; mean ops {sum(x['arm_ops'] for x in ok)/len(ok):.1f}, "
              f"mean wall {sum(x['arm_wall_s'] for x in ok)/len(ok):.0f} s; {dict(cl)}")
json.dump(out, open("d26.json", "w"), indent=1)
