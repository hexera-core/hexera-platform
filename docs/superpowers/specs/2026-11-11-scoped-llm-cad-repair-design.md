# Scoped LLM-Driven CAD Repair - Design

Status: approved design, 2026-11-11. Decisions in this document were settled with the product owner
across three rounds of questions; the ones marked AGENT CALL are mine and are flagged so they can be
checked while reading. This document is the spec that
`docs/superpowers/plans/2026-11-11-*.md` argue from.

## Intent, And Why Now

The CAD Repair System (CRS) today repairs one defect at a time and proves the result over the whole
part after every attempt. That single property - **a local edit triggers a global proof** - causes
everything we want to change:

- Cost. One re-inspection (`cad/repair/localize.py:localize_scan`) per attempt dominates the run.
  A measured run on the knee prosthesis paid 14 scans to keep 7 operations; 6 of those scans were
  paid for and discarded.
- Shallowness. Because each iteration is expensive, the budgets are small (`CAD_REPAIR_MAX_OPS`
  defaults to 6, `CAD_REPAIR_STALL_LIMIT` to 2, `CAD_REPAIR_TIMEOUT_S` to 120) and the stall limit
  is global, so two unrelated failures anywhere end the whole run.
- No feedback. The model-directed portion runs as a pre-pass and never sees the loop's ledger fill.
  `cad/repair/plan.py:RunHistory` says so in its own docstring: "NOT YET READ BY THE MODEL-DIRECTED
  PORTION... Feeding it back would require the model to re-plan mid-run, which the current async
  pre-pass against a synchronous loop cannot do."
- A ceiling on what repair may attempt. Judgement defects - `self_intersection`, `small_face` - are
  reserved for human review because the deterministic system cannot tell a modelling error from a
  deliberate overlap, or a sliver from a chamfer.

The hypothesis this design rests on: **a model can semantically understand a region of a part -
what it is and what should become of it - and can therefore be trusted to author scoped, reversible
work orders against it.** That capability is what unlocks the reserved defects, and scoping is what
makes iteration affordable.

The unifying observation is that a region is simultaneously the semantic unit (what the model
reasons about), the verification unit (a local edit needs only a local proof), the reversal unit (a
wrong hypothesis costs one region, not the run) and the cost unit (regions are independent, so they
parallelise). The reason to do this for semantics is the same reason it gets cheap.

### Evidence This Is Grounded, Not Speculative

Measured on `dev-vidhero` job `4624c395-0d83-4cf3-99c7-13b98941be2c` (repaired artefact hash
`427a77488a1f5599`, input `knee-demo.step` sha `71ac22b9`), and verified against the pristine
source with an OpenCASCADE probe:

- CRS created **zero** new defects. By fingerprint with `complete_scan: true`: `open_shell` 8 -> 0,
  `self_intersection` 13 -> 13, `small_edge` 6 -> 6, `small_face` 5 -> 5.
- Every survivor is bit-identical before and after, and every one is present in the **pristine**
  `Knee prosthesis.step`: the smallest are a 135 nanometre edge and a 1.97e-07 mm2 face.
- The pristine part fails `BRepAlgoAPI_Check` on its own with 93 self-interference faults, and has
  51 pairs of non-adjacent faces in exact (0 um) contact.
- CRS restored volume to within 4e-11 relative of pristine after the fixture stripped one planar
  face per solid.

So the expensive geometry is inherent, not introduced, and the current caps were nowhere near
binding: area was declared to the microgram (4774.471117 mm2 declared against the same measured),
the diagonal moved by zero, and kernel tolerance never left 3.3e-05 mm. **The constraint is what
repair is allowed to attempt and how expensively it proves itself - not its safety limits.**

## Goals

1. Repair reaches **clean** parts, where clean means both: no defect of any severity remains
   undischarged, AND the part meshes within a declared cost budget.
2. The model authors scoped work orders ("target packages") and revises them on measured evidence,
   mid-run.
3. A wrong model judgement is detected, bounded, reverted and recorded - never silently absorbed.
4. Iteration cost falls enough that a run can afford many attempts: local edits get local proofs.
5. Every run emits a transition log usable as reinforcement-learning environment data.

## Non-Goals

- Replacing the mesher's own sizing intelligence. The handoff informs it; it does not override it.
- Repairing parts the inspector cannot read at all (`CAD_INSPECT_MAX_BYTES`, unreadable STEP).
- A synchronous human-in-the-loop. Escalation is asynchronous by decision.
- Multi-engine comparison. One repair surface, as today.

## Anti-Goals

These would make the project a failure even if it technically worked:

- **Reaching "clean" by relabelling.** Reclassification discharges a defect (decided below), so the
  cheapest route to a clean report is to call everything intentional. If the adversarial score does
  not hold this down, the feature is worse than what it replaces.
- **A part that is clean, meshable and geometrically wrong.** Scoped rebuild can move a feature.
  Every existing cap is a count, an area or a volume; none detects "this face is now in the wrong
  place."
- **Losing attribution.** If we cannot say whether an improvement came from model direction, the
  benchmark stops meaning anything (`docs/research/public-benchmarks.md`).
- **Deviation written into the customer's artefact without a declaration.** Repair-time deviation is
  permanent; it ships.

## Decisions Already Made

| # | Decision | Consequence |
|---|---|---|
| 1 | Clean = zero undischarged defects of any severity AND meshable within budget | Model must own the reserved judgement codes; `HELD_ANSWERING_SOURCES` licensing is on the critical path |
| 2 | A defect may be **discharged by reclassification** with a recorded rationale | "Clean" means nothing unexplained remains. Relabelling is the primary reward-hacking surface |
| 3 | Full scoped primitive set: scoped delete, scoped tolerance-merge, scoped local rebuild | Three new operations, each needing its own declaration and adversarial tests |
| 4 | Rebuild output is **parametric B-rep by default with declared deviation**, faceted only behind a deployment flag and recorded per region | Keeps the artefact editable CAD; fallback exists for hard tangles |
| 5 | The region loop **replaces the rules planner in the live path** | No dual live path |
| 6 | The rules planner is **retained as a benchmark baseline only**, reachable by the corpus runner, never by a customer job | Attribution to model direction survives (`public-benchmarks.md` requires it) |
| 7 | Escalation is **asynchronous**: committed regions ship, the failed region queues | A customer can receive a knowingly partial repair; the report must say so plainly |
| 8 | Escalation **reuses the existing review machinery** - `agents/repair_review/`, `CAD_REPAIR_REVIEW_ENABLED`, the `repair_review` tables | Region-shaped escalations are fitted into the per-defect schema |
| 9 | Model budget is **per region**, and a region that exhausts it escalates rather than degrading | Job cost scales with region count; a pathological part is expensive |
| 10 | Mesher side is **in scope** in this worktree, including the OCC import-healing options that are currently never set anywhere in `src/` | Closes the loop end to end; touches another engineer's area |
| 11 | Build the full architecture first, then evaluate on the corpus | No shadow gate. The scoring harness still lands early so evaluation is same-week, not from scratch |
| 12 | Delivery: one branch, per-phase commits, PR when a phase is demonstrably working | |

## The Architecture

### The Action: A Target Package

Today the model's action is a single `(operation, target)` directive decided in a pre-pass. It
becomes a package - a scoped work order:

- **locus** - entity fingerprints plus the region they span. The area the model claims to understand.
- **intent** - what the model believes is there and what should become of it, in words. *"These five
  slivers are a fan from a bad translation; dissolve them."* *"This opening is a bolt hole; leave it."*
- **plan** - an ordered list of catalogue operations scoped to the locus, or a reclassification.
- **predicate** - what the model asserts will be true afterwards, **stated before execution**.
- **budget** - operations, seconds, model calls, and allowed deviation *inside this locus*.

A package is carried by the existing directive channel. `cad/repair/directives.py` already states
the contract this design needs: "a directive is a planner directive, NOT a cap waiver - the
session's caps, rollback and progress rule judge every candidate exactly as they judge the rules
planner's." Packages are judged the same way.

### The Loop

```
decompose defects into regions                  (deterministic clustering)
per region, concurrently:
    model authors package                       (intent + plan + predicate + budget)
    snapshot at package boundary
    execute plan inside locus
    scoped verify: predicate + global invariants (cheap)
      predicate held   -> commit region
      predicate failed -> revert region, return the measured delta to the model
                          model revises within region budget
      budget spent     -> escalate region with its rationale, commit the rest
merge regions -> ONE global verify
```

Three qualitative changes: the model re-plans on evidence (regions being independent dissolves the
async-pre-pass-against-synchronous-loop blocker the code names); failure is local (a hard tangle
burns its own budget while clean regions finish); and partial success ships.

### Why The Pre-Registered Predicate Is The Safety Story

The model declares its expected outcome before acting, and the harness checks that exact claim. The
model therefore cannot grade itself; it can only make a claim something else measures. A wrong
claim is detected rather than absorbed, and the delta that contradicts it is the feedback signal.
Because the claim is fixed before the evidence exists, the reward is not gameable by relabelling
the outcome.

This is the one condition the refinement literature requires of a loop
(`docs/research/repair-solve-feedback-loop-literature-review.md`, clusters 1 and 6). The
progress-mirage result measures the alternative: across 54 cycles an agent claimed improvement every
time while 56% were flat or worse, and a strong in-band judge reading full artifacts and diffs still
accepted 44% real regressions.

A predicate must be **specific** or the model learns to promise nothing. "The shape is still valid"
always holds. Predicate specificity is scored, and a vacuous predicate is a failed package.

### Invariants In Three Tiers

Global constants become per-package declarations.

**Inviolable** - no package may request an exemption: no inverted or degenerate elements, no lost
solids, manifold output, valid shape. These are the kernel's floor, not policy.

**Declared per package** - the model asks for what it needs and says why; the harness holds it to
exactly the declaration: deviation inside the locus, faces removed as a count, area and volume
delta. This is how `cad/repair/caps.py` already reasons about area - "An operation that declared
50 mm2 and moved 200 is refused - not for moving surface, but for not understanding what it did" -
and the pattern generalises. A declaration is more permissive than a constant where it is earned,
and more accountable everywhere.

**Escalation, not refusal** - a region that cannot satisfy its predicate within budget goes to
review with the model's rationale attached, and the rest of the part ships.

Two existing caps change independent of all the above:

- `cumulative_refusal` returns `""` when `original.solids == 0`, so the volume cap is **structurally
  disabled** for any upload arriving as shells with no solid - exactly the shells-to-solids case
  fills create. On the knee, volume moved -6454.25 mm3 (-12%) with `declared_volume_mm3: 0.0` and
  nothing could look. This is a hole to close, not a cap to loosen.
- `CAD_REPAIR_MAX_HOLE_SPAN_RATIO` is 0.75 and the knee's largest open shell has
  `relative_size: 0.721691` - **96% of the cap**. Parts marginally larger than our own demo part
  cannot be filled at all. AGENT CALL: this needs headroom, and the right fix is per-package
  declaration rather than a bigger constant.

### Clean, Operationally

A part is clean when every defect in the final scan is **discharged** - either geometrically removed,
or reclassified as intentional with a recorded rationale - and the mesh of that part completes
within its declared cost budget. Both halves are required. A reclassified defect still appears in
the report, carrying its rationale; it is discharged, not hidden.

### The Transition Log

Every package is one transition: observation (the region's defects with measurements and
provenance), action (the package), outcome (predicate held or failed, measured deltas, cost in scans
and seconds), reward (predicate held, cost spent, and the downstream mesh cost of the geometry it
produced). Provenance travels with its confidence: `cad/repair/identity.py:EntityMapping` carries
`basis: kernel | geometric` precisely "so a training signal that cannot tell those apart" does not
overstate itself. Every operation in the measured knee run reported `basis: geometric`, so credit
assignment is weakest exactly where the fills did the work - a known gap, recorded here so the plan
can address it.

## Constraints

- Python, OpenCASCADE via OCP, as the existing repair surface.
- Fast host gates must pass: `make lint`, `make typecheck`, `make test-fast`, `make check-fast`.
- Anything needing a kernel, a worker or a mesh goes to a personal dev slug, never shared dev
  (`AGENTS.md`). Agents do not deploy shared dev.
- `CAD_REPAIR_*` posture must keep failing closed: an unknown value reads as off, because "a typo in
  a deployment must not put a model in charge of geometry."
- Repaired geometry stays content-addressed; an identical re-delivery writes identical bytes
  (`artifact_keys.py`).
- The repair report is the carrier for everything downstream reads. It currently records **no
  elapsed time at all**, despite `TIME_REPAIR_CLOCK` existing in `engines/determinism.py` for the
  purpose.

## What Is Left To The Builder

- The clustering rule for region decomposition. It must be conservative - over-merge rather than
  under-merge, because a self-intersection tangle can span what looks like two regions. The
  codebase already knows defects cluster: `RunHistory.declined` is keyed by aim because "one tangle
  of crossing faces is reported once per member face and every member proposes the IDENTICAL heal."
- The scoped-scan mechanism. `localize_scan(shape, *, valid=None, face_scan_s=0.0)` has no region
  parameter and always walks the whole shape. This is the highest-value new primitive, because it
  is what makes iteration affordable.
- Predicate grammar and the specificity score.
- Prompt and context construction for package authoring, reusing `agents/` conventions.
- Which global invariants are cheap enough to check on every package versus only at the final merge.

## Risks

| Risk | Why it matters | Mitigation in scope |
|---|---|---|
| Relabelling reaches "clean" cheaply | Anti-goal #1 | Adversarial corpus track scoring intentional-claims; reclassification rate reported per run |
| Scoped rebuild moves a feature | Anti-goal #2; no existing cap detects it | Parametric default with declared deviation; faceted behind a flag; rebuild lands last, behind its own switch |
| Regions are not independent | Scoped proof misses action at a distance | Conservative over-merge; cheap global invariants every package; one full scan at merge |
| No deterministic live fallback (decision 5) | Model off or unreachable means no repair | Accepted by decision. Posture still fails closed; the baseline planner remains for the corpus runner |
| Cost moves rather than falling | Fewer scans, more model calls | Per-region budgets (decision 9); fingerprint-keyed cache of judgements, since fingerprints are content-derived and a judgement is valid for every future upload containing that entity |
| Partial repairs reach customers (decision 7) | Trust | The report states plainly what shipped unrepaired and why; escalation carries the rationale |
