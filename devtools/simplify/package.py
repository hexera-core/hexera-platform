# Responsibility: Test whether one-feature-at-a-time verdicts add up: remove a whole PACKAGE of features from a part the harness already ran, solve once, and compare the package's true error and cells saved with the sums of its members' individual results.
# Boundaries: a developer tool run on the bench VM after harness.py; nothing in the product imports it. It reuses the harness's recognition, removal, meshing and solving, and the original's own solve as the reference.
# Collaborates with: devtools/simplify/harness.py (everything it runs), docs/superpowers/specs/2026-11-19-cad-simplification-design.md (the error ledger assumes errors roughly add; this measures it), docs/simplification/CFD-NOTES.md (where CFD found they did not).
"""DO THE ERRORS ADD?

    python devtools/simplify/package.py part.step --harness results/part [--budget 0.001]

Two packages per part, each removed in ONE defeaturing call (falling back to one face set at a time
when OCCT refuses the union, and saying which):

- `accepted`: every feature whose own removal changed compliance by at most the budget;
- `all`: every feature the harness removed and solved.

For each: the package's compliance change against the original (the harness's own reference solve,
same size and settings), the SUM of its members' individual changes, the ratio, and cells saved
against the sum of members' cells saved. The ledger in the design treats the sum as the package's
error; a ratio far from 1 is the evidence that it may not.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402  (a sibling script, not a package)


def _remove_all(shape, faces, groups: list[list[int]]):
    """One call for the union; else one group at a time, skipping groups OCCT refuses."""
    union = [faces[j] for g in groups for j in g]
    out, why = H.remove(shape, union)
    if out is not None:
        return out, "one call", [], len(groups)
    cur, skipped, kept = shape, [], 0
    for g in groups:
        nxt, _ = H.remove(cur, [faces[j] for j in g])
        if nxt is None:
            skipped.append(g)
            continue
        cur, kept = nxt, kept + 1
    return (cur if kept else None), f"sequential (union refused: {why})", skipped, kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step")
    ap.add_argument("--harness", required=True)
    ap.add_argument("--budget", type=float, default=0.001)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args(argv)
    hd = Path(a.harness)
    r = json.loads((hd / "result.json").read_text())
    ref = r["original"]
    if not ref.get("ok"):
        print("the original did not solve; nothing to compare against")
        return 1
    shape, diag, lc = H.prepare(a.step)
    feats, faces, _ = H.recognise(shape, diag)
    by_id = {f.id: f for f in feats}
    solved = {fid: row for fid, row in r["features"].items() if "error" in row and fid in by_id
              and by_id[fid].faces == row["faces"]}
    packs = {"accepted": [fid for fid, row in solved.items() if abs(row["error"]["compliance"]) <= a.budget],
             "all": list(solved)}
    jobs, meta = [], {}
    for name, ids in packs.items():
        if len(ids) < 2:
            meta[name] = {"members": ids, "why": "fewer than two members"}
            continue
        simp, how, skipped, kept = _remove_all(shape, faces, [by_id[i].faces for i in ids])
        if simp is None:
            meta[name] = {"members": ids, "why": f"no removal succeeded ({how})"}
            continue
        skipped_ids = [i for i in ids if by_id[i].faces in skipped]
        members = [i for i in ids if i not in skipped_ids]
        path = hd / "geom" / f"package_{name}.brep"
        H.write_brep(simp, path)
        excl = {name: [(by_id[i].bbox, solved[i]["pad"]) for i in members]}
        meta[name] = {"members": members, "skipped": skipped_ids, "how": how,
                      "volume_change": (H.volume(simp) - r["volume"]) / r["volume"]}
        jobs.append((f"package_{name}", str(path), str(hd / "work" / f"package_{name}"), asdict(lc), r["h"],
                     excl, False))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = dict(ex.map(H._job, jobs))
    out = {}
    for name, m in meta.items():
        row = dict(m)
        got = res.get(f"package_{name}")
        if got and got["ok"]:
            members = m["members"]
            dc = (got["qoi"]["compliance"] - ref["qoi"]["compliance"]) / ref["qoi"]["compliance"]
            sum_dc = sum(solved[i]["error"]["compliance"] for i in members)
            cells = 1 - got["elements"] / ref["elements"]
            sum_cells = sum(solved[i].get("cells_saved", 0.0) for i in members)
            row |= {"ok": True, "elements": got["elements"], "compliance_change": dc, "sum_of_members": sum_dc,
                    "ratio": dc / sum_dc if sum_dc else None, "cells_saved": cells,
                    "sum_cells_saved": sum_cells, "max_member": max(abs(solved[i]["error"]["compliance"])
                                                                   for i in members)}
            ratio = "n/a" if row["ratio"] is None else f"{row['ratio']:.2f}"
            print(f"{name:9}: {len(members):3} members ({m['how']}), dC {dc:+.3%} vs sum {sum_dc:+.3%} "
                  f"(ratio {ratio}), cells saved {cells:+.1%} vs sum {sum_cells:+.1%}")
        else:
            row |= {"ok": False, "why": m.get("why") or (got or {}).get("why")}
            print(f"{name:9}: {row['why']}")
        out[name] = row
    (hd / f"package_b{a.budget:g}.json").write_text(json.dumps(out, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
