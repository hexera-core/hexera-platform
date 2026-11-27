"""Lane C's grid extensions for the D21 oracle sweep: candidates the deterministic sweep CANNOT reach today.

`oracle.py` imports `extra_rows(state)` and appends its candidates to the det sweep's at every state; `annotate`
classifies where a failing member's located faces sit relative to the op that produced the state.

BODY OPS, from lane C's measurements on the dev overlap set (C4, C11):
1. KIND GATING. `DetGenerator._sweep` proposes body ops only at `overlap` / `intersection` sites. When a mesher's
   failure and a layer-1 body_overlap fall on the same bodies, the merged site takes the mesher's kind
   (`self_overlap` on dev 0057_00574915 and 0051_00518960), so the overlapping bodies never get a cut. Here: any site
   that carries 2+ BODIES gets the body rows, whatever its kind.
2. ONE KEEPER, MANY VICTIMS. The sweep cuts PAIRS. On 0051 one body (B4) overlaps three others, and the cut that
   resolved layer 1 was `cut_by:B4` over all four bodies (C11). Here: `cut_by:X` over all of the site's bodies, for
   every X.
3. THE PAIR CAP. The sweep cuts at most `_MAX_CUT_PAIRS` (3) pairs per site. Here: every pair, both ways, up to
   MAX_BODIES bodies per site.
4. FUSE OF THE WHOLE GROUP. The sweep fuses pairs only; here every site's body group is fused at once.
Rows the det sweep already makes for the same site (kind overlap / intersection, not a whole-part site, within its
pair cap) are NOT repeated: the oracle evaluates every candidate, so a duplicate is pure cost.
"""
from __future__ import annotations

import itertools
from typing import Any

#: bodies per site the grid will combine; a site naming more is cut over its first MAX_BODIES (pairs grow as n^2)
MAX_BODIES = 8
#: the det sweep's own pair cap (`search._MAX_CUT_PAIRS`), mirrored so its rows are not repeated
DET_CUT_PAIRS = 3


def _bodies(site: Any) -> list[str]:
    return [e for e in site.entities if isinstance(e, str) and e[:1] == "B"][:MAX_BODIES]


def _det_covers(site: Any) -> bool:
    """True where `DetGenerator._sweep` already proposes body ops for this site (lane C10's whole_part rule)."""
    coarse = ((getattr(site, "graph", None) or {}).get("coarse") or {})
    return site.kind in ("overlap", "intersection") and not coarse.get("whole_part")


def _cand(op: str, targets: list[str], params: dict, why: str) -> Any:
    from meshpipeline.cad.repair_v4.search import Candidate

    return Candidate(op, list(targets), dict(params), "det", f"oracle_grid: {why}")


def extra_rows(state: Any) -> list:
    rows: list = []
    seen: set[tuple] = set()

    def add(op: str, targets: list[str], params: dict, why: str) -> None:
        key = (op, tuple(sorted(targets)), tuple(sorted(params.items())))
        if key not in seen:
            seen.add(key)
            rows.append(_cand(op, targets, params, why))

    for site in getattr(state, "sites", None) or []:
        bodies = _bodies(site)
        if len(bodies) < 2:
            continue
        det = _det_covers(site)
        det_pairs = (set(list(itertools.combinations(bodies, 2))[:DET_CUT_PAIRS])
                     if det and site.kind == "overlap" else set())
        for a, b in itertools.combinations(bodies, 2):
            if (a, b) in det_pairs:
                continue
            for keep in (a, b):
                add("conformalise", [a, b], {"mode": f"cut_by:{keep}", "fuzzy_mm": 0.0},
                    f"cut_by {keep} on pair {a}/{b} at a {site.kind} site (det: kind-gated or past its pair cap)")
            add("fuse", [a, b], {"fuzzy_mm": 0.0}, f"fuse {a}+{b} at a {site.kind} site")
        if len(bodies) >= 3:
            for keep in bodies:
                add("conformalise", bodies, {"mode": f"cut_by:{keep}", "fuzzy_mm": 0.0},
                    f"one keeper {keep} cut out of all {len(bodies)} bodies (C11: 0051's B4)")
            add("fuse", bodies, {"fuzzy_mm": 0.0}, f"fuse all {len(bodies)} bodies of the site")
        if not det:
            add("conformalise", bodies, {"mode": "fragment", "fuzzy_mm": 0.0},
                f"fragment the bodies at a {site.kind} site (det: kind-gated)")
    return rows


def annotate(parent_ws: Any, new_ws: Any, verdict: Any) -> dict:
    """Per failing panel member: its located faces split into NEW (made or changed by the op that produced `new_ws`
    from `parent_ws`: the id is not in the parent with the same fingerprint), OLD (the parent's own face, untouched),
    and UNLOCATED (a point or no entity). Located entities are fingerprints on the workspace's grid (.6).

    Measured floor, lane C12: on v3c's input rows 1 of 29 located faces named no face at all (gmsh_alt
    periodic_surface on 68ef8f5e662d), so an entity that matches NO face of `new_ws` is counted UNPLACED, not NEW."""
    by_fp = {fp: sid for sid, fp in new_ws.ids.items() if sid[:1] == "F"}
    out: dict = {}
    for m in getattr(verdict, "panel", None) or []:
        if getattr(m, "status", "pass") == "pass":
            continue
        new, old, unplaced, unlocated = [], [], 0, 0
        for loc in getattr(m, "located", None) or []:
            ent = getattr(loc, "entity", None)
            if not ent:
                unlocated += 1
                continue
            sid = by_fp.get(ent)
            if sid is None:
                unplaced += 1
            elif parent_ws.ids.get(sid) == ent:
                old.append(sid)
            else:
                new.append(sid)
        out[m.name] = {"kind": getattr(m, "error_kind", None), "new": new[:12], "old": old[:12],
                       "unplaced": unplaced, "unlocated": unlocated}
    return out
