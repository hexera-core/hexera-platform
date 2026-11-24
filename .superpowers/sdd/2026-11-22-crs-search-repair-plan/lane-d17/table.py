"""D17 per-file table from d17_diag outputs: which member, where, why (geometric flags on the located faces)."""
from __future__ import annotations

import collections
import glob
import json
import sys

SMALL = 0.1   # a located face's shortest edge below this fraction of gmsh_prod's element size (diag/10)


def flags(face: dict) -> list[str]:
    out = []
    if face.get("u_periodic") or face.get("v_periodic"):
        out.append("periodic" + ("+seam" if face.get("seams") else ""))
    if face.get("degenerate_edges"):
        out.append("pole")
    if face.get("min_interior_deg") is not None and face["min_interior_deg"] < 10:
        out.append(f"sharp{face['min_interior_deg']:.0f}deg")
    if face.get("tangent_edges"):
        out.append("tangent")
    if face.get("min_edge_over_h") is not None and face["min_edge_over_h"] < SMALL:
        out.append(f"short_edge{face['min_edge_over_h']:.2g}h")
    if face.get("size_over_h") is not None and face["size_over_h"] < SMALL:
        out.append(f"tiny_face{face['size_over_h']:.2g}h")
    if face.get("type", "").endswith(("BSplineSurface", "BezierSurface", "OtherSurface", "OffsetSurface")):
        out.append(face["type"].replace("GeomAbs_", "").replace("Surface", ""))
    return out or ["plain"]


def main(d: str) -> None:
    recs = [json.load(open(p)) for p in sorted(glob.glob(f"{d}/[0-9][0-9][0-9]_*.json"))]
    why_count: collections.Counter = collections.Counter()
    kind_why: dict = collections.defaultdict(collections.Counter)
    placed = collections.Counter()
    print("| # | file | member: kind (message) | prod surfaces placed as-is/unpadded | sites (faces) | located-face flags | part: faces, periodic, poles, sharp<10, min edge/h |")
    print("|---|---|---|---|---|---|---|")
    for r in recs:
        if r.get("error"):
            print(f"| {r['index']} | {r['sha256'][:10]} | ERROR {r['error'][:80]} | | | | |")
            continue
        mem = []
        for m in r.get("members", []):
            if m["status"] == "pass":
                continue
            mem.append(f"{m['name']}: {m['error_kind'] or m['status']} ({(m['message'] or '')[:50]})")
            ps = m.get("prod_surfaces_placed")
            if ps:
                placed["as_is"] += ps["as_is"]
                placed["unpadded"] += ps["unpadded"]
                placed["surfaces"] += ps["surfaces"]
        if not mem:
            mem = ["(both pass now)"]
        ps = next((m.get("prod_surfaces_placed") for m in r.get("members", []) if m.get("prod_surfaces_placed")), None)
        sites = r.get("sites") or []
        fl: collections.Counter = collections.Counter()
        nfaces = 0
        for s in sites:
            for fid, g in (s.get("faces") or {}).items():
                if g:
                    nfaces += 1
                    for x in flags(g):
                        fl[x.rstrip("0123456789.e-h").replace("deg", "")] += 1
        located = bool(nfaces)
        p = r.get("part") or {}
        h = r.get("h_prod_mm") or 0
        why = sorted(fl) if located else ["UNLOCATED"]
        for m in r.get("members", []):
            if m["status"] != "pass":
                key = f"{m['name']}:{m['error_kind'] or m['status']}"
                for w in why:
                    kind_why[key][w] += 1
        for w in why:
            why_count[w] += 1
        print(f"| {r['index']} | {r['sha256'][:10]} | {'; '.join(mem)} | "
              f"{(str(ps['as_is']) + '/' + str(ps['unpadded']) + ' of ' + str(ps['surfaces'])) if ps else '-'} | "
              f"{len(sites)} ({nfaces}) | {', '.join(f'{k} {v}' for k, v in sorted(fl.items())) or '-'} | "
              f"{p.get('faces')}, {p.get('periodic_faces')}, {p.get('degenerate_edges')}, {p.get('sharp_edges')}, "
              f"{(p['min_edge_mm'] / h) if p.get('min_edge_mm') and h else None:.3g} |"
              if p.get('min_edge_mm') and h else
              f"| {r['index']} | {r['sha256'][:10]} | {'; '.join(mem)} | - | {len(sites)} ({nfaces}) | - | - |")
    print("\nprod surfaces placed:", dict(placed))
    print("\nfiles by located-face flag:", dict(why_count.most_common()))
    print("\nby member:kind -> flags (files):")
    for k, v in sorted(kind_why.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {k}: {dict(v.most_common())}")


if __name__ == "__main__":
    main(sys.argv[1])
