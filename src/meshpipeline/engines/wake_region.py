# Responsibility: Size an external flow's wake refinement: graded boxes downstream of the body, along the declared flow.
# Owns: the plan's "wake" knob (defaults, reading, validation), the boxes' geometry and their cell cost against the budget.
# Boundaries: engine-neutral arithmetic on a body box, a domain box and a cell size per level; it reads no file and
#       renders no dictionary - an engine turns the tiers into its own syntax.
# Collaborates with: engines/far_field.py (the flow axis reading), engines/ground_plane.py (the floor),
#       engines/snappy/snappy_runner.py (renders the tiers as refinement boxes).
"""The wake behind an external body, refined on purpose.

Without it the cells behind a body coarsen straight back to the background mesh a few surface
cells downstream: the near-wall distance bands stop and nothing else asks for resolution. The
review then reads "the wake behind the body is not resolved - drag and separation will be wrong",
asks for a rebuild, and the rebuild cannot answer it because nothing in the case can change (job
679ae2a9, an F1 front wing; the CRM high-lift demo before it).

Standard external-aero practice is a set of nested boxes behind the body, each one level coarser
and longer than the one inside it, so the wake is resolved where it is strong and the mesh grades
smoothly back to the far field. That is what this plans:

- along the declared flow axis and sign, starting a little upstream of the body's trailing extent
  (so the separation off the body's rear shares the refinement and there is no seam), reaching
  `length` wake lengths downstream, clipped to the domain;
- tier k reaches length * 2**(k - (tiers-1)) downstream - with the defaults 1, 2 and 4 wake
  lengths - at `levels_below_surface + k` levels coarser than the wall;
- its cross-section is the body's cross-flow extent grown by a tenth of itself, plus a slow
  spread with distance (the wake widens) and two of its own cells;
- a body on the ground keeps every box down on the floor;
- the boxes' cell cost is estimated and held to a share of the run's cell budget: over it, the
  boxes are made shorter and then coarser, one step at a time, and every step is said.

The WAKE LENGTH is the body's size the wake scales with: the stated reference length, or the
body's length along the flow, or - for a disc facing the flow, a rotor or a propeller - its smaller
cross-flow size, whichever is largest.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from meshpipeline.engines.ground_plane import VERTICAL_AXIS

#: The plan's "wake" field when it says nothing: on, four wake lengths downstream in three graded
#: boxes, the finest one level coarser than the wall, starting a tenth of the body's length
#: upstream of its trailing extent.
WAKE_DEFAULTS: dict = {"enabled": True, "length": 4.0, "levels_below_surface": 1, "tiers": 3,
                       "start": 0.1}
#: The share of the run's cell budget the wake may spend. The near-wall band and the thin-feature
#: boxes are held to shares of the same budget (snappy_runner.NEAR_BAND_BUDGET_SHARE).
WAKE_BUDGET_SHARE = 0.25
#: Each side of the cross-section grows by this fraction of the body's own extent on that axis ...
WAKE_PAD = 0.1
#: ... and by this much per metre downstream (about 3 degrees: the wake widens as it goes).
WAKE_SPREAD = 0.05
#: Cells of its own level every box keeps beyond the body on each side, so a thin body's wake is
#: never a box thinner than its cells.
WAKE_EDGE_CELLS = 2.0
#: The transition cells a mesher adds outside each box (snappyHexMesh nCellsBetweenLevels 3),
#: counted in the cost so the estimate does not undershoot the boxes' real price.
WAKE_BUFFER_CELLS = 3.0
#: Knob bounds - what the plan may ask for.
MAX_TIERS = 4
MAX_LENGTH = 20.0
MAX_START = 1.0
#: Below this many wake lengths a budget-shortened box is not worth keeping.
MIN_LENGTH = 0.5

_KNOBS = ("enabled", "length", "levels_below_surface", "tiers", "start")


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def validate_wake(raw: object) -> list[tuple[str, str]]:
    """(path, message) for every wrong thing in a plan's "wake" field; empty when it is usable.
    The field is false (no wake refinement), true (the defaults) or an object of knobs."""
    if raw is None or isinstance(raw, bool):
        return []
    if not isinstance(raw, Mapping):
        return [("wake", "wake must be false, true or an object {enabled, length, "
                         "levels_below_surface, tiers, start}")]
    out: list[tuple[str, str]] = []
    for k in sorted(set(raw) - set(_KNOBS)):
        out.append((f"wake.{k}", f"unknown wake knob {k!r} - only {list(_KNOBS)}"))
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        out.append(("wake.enabled", "enabled must be true or false"))
    if "length" in raw and not (_is_num(raw["length"]) and 0.0 <= float(raw["length"]) <= MAX_LENGTH):
        out.append(("wake.length", f"length must be a number of wake lengths from 0 to {MAX_LENGTH:g} "
                                   "(0 = no wake refinement)"))
    if "levels_below_surface" in raw and not (
            isinstance(raw["levels_below_surface"], int)
            and not isinstance(raw["levels_below_surface"], bool)
            and 1 <= raw["levels_below_surface"] <= 6):
        out.append(("wake.levels_below_surface",
                    "levels_below_surface must be an integer from 1 to 6"))
    if "tiers" in raw and not (isinstance(raw["tiers"], int) and not isinstance(raw["tiers"], bool)
                               and 1 <= raw["tiers"] <= MAX_TIERS):
        out.append(("wake.tiers", f"tiers must be an integer from 1 to {MAX_TIERS}"))
    if "start" in raw and not (_is_num(raw["start"]) and 0.0 <= float(raw["start"]) <= MAX_START):
        out.append(("wake.start", f"start must be a number of body lengths from 0 to {MAX_START:g}"))
    return out


def wake_knobs(raw: object) -> dict:
    """The plan's "wake" field read into whole knobs: absent or true is the defaults, false is
    off, an object overrides the defaults key by key. A value that fails validate_wake keeps its
    default - the renderer never fails a mesh over a wake knob."""
    out = dict(WAKE_DEFAULTS)
    if raw is False:
        out["enabled"] = False
        return out
    if not isinstance(raw, Mapping):
        return out
    bad = {p.split(".", 1)[1] for p, _ in validate_wake(raw) if "." in p}
    for k in _KNOBS:
        if k in raw and k not in bad:
            out[k] = raw[k]
    out["length"] = float(out["length"])
    out["start"] = float(out["start"])
    if out["length"] <= 0.0:
        out["enabled"] = False
    return out


@dataclass(frozen=True)
class WakeRequest:
    """What a renderer needs to plan the wake: the plan's knobs, the declared flow axis (None:
    read from where the domain has the most room) and the stated reference length."""
    knobs: object = None
    flow_axis: str | None = None
    ruler_m: float | None = None


@dataclass(frozen=True)
class WakeTier:
    level: int                      # refinement level (cell = base cell / 2**level)
    cell_m: float                   # the cell edge that level gives
    box_min: list[float]
    box_max: list[float]
    reach_m: float                  # how far past the body's trailing extent the box reaches
    cells: float                    # estimated cells this tier adds


@dataclass
class WakePlan:
    tiers: list[WakeTier] = field(default_factory=list)
    knobs: dict = field(default_factory=dict)       # what was asked (whole knobs)
    axis: int = 0
    sign: int = 1
    scale_m: float = 0.0                             # the wake length
    trailing_m: float = 0.0                          # the body's trailing extent on the axis
    cells: float = 0.0                               # estimated cells, all tiers
    allowance: float = 0.0                           # the cells it may spend
    budget: float = 0.0                              # the run's cell budget
    body_cells: float | None = None                  # the estimated cells the body itself needs
    changes: list[str] = field(default_factory=list)   # budget cuts, in plain words
    reason: str = ""                                 # why there is no wake, when there is none

    @property
    def active(self) -> bool:
        return bool(self.tiers)

    def record(self) -> dict:
        """The honest record (manifest, run summary): what was built and what was cut."""
        return {
            "enabled": self.active,
            "axis": "xyz"[self.axis], "sign": "+" if self.sign > 0 else "-",
            "wake_length_m": round(self.scale_m, 6),
            "trailing_edge_m": round(self.trailing_m, 6),
            "requested": dict(self.knobs),
            "tiers": [{"level": t.level, "cell_m": round(t.cell_m, 6),
                       "reach_m": round(t.reach_m, 6),
                       "reach_wake_lengths": round(t.reach_m / self.scale_m, 3)
                       if self.scale_m else None,
                       "box_min": [round(v, 6) for v in t.box_min],
                       "box_max": [round(v, 6) for v in t.box_max],
                       "est_cells": int(t.cells)} for t in self.tiers],
            "est_cells": int(self.cells), "budget_allowance": int(self.allowance),
            "budget_cells": int(self.budget),
            "body_cells_estimate": int(self.body_cells) if self.body_cells is not None else None,
            "reduced": list(self.changes), "reason": self.reason,
        }

    def summary(self) -> str:
        """One plain sentence for the run note."""
        if not self.active:
            return f"no wake refinement - {self.reason}" if self.reason else "no wake refinement"
        reach = self.tiers[-1].reach_m / self.scale_m if self.scale_m else 0.0
        cells = " / ".join(f"{_mm(t.cell_m)}" for t in self.tiers)
        s = (f"wake refined {reach:.3g} body lengths downstream in {len(self.tiers)} graded "
             f"box{'es' if len(self.tiers) > 1 else ''} (cells {cells}, about "
             f"{self.cells / 1e6:.2g} M)")
        if self.changes:
            s += (f"; to fit the {self.budget / 1e6:.2g} M cell budget"
                  + (f" (the body itself needs about {self.body_cells / 1e6:.2g} M)"
                     if self.body_cells is not None else "")
                  + " it was " + ", then ".join(self.changes))
        return s


def _mm(m: float) -> str:
    return f"{m * 1000:.3g} mm" if m < 1.0 else f"{m:.3g} m"


def _flow_of(flow_axis: str | None, body_min, body_max, domain_min, domain_max) -> tuple[int, int]:
    """(axis, sign) of the flow: the declared one, else the side of the body with the most
    domain behind it - every far-field sizing puts the most room downstream."""
    if flow_axis and str(flow_axis).strip():
        from meshpipeline.engines.far_field import flow_axis_of
        return flow_axis_of(flow_axis)
    best, out = -1.0, (0, 1)
    for i in range(3):
        for sign in (1, -1):
            room = (float(domain_max[i]) - float(body_max[i]) if sign > 0
                    else float(body_min[i]) - float(domain_min[i]))
            if room > best + 1e-12:
                best, out = room, (i, sign)
    return out


def wake_scale(body_min, body_max, axis: int, ruler_m: float | None) -> float:
    """The wake length: the stated reference length, the body's length along the flow, or its
    smaller cross-flow size (a rotor or propeller disc facing the flow), whichever is largest."""
    ext = [float(body_max[i]) - float(body_min[i]) for i in range(3)]
    cross = [ext[j] for j in range(3) if j != axis]
    r = float(ruler_m) if _is_num(ruler_m) and float(ruler_m or 0) > 0 else 0.0  # type: ignore[arg-type]
    return max(r, ext[axis], min(cross))


def _volume(lo: Sequence[float], hi: Sequence[float]) -> float:
    return math.prod(max(0.0, float(hi[i]) - float(lo[i])) for i in range(3))


def _grown(lo, hi, by: float, dmin, dmax) -> tuple[list[float], list[float]]:
    return ([max(float(dmin[i]), float(lo[i]) - by) for i in range(3)],
            [min(float(dmax[i]), float(hi[i]) + by) for i in range(3)])


def _tiers(*, finest: int, n: int, length: float, start: float, body_min, body_max,
           domain_min, domain_max, axis: int, sign: int, scale: float, base_cell: float,
           grounded: bool) -> list[WakeTier]:
    ext = [float(body_max[i]) - float(body_min[i]) for i in range(3)]
    trailing = float(body_max[axis]) if sign > 0 else float(body_min[axis])
    lead_in = start * ext[axis]
    out: list[WakeTier] = []
    # level 0 is the background: no box to draw. With fewer levels than tiers the coarsest box
    # that remains still reaches the whole length.
    n = max(0, min(int(n), int(finest)))
    for k in range(n):
        level = finest - k
        cell = base_cell / 2.0 ** level
        reach = length * scale * 2.0 ** (k - (n - 1))
        lo = [0.0, 0.0, 0.0]
        hi = [0.0, 0.0, 0.0]
        if sign > 0:
            lo[axis], hi[axis] = trailing - lead_in, trailing + reach
        else:
            lo[axis], hi[axis] = trailing - reach, trailing + lead_in
        for j in range(3):
            if j == axis:
                continue
            grow = WAKE_PAD * ext[j] + WAKE_SPREAD * reach + WAKE_EDGE_CELLS * cell
            lo[j], hi[j] = float(body_min[j]) - grow, float(body_max[j]) + grow
            if grounded and j == VERTICAL_AXIS:
                lo[j] = float(domain_min[j])       # the body stands on the floor: so does its wake
        lo = [max(float(domain_min[i]), lo[i]) for i in range(3)]
        hi = [min(float(domain_max[i]), hi[i]) for i in range(3)]
        if any(hi[i] - lo[i] <= 0.0 for i in range(3)):
            break
        out.append(WakeTier(level=level, cell_m=cell, box_min=lo, box_max=hi,
                            reach_m=min(reach, abs((hi[axis] if sign > 0 else lo[axis]) - trailing)),
                            cells=0.0))
    # the cost: each tier's cells sit in its box (with the mesher's transition cells round it)
    # less the finer box inside it
    priced: list[WakeTier] = []
    inner = 0.0
    for t in out:
        glo, ghi = _grown(t.box_min, t.box_max, WAKE_BUFFER_CELLS * t.cell_m, domain_min, domain_max)
        vol = _volume(glo, ghi)
        priced.append(WakeTier(level=t.level, cell_m=t.cell_m, box_min=t.box_min,
                               box_max=t.box_max, reach_m=t.reach_m,
                               cells=max(0.0, vol - inner) / t.cell_m ** 3))
        inner = vol
    return priced


def plan_wake(*, body_min, body_max, domain_min, domain_max, flow_axis: str | None,
              base_cell: float, surface_level: int, budget_cells: float, knobs: object = None,
              ruler_m: float | None = None, grounded: bool = False,
              body_cells: float | None = None,
              share: float = WAKE_BUDGET_SHARE) -> WakePlan:
    """The wake's boxes for this body, domain and mesh. `base_cell` is the background cell the
    mesher really uses and `surface_level` the wall's level on it; `knobs` is the plan's "wake"
    field (see wake_knobs). The boxes may spend `share` of `budget_cells`, and never more than
    the budget leaves once the body's own `body_cells` (an estimate, when the engine has one) are
    paid for: over that they are cut - first shorter by half, then one level coarser,
    alternately - until they fit, and each cut is recorded."""
    kn = wake_knobs(knobs)
    axis, sign = _flow_of(flow_axis, body_min, body_max, domain_min, domain_max)
    scale = wake_scale(body_min, body_max, axis, ruler_m)
    allowance = max(0.0, float(share) * float(budget_cells))
    if body_cells is not None and _is_num(body_cells):
        allowance = min(allowance, max(0.0, float(budget_cells) - float(body_cells)))
    plan = WakePlan(knobs=kn, axis=axis, sign=sign, scale_m=scale,
                    trailing_m=float(body_max[axis]) if sign > 0 else float(body_min[axis]),
                    allowance=allowance, budget=float(budget_cells),
                    body_cells=float(body_cells) if body_cells is not None and _is_num(body_cells) else None)
    if not kn["enabled"]:
        plan.reason = "the plan asked for none"
        return plan
    if not (_is_num(base_cell) and base_cell > 0.0 and scale > 0.0):
        plan.reason = "the body or the background cell has no size to scale it by"
        return plan
    finest = int(surface_level) - int(kn["levels_below_surface"])
    if finest < 1:
        plan.reason = ("the wall is already within a level of the background mesh, so the far "
                       "field is as fine as a wake box would be")
        return plan
    length, n = float(kn["length"]), int(kn["tiers"])
    changes: list[str] = []
    shortened = False
    while True:
        tiers = _tiers(finest=finest, n=n, length=length, start=float(kn["start"]),
                       body_min=body_min, body_max=body_max, domain_min=domain_min,
                       domain_max=domain_max, axis=axis, sign=sign, scale=scale,
                       base_cell=float(base_cell), grounded=grounded)
        cost = sum(t.cells for t in tiers)
        if tiers and cost <= plan.allowance:
            plan.tiers, plan.cells, plan.changes = tiers, cost, changes
            return plan
        if not tiers:
            plan.reason = "the domain leaves no room behind the body for one"
            return plan
        # shorter first (the near wake keeps its cells), then coarser; alternate
        if not shortened and length / 2.0 >= MIN_LENGTH:
            length /= 2.0
            shortened = True
            changes.append(f"shortened to {length:.3g} body lengths")
        elif finest > 1:
            finest -= 1
            shortened = False
            changes.append(f"made one level coarser (cells {_mm(base_cell / 2.0 ** finest)})")
        elif length / 2.0 >= MIN_LENGTH:
            length /= 2.0
            changes.append(f"shortened to {length:.3g} body lengths")
        else:
            plan.reason = (f"even its smallest form (about {cost / 1e6:.2g} M cells) does not "
                           f"fit the {plan.allowance / 1e6:.2g} M cells the "
                           f"{plan.budget / 1e6:.2g} M budget leaves for it"
                           + (f" after the body's own (about {plan.body_cells / 1e6:.2g} M)"
                              if plan.body_cells is not None else "")
                           + "; raise max_cells for a refined wake")
            plan.changes = changes
            return plan


__all__ = ["MAX_TIERS", "WAKE_BUDGET_SHARE", "WAKE_DEFAULTS", "WakePlan", "WakeRequest", "WakeTier",
           "plan_wake", "validate_wake", "wake_knobs", "wake_scale"]
