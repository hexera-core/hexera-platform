#!/bin/bash
# VERIFY THE FINAL MESH SET, from the bucket, by extracting it. Not by trusting the upload.
#
# Checks, each one a thing that has actually gone wrong on this job at some point:
#   1. every non-processor patch, its type and its face count summed over processors
#   2. the conjugate interface matches face for face: solid_to_fluid == fluid_to_solid
#   3. the chip coupling matches: solid_to_chip == chip_to_solid
#   4. NOTHING is a mappedWall pointing at `air`, a region we do not ship
#   5. the air-facing patches are plain walls
#   6. the layered coolant still has the same interface and both ports
#   7. cell, face and point counts straight off each owner header
set -u
B=gs://dev-rehaan-artifacts-224734058693/corintis
W=/mnt/work/verify
sudo rm -rf $W; sudo mkdir -p $W; sudo chmod 777 $W; cd $W

say() { printf "%s\n" "$*"; }

pull () {   # $1 = bucket path, $2 = local dir name
  mkdir -p "$W/$2"
  gsutil -q cp "$1" "$W/$2.tar.gz" || { say "FATAL: cannot fetch $1"; return 1; }
  # only the small metadata files are needed; the stream is read once either way
  # BOTH shapes: a decomposed archive (processorN/constant/<region>/polyMesh) and a
  # reconstructed one (constant/polyMesh). bl8 is the second and the first glob missed it.
  tar xzf "$W/$2.tar.gz" -C "$W/$2" --wildcards "*polyMesh/boundary" "*polyMesh/owner" 2>/dev/null
  rm -f "$W/$2.tar.gz"
}

report () {  # $1 = local dir name
  python3 - "$W/$1" "$1" <<'PY'
import glob, os, re, sys
root, label = sys.argv[1], sys.argv[2]
bs = sorted(glob.glob(os.path.join(root, "**", "polyMesh", "boundary"), recursive=True))
if not bs:
    print("   %-10s NO BOUNDARY FILES FOUND" % label); sys.exit(0)
tot, types, samp = {}, {}, {}
for p in bs:
    t = open(p, encoding="utf-8", errors="replace").read()
    i = t.find("// * * *")
    t = t[i:] if i >= 0 else t
    for m in re.finditer(r"\n    ([A-Za-z_][A-Za-z0-9_]*)\s*\n    \{(.*?)\n    \}", t, re.S):
        name, body = m.group(1), m.group(2)
        if name.startswith("processor"):
            continue
        nf = re.search(r"nFaces\s+(\d+)", body)
        ty = re.search(r"type\s+(\w+)", body)
        sr = re.search(r"sampleRegion\s+(\w+)", body)
        tot[name] = tot.get(name, 0) + (int(nf.group(1)) if nf else 0)
        if ty: types[name] = ty.group(1)
        if sr: samp[name] = sr.group(1)
# cells/faces/points off the owner note
note = {}
for p in sorted(glob.glob(os.path.join(root, "**", "polyMesh", "owner"), recursive=True))[:1]:
    h = open(p, "rb").read(3000).decode("ascii", "replace")
    m = re.search(r"nPoints:(\d+)\s+nCells:(\d+)\s+nFaces:(\d+)\s+nInternalFaces:(\d+)", h)
    if m:
        note = dict(zip(("points", "cells", "faces", "internal"), (int(x) for x in m.groups())))
print("   %s  (%d processor boundary files)" % (label, len(bs)))
for k in sorted(tot):
    print("      %-18s %-12s %12d%s" % (k, types.get(k, "?"), tot[k],
          ("   -> region " + samp[k]) if k in samp else ""))
if note:
    print("      processor0 header: cells %d  faces %d  internal %d  points %d"
          % (note["cells"], note["faces"], note["internal"], note["points"]))
open(os.path.join(root, "..", label + ".counts"), "w").write(repr(tot))
PY
}

say "################ FINAL MESH SET, VERIFIED FROM THE BUCKET ################"
say "$(date -u)"
say
say "=== the corrected three regions, mesh/cbox8fixed/ ==="
for r in solid chip fluid; do
  pull "$B/mesh/cbox8fixed/cbox8_${r}.tar.gz" "$r" && report "$r"
done
say
say "=== the layered coolant as shipped, mesh/bl8/ ==="
pull "$B/mesh/bl8/bl8_fluid_layered.tar.gz" "bl8" && report "bl8"
say
say "=== GATES ==="
python3 - <<'PY'
import ast, os
W = "/mnt/work/verify"
def c(lbl):
    p = os.path.join(W, lbl + ".counts")
    return ast.literal_eval(open(p).read()) if os.path.exists(p) else {}
s, ch, f, b = c("solid"), c("chip"), c("fluid"), c("bl8")
ok = True
def gate(name, got, want):
    global ok
    good = got == want
    ok = ok and good
    print("   %-46s %-12s %s" % (name, "OK" if good else "FAIL", "%s vs %s" % (got, want)))
gate("solid_to_fluid == 16,619,777", s.get("solid_to_fluid"), 16619777)
gate("fluid_to_solid (unlayered) == 16,619,777", f.get("fluid_to_solid"), 16619777)
gate("solid_to_chip == 270,816", s.get("solid_to_chip"), 270816)
gate("chip_to_solid == 270,816", ch.get("chip_to_solid"), 270816)
gate("chip_to_air == 1,492,860", ch.get("chip_to_air"), 1492860)
gate("solid_to_air == 1,213,523", s.get("solid_to_air"), 1213523)
if b:
    gate("layered fluid_to_solid == 16,619,777", b.get("fluid_to_solid"), 16619777)
    gate("port_a + port_b == 2,639",
         (b.get("port_a") or 0) + (b.get("port_b") or 0), 2639)
print()
print("   EVERY GATE PASSED" if ok else "   AT LEAST ONE GATE FAILED")
PY
say
say "=== is anything still a mappedWall pointing at air? ==="
# NOT `grep | head`: the pipeline exit status is head\'s, and head exits 0 on empty input,
# so that form reported FAIL on a clean set. Collect, then test for non-empty.
grep -rl "sampleRegion    air" $W/*/ 2>/dev/null > $W/air_hits || true
if [ -s $W/air_hits ]; then
  say "   FAIL: these still reference region air"
  head -5 $W/air_hits | sed "s/^/      /"
else
  say "   OK: nothing references region air"
fi
say "   every sampleRegion value present across the set:"
grep -rho "sampleRegion *[a-z]*" $W/*/ 2>/dev/null | sort | uniq -c | sed "s/^/      /"
say
say "=== are the air-facing patches plain walls? ==="
grep -h -A3 "_to_air" $W/chip/processor0/constant/*/polyMesh/boundary 2>/dev/null | grep -E "type" | head -2
grep -h -A3 "_to_air" $W/solid/processor0/constant/*/polyMesh/boundary 2>/dev/null | grep -E "type" | head -2
say
du -sh $W
say "VERIFY DONE $(date -u)"
