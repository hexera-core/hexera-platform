import json, glob, collections, sys
from pathlib import Path
root = Path(sys.argv[1])
recs = [json.load(open(f)) for f in sorted(glob.glob(str(root / "*" / "b10.json")))]
T = ["pre_b8", "b8_static", "b8b_adaptive"]
print("files with output:", len(recs), " errors:", sum(1 for r in recs if r.get("error")))
ok = [r for r in recs if not r.get("error") and all("error" not in r["tiers"].get(t, {"error": 1}) for t in T)]
print("files with all three tiers:", len(ok))
def passes(p): return sum(1 for s in p.values() if s == "pass")
for t in T:
    kept = collections.Counter(); rb = collections.Counter(); fb = collections.Counter(); drop = collections.Counter()
    clean = changed = 0; secs = []; regress = 0
    for r in ok:
        x = r["tiers"][t]
        kept.update(s for s in x["kept"] if s != "baseline"); rb.update(x["rolled_back"])
        if x["fell_back_to"] is not None:
            fb[x["fell_back_to"]] += 1; drop.update(x["dropped"])
        clean += x["final_clean"]; changed += x["changed"]; secs.append(x["total_s"])
        regress += passes(x["final_panel"]) < passes(r["input"]["panel"])
    print(f"\n== {t}: final clean {clean}/{len(ok)}, healer changed the part on {changed}, final panel-pass regressions {regress}, mean wall {sum(secs)/len(secs):.0f}s")
    print("   kept per stage:", dict(kept)); print("   rolled back per stage:", dict(rb))
    print("   confirm_staged fell back on", sum(fb.values()), "files:", dict(fb), " stages dropped:", dict(drop))
for a, b in (("pre_b8", "b8_static"), ("pre_b8", "b8b_adaptive"), ("b8_static", "b8b_adaptive")):
    newly = collections.Counter(); unrolled = collections.Counter()
    for r in ok:
        ka, kb = set(r["tiers"][a]["kept"]), set(r["tiers"][b]["kept"])
        rba, rbb = set(r["tiers"][a]["rolled_back"]), set(r["tiers"][b]["rolled_back"])
        newly.update(s for s in ka & rbb); unrolled.update(s for s in rba & kb)
    print(f"\n{a} -> {b}: kept before, ROLLED BACK now: {dict(newly)} | rolled back before, kept now: {dict(unrolled)}")
print("\nper-file final clean (pre/static/adaptive) and input:")
for r in ok:
    print(" ", Path(r["file"]).name[:44].ljust(44), "in", passes(r["input"]["panel"]), "clean" if r["input"]["clean"] else "",
          [("C" if r["tiers"][t]["final_clean"] else passes(r["tiers"][t]["final_panel"])) for t in T],
          [r["tiers"][t]["fell_back_to"] for t in T])
