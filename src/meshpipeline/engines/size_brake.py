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


#: castellation's end in snappyHexMesh's log, after the castellated mesh is written ("Mesh refined in = N s.";
#: each refinement iteration prints "Refined mesh in = ..." instead)
_CAST_END = re.compile(r"(?m)^\s*Mesh refined in\s*=")
#: one patch of a polyMesh/boundary file: its name, type and face count
_BOUNDARY_PATCH = re.compile(r"(?s)\n\s*\"?([^\s{}\"]+)\"?\s*\{\s*type\s+(\w+);.*?nFaces\s+(\d+);")
#: the per-patch layer counts of a snappyHexMeshDict (addLayersControls' layers block)
_LAYERS_ENTRY = re.compile(r"\"?([^\s{}\";]+)\"?\s*\{\s*nSurfaceLayers\s+(\d+)\s*;")
#: the stage name of a with-layers count read off the castellated mesh's own wall (RUN-12)
CASTELLATED_WALL = "castellated wall"


def layer_counts_of(workspace) -> dict[str, int]:
    """{patch name or pattern: layers} from the case's snappyHexMeshDict ({} when it has none)."""
    try:
        text = (Path(workspace) / "system" / "snappyHexMeshDict").read_text(errors="replace")
    except OSError:
        return {}
    block = text.split("addLayersControls", 1)[-1] if "addLayersControls" in text else ""
    return {name: int(n) for name, n in _LAYERS_ENTRY.findall(block) if int(n) > 0}


def castellated_layer_cells(case_dir, layers: dict[str, int]) -> tuple[int, int] | None:
    """(the wall faces that take layers, the cells those layers add) on the castellated mesh snappyHexMesh wrote to
    the case (processor*/constant/polyMesh/boundary, or constant/polyMesh/boundary on one rank). None when no
    boundary file is there or no patch takes layers."""
    root = Path(case_dir)
    files = sorted(root.glob("processor*/constant/polyMesh/boundary")) or [root / "constant" / "polyMesh" / "boundary"]
    faces = cells = 0
    read = False
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        read = True
        for name, kind, n in _BOUNDARY_PATCH.findall(text):
            if kind.startswith("processor"):
                continue
            k = next((v for pat, v in layers.items() if pat == name or _matches(pat, name)), 0)
            if k:
                faces += int(n)
                cells += int(n) * int(k)
    return (faces, cells) if read and faces else None


def _matches(pattern: str, name: str) -> bool:
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error:
        return False


class SizeBrake:
    """A `watch` for sandbox/safe_exec.run_guarded: called while snappyHexMesh runs, it reads the new
    part of its log and returns a reason (and keeps `tripped`) once a printed count passes the
    margin. Reads only what was appended since the last call, so a long log costs nothing.

    AT CASTELLATION'S END (RUN-12), given `case_dir` and the case's `layers`: the castellated mesh is on disk, so its
    wall's faces say what the layers will add - the with-layers count snappy prints only after snapping. A pass
    that will be thrown away stops before it snaps (RM5's first pass: 21 of its 44 minutes were castellation, then
    14 snapping a mesh the brake stopped)."""

    def __init__(self, log_path, budget: int, *, margin: float = SIZE_BRAKE_MARGIN,
                 counts=SNAPPY_COUNTS, case_dir=None, layers: dict[str, int] | None = None) -> None:
        self.log_path, self.budget, self.margin = Path(log_path), int(budget), float(margin)
        self.counts = tuple(counts)
        self.case_dir = Path(case_dir) if case_dir is not None else None
        self.layers = dict(layers or {})
        self._offset, self._carry = 0, ""
        self.tripped: dict | None = None
        #: every count read so far, by stage (the last of each): a stop on the with-layers count
        #: carries the castellated count beside it, so the re-plan can tell the wall's layer cells
        #: from the rest (learned_face_factor)
        self.seen: dict[str, int] = {}

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
                self.seen[stage] = cells
                if cells > margin * self.budget:
                    self.tripped = {"stage": stage, "cells": cells, "budget": self.budget,
                                    "margin": margin, "ratio": round(cells / self.budget, 2),
                                    **({"castellated": self.seen["castellated"]}
                                       if stage != "castellated" and "castellated" in self.seen else {})}
                    return (f"the mesh {stage} reads {cells:,} cells, {cells / self.budget:.1f} times its "
                            f"budget of {self.budget:,}")
        if self.case_dir is not None and self.layers and "castellated" in self.seen and _CAST_END.search(text):
            got = castellated_layer_cells(self.case_dir, self.layers)
            if got is not None:
                faces, added = got
                cells = self.seen["castellated"] + added
                self.seen[CASTELLATED_WALL] = cells
                margin = max(self.margin, SIZE_BRAKE_LAYER_MARGIN)
                if cells > margin * self.budget:
                    # read as the with-layers count (learned_face_factor), from the castellated wall
                    self.tripped = {"stage": "with layers", "source": CASTELLATED_WALL, "cells": cells,
                                    "budget": self.budget, "margin": margin, "ratio": round(cells / self.budget, 2),
                                    "castellated": self.seen["castellated"], "wall_faces": faces}
                    return (f"the mesh with layers comes to {cells:,} cells on its castellated wall ({faces:,} "
                            f"faces), {cells / self.budget:.1f} times its budget of {self.budget:,} - stopped "
                            "before snapping")
        return None


#: The deepest one re-plan cuts the castellation budget: half. snappyHexMesh's castellated count is
#: not proportional to its budget - the surface must reach the level that seals the body whatever
#: the budget says - so scaling the budget by the overshoot (3x, 6x) cut it far below what sealing
#: needs and the next pass lost the body (lab, 25 Oct: A2 2M -> 0.57M -> 0.2M, no body faces). One
#: halving per pass is the step the old clock-driven re-plan took, and it landed both plates inside
#: their plans' margin (1.8M and 2.8M on a 2M plan).
SIZE_BRAKE_STEP = 0.5


#: THE WALL'S LAYER CELLS, LEARNED FROM A STOPPED PASS (RUN-8). A plan prices its layers at one cell
#: per wall face per layer, its wall faces about the wall area over the wall cell squared
#: (snappy_runner.final_estimate). On an assembly of many parts with sharp edges, snappy's own wall
#: holds several times that: the feature edges are refined two levels finer, sixteen faces to one.
#: The RM5 surge flap's first pass read 3.12 M castellated cells and 11.65 M with its 17 layers -
#: 0.50 M wall faces where its plan had priced at most 0.15 M - so its plan never knew it was over, and
#: the floor pricing (rulebook 75) never fired. A pass stopped on its with-layers count has told the
#: run what its wall really holds: (with layers - castellated) / layers. The re-plan's estimate
#: multiplies the plan's wall faces by that over what the plan had priced, never less than 1 and
#: never more than LEARNED_FACE_FACTOR_MAX.
LEARNED_FACE_FACTOR_MAX = 25.0


def learned_face_factor(tripped: dict | None, plan: dict | None) -> float | None:
    """How many times the wall faces the stopped pass's plan priced its wall really held: from the
    brake's stop on the with-layers count (its castellated count beside it) and the plan record's
    priced wall faces and layer count (PLAN_CELLS_FILE: wall_faces_est, layers_est). None when the
    stop was not on the with-layers count or either side is missing."""
    try:
        t, p = dict(tripped or {}), dict(plan or {})
        if t.get("stage") != "with layers":
            return None
        ideal, cast = float(t.get("cells") or 0), float(t.get("castellated") or 0)
        faces, n = float(p.get("wall_faces_est") or 0), int(p.get("layers_est") or 0)
        if ideal <= cast or cast <= 0 or faces <= 0 or n <= 0:
            return None
        return float(min(LEARNED_FACE_FACTOR_MAX, max(1.0, (ideal - cast) / n / faces)))
    except (TypeError, ValueError):
        return None


def plan_record(workspace) -> dict:
    """The case's plan record (PLAN_CELLS_FILE), {} when it cannot be read."""
    import json
    try:
        rec = json.loads((Path(workspace) / PLAN_CELLS_FILE).read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def scaled_budget_factor(tripped: dict) -> float:
    """The factor the next pass's castellation budget is cut by: SIZE_BRAKE_STEP (half), or less of
    a cut when the run was only a little over - never deeper than half in one pass."""
    cells, budget = float(tripped.get("cells") or 0), float(tripped.get("budget") or 0)
    if cells <= 0 or budget <= 0:
        return SIZE_BRAKE_STEP
    return max(SIZE_BRAKE_STEP, min(0.9, SIZE_BRAKE_TARGET * budget / cells))


__all__ = ["BRAKE_RELEASED", "BRAKE_RULE", "CASTELLATED_WALL", "castellated_layer_cells", "layer_counts_of", "LEARNED_FACE_FACTOR_MAX", "SIZE_BRAKE_STEP", "PLAN_CELLS_FILE",
           "CFMESH_COUNTS", "learned_face_factor", "plan_record",
           "SIZE_BRAKE_MARGIN", "SIZE_BRAKE_LAYER_MARGIN", "SIZE_BRAKE_TARGET", "SNAPPY_COUNTS",
           "SizeBrake", "background_cells", "case_budget", "release_brake", "scaled_budget_factor"]
