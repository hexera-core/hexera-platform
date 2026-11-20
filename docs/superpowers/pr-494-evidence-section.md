## Evidence (CRS v3 evaluation)

> Rendered by `devtools/validation/v3_eval/render_pr_section.py` from the results JSON. **[PENDING]** means not measured yet; it is never a zero. No arm passes or fails: the numbers are reported as measured.

### Files fixed

- CRS v3 files fixed (CAD-repaired, and the repaired STEP newly meshes in gmsh): **[PENDING]** of **[PENDING]**; deterministic catalogue: **[PENDING]** of **[PENDING]**.
- Clean control: **[PENDING]** of **[PENDING]** parts still mesh with no new defect, **[PENDING]** newly broken. For information: repair changed **[PENDING]** clean parts (a kept operation or a crash).
- Zero-break bound (information): with no break in the v3 arm's failing sample plus the clean control, the 95% upper bound on the break rate is **[PENDING]** (rule of three over the pooled parts). It is a bound, not a proof of safety.

### Per arm

| | T6 rules-only baseline | T29 deterministic | T29 v3 | T19 tournament off | T19 tournament on (= T29 v3 run) |
|---|---|---|---|---|---|
| files | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| meshed before | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| meshed after | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| **files fixed** (newly meshed by CAD repair) | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| **newly broken** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| model calls | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| cost (model + VM) | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| shard wall time | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| cost per fixed part | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| commit | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| image | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |
| sample sha256 | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |

Endings, operations kept and rolled back:

- **T6 rules-only baseline**: endings **[PENDING]**; kept **[PENDING]**; rolled back **[PENDING]**
- **T29 deterministic**: endings **[PENDING]**; kept **[PENDING]**; rolled back **[PENDING]**
- **T29 v3**: endings **[PENDING]**; kept **[PENDING]**; rolled back **[PENDING]**
- **T19 tournament off**: endings **[PENDING]**; kept **[PENDING]**; rolled back **[PENDING]**
- **T19 tournament on (= T29 v3 run)**: endings **[PENDING]**; kept **[PENDING]**; rolled back **[PENDING]**

### Diagnostic (appendix, not a result)

Off by default (`repair_corpus --tolerant-delivery`). Not a fix, not a product fallback, and never counted in files fixed.

| | T6 rules-only baseline | T29 deterministic | T29 v3 | T19 tournament off | T19 tournament on (= T29 v3 run) |
|---|---|---|---|---|---|
| tolerant-mesher meshable (not counted as CAD repair) | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** | **[PENDING]** |

### What this does not show

- **T6 is the rules-only baseline.** macOS, commit `0bf78625`, no model configured, and the tournament and memory inert because their settings rows did not exist yet. On its first 90 files it newly meshed **0** parts.
- **The agentic arm had never been measured before this run.** Every v3 number above is the first.
- **The probe is not the product mesher.** `meshes_before/after` is the coarse gmsh probe. A part it meshes can still fail the product ladder, and the reverse.
- **Sample size.** 120 failing files (stratified, seed 20261120) plus 100 clean. A difference of one or two parts is noise. One run per arm; model arms are nondeterministic.
- **The earlier "130 small edges repaired" is withdrawn.** It was substantially false credit from the naming-grid bug fixed below.

## What this PR fixes beyond the plan

Found while landing or running the plan, each with its own commit and test:

- **Naming grid, one per run** (`f0ac2c28`, `5d28389a`). The grid scaled off the part's *current*
  diagonal, so a repair that moved the extent renamed every entity and the progress rule read the
  renamed defect as gone: a false keep. The grid now snaps to a binary octave and is fixed per run.
  This is why the earlier "130 small edges repaired" figure is **withdrawn**: four of four keeps
  probed were false credit.
- **`refused` means a gate said no** (`d304667a`, `2b493788`). Four other answers no longer share
  the word: nothing to repair, no operation claims it, attempted and none held, scan incomplete.
- **Whole-shape sweeps declare what they rebuilt and removed** (`309cc285`). `fix_small_edges`,
  `fix_shape_global` and `sew_faces` returned an empty `rebuilt` set, so the held-for-review guard
  never fired for them. On the way: the sewing reader called a zero-argument `DeletedFace()` the
  binding does not have and swallowed the `TypeError`, so every sewing reported "deleted nothing".
- **The live investigator could never edit** (`2cf9a586`). Three wiring faults meant every `edit`
  was refused with "no scratch clone"; the tests injected a fake factory and hid it.
- **Rehearsal clock** (`812ae022`). A rehearsal's clone got a number where the session calls a
  function, so every tournament candidate that reached a mesh probe failed with a swallowed
  `TypeError`, whatever its geometry did.
- **fTetWild in the pipeline image** (`e087833a`). Tier 2b read `unavailable` on the workers that
  hold the parts. Not yet confirmed on a deployed worker.
- **Five v3 flags plumbed, with settings rows** (`e087833a`, `5d496416`). Tournament, memory,
  review objection, handback and standalone had no settings rows, so every consumer's fallback read
  `off` whatever the environment said. T6 at `0bf78625` ran before this fix, which is why it is
  reported as the rules-only baseline and never as "everything on".
- **CI fixes** (`afb8a5d3`, `feeb1596`, `43053255`). The standalone route joins the operator
  allowlist, the editor seam is typed, and the publication-authority manifest is reconciled with
  the new call sites.
