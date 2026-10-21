# Responsibility: Size the cells of a mesh from its MEASURED narrowest gaps, and check before meshing that the cell limit can hold the rule.
# Boundaries: engine-neutral arithmetic on a measured gap field (engines/passage reads it); it authors no case and judges no mesh.
# Collaborates with: engines/passage.narrow_passage_regions (the boxes), the snappy and cfMesh drivers (which mesh them),
#                    cad/shape_traits (the estimate the user sees at approval), and every flow engine's
#                    resolution_floor gate (which judges the mesh by the same rule).
"""SIZE CELLS FROM THE MEASURED NARROWEST GAP.

THE RULE. Every passage or gap the flow goes through gets at least GAP_CELLS_ACROSS cells across
it, measured locally: the cell at a spot is the gap there over the rule's count. So a 2 mm channel
and a 40 m hull are held to the same rule, by their own widths, never by a fixed size. 13 across is
the product's own target (engines/passage.PASSAGE_CELLS_ACROSS); the resolution_floor gate holds 12
at the narrowest wall (PASSAGE_FLOOR_CELLS), and the half cell between them is the snapped wall's
reading scatter. That is the low end of established practice: a laminar channel profile needs about
10 cells across to carry its pressure drop within a few percent, RANS internal-flow practice is
20-40 across, and the meshers' own proximity sizing defaults sit well under ten (Fluent's "cells per
gap" 3, snappyHexMesh's gap level 3-4). 13 is the bar every flow engine's gate already enforces, so a
mesh sized by this rule is one the gate passes, and one sized by anything less is one it refuses.

WHERE. Only the narrow spots are refined (engines/passage.narrow_passage_regions: boxes round the
wall points whose planned cell puts fewer than the rule across them, each one octree level finer per
halving it needs). The rest of the part keeps its planned cell. A whole-part refinement fine enough
for an aorta's 3 mm branches multiplied its 20 mm trunk's cells too (the Fluent aorta, 2026-10-04).

THE BUDGET, UP FRONT. Before this module the boxes were held to half the planned budget and the
ones that did not fit were dropped with a one-line note; the mesh then ran for up to an hour and the
gate refused it for the very spots that were dropped (19 of 59 dev-side failures of the round-4
lab sweep). Now the cost of meeting the rule everywhere is worked out BEFORE meshing:
  * it fits the plan's budget - meshed as planned;
  * it fits the compute limit (CELL_HARD_LIMIT, the only cell-count gate) - the run's budget is
    raised to what the rule needs, and the run says so;
  * it does not fit even the limit - the narrow spots are refined to as many cells across as
    the limit holds, and the run says so with the user's choices (GapPlan.options): accept fewer
    cells across the named narrowest spots, a larger cell limit, or a smaller piece of the
    geometry. The mesh is still held to the gate's full floor: the estimate is cautious (it
    prices every narrow spot at the rule, where the gate reads the narrowest wall's 5th
    percentile), and a mesh it thought short has cleared the gate (a shell-side heat exchanger:
    estimate 11.9 across at the limit, mesh 6.5 M cells, passed). Refusing it before meshing
    would turn a delivered file into a failed one. Choosing the first option (engine param
    `narrow_gaps` = within_limit) holds the mesh to the share of what was accepted instead.
  * not even MIN_ACCEPTED_CELLS across fits - the plain mesh already nearly fills the limit - and
    the run meshes exactly as it did before this module (the boxes held to the plan's share,
    OVER_LIMIT), says the rule could not be held and lists the choices. NEVER a stop before
    meshing: a shell-side heat exchanger whose plain mesh fills most of the limit was stopped
    that way on cfMesh, where the plain mesh passes the gate on snappyHexMesh (lab, 2026-10-21).
In every case short of the rule the record says so (`resolution_short`), and the gate measures.

THE LIMIT IS ON THE DELIVERED MESH, LAYERS INCLUDED. A mesher's cell ceiling (snappyHexMesh's
maxGlobalCells) stops its REFINEMENT; the wall layers are added after it, one cell per wall face
per layer. Priced without them, the gap plan raised the ceiling to the limit and the layers came
on top: a shell-side heat exchanger 7.1 M refined + 5.9 M layer cells = 13.0 M, a valve cage
adapter 6.8 M + 2.5 M = 9.4 M, both over the 8 M limit, both delivered on main at 6.5 M and 2.7 M
(lab, 2026-10-21). So a plan prices the layers (`base_after`, each box's `layer_cells`), and the
ceiling a mesher refines to (GapPlan.ceiling) leaves room for them under the limit.
The same arithmetic, on the geometry check's own chords (cad/shape_traits `gap_cells`), is what
the user sees at approval (estimate_from_traits), so the limit is met or a choice is made before
any mesh is paid for.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS, PASSAGE_FLOOR_CELLS

logger = logging.getLogger(__name__)

#: THE RULE: cells across every gap the flow goes through (see the module docstring).
GAP_CELLS_ACROSS = float(PASSAGE_CELLS_ACROSS)
#: ...and the floor the resolution gates hold at the narrowest wall.
GAP_FLOOR_CELLS = float(PASSAGE_FLOOR_CELLS)
#: The floor over the target: a mesh built to put T across reads about this share of T at its
#: narrowest wall (12 of 13), so a mesh built to an ACCEPTED count is judged by the same share.
FLOOR_SHARE = GAP_FLOOR_CELLS / GAP_CELLS_ACROSS
#: The fewest cells across a narrow spot the user may accept in place of the rule. Under 4 a
#: snapped octree mesh does not carry a velocity profile at all, and a port that narrow seals over
#: (snappy_runner.PORT_MIN_CELLS_ACROSS is 6 for the same reason).
MIN_ACCEPTED_CELLS = 4.0
#: The run's budget over the estimate: the boxes are costed on their fluid at their cell, and an
#: octree grades its levels out over a few cells (nCellsBetweenLevels), which the estimate leaves
#: out. A budget exactly at the estimate is one snappy stops short of (maxGlobalCells).
BUDGET_HEADROOM = 1.15
#: ...and the cell ceiling a raised run is given (snappy's maxGlobalCells, where it stops
#: refining), within the limit: room for the estimate to be short, since a mesher stopped at its
#: ceiling leaves exactly the narrow spots unrefined. It is a ceiling, not a target - the mesh
#: makes only the cells its refinement asks for.
CEILING_SLACK = 1.5
#: The engine param through which the user's choice reaches the run (snappy and cfMesh ParamSpec).
NARROW_GAPS_PARAM = "narrow_gaps"
FULL_RULE = "full"
WITHIN_LIMIT = "within_limit"
#: The record of a run's gap plan, beside its case (read by the resolution gates).
GAP_RECORD = "gap_sizing.json"


def _param_spec():
    from meshpipeline.engines.base import ParamSpec
    return ParamSpec(
        key=NARROW_GAPS_PARAM, values=(FULL_RULE, WITHIN_LIMIT), default="", required=False,
        ask=("NEVER ask this on your own. Set it ONLY when the measured engine recommendation says "
             "the narrow gaps need more cells than one job may build and the user, offered the "
             "choices it lists, picks fewer cells across the narrowest spots: then "
             f"'{WITHIN_LIMIT}' (the run puts as many across them as the cell limit allows, 13 "
             f"everywhere else). '{FULL_RULE}' keeps 13 cells across every gap. Leave it out "
             "otherwise."))


#: The optional engine param the user's choice travels in (declared by snappy and cfMesh).
NARROW_GAPS_PARAM_SPEC = _param_spec()

FITS = "fits"
RAISED = "raised"
OVER_LIMIT = "over_limit"
ACCEPTED_FEWER = "accepted_fewer"
#: over the limit, nobody chose fewer: refined to what the limit holds, judged at the full floor
LIMITED = "limited"
NO_NARROW = "none"


def accepted_fewer(engine_params) -> bool:
    """Whether the approved run chose fewer cells across the narrowest spots to stay within the
    cell limit (the `narrow_gaps` engine param)."""
    try:
        return str((engine_params or {}).get(NARROW_GAPS_PARAM) or "").strip().lower() == WITHIN_LIMIT
    except AttributeError:
        return False


def _mm(metres: float) -> str:
    v = float(metres) * 1000.0
    return f"{v:.2g} mm" if v < 10 else f"{v:.0f} mm"


def _cells(n: float) -> str:
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f} M"
    if n >= 1e3:
        return f"{n / 1e3:.0f} k"
    return f"{n:.0f}"


def box_cost(boxes, cell_m: float, *, whole_box: bool = False) -> float:
    """Cells a set of narrow-passage boxes adds: each box's volume at its own cell (cell_m halved
    `level_bump` times). `whole_box`: the mesher refines EVERY cell inside the box, fluid or not
    (snappyHexMesh refines the whole background before it keeps the fluid side), so the box's own
    volume is what it costs - offline, vmr_0092's 61 boxes priced at their fluid read 3.8 M and
    snappy's shell refinement passed 5.7 M one level in. Otherwise only the passage the wall
    points bound, as a round tube holds it (`fluid_m3`, area x r / 2 - exact for a tube, where
    narrow_passage_regions' own `volume_m3` counts it twice over), never more than the box.
    A box's `layer_cells` - the wall layers its finer wall cells carry, added after refinement -
    count too (box_layers)."""
    total = 0.0
    for b in boxes or ():
        c = float(b.get("cell_m") or float(cell_m) / (2 ** int(b.get("level_bump", 1))))
        if c <= 0.0:
            continue
        total += float(b.get("layer_cells") or 0.0)
        if b.get("refine_m3") is not None:
            # refined by distance from the spot's own wall (the snappy driver's surfaces)
            v = float(b["refine_m3"])
        elif whole_box and b.get("min") is not None and b.get("max") is not None:
            v = 1.0
            for k in range(3):
                v *= max(0.0, float(b["max"][k]) - float(b["min"][k]))
        else:
            v = float(b.get("fluid_m3", b.get("volume_m3")) or 0.0)
        total += v / c ** 3
    return total


def fill_cells(areas, radius, cell_m: float, depth: float | None = None) -> float:
    """Cells of size `cell_m` that fill the fluid a measured wall bounds - all of it, or only what
    lies within `depth` of the wall. Per wall point (its area, the local passage half-width r) the
    fluid is a tube's: area x d x (1 - d / 2r) for d = min(depth, r), which is area x r / 2 - a
    round tube's volume exactly - once d reaches r (the model snappy_runner.near_band_cost costs
    the near-wall band with)."""
    import numpy as np
    a = np.asarray(areas, dtype=float)
    r = np.asarray(radius, dtype=float)
    ok = np.isfinite(a) & np.isfinite(r) & (a > 0.0) & (r > 0.0)
    if cell_m <= 0.0 or not ok.any():
        return 0.0
    a, r = a[ok], r[ok]
    d = r if depth is None else np.minimum(float(depth), r)
    return float((a * d * (1.0 - d / (2.0 * r))).sum()) / float(cell_m) ** 3


@dataclass
class GapPlan:
    """The narrow spots of one pass and what meeting the rule there costs, against the budget."""

    status: str
    regions: list = field(default_factory=list)
    target: float = GAP_CELLS_ACROSS
    cost: float = 0.0
    base_cells: float = 0.0
    budget_in: int = 0
    budget: int = 0
    limit: int = 0
    full_cost: float = 0.0
    affordable: float | None = None
    affordable_cost: float | None = None
    narrowest_m: float | None = None
    spots: int = 0
    where_m: tuple | None = None
    #: the count the rule asks across every narrow spot (13; an engine may ask more)
    rule: float = GAP_CELLS_ACROSS
    #: the wall-layer cells of the mesh without the boxes (n_layers x its wall faces): added after
    #: refinement, so no refinement ceiling holds them (`base_cells` includes them)
    base_after: float = 0.0
    #: ...and the wall-layer cells of the mesh as planned: base_after plus the chosen boxes' own
    after_cells: float = 0.0

    @property
    def needed(self) -> float:
        """Cells a mesh meeting the rule everywhere comes to."""
        return self.base_cells + self.full_cost

    @property
    def resolution_short(self) -> bool:
        """The narrow spots get fewer cells across than the rule: the limit could not hold it (or
        the user chose fewer). Flagged in the run's record and its note; the mesh is measured by
        the resolution gate as every mesh is."""
        return self.status in (LIMITED, ACCEPTED_FEWER, OVER_LIMIT)

    def ceiling(self) -> int:
        """The cell ceiling the mesher REFINES to (snappyHexMesh's maxGlobalCells): the plan's own
        budget, plus room for the boxes' refined cells (their estimate x CEILING_SLACK - a ceiling
        at the estimate is one snappy stops a level short of), and never so high that the wall
        layers added after refinement take the mesh over the limit. Never under the plan's own
        budget: the mesh without the boxes is what the run built before this module. A plan that
        refines nothing more than planned (FITS, OVER_LIMIT, none) keeps the plan's budget."""
        if self.status in (FITS, OVER_LIMIT, NO_NARROW) or not self.regions:
            return int(self.budget_in)
        refined = max(0.0, self.cost - (self.after_cells - self.base_after))
        room = self.limit / BUDGET_HEADROOM - self.after_cells
        return int(max(self.budget_in, min(self.budget_in + refined * CEILING_SLACK, room)))

    def options(self) -> list[str]:
        """The user's choices when the limit cannot hold the rule, in plain words with numbers."""
        if self.status not in (OVER_LIMIT, ACCEPTED_FEWER, LIMITED):
            return []
        out = []
        where = (f", for example at ({', '.join(f'{v * 1000:.0f}' for v in self.where_m)}) mm"
                 if self.where_m else "")
        if self.affordable is not None and self.affordable_cost is not None:
            out.append(
                f"Accept about {self.affordable:.0f} cells across the {self.spots} narrowest spot"
                f"{'s' if self.spots != 1 else ''} (down to {_mm(self.narrowest_m or 0.0)} "
                f"across{where}) and {self.rule:.0f} everywhere else - about "
                f"{_cells(self.base_cells + self.affordable_cost * BUDGET_HEADROOM)} cells, "
                "inside the limit.")
        out.append(f"Keep {self.rule:.0f} cells across every gap - about "
                   f"{_cells(self.needed * BUDGET_HEADROOM)} cells, which needs the "
                   f"{_cells(self.limit)}-cell limit of one job raised.")
        out.append("Mesh a smaller piece of the geometry - the part you need, cut away from the "
                   f"narrowest spots{where}.")
        return out

    def words(self, cell_m: float | None = None, *, auto: bool = False) -> str:
        """What the run says about its narrow spots (`cell_m`: the cell they are measured
        against, named when given). `auto`: a run with no floor gate (external flow), which meshes
        over the limit with as many across as it holds rather than stopping."""
        if self.status == NO_NARROW or not self.regions and self.status != OVER_LIMIT:
            return ""
        if auto:
            head = (f"Gaps between parts - {self.spots} region(s) down to "
                    f"{_mm(self.narrowest_m or 0.0)} across")
            if self.status in (ACCEPTED_FEWER, LIMITED):
                return (f"{head}; {self.rule:.3g} cells across them would need about "
                        f"{_cells(self.needed * BUDGET_HEADROOM)} cells, over the "
                        f"{_cells(self.limit)} limit, so they are refined to about "
                        f"{self.target:.0f} across, as many as the limit holds")
            if self.status == OVER_LIMIT:
                return (f"{head}; {self.rule:.3g} cells across them would need about "
                        f"{_cells(self.needed * BUDGET_HEADROOM)} cells, over the "
                        f"{_cells(self.limit)} limit, so they keep the planned wall cell")
        rule = f"{self.target:.3g}" if self.status in (FITS, RAISED) else f"{self.rule:.3g}"
        head = (f"{'Gaps between parts' if auto else 'Narrow passages'} - {self.spots} region(s) down to "
                f"{_mm(self.narrowest_m or 0.0)} across"
                + (f", under {rule} cells at the {cell_m * 1000:.2g} mm cell at the wall"
                   if cell_m else ""))
        if self.status == FITS:
            return (f"{head}; refining them locally to {self.target:.3g} across (about "
                    f"{_cells(self.cost)} cells, inside the {_cells(self.budget)}-cell budget)")
        if self.status == RAISED:
            return (f"{head}; refining them locally to {self.target:.3g} across - about "
                    f"{_cells(self.cost)} cells more, so the cell budget was raised from "
                    f"{_cells(self.budget_in)} to {_cells(self.budget)} before meshing (the "
                    f"limit is {_cells(self.limit)})")
        if self.status == ACCEPTED_FEWER:
            return (f"{head}; {self.rule:.3g} cells across them would need about "
                    f"{_cells(self.needed * BUDGET_HEADROOM)} cells, over the {_cells(self.limit)} "
                    f"limit, so as you chose they are refined to about {self.target:.0f} across")
        if self.status == LIMITED:
            return (f"{head}; {self.rule:.3g} cells across every one of them would need about "
                    f"{_cells(self.needed * BUDGET_HEADROOM)} cells, over the {_cells(self.limit)} "
                    f"limit, so they are refined to about {self.target:.0f} across, as many as the "
                    f"limit holds; the mesh is still checked for {GAP_FLOOR_CELLS:.0f} cells across "
                    "its narrowest wall")
        # OVER_LIMIT: not even MIN_ACCEPTED_CELLS across fits beside the rest of the mesh - meshed
        # as planned, never stopped before meshing (the module docstring)
        return (f"{head}; {self.rule:.3g} cells across every one of them needs about "
                f"{_cells(self.needed * BUDGET_HEADROOM)} cells, more than the {_cells(self.limit)} "
                f"one job may build, and not even {MIN_ACCEPTED_CELLS:.0f} across them fits beside "
                "the rest of the mesh, so they are refined only as far as the planned cell budget "
                f"goes; the mesh is still checked for {GAP_FLOOR_CELLS:.0f} cells across its "
                "narrowest wall")

    def record(self) -> dict:
        return {"status": self.status, "target": round(self.target, 2),
                "rule_cells_across": self.rule, "floor": round(self.floor(), 2),
                "spots": self.spots, "narrowest_m": self.narrowest_m,
                "where_m": list(self.where_m) if self.where_m else None,
                "cost": int(self.cost), "full_cost": int(self.full_cost),
                "base_cells": int(self.base_cells), "budget_in": int(self.budget_in),
                "budget": int(self.budget), "limit": int(self.limit),
                "needed": int(self.needed * BUDGET_HEADROOM),
                "affordable": (round(self.affordable, 1) if self.affordable is not None
                               else None),
                "layer_cells": int(self.after_cells), "ceiling": self.ceiling(),
                "resolution_short": self.resolution_short}

    def floor(self) -> float:
        """The floor the resolution gate holds this run to: the rule's, or - when the user
        accepted fewer across the narrowest spots - the same share of what was accepted."""
        if self.status == ACCEPTED_FEWER:
            return min(GAP_FLOOR_CELLS, FLOOR_SHARE * self.target)
        return GAP_FLOOR_CELLS

    def write(self, workspace) -> None:
        try:
            (Path(workspace) / GAP_RECORD).write_text(json.dumps(self.record()), encoding="utf-8")
        except OSError:
            logger.warning("gap sizing: the record could not be written", exc_info=True)


def read_record(workspace) -> dict | None:
    try:
        d = json.loads((Path(workspace) / GAP_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def accepted_floor(workspace, engine_params) -> float | None:
    """The floor a resolution gate holds this mesh to when the approved run accepted fewer cells
    across its narrowest spots - read from the run's own gap record - or None (the rule's floor)."""
    if not accepted_fewer(engine_params):
        return None
    rec = read_record(workspace)
    if not rec or rec.get("status") != ACCEPTED_FEWER:
        return None
    try:
        f = float(rec.get("floor") or 0.0)
    except (TypeError, ValueError):
        return None
    return f if 0.0 < f < GAP_FLOOR_CELLS else None


#: The share of a plan's budget its narrow spots may spend without raising it - the share the
#: drivers held them to before (the near-wall band and the thin features each get the same).
PLAN_BOX_SHARE = 0.5


def plan_gaps(regions_at, *, cell_m: float, budget: int, limit: int,
              base_cells: float | None = None, target: float = GAP_CELLS_ACROSS,
              accept_fewer: bool = False, whole_box: bool = False,
              base_after: float = 0.0) -> GapPlan:
    """The gap plan of one pass. `regions_at(t)` returns the narrow-passage boxes that put `t`
    cells across every narrow spot (engines/passage.narrow_passage_regions with no budget, the
    spots always DETECTED at the rule's count so the same spots are named at any t). `cell_m` is
    the cell the boxes refine from, `budget` the plan's cell budget (the ceiling the mesher refines
    to), `limit` the compute limit on the DELIVERED mesh. `base_cells` is the refined mesh without
    the boxes; by default the plan's budget less the share its narrow spots were always allowed
    (PLAN_BOX_SHARE). `base_after` is the wall-layer cells that mesh carries (added after
    refinement, so under no ceiling); a box's own wall layers travel as its `layer_cells`.
    `whole_box` as in box_cost: the mesher refines the whole box (snappyHexMesh). Nothing here
    meshes anything."""
    budget = int(budget)
    limit = int(limit)
    refined = float((1.0 - PLAN_BOX_SHARE) * budget if base_cells is None else base_cells)
    after = max(0.0, float(base_after or 0.0))
    base = refined + after

    def layers(rs) -> float:
        return float(sum(float(b.get("layer_cells") or 0.0) for b in rs or ()))

    def plan(status, rs, t, cost, **kw) -> GapPlan:
        p = GapPlan(status, rs, t, cost, base_after=after, after_cells=after + layers(rs),
                    **kw)
        if status in (RAISED, LIMITED, ACCEPTED_FEWER):
            # the run's budget is the ceiling it refines to, the layers' room left under the limit
            p.budget = max(int(p.budget_in), p.ceiling())
        return p

    full = list(regions_at(float(target)) or [])
    if not full:
        return GapPlan(NO_NARROW, [], float(target), 0.0, base, budget, budget, limit,
                       base_after=after, after_cells=after)
    full_cost = box_cost(full, cell_m, whole_box=whole_box)
    narrowest = min(full, key=lambda b: float(b["radius_m"]))
    common: dict[str, Any] = {"narrowest_m": 2.0 * float(narrowest["radius_m"]), "spots": len(full),
              "where_m": tuple(round(0.5 * (float(narrowest["min"][k]) + float(narrowest["max"][k])),
                                     6) for k in range(3)),
              "base_cells": base, "budget_in": budget, "limit": limit, "full_cost": full_cost,
              "rule": float(target)}
    need = math.ceil((base + full_cost) * BUDGET_HEADROOM)
    if refined + full_cost - layers(full) <= budget and need <= limit:
        return plan(FITS, full, float(target), full_cost, budget=budget, **common)
    if need <= limit:
        return plan(RAISED, full, float(target), full_cost, budget=budget, **common)
    # OVER THE LIMIT: the most cells across the narrow spots the limit holds (bisection on the
    # count; the cost is a step function of it, one octree level per halving)
    room = limit / BUDGET_HEADROOM - base
    best: tuple[float, list, float] | None = None
    lo, hi = MIN_ACCEPTED_CELLS, float(target)
    if room > 0.0:
        low = list(regions_at(lo) or [])
        if box_cost(low, cell_m, whole_box=whole_box) <= room:
            best = (lo, low, box_cost(low, cell_m, whole_box=whole_box))
            for _ in range(12):
                mid = 0.5 * (lo + hi)
                rs = list(regions_at(mid) or [])
                c = box_cost(rs, cell_m, whole_box=whole_box)
                if c <= room:
                    best, lo = (mid, rs, c), mid
                else:
                    hi = mid
    # (a LOWER FLOOR is always the user's choice, never ours: unchosen, the mesh is refined to
    # what the limit holds and still held to the full floor - the gate measures, the estimate is
    # cautious, and a refusal before meshing would fail files that deliver)
    aff, aff_cost = (best[0], best[2]) if best is not None else (None, None)
    if best is not None:
        return plan(ACCEPTED_FEWER if accept_fewer else LIMITED, best[1], best[0], best[2],
                    budget=limit, affordable=aff, affordable_cost=aff_cost, **common)
    # not even MIN_ACCEPTED_CELLS across fits beside the rest: no boxes from this plan - the
    # caller meshes as planned (its boxes held to the plan's share, as before this module)
    return plan(OVER_LIMIT, [], float(target), 0.0, budget=budget, affordable=aff,
                affordable_cost=aff_cost, **common)


# #
# at approval: the estimate on the geometry check's own chords
# #

@dataclass(frozen=True)
class GapEstimate:
    """What the rule costs on the uploaded geometry, before any engine stages it."""

    cells: float                    # the leanest mesh meeting the rule: every gap at its own cell
    limit: int
    narrowest_m: float | None       # the narrowest measured gap, metres (when the unit is known)
    narrowest_rel: float | None     # ... over the part's size

    @property
    def over_limit(self) -> bool:
        return self.cells * BUDGET_HEADROOM > self.limit

    @property
    def affordable(self) -> float:
        """About how many cells across every gap the limit holds: the rule's cost goes with the
        cube of the count."""
        return GAP_CELLS_ACROSS * (self.limit / (BUDGET_HEADROOM * self.cells)) ** (1.0 / 3.0)

    def sentence(self) -> str:
        """What the user is told before approving, in plain words."""
        where = (f"the narrowest gap is about {_mm(self.narrowest_m)} across"
                 if self.narrowest_m else "the part has narrow gaps")
        if not self.over_limit:
            return (f"Cell sizing: {where}. Every gap gets at least {GAP_CELLS_ACROSS:.0f} cells "
                    f"across, refined only where it is narrow - at least about "
                    f"{_cells(self.cells)} cells, inside the {_cells(self.limit)}-cell limit of "
                    "one job.")
        return (f"Cell sizing: {where}. {GAP_CELLS_ACROSS:.0f} cells across every gap needs at "
                f"least about {_cells(self.cells * BUDGET_HEADROOM)} cells, more than the "
                f"{_cells(self.limit)} one job may build. You can pick one: (1) accept "
                f"fewer cells across the narrowest spots - about {max(self.affordable, 1):.0f} or "
                "more, as many as the limit holds, with the spots named in the result; (2) have "
                "the cell limit raised; (3) mesh a smaller piece of the geometry. With no pick, "
                "the narrowest spots get as many cells across as the limit holds, and the mesh "
                f"is still checked for {GAP_FLOOR_CELLS:.0f} across its narrowest wall.")

    def guidance(self, takers: tuple[str, ...] | None = None) -> str:
        """What the intake model is told to do with the user's pick. `takers` are the engines (by
        label) that declare the narrow_gaps choice; None = not said. An engine without it would
        refuse the parameter, so the model is told to send it only with one of them."""
        if not self.over_limit:
            return ""
        if takers is not None and not takers:
            return ("None of the engines listed takes this choice: leave "
                    f"{NARROW_GAPS_PARAM} out whatever the user picks.")
        only = (f" This choice is only for {' and '.join(takers)}: with any other engine leave "
                f"{NARROW_GAPS_PARAM} out (it does not take it)." if takers else "")
        return (f"If the user picks (1), submit engine_params {NARROW_GAPS_PARAM}="
                f"'{WITHIN_LIMIT}'. Otherwise leave {NARROW_GAPS_PARAM} out: the run then checks "
                "the limit again before meshing and, if it still cannot hold the rule, refines the "
                "narrowest spots as far as the limit holds and holds the mesh to the full floor."
                + only)


def estimate_from_traits(traits, *, limit: int | None = None) -> GapEstimate | None:
    """The rule's cost on the geometry check's measurement (cad/shape_traits `gap_cells`, a
    dimensionless count, so it needs no unit), or None when the chords did not measure it.
    `traits` is a ShapeTraits or its dict (a recommendation carries the dict)."""
    from collections.abc import Mapping

    def get(key: str):
        return traits.get(key) if isinstance(traits, Mapping) else getattr(traits, key, None)

    cells = get("gap_cells")
    if not isinstance(cells, (int, float)) or isinstance(cells, bool) or cells <= 0:
        return None
    if limit is None:
        import meshpipeline.settings.policy as polcfg
        limit = int(polcfg.CELL_HARD_LIMIT)
    return GapEstimate(cells=float(cells), limit=int(limit), narrowest_m=get("narrowest_gap"),
                       narrowest_rel=get("narrowest_gap_rel"))


__all__ = ["ACCEPTED_FEWER", "BUDGET_HEADROOM", "CEILING_SLACK", "FITS", "FLOOR_SHARE", "FULL_RULE", "GAP_CELLS_ACROSS",
           "GAP_FLOOR_CELLS", "GAP_RECORD", "LIMITED", "MIN_ACCEPTED_CELLS", "NARROW_GAPS_PARAM", "NARROW_GAPS_PARAM_SPEC", "NO_NARROW",
           "OVER_LIMIT", "PLAN_BOX_SHARE", "RAISED", "WITHIN_LIMIT", "GapEstimate", "GapPlan", "accepted_floor",
           "accepted_fewer", "box_cost", "estimate_from_traits", "fill_cells", "plan_gaps",
           "read_record"]
