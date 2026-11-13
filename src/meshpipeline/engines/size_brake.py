# Responsibility: Stop a native mesher (snappyHexMesh, cartesianMesh) whose mesh is going to come out far bigger than its plan,
# as soon as its own log says so - after castellation, and before the layer stage spends hours on it.
# Owns: the size brake's margin, reading snappy's cell counts as they are printed, and the verdict.
# Boundaries: reads the run's log only; the run is stopped by sandbox/safe_exec (the `watch` hook) and
# re-planned smaller by the snappy driver (engines/snappy/drivers, planner.coarsen_after_timeout) or the cfMesh builder.
"""A DETERMINISTIC SIZE BRAKE, NOT A CLOCK (Areen, 25 Oct).

The old 2,400 s cap was quietly acting as a size brake: on the two propeller-rig plates the lab plan
asked for 2M cells, the first pass was cut by the clock and re-planned smaller (1.8M / 2.8M). With
the clock gone (contracts/native_time) the full first pass ran to 7.1M and 13.0M cells - the layer
stage alone added 10M on A2's 2.2M wall faces. snappyHexMesh prints its own counts as it goes, and
they are the same on every run of the same case:

  "Refined mesh : cells:N"            - the castellated mesh, when castellation ends
  "Ideal layer addition : cells:X"    - the mesh WITH every requested layer, printed after snapping
                                        and BEFORE the layer stage runs (the final count is within a
                                        few percent of it: 0.92-1.00 of it on 70 lab runs)

The brake reads them. When the castellated count passes SIZE_BRAKE_MARGIN times the case's own cell
budget (its plan's, PLAN_CELLS_FILE), or the with-layers count passes SIZE_BRAKE_LAYER_MARGIN times
it, the run is stopped there and the driver re-plans the pass with a smaller budget.

THE MARGIN, FROM DATA: on 71 finished external and internal lab runs (plan = maxGlobalCells), the
castellated count read 0.03-0.83x the plan at the 95th percentile (one 1.62x), and the ideal-with-
layers count 0.04-1.18x - with nothing between 1.18x and 3.2x (hx_shell_side 3.29x, then the plates
at 3.5x and 6.5x). 1.5x sits in that gap for the castellated count.

THE WITH-LAYERS COUNT GETS 2.0x: it is what snappy WOULD make if every layer went on everywhere, and
on a thin plate it is well above what comes out. A2.SPlate's pass at a 1M castellation budget read
3,403,690 cells with layers - 1.70x its 2M plan - and finished production-grade at 2,776,512 (1.39x,
0.82 of the estimate; main, lab 25 Oct). At 1.5x the brake stopped that good pass and cut the plate
to 0.54M (lab, cfdtime-brake3). 2.0x keeps it, and still stops every run that was really running
away (hx_shell_side 3.29x, the plates' first passes at 3.5x and 6.5x).

External bodies only, for now: the internal path's budget is raised by its own gap and thin-feature
boxes after the plan, and two internal vessels read 2.3-2.7x their lab plan while staying inside the
cell limit; they keep meshing as before until the internal budget is measured the same way."""
from __future__ import annotations

import re
from pathlib import Path

#: How far over its own cell budget a run's castellated mesh may come before it is stopped and re-planned.
SIZE_BRAKE_MARGIN = 1.5
#: The same for snappy's with-layers estimate, which reads above what the layer stage really adds
#: (A2.SPlate: estimate 1.70x the plan, finished mesh 1.39x) - see the module docstring.
SIZE_BRAKE_LAYER_MARGIN = 2.0
#: The re-planned budget aims this far under the budget the overshoot is measured against, so the
#: next pass lands inside its plan rather than on its edge.
SIZE_BRAKE_TARGET = 0.9

_REFINED = re.compile(r"(?m)^\s*Refined mesh\s*:\s*cells:\s*([0-9.eE+]+)")
_IDEAL = re.compile(r"(?m)^\s*Ideal layer addition\s*:\s*cells:\s*([0-9.eE+]+)")
#: snappyHexMesh's two counts, in the order it prints them, each with its own margin (None: the
#: brake's own)
SNAPPY_COUNTS = (("castellated", _REFINED, None), ("with layers", _IDEAL, SIZE_BRAKE_LAYER_MARGIN))
#: cartesianMesh's: the mesh it builds from its octree, printed as "<N> cells" before it maps the
#: surface, adds layers and optimises (cfMesh, engines/cfmesh/native)
CFMESH_COUNTS = (("from the octree", re.compile(r"(?m)^\s*(\d+) cells\s*$"), None),)
_MAX_GLOBAL = re.compile(r"maxGlobalCells\s+(\d+)")


#: The workspace fact a snappy case records its PLAN's cell budget in (the finished mesh's, layers
#: and all) - which can be above the castellation budget written as maxGlobalCells, when the plan
#: left room for the layers (snappy_runner.castellation_budget).
PLAN_CELLS_FILE = "system/plan_cells.json"


#: blockMesh's blocks: `hex (<8 vertices>) (<nx> <ny> <nz>)`
_HEX_BLOCK = re.compile(r"hex\s*\([^)]*\)\s*\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)")


def background_cells(workspace) -> int:
    """The cells the case's background mesh starts from: every blockMesh block's nx*ny*nz in its
    system/blockMeshDict (0 when it cannot be read). snappyHexMesh only ever refines and removes
    from this mesh, and no castellation budget changes it."""
    try:
        text = (Path(workspace) / "system" / "blockMeshDict").read_text(errors="replace")
    except OSError:
        return 0
    return sum(int(a) * int(b) * int(c) for a, b, c in _HEX_BLOCK.findall(text))


#: The plan record's flag for a case the brake no longer stops (release_brake).
BRAKE_RELEASED = "brake_released"
#: The rule behind both: the brake measures what the case was priced at, and never stops a run its
#: re-plan cannot make smaller.
BRAKE_RULE = "RUN-8"


def release_brake(workspace, why: str) -> bool:
    """THE BRAKE ONLY STOPS WHAT IT CAN MAKE SMALLER (RUN-8). After a pass the brake stopped, the
    re-plan is meant to author a smaller case; when it authors the SAME case byte for byte (nothing
    left to cut: the gap boxes, the background mesh or the budget's own floor price it back in),
    stopping it again could only end the run with no mesh. That case runs once more with the brake
    released - its size is said in the run, never refused for it. Returns whether the case's plan
    record now says so."""
    import json
    p = Path(workspace) / PLAN_CELLS_FILE
    try:
        rec = json.loads(p.read_text()) if p.exists() else {}
        rec = rec if isinstance(rec, dict) else {}
        rec[BRAKE_RELEASED] = str(why or "the re-plan authored the same case")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec))
        return True
    except (OSError, ValueError, TypeError):
        return False


def case_budget(workspace) -> int | None:
    """The cell budget the brake measures a case against: its plan's (PLAN_CELLS_FILE), else
    maxGlobalCells in its snappyHexMeshDict; None when neither can be read. Never the castellation
    budget alone: on a large thin plate the layers are about 3.5 times the castellated cells whatever
    its size, so measured against the castellation budget the brake would trip on every pass and
    shrink the mesh until the body was lost (lab, 25 Oct: A1/A2 down to 0.39M cells, no body).

    NEVER BELOW THE BACKGROUND MESH (background_cells): the brake answers a mesh REFINED far past its
    plan, and re-plans the castellation budget smaller - which cannot shrink the background the case
    starts from. A plan whose budget is under its own background (a 40,000-cell plan on a 138,240-cell
    blockMesh: release validation's native snappy pass, 26 Oct) read "3.3 times its budget" before any
    refinement, was stopped and could only be stopped again on every re-plan. And whether it was
    stopped at all depended on timing - the watch looks every 10 s, so a fast run on 4 ranks finished
    first and passed while the same case on 8 oversubscribed ranks was stopped - a size brake must
    give the same answer on every machine.

    THE CELLS THE CASE WAS PRICED AT (RUN-8): the plan's record carries the cells the gap boxes
    between an external body's parts raised snappy's ceiling by (snappy_runner.render_snappy_case),
    and a case whose brake was RELEASED (release_brake) has no budget at all - None, no brake."""
    import json
    budget: int | None = None
    try:
        rec = json.loads((Path(workspace) / PLAN_CELLS_FILE).read_text()) or {}
        if isinstance(rec, dict) and rec.get(BRAKE_RELEASED):
            return None
        v = int(rec.get("plan_cells") or 0)
        if v > 0:
            budget = v
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if budget is None:
        try:
            m = _MAX_GLOBAL.search((Path(workspace) / "system" / "snappyHexMeshDict").read_text(errors="replace"))
        except OSError:
            return None
        if not m:
            return None
        budget = int(m.group(1))
    return max(budget, background_cells(workspace))


class SizeBrake:
    """A `watch` for sandbox/safe_exec.run_guarded: called while snappyHexMesh runs, it reads the new
    part of its log and returns a reason (and keeps `tripped`) once a printed count passes the
    margin. Reads only what was appended since the last call, so a long log costs nothing."""

    def __init__(self, log_path, budget: int, *, margin: float = SIZE_BRAKE_MARGIN,
                 counts=SNAPPY_COUNTS) -> None:
        self.log_path, self.budget, self.margin = Path(log_path), int(budget), float(margin)
        self.counts = tuple(counts)
        self._offset, self._carry = 0, ""
        self.tripped: dict | None = None

    def _new_text(self) -> str:
        try:
            with self.log_path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
                self._offset += len(data)
        except OSError:
            return ""
        text = self._carry + data.decode("utf-8", errors="replace")
        cut = text.rfind("\n")
        self._carry = text[cut + 1:] if cut >= 0 else text
        return text[:cut + 1] if cut >= 0 else ""

    def __call__(self) -> str | None:
        if self.tripped is not None or self.budget <= 0:
            return None
        text = self._new_text()
        for stage, rx, own in self.counts:
            margin = max(self.margin, own) if own else self.margin
            for raw in rx.findall(text):
                cells = int(float(raw))
                if cells > margin * self.budget:
                    self.tripped = {"stage": stage, "cells": cells, "budget": self.budget,
                                    "margin": margin, "ratio": round(cells / self.budget, 2)}
                    return (f"the mesh {stage} reads {cells:,} cells, {cells / self.budget:.1f} times its "
                            f"budget of {self.budget:,}")
        return None


#: The deepest one re-plan cuts the castellation budget: half. snappyHexMesh's castellated count is
#: not proportional to its budget - the surface must reach the level that seals the body whatever
#: the budget says - so scaling the budget by the overshoot (3x, 6x) cut it far below what sealing
#: needs and the next pass lost the body (lab, 25 Oct: A2 2M -> 0.57M -> 0.2M, no body faces). One
#: halving per pass is the step the old clock-driven re-plan took, and it landed both plates inside
#: their plans' margin (1.8M and 2.8M on a 2M plan).
SIZE_BRAKE_STEP = 0.5


def scaled_budget_factor(tripped: dict) -> float:
    """The factor the next pass's castellation budget is cut by: SIZE_BRAKE_STEP (half), or less of
    a cut when the run was only a little over - never deeper than half in one pass."""
    cells, budget = float(tripped.get("cells") or 0), float(tripped.get("budget") or 0)
    if cells <= 0 or budget <= 0:
        return SIZE_BRAKE_STEP
    return max(SIZE_BRAKE_STEP, min(0.9, SIZE_BRAKE_TARGET * budget / cells))


__all__ = ["BRAKE_RELEASED", "BRAKE_RULE", "SIZE_BRAKE_STEP", "PLAN_CELLS_FILE", "CFMESH_COUNTS",
           "SIZE_BRAKE_MARGIN", "SIZE_BRAKE_LAYER_MARGIN", "SIZE_BRAKE_TARGET", "SNAPPY_COUNTS",
           "SizeBrake", "background_cells", "case_budget", "release_brake", "scaled_budget_factor"]
