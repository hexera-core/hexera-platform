# Responsibility: Say how much work a step is planned to do, and turn that into the one clock it may have: a guard set from the planned work, never a decision.
# Owns: the TIME rule IDs (docs/RULES.md), the guard factor, each engine's slow fill rate and old fixed cap, the guard arithmetic, the guard-trip outcome (replay once, then "ran out of time"), and the words for a trip.
# Boundaries: pure arithmetic over plain numbers. It reads no clock, starts nothing and records nothing: engines/determinism.note_guard_trip records a trip, pipeline/graph acts on one.
# Collaborates with: the native runners and their callers (engines/dispatch, engines/snappy/drivers, engines/cfmesh/cfmesh_runner, agents/builder/tools/meshing), pipeline/executor and pipeline/graph (a trip ends the attempt as ours), contracts/decision_record (the work-limit and guard entries replay through the functions here), the FEA driver (the same interface).
"""PLANNED WORK DECIDES. THE CLOCK ONLY GUARDS.

A run must come out the same on a quiet machine and on a busy one. So nothing a run decides may
depend on how long something took: not whether a step runs, not how coarse a pass is, not which
engine runs next, not the verdict. Each of those is decided by PLANNED WORK - cells, iterations,
triangles, samples, ladder steps, attempts - which is the same on every machine.

A clock is still needed, so a run that hangs cannot hold a machine forever. It is a GUARD:

    planned seconds = planned work / the step's slow rate (the 10th percentile the lab measured)
    guard           = GUARD_FACTOR x planned seconds,
                      never under the step's old fixed cap (so nothing that finished before is
                      cut now), never over the ceiling the job around it allows

A guard that trips says nothing about the shape. It is an INFRASTRUCTURE failure: the same attempt
is replayed once (TIME-3), and a second trip ends the run as "ran out of time". A trip never picks
a lever, skips a step, makes a pass coarser, picks or switches an engine, or changes a verdict.

THE INTERFACE (any driver, the FEA driver included):

    work = guard_for("snappy", cells=2_000_000)      # or PlannedWork(...) for a step of your own
    run(..., timeout=work.guard_seconds)             # the guard is the only clock
    if it timed out:
        determinism.note_guard_trip(work.where, workspace=ws, **work.as_dict())
        ... and end the attempt (the executor reads the trip and the graph replays it)

Every rule ID below is written in docs/RULES.md; tests/unit/hygiene/test_rulebook.py holds the
two together.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

# #
# the rules (docs/RULES.md, "TIME")
# #

#: Planned work decides: a step runs its planned work, and the clock never decides what a run does.
TIME_PLANNED_WORK = "TIME-1"
#: The guard is GUARD_FACTOR times the planned time at the slow rate, within the floor and ceiling.
TIME_GUARD = "TIME-2"
#: A guard that trips is an infrastructure failure: replayed once, then "ran out of time".
TIME_TRIP = "TIME-3"
#: A wait for a measurement or an answer always waits; its guard tripping is that step failing.
TIME_WAIT = "TIME-4"
#: The job-level guards (the builder attempt and total, the pipeline, the reviewer, the planner).
TIME_JOB_GUARD = "TIME-5"
#: Clocks that still decide, left for the CAD repair work (#201): inspection and repair.
TIME_REPAIR_CLOCK = "TIME-6"

TIME_RULES = (TIME_PLANNED_WORK, TIME_GUARD, TIME_TRIP, TIME_WAIT, TIME_JOB_GUARD,
              TIME_REPAIR_CLOCK)

# #
# the numbers
# #

#: A guard is this many times the planned time (determinism plan, section 7).
GUARD_FACTOR = 3.0
#: How many times an attempt whose guard tripped is replayed, as the same attempt, before the run
#: ends as "ran out of time" (TIME-3).
GUARD_REPLAYS = 1

#: Each engine's SLOW fill rate, in cells per second of the whole native run (meshing, checkMesh,
#: the exports): about the 10th percentile over every delivered mesh of the e2e sweeps up to
#: 2026-10-20 (snappy 1,752 meshes, p10 1,513 / p50 2,713; cfMesh 1,272, p10 1,229 / p50 4,953 -
#: threaded, refitted for one thread in D5; VMTK 60, p10 4,185). An engine with no lab record has
#: none, and its guard is its old fixed cap.
FILL_RATES: Mapping[str, float] = {
    "snappy": 1500.0,
    "cfmesh": 1200.0,
    "vmtk": 4000.0,
}

#: Each engine's old fixed run cap (seconds): the FLOOR of its guard, so a run that finished under
#: the old cap is never cut by the new guard.
FLOORS_S: Mapping[str, float] = {
    "snappy": 2400.0,
    "cfmesh": 1200.0,
    "vmtk": 3000.0,
    "gmsh": 1500.0,
    "snappy_multiregion": 3000.0,
    "snapgrid": 3600.0,
}

#: The longest guard one native run may get (seconds). snappy: three passes fit the Cloud Run mesh
#: job's 4-hour task timeout with room for the replay's. The others keep their old caps as the
#: ceiling: their run sits inside one builder attempt (BUILDER_LOOP_TIMEOUT, 3,600 s).
CEILINGS_S: Mapping[str, float] = {
    "snappy": 4800.0,
    "cfmesh": 3000.0,
    "vmtk": 3000.0,
    "gmsh": 1500.0,
    "snappy_multiregion": 3000.0,
    "snapgrid": 3600.0,
}


@dataclass(frozen=True)
class PlannedWork:
    """One step's planned work and the guard that follows from it.

    where      the step ("snappy", "cfmesh.rescue", "fea.gmsh", ...)
    unit       what the work is counted in ("cells", "iterations", "triangles", "elements", ...)
    amount     how much of it is planned (0 or None when the plan does not say)
    rate       the step's slow rate in units per second (None when there is no lab record)
    floor_s    the least guard (the step's old fixed cap)
    ceiling_s  the most guard (what the job around the step allows)
    steps      planned passes of the same work inside the step (a rescue's extra runs), >= 1
    """
    where: str
    unit: str
    amount: float | None
    rate: float | None
    floor_s: float
    ceiling_s: float
    steps: float = 1.0

    @property
    def planned_seconds(self) -> float | None:
        """The planned time at the slow rate, or None when the plan or the rate is missing."""
        if not self.amount or not self.rate or self.amount <= 0 or self.rate <= 0:
            return None
        return float(self.amount) / float(self.rate) * max(1.0, float(self.steps))

    @property
    def guard_seconds(self) -> int:
        return guard_seconds(self.planned_seconds, floor_s=self.floor_s, ceiling_s=self.ceiling_s)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["planned_s"] = None if self.planned_seconds is None else round(self.planned_seconds, 1)
        d["guard_s"] = self.guard_seconds
        d["factor"] = GUARD_FACTOR
        return d


def guard_seconds(planned_seconds: float | None, *, floor_s: float, ceiling_s: float,
                  factor: float = GUARD_FACTOR) -> int:
    """TIME-2: factor x the planned time, never under the floor, never over the ceiling (the
    floor wins when the two cross: an old cap is never shortened). No plan: the floor."""
    floor = max(1.0, float(floor_s))
    ceiling = max(floor, float(ceiling_s))
    if not planned_seconds or planned_seconds <= 0:
        return int(floor)
    return int(min(ceiling, max(floor, factor * float(planned_seconds))))


def guard_for(engine: str, *, cells: float | None = None, steps: float = 1.0,
              floor_s: float | None = None, ceiling_s: float | None = None) -> PlannedWork:
    """The planned work and guard of one native run of `engine` planned to make `cells` cells."""
    floor = float(floor_s if floor_s is not None else FLOORS_S.get(engine, 1800.0))
    ceiling = float(ceiling_s if ceiling_s is not None else CEILINGS_S.get(engine, floor))
    return PlannedWork(where=engine, unit="cells", amount=(float(cells) if cells else None),
                       rate=FILL_RATES.get(engine), floor_s=floor, ceiling_s=ceiling,
                       steps=steps)


def work_limit(inputs: Mapping[str, Any]) -> tuple[int, str]:
    """The guard a step was given, decided again from its recorded planned work (the why record's
    `work_limit` entry): (guard seconds, TIME-2). Nothing here reads the clock, so the same plan
    gives the same guard on any machine."""
    try:
        work = PlannedWork(where=str(inputs.get("where") or ""), unit=str(inputs.get("unit") or ""),
                           amount=_num(inputs.get("amount")), rate=_num(inputs.get("rate")),
                           floor_s=float(inputs.get("floor_s") or 1.0),
                           ceiling_s=float(inputs.get("ceiling_s") or 1.0),
                           steps=float(inputs.get("steps") or 1.0))
    except (TypeError, ValueError):
        return 0, ""
    return work.guard_seconds, TIME_GUARD


#: What a run does after a guard trip (TIME-3).
REPLAY = "replay"
RAN_OUT_OF_TIME = "ran_out_of_time"


def after_trip(inputs: Mapping[str, Any]) -> tuple[str, str]:
    """(what the run does after a guard trip, TIME-3). inputs: replays (how many times this attempt
    was already replayed after a trip), allowed (GUARD_REPLAYS). How long anything took is not an
    input."""
    raw = inputs.get("allowed")
    try:
        done = int(inputs.get("replays") or 0)
        allowed = GUARD_REPLAYS if raw is None else int(raw)
    except (TypeError, ValueError):
        return "", ""
    return (REPLAY if done < allowed else RAN_OUT_OF_TIME), TIME_TRIP


def trip_words(trip: Mapping[str, Any] | None) -> str:
    """One plain sentence for a trip: where, and the guard it hit."""
    trip = trip or {}
    where = str(trip.get("where") or "a step")
    guard = _num(trip.get("guard_s") or trip.get("budget_s"))
    planned = _num(trip.get("planned_s"))
    out = f"{where} ran past its guard clock"
    if guard:
        out += f" ({guard / 60.0:.0f} min"
        out += f", about {GUARD_FACTOR:g} times its planned {planned / 60.0:.0f} min)" if planned \
            else ")"
    return out


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["CEILINGS_S", "FILL_RATES", "FLOORS_S", "GUARD_FACTOR", "GUARD_REPLAYS", "PlannedWork",
           "RAN_OUT_OF_TIME", "REPLAY", "TIME_GUARD", "TIME_JOB_GUARD", "TIME_PLANNED_WORK",
           "TIME_REPAIR_CLOCK", "TIME_RULES", "TIME_TRIP", "TIME_WAIT", "after_trip",
           "guard_for", "guard_seconds", "trip_words", "work_limit"]
