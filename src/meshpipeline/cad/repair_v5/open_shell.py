# Responsibility: v5's open_shell_volume DETECTORS: turn the `v5_shell_facts` measurement into each defect's CAUSE KIND
#                 (which picks the rule seed and the menu), its parameters (the measured sewing gap and the next gaps,
#                 the free loop to fill, the non-manifold edges to split), and the UNREPAIRABLE verdict for a genuine
#                 sheet body or a missing face too large to fill. Also the re-measured local check for these kinds.
# Owns: `KINDS`, `KIND_REPAIRS`, `shell_cause` (pure), `attach_shell_causes` (pure), `measure_shells`, `remeasured`.
# Boundaries: no OCCT here; the measurement is `open_shell_kernel.v5_shell_facts` through the pool. The class is
#             `diagnose`'s (layer-1 open_shell/no_solids/non_manifold/non_positive_volume, mesher no_volume/open_loop);
#             this module only fills the cause. Applying a repair is `seq.apply_repair`.
"""What is open, and how: the open_shell_volume class's cause kinds.

THE RULES, on one part's measured shells (first match wins for the defect's code):

- `non_positive_volume` on a CLOSED solid (no free edge) -> `inside_out`: orient_and_close.
- `non_manifold` -> `non_manifold`: split the multiply-shared edges' shells into manifold shells (split_non_manifold).
- any other code, while free edges remain:
  - free edges with a PARTNER (another face's free edge within the reach, `open_shell_kernel`) -> `gap`: sew at the
    measured gap (x `SEW_MARGIN`), never a blind ladder; the NEXT gap levels (the measured gaps grouped by factor
    `GAP_LEVEL_FACTOR`) are the tournament's variants;
  - else the smallest closed free loop: span <= `FILL_CAP_OF_DIAGONAL` of the part -> `hole`: fill_loop aimed at THAT
    loop's edges; larger -> UNREPAIRABLE (owner: local re-surfacing is deferred): `sheet` when the open unit is a
    shell or loose faces that could never enclose a volume and the loop is its own outline, else `large_hole`;
    a `sheet` beside only CLOSED solids is a STRAY sheet and is DROPPED instead (round 2, owner-approved: drop_sheet);
  - free edges with neither a partner nor a closed loop -> UNREPAIRABLE `open_chain` (nothing to sew, nothing to fill).
- no free edge left:
  - a closed shell outside any solid (or no solid at all) -> `closed_shell`: make_volume;
  - a mesher `open_loop` on a face whose wires have a measured gap -> `wire_gap` (the face's own gap);
  - a mesher `no_volume`/`open_loop` on closed, manifold, positive solids -> `mesher_closed`: the topology is not
    open, so the failure is the mesher's (the v4 measured row for the code is the menu, not sew/fill).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from meshpipeline.cad.repair_v4.local_verify import Defect

CLASS = "open_shell_volume"
#: sewing tolerance = this x the largest measured gap it must close (the measurement's own rounding, not a rung)
SEW_MARGIN = 1.1
#: the smallest tolerance sewn at, as a share of the part diagonal: twice the sew ops' own floor (1e-6 x diagonal,
#: `shells.ladder_floor`). Measured on the dev files: most partnered free edges are COINCIDENT (gap 0 to 1e-11 mm),
#: an open solid the staged healer's `sew` refuses because it carries a solid, and 1.1 x 0 is no tolerance at all.
SEW_FLOOR_OF_DIAGONAL = 2e-6
#: measured gaps within this factor of each other are one level; each level is a "next gap" to sew at
GAP_LEVEL_FACTOR = 2.0
#: a loop wider than this share of the part diagonal is a missing FACE, not a hole: not filled (owner: re-surfacing is
#: deferred). 5% is v4's old fill_loop span limit, the one value with a history in this code.
FILL_CAP_OF_DIAGONAL = 0.05
#: a free loop at least this share of its own unit's diagonal is that unit's outline (a sheet's edge)
OUTLINE_OF_UNIT = 0.5

KINDS = ("inside_out", "non_manifold", "gap", "hole", "closed_shell", "wire_gap", "mesher_closed",
         "sheet", "large_hole", "open_chain")
#: kinds measured on the whole part (not aimed at one loop, edge or solid): one validity defect per kind
WHOLE_PART = frozenset({"gap", "closed_shell", "sheet", "large_hole", "open_chain"})
#: kinds that CAN be unrepairable (`sheet` only when it is no stray sheet, `stray_sheet`): the tournament reads the
#: cause's own `unrepairable` key
UNREPAIRABLE = frozenset({"sheet", "large_hole", "open_chain"})
#: the rule seed (first) and the class menu per cause kind; `loop.CLASS_REPAIRS` takes these rows as
#: `open_shell_volume:<kind>`. An unrepairable kind has no row: its defect is recorded, not attempted.
KIND_REPAIRS: dict[str, tuple[tuple[str, ...], ...]] = {
    "inside_out": (("orient_and_close",),),
    "non_manifold": (("split_non_manifold",), ("split_non_manifold", "orient_and_close")),
    "gap": (("sew_gap",), ("sew_gap", "make_volume"), ("fill_loop",)),
    "hole": (("fill_loop",),),
    # round 2 (owner-approved 2026-11-25): a stray sheet body is removed and reported as a removal
    "sheet": (("drop_sheet",),),
    "closed_shell": (("make_volume",), ("orient_and_close",)),
    "wire_gap": (("tighten_tolerances",), ("split_closed",), ("defeature",)),
    # v4's measured no_volume winners (seq.RULE_TABLE): a closed part the mesher cannot fill is a mesh problem
    "mesher_closed": (("split_by_proximity", "remove_small"), ("defeature",)),
}


def stray_sheet(units: list[dict]) -> bool:
    """True when a sheet on a part of these measured units can be DROPPED: at least one solid of positive volume, and
    every solid closed (no free edge), so the sheet is no solid's missing face. Pure. (`ops/sheets.py` re-checks this
    and that the sheet touches no solid on the shape itself.)"""
    solids = [u for u in units if u.get("kind") == "solid"]
    return (any((u.get("volume") or 0.0) > 0 for u in solids)
            and all(not u.get("free_edges") for u in solids))


def _levels(gaps: list[float]) -> list[float]:
    """The measured gaps grouped into levels (each within GAP_LEVEL_FACTOR of the level's smallest), each level as the
    largest gap in it, ascending."""
    out: list[float] = []
    lo = None
    for g in sorted(gaps):
        if lo is None or g > GAP_LEVEL_FACTOR * lo:
            lo = g
            out.append(g)
        else:
            out[-1] = g
    return out


def shell_cause(code: str, facts: dict, *, member: str | None = None, faces: tuple[int, ...] = ()) -> dict:
    """The cause of one open_shell_volume defect of `code` on a part measured as `facts` (kernel indices). Pure.

    Returns `{kind, why, ...}`; `unrepairable` (the reason) on a kind no repair is attempted for. Entities are KERNEL
    indices here (`aim_edges`, `aim_solids`, `aim_faces`); `attach_shell_causes` translates them."""
    diag = float(facts.get("diag_mm") or 0.0)
    units = facts.get("units") or []
    solids = [u for u in units if u.get("kind") == "solid"]
    gaps = [g for g in facts.get("gaps") or [] if g.get("gap_mm") is not None]
    loops = facts.get("loops") or []
    free = int(facts.get("free_edges") or 0)
    base = {"free_edges": free, "paired_edges": len(gaps), "diag_mm": diag,
            "non_manifold_edges": len(facts.get("non_manifold") or [])}

    inside_out = [u for u in solids if (u.get("volume") or 0.0) < 0 and not u.get("free_edges")]
    if code == "non_positive_volume" and inside_out:
        return {**base, "kind": "inside_out", "aim_solids": [u["index"] for u in inside_out],
                "why": f"{len(inside_out)} closed solid(s) with negative volume (inside out)"}
    if code == "non_manifold" and facts.get("non_manifold"):
        nm = facts["non_manifold"]
        return {**base, "kind": "non_manifold", "aim_edges": [n["edge"] for n in nm],
                "why": f"{len(nm)} edge(s) shared by more than two faces (up to {max(len(n['faces']) for n in nm)})"}
    if free:
        if gaps:
            values = [float(g["gap_mm"]) for g in gaps]
            levels = _levels(values)
            floor = SEW_FLOOR_OF_DIAGONAL * diag
            tol = max(SEW_MARGIN * max(values), floor)
            return {**base, "kind": "gap", "gap_mm": max(values), "sew_tol_mm": tol,
                    "gap_levels_mm": levels, "sew_ladder_mm": sorted({max(SEW_MARGIN * v, floor) for v in levels}),
                    "why": f"{len(gaps)} of {free} free edge(s) have a partner, gaps up to {max(values):.3g} mm "
                           f"({len(levels)} level(s)): sew at {tol:.3g} mm"}
        if loops:
            loop = loops[0]
            span = float(loop["span_mm"])
            if diag > 0 and span <= FILL_CAP_OF_DIAGONAL * diag:
                return {**base, "kind": "hole", "aim_edges": list(loop["edges"]), "span_mm": span,
                        "why": f"no partner for any free edge; smallest free loop {span:.3g} mm "
                               f"({span / diag:.1%} of the part) is filled"}
            names = set(loop.get("units") or ())
            owners = [u for u in units if u["name"] in names]
            # the unit's OUTLINE is its widest free loop (a sheet with a hole in it has the hole as its smallest)
            outline = max((float(lp["span_mm"]) for lp in loops if set(lp.get("units") or ()) & names), default=span)
            sheet = bool(owners) and all(u["kind"] in ("shell", "loose") and not u.get("closable")
                                         and outline >= OUTLINE_OF_UNIT * float(u.get("diag_mm") or 0.0)
                                         for u in owners)
            kind = "sheet" if sheet else "large_hole"
            reason = (f"a genuine sheet body: {'/'.join(sorted(names))} is open along its own outline "
                      f"({outline:.3g} mm) and could never enclose a volume" if sheet else
                      f"a missing face: the smallest free loop spans {span:.3g} mm, "
                      f"{span / diag:.1%} of the part (> {FILL_CAP_OF_DIAGONAL:.0%} fill cap)")
            if sheet and stray_sheet(units):
                # round 2 (owner-approved 2026-11-25): a STRAY sheet beside closed solids is dropped (drop_sheet,
                # reported as a removal); one that could be an open solid's missing face stays unrepairable
                return {**base, "kind": kind, "span_mm": span,
                        "why": f"{reason}; every solid is closed, so it is a stray sheet: drop it"}
            return {**base, "kind": kind, "span_mm": span, "unrepairable": reason, "why": reason}
        reason = (f"{free} free edge(s) with no partner within reach and no closed free loop: nothing to sew and "
                  "nothing to fill")
        return {**base, "kind": "open_chain", "unrepairable": reason, "why": reason}
    closed_shells = [u for u in units if u["kind"] in ("shell", "loose") and u.get("closable")]
    if closed_shells or (code == "no_solids" and not solids):
        return {**base, "kind": "closed_shell",
                "why": f"no free edge; {len(closed_shells)} closed shell(s) outside a solid: make the volume"}
    if code == "open_loop":
        wire = max((w for w in facts.get("wire_gaps") or [] if not faces or w["face"] in faces),
                   key=lambda w: w["gap_mm"], default=None)
        if wire is not None and wire["gap_mm"] > 0:
            return {**base, "kind": "wire_gap", "aim_faces": [wire["face"]], "wire_gap_mm": wire["gap_mm"],
                    "why": f"face wire gap {wire['gap_mm']:.3g} mm where the mesher's 1D loop does not close"}
    return {**base, "kind": "mesher_closed",
            "why": f"{member or 'the mesher'} reported {code}, but the part is closed, manifold and positive: "
                   "the topology is not open"}


def attach_shell_causes(defects: list[Defect], facts: dict | None, reverse: dict) -> list[Defect]:
    """`defects` with each open_shell_volume defect's cause filled from `facts` (kernel ids -> stable ids through
    `reverse`, the workspace's `(prefix, index) -> id`). Pure. Without facts the defects are returned unchanged (a
    failed measurement degrades the cause, never loses the defect)."""
    if facts is None:
        return defects
    out: list[Defect] = []
    first: dict[str, int] = {}
    for d in defects:
        if d.cls != CLASS:
            out.append(d)
            continue
        faces = tuple(i for (p, i), sid in reverse.items() if p == "F" and sid in set(d.entities))
        cause = shell_cause(d.code, facts, member=d.member, faces=faces)
        aim = ([reverse[("E", i)] for i in cause.pop("aim_edges", []) if ("E", i) in reverse]
               + [reverse[("B", i)] for i in cause.pop("aim_solids", []) if ("B", i) in reverse]
               + [reverse[("F", i)] for i in cause.pop("aim_faces", []) if ("F", i) in reverse])
        if aim:
            cause["aim"] = aim
        if d.kind == "validity" and cause["kind"] in WHOLE_PART and cause["kind"] in first:
            # one part-wide fault reported under two layer-1 codes (open_shell AND no_solids on loose faces): one
            # defect, one tournament - the second would only replay the first's repair on its result
            k = first[cause["kind"]]
            out[k] = dataclasses.replace(out[k], cause={**out[k].cause,
                                                        "also_codes": [*out[k].cause.get("also_codes", []), d.code]})
            continue
        if d.kind == "validity" and cause["kind"] in WHOLE_PART:
            first[cause["kind"]] = len(out)
        out.append(dataclasses.replace(d, cause={**d.cause, **cause},
                                       rule=f"{d.rule}; measured {cause['kind']}: {cause['why']}"))
    return out


#: the shell measurement's share of one diagnosis
FACTS_BUDGET_S = 60.0


def measure_shells(pool: Any, ws: Any, faces: list[int] | None = None) -> dict | None:
    """One `v5_shell_facts` call on `ws` (wire gaps of `faces`, kernel indices); None when the kernel gave no answer."""
    from meshpipeline.cad.repair_v4.sites import _call

    return _call(pool, "v5_shell_facts", {"brep_path": ws.brep_path, "budget_s": FACTS_BUDGET_S,
                                          "faces": list(faces or [])})


def _count(kind: str, facts: dict) -> int:
    """How much of `kind` the part still has (what `remeasured` compares)."""
    units = facts.get("units") or []
    if kind == "inside_out":
        return sum(1 for u in units if u.get("kind") == "solid" and (u.get("volume") or 0.0) <= 0)
    if kind == "non_manifold":
        return len(facts.get("non_manifold") or [])
    if kind == "gap":
        return sum(1 for g in facts.get("gaps") or [] if g.get("gap_mm") is not None)
    if kind == "hole":
        return int(facts.get("free_edges") or 0)
    if kind == "sheet":
        # the stray sheets left: non-solid units that still have free edges
        return sum(1 for u in units if u.get("kind") in ("shell", "loose") and u.get("free_edges"))
    if kind == "closed_shell":
        return sum(1 for u in units if u.get("kind") in ("shell", "loose")) + (0 if any(
            u.get("kind") == "solid" for u in units) else 1)
    return 0


#: the kinds `remeasured` judges (layer 1's local check sees solids only: a free shell, a missing solid and a gap
#: between two loose faces are invisible to it)
REMEASURED = frozenset({"inside_out", "non_manifold", "gap", "hole", "closed_shell", "sheet"})


def remeasured(pool: Any, ws_before: Any, ws_after: Any, defect: Defect, local: Any,
               measure: Callable[..., dict | None] = measure_shells) -> Any:
    """`local` re-judged by the shell measurement on both states: gone = none of the defect's cause kind left, or for a
    gap/hole fewer than before (partnered free edges; free edges) - the next round re-measures the rest -, and a NEW defect when the repair made
    more free edges, more non-manifold edges or more non-positive solids than the part had. A measurement that fails
    keeps `local`'s answer and says so."""
    kind = (defect.cause or {}).get("kind")
    if defect.cls != CLASS or kind not in REMEASURED:
        return local
    before, after = measure(pool, ws_before), measure(pool, ws_after)
    if before is None or after is None:
        return dataclasses.replace(local, detail={**local.detail, "remeasured": "unavailable"})
    left_before, left = _count(kind, before), _count(kind, after)
    # a gap or hole REDUCED is progress the next round re-measures and repairs again (measured on a VM: sewing a staged
    # state at the measured gap took 247 partnered free edges to 5, which an all-or-nothing rule threw away)
    gone = left == 0 or (kind in ("gap", "hole") and left < left_before)
    worse = [name for name, k in (("free edges", "free_edges"),) if int(after.get(k) or 0) > int(before.get(k) or 0)]
    if len(after.get("non_manifold") or []) > len(before.get("non_manifold") or []):
        worse.append("non-manifold edges")
    if _count("inside_out", after) > _count("inside_out", before):
        worse.append("non-positive solids")
    new_hard = list(local.new_hard) + [{"code": f"more {w}", "entities": []} for w in worse]
    return dataclasses.replace(local, gone=gone, new_hard=new_hard, detail={
        **local.detail, "remeasured": f"{kind}: {left_before} -> {left}" + (f"; more {', '.join(worse)}" if worse
                                                                              else "")})
