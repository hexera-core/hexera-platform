"""D28: classify gmsh_prod's real failure text into kinds, with counts. First the raw distinct lines (so the kinds come
from what gmsh actually said), then the buckets."""
import collections
import glob
import json
import re
import sys

recs = [json.load(open(p)) for p in sorted(glob.glob(sys.argv[1] + "/*.json"))]
print(f"files: {len(recs)}; ok (prod PASSED on the input engine form): {sum(1 for r in recs if r.get('ok'))}")


def text(r):
    return "\n".join([r.get("exception") or "", r.get("printed") or "", *(r.get("log") or [])])


def norm(line):
    line = re.sub(r"\d+(\.\d+)?(e[-+]?\d+)?", "#", line)
    return re.sub(r"\s+", " ", line).strip()[:140]


lines = collections.Counter()
for r in recs:
    if r.get("ok"):
        continue
    for l in set(norm(x) for x in text(r).splitlines() if re.search(r"(?i)error|fail|invalid|intersect|could not|unable|warning", x)):
        lines[l] += 1
print("\n## distinct error/warning lines (files carrying each)")
for l, n in lines.most_common(40):
    print(f"{n:3d}  {l}")

KINDS = [
    ("self-intersecting surface mesh (HXT intersecting triangles)", r"el1 = np|intersecting|self-intersect|facets? intersect|PLC Error|segment and a facet"),
    ("boundary recovery failure", r"(?i)recover|missing (edge|face)|could not insert|boundary"),
    ("overlapping / duplicate facets", r"(?i)overlapping facets|duplicat"),
    ("degenerate / tiny element or edge", r"(?i)small|degenerat|too short|zero (length|area)"),
    ("surface mesh failure (2D)", r"(?i)meshing surface|surface .* fail|2D mesh|mesh surface"),
    ("no volume / empty", r"(?i)no (elements|tetrahedra)|empty"),
    ("timeout / killed", r"(?i)timeout|killed"),
]
buckets = collections.defaultdict(list)
for r in recs:
    if r.get("ok"):
        buckets["(prod passes on the input: fails only on a later state)"].append(r)
        continue
    t = text(r)
    for name, pat in KINDS:
        if re.search(pat, t):
            buckets[name].append(r)
            break
    else:
        buckets["unclassified: " + norm(r.get("exception") or "")[:60]].append(r)
print("\n## kinds (first match in order; a file counts once)")
for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
    print(f"- {k}: {len(v)}  stage {dict(collections.Counter(r.get('stage') for r in v))}  e.g. {[r['sha'][:10] for r in v[:3]]}")
json.dump({k: [r["sha"] for r in v] for k, v in buckets.items()}, open("kinds.json", "w"), indent=1)
