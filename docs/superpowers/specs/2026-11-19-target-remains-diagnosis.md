# Why 81 operations applied and left their target: the `target_remains` diagnosis

Date: 2026-11-19. Branch: `crs/v3-agentic`. Measurement under diagnosis: the 90-part run of the
real-failure corpus in `/tmp/eval-baseline/rows-{1,2,3}.json` (deterministic catalogue, default
four operations), which produced 99 non-kept operations of which 81 were `target_remains`.

## The answer in one paragraph

**Hypothesis 2 is false, and it is false in a way worth stating plainly: we are not rolling back
real repairs.** All 81 `target_remains` verdicts are honest. The predicate asks whether the
defect's own fingerprint still carries the defect's own code in the re-inspection, and because a
fingerprint is a quantised bounding box, a verdict of `target_remains` can only be reached when an
entity of the same type, at the same box to the micrometre, still carries the same defect. On the
parts probed, the operations that got that verdict had changed nothing at all, or had changed
something irrelevant to the opening they were aimed at. **Hypothesis 1 holds for all three
operations**: wrong tool, honestly reported.

**But the same investigation found the fingerprint bug hypothesis 2 was looking for, running the
other way round.** The naming grid was scaled off the part's *current* diagonal, so any repair
that changed the part's extent by any amount renamed *every entity on the part*, including
entities nobody had touched. The predicate then could not find the defect it had just named and
read that as the defect being gone. That does not produce false rollbacks; it produces **false
keeps**, and all four of the four `fix_small_edges` keeps probed in detail are false. Fixed here.

## Per-operation verdict

| Operation | `target_remains` | Verdict | Recoverable |
|---|---|---|---|
| `fix_shape_global` | 37 (34 `small_edge`, 3 `invalid_brep`) | **H1** — a measured no-op | 0 |
| `fix_small_edges` | 30 (13 first attempts, 17 retries) | **H1** — plus a structural retry waste | 0 |
| `sew_faces` | 14 (`open_shell`) | **H1** — sewing cannot invent a cap | 0, but see below |

### `fix_shape_global`: 0-for-40 because it does literally nothing

Probed on `0055_00559248_58af66bdf106880f7ce00e79_step_001`,
`0059_00592781_58be5d61afe56e0fcdda08e3_step_000` and
`0056_00565386_21196964b04a3b9381ec52d8_step_000`, driving `ShapeFix_Shape` with exactly this
operation's modes (`FixSolidMode`/`FixFreeShellMode`/`FixFreeFaceMode`/`FixSameParameterMode = 1`,
`FixVertexPositionMode = 0`):

```
--- 0055_00559248_...
precision=0.026391 same_shape=True equal=True reshape_replaced=0 faces 24->24 edges 82->82 area 701.082897->701.082897 maxtol 1.000e-05->1.000e-05 valid False->False
precision=0.26391  same_shape=True equal=True reshape_replaced=0 ... (identical)
precision=2.6391   same_shape=True equal=True reshape_replaced=0 ... (identical)
--- 0059_00592781_...
precision=0.1 same_shape=True equal=True reshape_replaced=0 faces 110->110 edges 584->584 area 4127079.955356->4127079.955356 maxtol 1.000e-05->1.000e-05 valid True->True
--- 0056_00565386_...
precision=0.1 same_shape=True equal=True reshape_replaced=0 faces 141->141 edges 658->658 area 957276305.532404->957276305.532404 maxtol 1.000e-05->1.000e-05 valid False->False
```

`fixer.Shape().IsSame(shape)` is **TRUE** — the kernel hands back the same TShape — and the
`ShapeBuild_ReShape` context records **zero** replacements across every face, edge and vertex.
This holds at the run's precision and at ten and a hundred times it, which **rules out hypothesis
4**: it is not running too tight, it is not running at all. The same measurement with the mode
assignments removed is identical, so this is not a mis-wired mode either (the OCP attributes are
real `int` fields, checked).

Confirmed through the real loop (`loop.run`, same tolerance the driver computes):

```
  fix_shape_global [rolled_back] 'target_remains' basis=geometric
    targets=('face:b0e3b30839cd543d',) code=invalid_brep
    measures changed: NOTHING CHANGED
```

Every one of solids, shells, faces, edges, surface area, volume, max tolerance and validity is
bit-identical. The predicate asked "is `face:b0e3b30839cd543d` still invalid?" and the honest
answer was yes.

**What changed because of this.** `small_edge` is dropped from `FixShapeGlobal.answers`. 34 of the
37 attempts answered a `small_edge`, and `ShapeFix_Shape` is not the kernel's tool for a short
edge — `ShapeFix_Wireframe` is, `fix_small_edges` runs it, and at scope 1 it has always run first
and already failed on that exact edge. Nothing goes unclaimed: `small_edge` is produced in exactly
one place (`localize._size_defects`) and always on an edge, so `fix_small_edges` always claims it
and `fix_small_faces` claims it too. The 3 `invalid_brep` attempts stay — that is the defect only
this operation claims, and it is why the operation exists.

### `fix_small_edges`: right tool, but one call per part is all there is

The class docstring already said "an honest outcome on a solid is often no progress", and it is
right. Probed through the loop, the rolled-back attempts show `measures changed: NOTHING CHANGED`.
What the rows add is the structure of the waste:

```
first attempt per file: kept 19   target_remains 13
later attempts:         kept  0   target_remains 17
```

**Not one attempt after the first on a file was ever kept.** `ShapeFix_Wireframe` is whole-shape:
one call merges every small edge it is willing to merge, so from the second attempt on, the target
is by construction an edge the fixer has already declined, and the operation repeats the identical
call. 17 of the 81 are that. Suppressing the retry belongs to the planner's history
(`plan.py` / `directives.py`), not to `targets_for`, which is handed one defect and knows nothing
of the run — recorded in the docstring and **reported as a seam**.

### `sew_faces`: the opening is a missing face, and sewing invents no surface

`0052_00523121_d60ba5a2d4967a56c0268417_step_002` is a 22.5 mm tube, z from -5 to 45, with both
ends open — two closed circular free boundaries of length 70.6858 mm (= 2π·11.25).

```
[before] solids=1 shells=2 faces=16 edges=69 diag=190.3801 valid=True
evidence: {'bodies_sewn': 2, 'tolerance_mm': 0.1}
[after]  solids=1 shells=1 faces=16 edges=69 diag=190.3801 valid=True
   open_shell edge:0badc0bad52c81db box=[-11.25,-11.25,-5.0]/[11.25,11.25,-5.0]   <== FP MATCH
   open_shell edge:3e8dc917322224d2 box=[-11.25,-11.25,45.0]/[11.25,11.25,45.0]
VERDICT: target_remains
```

The operation did real work — it merged the two shells into one — and left both rims free, to the
micrometre. That is **hypothesis 1, not 3**: the instance count did not drop either. The tool for a
missing planar cap is `fill_planar_hole`, which is proposed immediately afterwards and declines
`filling_disabled (CAD_REPAIR_FILL_PLANAR_HOLES)`. These 14 are not recoverable by any change to
`sew_faces` or to the predicate; they are an argument about that flag, and they are the strongest
evidence in the run for turning it on.

## The bug that was actually there: the naming grid moved under the whole part

`identity.quantum_for` scaled the grid off the part's current diagonal
(`diagonal_mm * 1e-6`), and `localize._stamped` names each scan at the diagonal of the shape it
just scanned. So the two scans either side of an operation used **different grids** whenever the
operation changed the part's extent at all.

Reproduction, `0059_00592781_58be5d61afe56e0fcdda08e3_step_000`, the one `fix_small_edges` the
driver kept:

```
diagonal  before 1822.8446848590597
          after  1822.9358082694791   (delta 0.0911 mm)
quantum   before 0.0018228446848590596
          after  0.001822935808269479

BEFORE: 20 defects, by code: {'small_edge': 4, 'small_face': 16}
AFTER:  20 defects, by code: {'small_edge': 4, 'small_face': 16}
 B edge:6559f5d05f367289  min=[-75.0,-0.0,-877.5] max=[-75.0,0.0,-877.5]
     under q_before -> edge:6559f5d05f367289
     under q_after  -> edge:b10229bc344c5a2b
 A edge:b10229bc344c5a2b  min=[-75.0,-0.0,-877.5] max=[-75.0,0.0,-877.5]
     under q_before -> edge:6559f5d05f367289
     under q_after  -> edge:b10229bc344c5a2b
```

All four zero-length edges sit at boxes identical to the micrometre before and after. All four
fingerprints changed, because a 5e-5 relative change in the quantum is 24 grid cells at
z = -877.5 mm. The operation removed no edge (584 before, 584 after), fixed no defect, inflated
the part's worst tolerance from 1e-05 mm to 0.0373 mm — **and was kept**, because the predicate
could not find `edge:6559f5d05f367289` anywhere.

The run then convicts itself. The next operation in the same file is aimed at
`edge:b10229bc344c5a2b` and rolled back `target_remains` — *the same physical edge, the same box,
counted once as a repair and once as a failure.* The identical pattern appears in
`0057_00573500_90145238c934a39e4c672ff5_step_000`: kept on `edge:c414d19c1e363f93`, then
`target_remains` on `edge:17d45a9e0306237b`, one zero-length edge at
`[-30.0, -32.522394, -211.250145]` throughout.

Four of the 19 `fix_small_edges` keeps were probed defect-by-defect. **All four are false**: the
`small_edge` count was unchanged across the operation in every one, with every fingerprint renamed
(`0052_00525324`: 1 → 1; `0057_00573500`: 1 → 1; `0059_00592781`: 4 → 4; `0054_00544687`: 12 → 12,
even though two edges really did leave the topology).

`deviation.py` had already met the symptom and worked around it locally — "that diagonal drifts a
little as a package's operations rewrite the part ... a face named at one quantum and looked up at
another would silently select nothing" — without anyone following it back to the progress rule.

### The fix

`quantum_for` snaps the diagonal **up to a binary octave** (`math.frexp` reads the exponent
exactly; no log, no rounding) before taking the fraction. Two scans of one part now agree unless a
repair changed its size by nearly a factor of two, which no cap permits. The cost is up to one
octave of resolution — the grid is between 0.5 and 1 ppm of the part either way — and the honest
residual limit is a part whose diagonal straddles a power of two, where the grid still moves once.

This **narrows** the predicate: it removes a route by which a non-repair was accepted, and adds
none by which a repair is refused. The paired test
`test_an_operation_that_really_removed_the_sliver_is_still_kept` exists to hold that line.

## How many of the 81 are recoverable

**Zero, by a predicate or fingerprint fix.** Stated with the uncertainty it deserves:

- **37 `fix_shape_global`: zero, high confidence.** `IsSame` on three parts at three precisions is
  as close to proof as this gets. 34 of them stop being attempted at all now, which is a cost
  saving, not a repair.
- **30 `fix_small_edges`: zero, high confidence** for the 17 retries (the call is idempotent and
  whole-shape) and **zero, medium confidence** for the 13 first attempts — those were probed on
  three parts only, all showing a bit-identical shape.
- **14 `sew_faces`: zero by any change to this operation, high confidence.** Plausibly ~14
  recoverable by `CAD_REPAIR_FILL_PLANAR_HOLES`, which is a separate decision with its own risk,
  on evidence from one part. Call that **low confidence** as a number and high confidence as a
  direction.

**The measurement will get worse before it gets better, and that is the point.** Most or all of
the 19 `fix_small_edges` keeps are artefacts of the moving grid; four of four probed are. A
re-run of the baseline on this change should be expected to show `fix_small_edges` keeping close
to 0 of 58 rather than 19, with the difference moving into `target_remains`. The catalogue's real
deterministic ceiling on this corpus is therefore **lower** than 20 of 99, not higher — and the
next honest move is capability (`fill_planar_hole`, a tool that can actually shorten a zero-length
edge), not predicate work.

## Changed, and deliberately not changed

Changed:
- `identity.py` — `quantum_for` snaps to an octave; the stale note that excused the drift replaced
  by what was measured.
- `operations.py` — `small_edge` dropped from `FixShapeGlobal.answers`; the measured no-op, the
  `fix_small_edges` retry structure and the `sew_faces` limit recorded in the three docstrings.
- `tests/unit/cad/test_repair_identity.py`, `test_repair_session.py`,
  `test_repair_operations_contract.py`.

Deliberately not changed:
- **`session._progress_refusal`.** It is correct. All 81 verdicts it issued were true.
- **`localize._stamped`** (another lane's file). It names a scan at the current shape's diagonal.
  With the octave snap that is now harmless, but the *right* shape of the call is to name every
  scan in a run at the UPLOAD's diagonal, which the session already has as
  `self.original.diagonal_mm` and already passes as `scope_diagonal_mm`. **Seam:** add a
  `quantum_diagonal_mm` parameter to `localize_scan` and have `session` pass the original.
- **The planner's retry of a whole-shape operation** (`plan.py` / `directives.py`). **Seam:** a
  candidate whose operation is whole-shape and was already tried on this shape cannot progress.
- **The caps and the new-defect guard**, as instructed. The 7 tolerance refusals and 3 invalidity
  refusals are the system working.
