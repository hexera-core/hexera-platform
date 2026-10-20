# The rulebook

Every decision a mesh run makes on its own is made by a written rule. Each rule has a stable ID,
and each decision the run records in its "why" record names the rule behind it
(`contracts/decision_record.py`). This file is the one home for those rules.

Rulebook version: **3**

How the book is kept honest:

- A rule's ID never changes and is never reused. A rule that stops applying is marked `retired`,
  not deleted.
- **In force** means the code makes this decision today. **Planned** means the ID is reserved
  for a later PR (named in brackets) and no code may cite it yet.
- `tests/unit/hygiene/test_rulebook.py` checks, on every PR, that every rule ID the code cites is
  in force here, that every in-force rule here is cited by the code, and that the version above
  matches `RULES_VERSION` in `contracts/decision_record.py`.
- Changing what a rule decides means changing this book in the same PR and raising the version.

Each decision in the record also says where it came from:

| Source | Meaning |
|---|---|
| `rule` | The code decided by the rule, from measured or declared facts. |
| `user` | The user decided (named an engine, or confirmed one). The rule says why that stands. |
| `ai-lever` | The AI proposed one approved lever, inside its limits (not used yet: AI levers are off). |
| `guard-clock` | A guard clock tripped (TIME-3), or one of the clocks still left for the CAD repair work decided (TIME-6). Since D3 no other clock decides anything. |

---

## ENG: which engine meshes the run

| ID | Status | Rule |
|---|---|---|
| ENG-1 | in force | **Only engines that can take the case are ranked.** An engine is a candidate only if its declared capability covers the flow, the file's form, the number of separate passages and the layers asked for. |
| ENG-2 | planned (D1) | **Home turf first.** When the shape's measured facts put an engine "home", that engine comes first. |
| ENG-3 | planned (D1) | **The scoreboard overrides home turf only when it clearly wins:** the challenger has at least 20 runs on this shape class, and its 95% lower bound is above the home engine's 95% upper bound. |
| ENG-4 | in force | **The best lab record on shapes like this one comes first.** The run's geometry is measured, and engines are ranked by their record on its shape class (`engines/fitness.py`). When that record is thin, the record says so. An engine whose declared turf puts the shape outside is never recommended. Ties go to fewer broken meshes, then fewer bad cells, then the declared turf, layers and ladder order. |
| ENG-5 | in force | **The fallback ladder walks the same ranking**, after the approved engine, minus the engines already tried and the engines that would refuse the case. |
| ENG-6 | in force | **The user's pick stands.** An engine the user named is never switched on its own; another engine is only offered. A dispute keeps the engine of the mesh it disputes. |
| ENG-7 | in force | **An engine that reads the upload's own format comes first**, measured or not: a conversion loses what the file describes (an ECXML's parts, materials and powers). |
| ENG-8 | in force | **Nothing measured, nothing named: the declared default.** When the run's geometry could not be measured, a run nobody chose an engine for takes the engine the declarations name for its flow (or the flow-only order). |
| ENG-9 | in force | **The user confirmed an engine the measured ranking did not put first** (for example, the ranking was not ready while the user was asked). The confirmed engine stands. This is a gap D1 closes: the proposal should always be the ranked one. |

## SW: whether a run moves to another engine after a failed attempt

Decided by `pipeline/engine_fallback.decide`. The run moves only on measured facts, never on a
review. The order the run moves in is ENG-5.

| ID | Status | Rule |
|---|---|---|
| SW-1 | in force | **Move on: the mesh came out broken** (checkMesh's fatal defects). The next engine in the ladder runs at once. |
| SW-2 | in force | **Move on: the engine could not build a mesh** (it crashed, stopped early or refused the input). |
| SW-3 | in force | **Move on: the same measured failure repeated** on this engine. |
| SW-4 | in force | **Move on: the shared attempts go to the untried engine** when a retry here would leave it no attempt of its own. |
| SW-5 | in force | **Stay: a measured check failed, and this engine's own levers are tried first**, because a retry here still leaves every untried engine an attempt. |
| SW-6 | in force | **Stay: a retry here fits** and still leaves every untried engine an attempt. |
| SW-7 | in force | **Stay: a review never moves the run.** The mesh passed every measured check. |
| SW-8 | in force | **Stay: the engine is not ours to change** (the user named it, or a dispute; see ENG-6). |
| SW-9 | in force | **Stay: another engine cannot change this failure** (the request, the approval or a region split). |
| SW-10 | in force | **Stay: no untried engine delivers the approved mesh** (same boundaries, units, cell kind and layers). |
| SW-11 | in force | **Stay: the untried engines would refuse** this many separate passages in one job. |
| SW-12 | in force | **Stay: no regular attempt is left.** |
| SW-13 | retired (D3) | **Stay: too little time left** for the next engine's run. Retired: no clock decides a switch any more (TIME-1). A next engine that runs past the run's guards is a guard trip (TIME-3). |
| SW-14 | in force | **Stay: the fallback ladder is switched off** by a setting. |
| SW-15 | in force | **No decision: the last attempt did not fail.** |

## DEL: which valid mesh a run delivers

When a run built more than one valid mesh, `application/review_delivery.rank_key` ranks them by a
fixed order of measured facts. The first fact that separates the winner from the runner-up is the
rule. An invalid mesh is never delivered.

| ID | Status | Rule |
|---|---|---|
| DEL-0 | in force | **The only valid mesh** the run built is the one delivered. |
| DEL-1 | in force | **A mesh the review confirmed is the wrong problem ranks last** (a cited, inspected wrong-problem finding, not a concern). Shadow period only: once `VERDICT_BY_LIMITS` is on, a review ranks nothing and this position never separates two meshes. |
| DEL-2 | in force | **A mesh that passes every fixed quality limit beats one that does not.** |
| DEL-3 | in force | **Fewer failed quality checks wins.** |
| DEL-4 | in force | **Meeting more of the approved plan wins** (layers on the wall, wake refinement, stated sizes). |
| DEL-5 | in force | **Better worst cells win**, compared to the whole degree of non-orthogonality. |
| DEL-6 | in force | **On a measured tie, the mesh whose review passed wins.** Shadow period only: once `VERDICT_BY_LIMITS` is on, a review ranks nothing and a tie on everything measured goes to the earlier attempt (DEL-10). |
| DEL-7 | in force | **Lower worst non-orthogonality wins** (exact value). |
| DEL-8 | in force | **Lower worst skewness wins.** |
| DEL-9 | in force | **More wall covered by prism layers wins.** |
| DEL-10 | in force | **The earlier attempt wins** when everything measured is the same. |

## VER: pass, delivered with concerns, or fail

Decided by `contracts/decision_record.verdict` from measured numbers alone, read off the delivered
mesh by `engines/verdict_limits.py`. Every finished run's why record carries one `verdict` entry:
the outcome, the VER rule, and every row below with its measured number, its limit and its
source, so the verdict replays from the record. Nothing the AI reviewer wrote is an input.

**The AI reviewer only adds notes.** It still looks at the pictures. Its points are shown to the
customer under "Reviewer notes", each one labelled "Reviewer note". It cannot change a status,
start a rebuild, a bonus attempt or a retry, rank one mesh over another, or fail a job as the
wrong problem. A user's own dispute ("rebuild it") is the user's decision and still rebuilds.

**Shadow first.** The setting `VERDICT_BY_LIMITS` (off by default) switches the limits verdict on.
While it is off, the review-led verdict still decides, and the limits verdict is worked out on
every run beside it: one `verdict shadow:` log line (limits outcome, rule, review-led outcome,
whether they agree, the limits missed) and the `verdict` entry in the why record.

| ID | Status | Rule |
|---|---|---|
| VER-1 | in force | **Fail: no valid mesh.** The executor's gates refused the last attempt, or the mesh's own numbers fail one of the engine's gating rows (its `criteria.py` rows marked gating: checkMesh fatal defects, wall captured, ports captured, the quality floors, the cell ceiling, regions and interfaces). An invalid mesh is never delivered. |
| VER-2 | in force | **Delivered with concerns:** the mesh is valid but misses at least one production limit (table below). Each one is listed as measured vs limit. |
| VER-3 | in force | **Clean pass:** valid, and every production limit that applies is met (a limit whose number was not measured is listed as unmeasured and decides nothing). |
| VER-4 | in force | **Wrong problem, by measured checks only.** The run failed at a gate that measures one of the ways a mesh can be a different problem than the one confirmed (table below). The gate's own measured account is what the user reads. |

### VER-2 production limits, per engine and purpose

| Limit | Engines | Applies to | Limit value | Source |
|---|---|---|---|---|
| Worst face non-orthogonality | snappy, cfMesh, multi-region | every flow | at most 70 degrees | checkMesh warns above 70 |
| Worst face non-orthogonality | gmsh | external and internal flow (not FEA) | at most 85 degrees | gmsh's own gating bar, so a mesh past it never validates |
| Share of skewed faces | cfMesh | every flow | at most 0.05% | the snappy-family gating bar (advisory for cfMesh, which often does not report it) |
| Wall covered by prism layers | snappy, cfMesh, multi-region, VMTK | when the approved plan builds layers | at least 80% | lab history: below it wall shear and y+ are unreliable on a material share of the wall |
| Layers built short of the layers asked | cfMesh | when the approved plan builds layers | none short | the layer census (layers in the meshDict vs the request) |
| Connected cell regions | snappy, cfMesh, gmsh, VMTK | every flow | at most 1 | checkMesh's region count: a second region is cut off from the flow |
| Cells across the narrowest passage | snappy, cfMesh, gmsh, VMTK | internal flow | at least the resolution floor (12) | the resolution floor's own reading; narrow side passages it set aside are listed here |
| Stated requirements met | every engine | every flow | no near-miss | the executor's measured requirement checks (`requirement_caveats`) |
| A believable size for what it is | every engine | every flow | no size concern | the unit-plausibility check on the delivered body |
| The engine's own declared advisory lists | snap-grid | every flow | empty | `engines/snapgrid/criteria.py` (thin layers short, resolution short) |

The table is `engines/verdict_limits.PRODUCTION_LIMITS`. A limit names engines and flows, never a
file, a part or a model. Changing a number here means changing the table, this book and the
version in the same PR.

### VER-4 measured checks

| Way the mesh is the wrong problem | Measured by | Today |
|---|---|---|
| The wrong side of the body was meshed | `port_areas`, `region_contract` | gates |
| Part of the confirmed geometry is missing | `file_fidelity` | gate (snap-grid); CAD fidelity checks to come (#225) |
| The wrong scale | the believable-size row of VER-2 | a concern, not a failure |
| The flow setup contradicts what was confirmed | `domain_extent`, `port_areas` | gates |
| A boundary contradicts what was confirmed | `patch_contract`, `boundary_types`, `port_areas` | gates |
| No flow path from inlet to outlet | none yet | no measured check: a review claim is a note, and the shadow log lists it |
| The meshed surface differs from the CAD | `file_fidelity` | gate (snap-grid); CAD fidelity checks to come (#225) |
| Disconnected junk regions | `regions_split` (multi-region); the regions row of VER-2 | gate; a concern on one-region engines |
| A stated requirement is absent | the requirement rows of VER-2 | a concern |

The table is `contracts/decision_record.WRONG_PROBLEM_CHECKS`.

## TIME: planned work decides, the clock only guards

A run must come out the same on a quiet machine and on a busy one. So **planned work** decides
what a run does: cells, iterations, triangles, samples, ladder steps and attempts. These are the
same on every machine. A clock is kept only as a **guard**, so a hung step cannot hold a machine
forever. The numbers live in `contracts/work_limits.py`. Every guard's planned work and size goes
into the run's why record as a `work_limit` entry, and every trip as a `guard` entry. Both replay
from their recorded inputs.

| ID | Status | Rule |
|---|---|---|
| TIME-1 | in force | **Planned work decides, never the clock.** A step runs its planned work to the end. Whether a step or a ladder rung runs, how coarse a pass is, which engine runs next and what the verdict is never depend on how long anything took. |
| TIME-2 | in force | **Each guard is 3 times the planned time.** Planned time is the planned work divided by the step's slow rate (the 10th percentile the lab measured: snappy 1,500, cfMesh 1,200, VMTK 4,000 cells a second). A guard is never shorter than the step's fixed cap before D3, so nothing that finished before is cut now. It is never longer than the ceiling the job around it allows. An engine with no lab rate keeps its old cap. |
| TIME-3 | in force | **A guard that trips is an infrastructure failure.** The attempt ends and nothing on disk is judged. The same attempt is run again once, unchanged. A second trip ends the run as "ran out of time". A trip never picks a lever, skips a step, makes a pass coarser, picks or switches an engine, or changes a verdict. |
| TIME-4 | in force | **A wait that feeds a decision waits for the answer.** This covers the measured engine order, the intake's recommendation and the geometry check's naming. Their guards are long (15, 5 and 9 minutes). A trip counts as that step failing, the same path as any error in it. It is never a different answer chosen because time ran out. |
| TIME-5 | in force | **The job's own clocks are guards only.** These are the builder's 3,600 s per attempt and 9,000 s in total, the pipeline's 21,600 s, the reviewer's 1,800 s and the Cloud Run task's 4 hours. A trip ends the attempt or the run as ours ("ran out of time"). The builder's and the pipeline's trips are recorded as guard trips; the reviewer's ends the review as one that did not conclude (our failure, never a verdict). (The snappy planner's 1,800 s setting is read by nothing.) |
| TIME-6 | in force | **The CAD repair steps still decide by the clock: inspection (25 s) and repair (120 s).** If inspection runs out, the repair status is "inconclusive". If repair runs out, the original file is meshed. These steps belong to the repair work (#201). They are recorded as `time_limited` with source `guard-clock`, and they are left for that work to bound by face count. |

### What each clock became

| Where | Before D3 | Now |
|---|---|---|
| snappy pass (`engines/snappy/drivers.py`) | a fixed 2,400 s cap; a pass that timed out made every later pass, and the next attempt, coarser | a guard of 3 x the pass's planned cells at 1,500 cells/s (at least 2,400 s, at most 4,800 s); a trip stops the passes and the attempt is replayed (TIME-3). A pass that came out broken still makes the next pass coarser: that is measured, not a clock. |
| native run caps (`engines/dispatch.py`) | fixed seconds, a stopped run noted as a decision | the same caps as each engine's guard floor; a stopped run is a guard trip |
| cfMesh run (`engines/cfmesh/cfmesh_runner.py`) | 1.25 x the estimate at 2,000 cells/s | a guard of 3 x the planned cells at 1,200 cells/s, counting the near-edge rescue (3 runs' worth), within 1,200 to 3,000 s. Whether a plan fits one run is still decided from the plan (`fits_one_run`), before it runs. |
| cfMesh rescue (`engines/cfmesh/rescue.py`) | skipped steps "with no time left", stopped the optimiser at its own share of the clock, dropped an unmeasured candidate | both steps always run when triggered and their cell limits allow; each runs inside the run's guard; a trip stops the rescue and is a guard trip |
| VMTK ladder and surface (`engines/vmtk/vmtk_runner.py`) | steps not started under 60 s or 10% left; a layered step got 60% of the clock, then the layer-free fill; folded-wall remeshes skipped near the end; the OpenFOAM export skipped under 60 s | every ladder step and every remesh runs when the step before it measurably failed; each gets what is left of the run's guard; a trip stops the run. The export always runs, with at least its own 600 s guard. |
| passage measure (240 s), passage flow (180 s), sealed spaces (120 s) | abandoned: the mesh shipped, or the floor was judged, without the reading | each reads a fixed number of samples or voxels; the clock is a guard; a trip is a guard trip |
| engine fallback (`pipeline/engine_fallback.py`) | "too little time left" kept the run on its engine (SW-13) | removed: the time left never decides a switch |
| engine order wait (60 s), intake recommendation (15 s) | the declared order, or no recommendation that turn | waits for the measurement (TIME-4, guards 900 s and 300 s) |
| review rerun (`pipeline/graph.py`) | not started again with too little time left | started again by its count alone (TIME-1); the run's deadline is a guard (TIME-5) |
| geometry check naming and vision | naming skipped | a guard (TIME-4): the same path as a model outage; the user confirms the proposal |
| builder run_mesh with too little loop budget left | refused, with a hint to reduce the work | a guard trip of the attempt (TIME-5), replayed as it was |
| a run that timed out still repairing broken cells | treated as a broken mesh, so the run moved to the next engine | a guard trip; what it was doing at the time decides nothing |
| CAD inspection (25 s), CAD repair (120 s) | as now | unchanged (TIME-6), left for the repair work |

### The interface for any driver (FEA step 4 included)

```python
from meshpipeline.contracts.work_limits import PlannedWork, guard_for
from meshpipeline.engines.determinism import note_guard_trip

work = guard_for("snappy", cells=planned_cells)       # or PlannedWork(where="fea.gmsh",
                                                      #   unit="elements", amount=n, rate=r,
                                                      #   floor_s=..., ceiling_s=...)
run(..., timeout=work.guard_seconds)                  # the only clock
if timed_out:
    note_guard_trip(work.where, workspace=ws, **work.as_dict())   # the executor ends the
                                                                  # attempt; the graph replays it
```

## Reserved families

These ID families are reserved. Their rules are written by the PRs that build them, and no code
may cite them before then.

| Family | For | Written by |
|---|---|---|
| `PLAN-<engine>-n` | the numbers of each engine's computed plan | D4 to D8 |
| `LAD-<engine>-n` | each engine's fixed ladder of approved levers | D4 to D8 |
