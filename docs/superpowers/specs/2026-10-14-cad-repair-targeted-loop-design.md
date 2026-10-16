# CAD repair: targeted operations and a verified repair loop

Date: 2026-10-14
Status: designed, awaiting review
Extends: [`2026-09-22-cad-repair-system-design.md`](2026-09-22-cad-repair-system-design.md) (slices 1–5, built)
Scope: slices 6–9. Slice 6 (deterministic loop) is specified in full; 7–9 are sketched.

## The problem this solves

Slices 1–5 locate defects per entity but repair the whole shape with one fixed recipe.
`repair_step_file` takes no targets, the triage targets are logged and never used, entity indices
change after every ShapeFix pass with nothing mapping old to new, and the pipeline never
re-inspects what it repaired. So the system can say *where* the CAD is broken, but nothing can act
on one defect or check whether that defect is gone — whether a rules planner or a model is doing
the choosing.

The end goal is an LLM planner with a feedback loop. This design builds that loop first with a
deterministic planner, so the actions, the progress signal and the training log exist and are
proven before any model sits in the seat.

## Decisions already made

- **Loop shape:** one small, capped operation per iteration, re-inspect after each, keep or roll
  back. Not "plan the whole repair, then run it".
- **Where it runs:** inside the existing repair attempt node, in process, on an in-memory shape.
  No file round trip per iteration.
- **Progress rule:** an operation is kept only if the defect it aimed at is gone from the targeted
  entity, and it introduced no new defect at warning severity or worse. Stated against the
  *defect*, not the entity: a hole patch leaves the rim edges in place and merely stops them being
  a free boundary, so "the targeted entity disappeared" would reject every successful fill. Entity
  disappearance is one way the defect can be gone, not the rule.
- **Entity tracking (approach A):** each operation reports its own old→new entity mapping from the
  kernel (`ShapeBuild_ReShape` history for ShapeFix, `Modified`/`ModifiedSubShape`/`DeletedFace`
  for sewing, "new face" for a patch). A geometric fingerprint is the outward-facing ID and the
  fallback where an op has no kernel mapping. Rejected: fingerprints alone (false rollbacks, can't
  tell fixed from renamed) and a STEP file per iteration (seconds per op, and the round trip shifts
  tolerances, which pollutes the delta being measured).

## Stage sequence

| slice | what | AI? |
|---|---|---|
| 6 | Target-taking ops, session with history, re-inspect loop, rules planner | no |
| 7 | Entity context: surface type, adjacency, paired-bore finding in the report; emit or delete `wire_gap` / `tolerance_outlier` | no |
| 8 | Model in the planner seat behind the same interface, shadow first, scored against slice 6 | yes |
| 9 | Operator queue writer, so human decisions accumulate as labels | no |

## Slice 6 — deterministic targeted repair loop

### Module layout

New modules under `src/meshpipeline/cad/repair/`, each doing one job:

| module | job |
|---|---|
| `identity.py` | `EntityRef` and its fingerprint: a name for an entity that survives a repair |
| `operations.py` | the operation set; every op takes targets |
| `caps.py` | the refusal checks, moved out of `conservative.py`, now split into per-op and cumulative |
| `session.py` | holds the shape, applies one op, rolls back, re-inspects |
| `plan.py` | the rules planner, which is the seat a model takes in slice 8 |
| `loop.py` | the driver and its budget |

`conservative.py` stays. `repair_step_file` keeps its signature and is reimplemented as
`loop.run(...)` with the rules planner, followed by `session.write(...)`, so the existing native
tests and `devtools/validation/repair_corpus.py` keep working unchanged.

### Identity

`EntityRef(entity_type, fingerprint, entity_index, ambiguous)`. The fingerprint is the entity type
plus its bounding box, both corners quantised to a fraction of the part's diagonal, hashed short.
The bounding box rather than a centroid-plus-size pair, because `localize.py` already computes it
for every defect, and because area and length are not comparable quantities to quantise with one
rule.

Two entities can share a box — a duplicated face, two coincident edges. The table that builds the
refs detects that and marks both `ambiguous`, which downgrades the verdict's basis to `geometric`
for those targets rather than letting a collision pass as a kernel fact.

`EntityDefect` (from `localize.py`) gains a `fingerprint` field alongside `entity_index`. The index
stays for kernel lookups within one shape; the fingerprint is the name used across iterations and
in reports. A defect with no measurable extent gets an empty fingerprint and is **untargetable**:
the planner skips it and says so.

### The operation contract

```python
@dataclass(frozen=True, slots=True)
class OpOutcome:
    shape: object              # a NEW shape; the input is never mutated
    mapping: EntityMapping     # old fingerprint -> new fingerprint, or GONE, from the kernel
    added: tuple[str, ...]     # fingerprints this op created (a patch face, a new edge)
    evidence: dict             # what it did, in numbers a reviewer can read;
                               # includes declared area_delta_mm2 / volume_delta_mm3

class RepairOp(Protocol):
    name: str
    answers: frozenset[DefectCode]   # the defects this op claims to address
    def targets_for(self, defect: EntityDefect) -> tuple[EntityRef, ...]: ...
    def apply(self, shape, targets, *, tolerance_mm: float) -> OpOutcome: ...
```

### Operation set

| op | targets | kernel call | answers |
|---|---|---|---|
| `fix_small_edges` | edges | `ShapeFix_Wireframe` on the owning faces | `small_edge`, `degenerate_edge` |
| `fix_face_boundary` | faces | `ShapeFix_Face` | `curve_inconsistency`, `wire_gap` |
| `sew_faces` | faces | `BRepBuilderAPI_Sewing` over the named faces and their neighbours | `open_shell` where boundaries coincide |
| `fill_planar_hole` | one edge loop | `BRepBuilderAPI_MakeFace(wire, OnlyPlane)`, then sew the patch | `open_shell` where a face is missing |
| `fix_shape_global` | none | today's whole-shape `ShapeFix_Shape` | last resort, so slice 6 is never worse than today |

Scoped ops splice their fixed sub-shape back into the parent through a shared
`ShapeBuild_ReShape` (`Replace`, then `Apply`), so a targeted fix lands in the real shape without
rebuilding it, and the same context supplies the mapping.

**`fix_orientation` was specified and then cut, during implementation.** It was to answer
`DefectCode.bad_orientation` through `ShapeFix_Shell`. That code turns out to be unreachable:
across five in-memory constructions — a reversed-face shell, a solid built from one, an
all-faces-reversed shell, two adjacent faces with one reversed, and a cylinder with its lateral
face reversed — `BRepCheck_Analyzer` never attributes `BadOrientation` or
`BadOrientationOfSubshape` to any sub-shape. The reversed-face solid does read `IsValid() == False`,
but with every sub-shape status `NoError`: the unattributable case `localize.py`'s own header
documents. So `localize.py` cannot emit the code, the planner could never propose the operation,
and the catalogue is five operations, not six. The capability is not lost — `fix_shape_global`
runs `ShapeFix_Shape` with `FixFreeShellMode = 1`, which repairs shell orientation — and an
invalid part nobody can localise already routes to a person. `bad_orientation` joins `wire_gap`
and `tolerance_outlier` on slice 7's emit-or-delete list, which is now three codes.

Guardrails carry over unchanged: no face removal, `FixVertexPositionMode` off, non-manifold sewing
off, `fill_planar_hole` only while `CAD_REPAIR_FILL_PLANAR_HOLES` is on and only under
`CAD_REPAIR_MAX_HOLE_SPAN_RATIO`. `_as_solid_if_closed` runs after any op that may close a shell.

### Session

```python
class RepairSession:
    def defects(self) -> tuple[EntityDefect, ...]     # localize_scan on the current shape, cached
    def try_op(self, op, targets, *, tolerance_mm) -> OpVerdict
    def write(self, destination: Path) -> None        # STEP, millimetres, as today
```

`try_op` runs in this order, and the order matters:

1. **Snapshot** the shape handle and the cached defect list. Rollback restores them; OCC shapes
   are handle-based and no op mutates its input, so this is O(1).
2. **Apply.** A kernel exception gives verdict `failed`, the snapshot is restored and the loop
   continues.
3. **Per-op sanity caps** (counts only, cheap).
4. **Cumulative caps** against the original upload.
5. **Re-inspect** with `localize_scan(shape, valid=...)` on the in-memory shape.
6. **Progress rule:** the targeted defect — its code at the target's fingerprint, or at whatever
   the mapping says that fingerprint became — is absent from the new defect list, and no defect at
   warning or worse appeared that was not there before.
7. **Keep or roll back.** The op log gets an entry either way.

Caps run before re-inspection because re-inspection is the expensive step.

After a kept op the session drops its cached defect list, and the next `localize_scan` re-stamps
every fingerprint from the new shape. That re-stamp *is* the recompute, and keeping no second
table means the names a verdict is judged against and the names the report carries cannot drift
apart. The op's `mapping` is what tells the progress rule which fingerprint to look for, and what
sets each verdict's `basis: "kernel" | "geometric"` — because a training signal that can't tell a
kernel fact from an inference overstates its own confidence.

**Which operations can ever report a kernel basis.** Only those that DELETE or MOVE the entity
they target — `fix_small_edges` merging a sliver away, for instance. An operation that adds
geometry beside a surviving entity, as `fill_planar_hole` does to a rim edge, leaves that entity
untouched in the kernel's history, so every `fate_of` is `unknown` and the progress conclusion
genuinely comes from diffing the two scans. Such a verdict reports `geometric`, and that is the
honest answer rather than a shortcoming: for a later model planner, "this came from re-inspection,
not from kernel attestation" is exactly the confidence information it needs.

Verdicts: `kept`, `rolled_back` (with reason: `target_remains`, `new_defect`, `cap:<name>`),
`failed` (kernel exception).

### Caps

**Per-op sanity:** no solid lost, no face lost unless the op allows it, valid → invalid refused.

**Cumulative, always measured against the original upload, never against the previous
iteration:** bounding-diagonal deviation (`CAD_REPAIR_MAX_DEVIATION_RATIO`), max tolerance
(`CAD_REPAIR_MAX_TOLERANCE_MM`), area change, volume change. Six ops each inside the cap cannot
drift past it together.

**Accounted area and volume.** Today area/volume reuses the 0.75 hole-span ratio as its allowance,
because a hole patch legitimately moves both. Instead, each op declares its expected area and
volume contribution in `evidence`. The check compares the measurement against
`original + sum(declared)`, with residual tolerances `CAD_REPAIR_MAX_AREA_CHANGE_RATIO` and
`CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO`. ShapeFix and sewing ops declare zero. A patch declares its
own face area; its volume contribution is unknown until the shell closes, so a volume that first
appears when a shell becomes a solid is accepted and recorded, not compared.

### Planner

```python
class Planner(Protocol):
    def propose(self, defects, history: RunHistory) -> Candidate | None: ...
```

`Candidate = (op, targets, defect_fingerprint)`. The rules planner orders by severity first,
narrowest scope second, and `fix_shape_global` last, only once no scoped candidate remains.

**Ineffective pairs.** When an op is rolled back, `(defect fingerprint, op name)` goes into
`history` and the planner never proposes that pair again in this run. That is the per-op learning
signal. In slice 8 it is what the model is shown on its next turn.

**Paired-rim refusal.** The bore detection (`_paired_rims`) moves out of the fill and into the
planner. If a free loop pairs with another on a parallel plane, the planner refuses to propose
`fill_planar_hole` for it and records `planner_refusals: [{defect, reason: "paired_rim"}]`, which
replaces a silent skip inside the executor.

### Loop driver

```python
def run(shape, *, planner, budget: LoopBudget, engine: str = "") -> RepairRun
```

The loop stops at the first of these:

- no candidate is left (`no_candidate`)
- no defect at error severity remains **and the scan was exhaustive** (`resolved`). A truncated
  scan (`localize_scan`'s second return value is false) stops the loop as `scan_incomplete`
  instead, because a short defect list from a stopped walk is not an absence of defects.
- `CAD_REPAIR_MAX_OPS` ops have been tried (`op_budget`)
- `CAD_REPAIR_TIMEOUT_S` has been spent, now for the whole loop (`time_budget`)
- `CAD_REPAIR_STALL_LIMIT` consecutive non-kept verdicts (`stalled`)

`RepairRun` carries the ordered op records (op, targets, verdict, basis, measured deltas,
evidence), the planner refusals, the final defect list, the final shape, and `stopped_because`.
Downstream code and the corpus need `resolved` kept apart from the budget stops.

### Wiring

The `node_repair_attempt` structure is unchanged: triage gate, then repair, then promote. Only the
middle call changes. Every refusal path, the declined records and promotion stay as they are.

- The `repair_attempt` state record gains `operations` (the ordered op records) and
  `stopped_because`. `resolved` is computed from the final defect list instead of inferred from
  `holes_left`.
- `repair_status` is untouched. It remains the inspection's verdict on the file the customer sent.
- `TrainingLogger` logs the whole op sequence, kept and rolled-back, with the defect each op aimed
  at. **That log is the slice 8 corpus**, and it starts filling on the day slice 6 is enabled.
- `RepairReport.operations` entries are extended, not replaced (no contract break).
  `RepairReport.entities` after a repair is the final defect list, so the delivered report states
  what is left.

### Configuration

`settings/cad_repair.py`, `docs/reference/configuration.md` and `.env.example` move together; the
configuration certification tests enforce this.

| setting | default | note |
|---|---|---|
| `CAD_REPAIR_MAX_OPS` | 6 | ops tried per run, kept or not |
| `CAD_REPAIR_STALL_LIMIT` | 2 | consecutive non-kept verdicts |
| `CAD_REPAIR_MAX_AREA_CHANGE_RATIO` | 0.02 | residual after declared deltas |
| `CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO` | 0.02 | residual after declared deltas |
| `CAD_REPAIR_TIMEOUT_S` | 120 (existing) | now covers the whole loop |

### Testing (tests first)

- **Unit, hermetic, in `make test-fast`:** planner ordering and ineffective pairs, paired-rim
  refusal, session snapshot/rollback and fingerprint carry-forward, caps arithmetic including the
  accounted area check, every loop stop reason. All of these use op doubles and no kernel.
- **Native:** each real op against a real broken fixture. The existing fixtures cover the open
  shell, the missing bore wall and the mixed assembly; three are added to `tests/cad_fixtures.py`:
  small edge, bad orientation, loose faces. Each test asserts that the target fingerprint is gone
  and that the verdict basis is `kernel` where the op has history.
- **Regression:** `repair_step_file` behaves as before on the existing native suite.
- **Corpus:** `repair_corpus.py --loop` prints the op sequence and `stopped_because` per file.
- **Deploy:** the node change is claimed only after a dev-environment run with
  `CAD_REPAIR_ENABLED` on, on a broken fixture, reading the `repair_attempt` op log in the run state.

### Success criteria

1. Each op fixes its targeted defect on its fixture, with the target's disappearance attributed to
   the kernel mapping.
2. A rolled-back op leaves the shape identical to its snapshot.
3. On the existing native suite and corpus, the loop is never worse than today's whole-shape
   repair: no file that repaired before now fails to repair.
4. Every run ends with a recorded `stopped_because` and a full op log.

## Slice 7 — entity context (sketch)

Add to each `EntityDefect`: surface type for faces (plane, cylinder, cone, B-spline, …), adjacent
face fingerprints and their types, and dihedral angle at edges where cheap. Report the paired-rim
finding as its own defect (`missing_bore_wall`) instead of leaving it as a planner-only refusal.
Emit `wire_gap` from `ShapeAnalysis_Wire` and `tolerance_outlier` from
`TOLERANCE_OUTLIER_MULTIPLE`, or delete both from the taxonomy and triage. Which one depends on
whether slice 6's native fixtures can produce them.

## Slice 8 — model planner (sketch)

A model implements `Planner.propose`. Its input is the defect list with slice 7 context, the run
history including ineffective pairs, and the op catalogue with parameter bounds. Its output is a
`Candidate` or abstain. The session, caps and progress rule are unchanged, so the model cannot do
anything the rules planner couldn't. It runs in shadow first: the rules planner acts, the model's
proposal is logged, and they are compared on the slice 6 training log. It gets promoted only if it
does as well on resolved rate with no more rollbacks.

## Slice 9 — operator queue writer (sketch)

Jobs that triage routes to manual cleanup, and loops that stop on `stalled` or `no_candidate`
with error defects left, enqueue into the existing operator queue. Operator decisions become
labels for slice 8.

## Open questions

1. Should `CAD_REPAIR_FILL_PLANAR_HOLES` default to on? Each fill is now targeted, capped,
   area-accounted and verified by re-inspection, a much stronger case than the blanket pass. It
   is still the one op that invents geometry. Leaning yes, decided after the slice 6 corpus run.
2. Small faces still route to a person. Whether they become advisory is the question already open
   on this branch; slice 6 adds no op for them either way.
