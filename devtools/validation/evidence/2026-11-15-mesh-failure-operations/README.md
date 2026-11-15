# Mesh-failure operations measured on the real-world track (2026-11-15)

**Verdict.** `newly_broken` is **0** (met) and the clean control is untouched (met). `newly_meshed`
is **3 against the baseline's 2**, which technically beats it, but **none of the three gains is
attributable to the new operations**: two are the same files the baseline already meshed, and the
third was reached by `--force-repair` plus the existing `fix_small_edges`. `rebuild_face` never ran
once, because every one of the 31 overlapping-facets files fails in gmsh's 3D stage (census below).
Nothing was tuned; these are the real numbers.

Track `mesh-failure-real`, 80 files (33 OCCT + 47 ABC; manifest
`devtools/validation/corpora/mesh-failure-real.json`), host darwin-arm64, one arm run sequentially.
Settings: the Plan 1 Task 8 arm-B environment (`CAD_REPAIR_ENABLED/AUTONOMOUS/FILL_PLANAR_HOLES/
FILL_NONPLANAR_HOLES/FIX_SMALL_FACES=true`, `MAX_OPS=200`, `STALL_LIMIT=50`, `TIMEOUT_S=300`,
`REGION_LOOP=live`, `REGION_ATTEMPTS=6`, `MESH_PROBE=live`) plus `CAD_REPAIR_SPLIT_CLOSED_FACES`,
`CAD_REPAIR_FRAGMENT_BODIES`, `CAD_REPAIR_REBUILD_FACE` all `true`; runner flags `--mesh-probe
--force-repair`. Model author and reviewer off (`llm_author: off`, `package_review: off` in both
bundles' `settings.json`). Files:
`bundle-ops/` (sealed), `rows-ops.json`, `run-ops.log`; control `bundle-clean-control/`,
`rows-clean-control.json`, `run-clean-control.log`; census `overlap-stage-census.{json,log}`;
`run-status.txt`.

## Headline (vs Plan 1 Task 8 arm B, `../2026-11-14-mesh-failure-location/`)

| | baseline (arm B, no new ops) | this run |
|---|---|---|
| meshed before / after | 1 / 3 | 2 / 5 |
| **newly_meshed** | 2 | **3** |
| **newly_broken** | 0 | **0** |
| still failing: edge_through_face | 25 | 27 |
| overlapping_facets | 31 | 31 |
| open_loop / periodic_surface | 4 / 3 | 4 / 2 |
| timeout | 10 | 7 |
| no_volume / probe_error / unreadable | 1 / 2 / 1 | 1 / 2 / 1 |
| stopped `unmeshable` | 5 | 14 |
| forced repairs (`--force-repair`) | n/a | 35 (refused 32, repaired 2, no_change 1) |
| outcomes | refused 35, manual_cleanup 22, nothing_to_repair 15, repaired 4, no_change 3, repair_failed 1 | refused 60, no_change 11, repaired 6, not_attempted:manual_cleanup 2, repair_failed 1 |

Who the three newly meshed files are:

- `trj3_s1-tc-214.stp` (edge_through_face): kept `fragment_bodies` once plus nine small-edge/face
  fixes. Baseline meshed it too, without `fragment_bodies`. `fragment_bodies` WAS kept on this file, so "not the new op" rests on the baseline comparison (same file meshed without it), not on row evidence alone; with one arm and nondeterminism unmeasured, a contribution from the op cannot be excluded outright.
- `trj7_k1-id-214.stp` (kind `other`): kept `sew_faces`. Baseline meshed it too.
- `00502800_...step_000.step` (periodic_surface): the only gain beyond baseline. It was
  `not_attempted:manual_cleanup` in the baseline; `--force-repair` ran a session and three kept
  `fix_small_edges` meshed it. An existing operation, newly reachable; again not a new op.

**newly_broken = 0 was barely exercised.** The two files that meshed before (`trj6_pm4-ec-203.stp`,
routed `not_attempted:manual_cleanup`; `00503200_...`, `not_attempted:manual_cleanup`) never entered
a session, so their `meshes_after` is null and carries the before verdict. The gate therefore says
nothing about the don't-break direction on this track; the clean control is what covers it.

Meshed before: 2 here vs 1 in the baseline. The extra file (`00503200_...`) meshes under the probe
here and did not at the baseline; one pass each, so that is nondeterminism or engine change, and it
is not a repair.

## Per-operation tallies (all rows)

| operation | kept | rolled_back | declined | failed |
|---|---|---|---|---|
| fix_small_faces | 6 | 747 | 0 | 0 |
| fix_small_edges | 46 | 315 | 0 | 0 |
| fix_shape_global | 0 | 314 | 0 | 0 |
| fragment_bodies | 10 | 33 | 0 | 0 |
| sew_faces | 2 | 20 | 0 | 0 |
| fill_planar_hole | 0 | 4 | 0 | 0 |
| fill_nonplanar_hole | 0 | 4 | 0 | 0 |
| fix_face_boundary | 0 | 2 | 0 | 0 |
| split_closed_faces | 0 | 1 | 0 | 0 |
| **rebuild_face** | **never exercised** | | | |

`fragment_bodies` was kept on 10 files, all edge_through_face; one file meshed afterwards (above,
and the baseline got it without the op). The other nine are `no_change`/`refused` and stay failing.
`split_closed_faces` ran once and rolled back.

**The `00506275_...` row (abc).** It kept `fragment_bodies` once and `fix_small_edges` three times, yet
its outcome reads `no_change`, `meshes_after` false. Read from the row: the result was not discarded
(`result_discarded` unset) and the delivered file did change (faces 1271 to 1279, edges 7123 to 7141,
2 solids). `no_change` is credited by re-inspection of the original defects, and none of its 96
`small_edge` / 675 `small_face` findings was cleared (`fixed={}`), so the outcome label does not mean
the geometry was untouched. The BEFORE mesh kind was `edge_through_face`; after the kept
`fragment_bodies` the probe fails differently, one `mesh_overlapping_facets` (face:943) remaining,
stopped `unmeshable`. That overlap is the failure revealed after fragmenting, not a file that
started as an overlapping_facets case, which reconciles with the sentence below that `fragment_bodies`
was kept on no file whose starting kind was overlapping_facets. "Declined" is 0 because no row records a declined
status; the planner's own refusals were 6 `no_operation_claims_it`. The rows record no
revealed-not-damage counts (the operation records carry only op, status, reason, basis, defect code
and targets), so **revealed counts are not reported: that is an instrumentation gap, not a zero.**

Mesh defects located at the end of the session (`mesh_located_remaining`): 74 of 80 files, against
12 of 80 in the baseline (codes: edge_through_face 73 entries, overlapping_facets 40, timeout 9,
open_loop 3, no_volume 2, probe_error 2, periodic_surface 1). **All 15 previously invisible files are
now located (15/15, baseline 0/15)**, because `--force-repair` starts a session on inspector-clean
files. That, not the operations, is the largest change this run made.

## Overlap-stage census: does rebuild_face have any reach?

`rebuild_face` only acts on a `mesh_overlapping_facets` defect whose measurement `stage` is `2d`
(a rule in `operations.py`). The rows do not carry per-defect measurements, so I re-probed the
original input of each of the 31 files with `kind == overlapping_facets` using the same
`mesh_probe.probe_file` (120 s budget, diagonal/40; `/tmp/census.py`, output
`overlap-stage-census.json`):

**31 of 31 fail in the 3D stage; 0 are 2D-stage.** (22 name one surface, 9 two surfaces; none
located from the log.) The session located 40 overlapping_facets defect entries across those 31
files, all on the same failures. **Conclusion: `rebuild_face` has no reach on this track** and could
not have run, which is why it is "never exercised". Its real-world value is unproven either way:
this corpus does not contain a case it is built for. The 3D-stage overlap is the contact between
solids that `fragment_bodies` addresses; that op was kept on no file that started as overlapping_facets (its 10
keeps are all edge_through_face files; `00506275_...` ended in an overlap, see above). Caveat: the census is a probe of the input, not the session's stored
measurement; same probe code, but not the same run.

## Cost

| | baseline arm B | this run |
|---|---|---|
| run seconds (sum `timings.total_s`) | 4858 | 9083 (77 rows with timings) |
| row seconds (incl. scoring probes) | 5185 | 9530 |
| scans | 1736 | 2306 |

About 1.9x the baseline. The extra rows are the 35 forced sessions on files the baseline never
scanned. Wall-clock for the run was about 8 h because the host was under heavy unrelated load
(load average 10 to 14, the runner niced); seconds above are the in-process timers.

## Clean control (no-op)

The 40-file clean reference sample from the earlier evidence was **not preserved as a list**, so
this is a re-sample, not the same 40: the first one or two parts of each of the 22 families of the
`hexera-cad-defect-generation` clone's `cad-samples/` (316 files, still on disk at `/tmp/corpus`),
round-robin to 40, copied to a scratch directory. Same settings, **no** `--force-repair`.

| | result |
|---|---|
| rows | 40 (37 nothing_to_repair, 3 refused) |
| operations kept | **0** |
| parts mutated (counts before vs after) | **0** |
| newly_meshed / newly_broken | 0 / **0** (meshed 34 of 40 before and after) |
| scans / seconds | 124 / 122 |

The three refused parts entered a session (triage `conservative_repair`) and every attempted op
rolled back (73 rolled_back, none kept). The 6 unmeshed clean parts (5 overlapping_facets, 1
edge_through_face) are unchanged, i.e. the gmsh probe fails on some clean references too, which is
itself a caution about how much a probe failure means.

## Provenance and differences from the baseline

| | baseline (arm B) | this run |
|---|---|---|
| `engine_commit` | `9b296a1e` | `acc4e5e5` (clean committed HEAD; `git status` clean for src/ and devtools/ before the run) |
| settings digest | `f6a08f15...3e5` | `155d271d5244933c11928f1a0e0047bb91ecb9653d0bd153c37103e5353e7603` (control: `6f04c033...b305`) |
| `created_at` | 2026-11-15T05:04Z | 2026-11-15T14:43Z |
| flags | split/fragment false, no rebuild_face | split, fragment, rebuild_face true; `--force-repair` |

Differences are not only the flags: 19 commits and about 1.6k changed source lines separate the two
commits. The large ones: contact location for `fragment_bodies`; severity-first authoring; the
revealed-not-damage rule; the three operations themselves (`split_closed_faces`, `fragment_bodies`,
`rebuild_face`); and the reviewer machinery, present but off. `--force-repair` also changes which
files are scanned, so the located and cost columns are not comparable to the baseline even before
the operations. Fetch instructions for the inputs: `../2026-11-14-mesh-probe-calibration/README.md`
(sources and sha256s are in `bundle-ops/manifest.json`). No CAD is committed.

## What this does NOT show

- **No model judgement.** Author and reviewer are off; this is the rules baseline only. Nothing here
  speaks to the model-directed planner or to reviewed repair. `rebuild_face` stays off for
  anything customer-facing until the reviewer is live.
- **No attribution to the new operations.** With one arm, no off-arm at this commit and `--force-repair`
  confounded in, the +1 over baseline cannot be credited to `fragment_bodies`, `split_closed_faces`
  or `rebuild_face`; the evidence points the other way.
- **gmsh is not the product mesher.** Meshing is scored with gmsh 4.15.2 at diagonal/40, 120 s; a
  part meshing here may fail the product's mesher and vice versa. The clean control shows the probe
  failing on 6 of 40 clean references.
- **Reader effect.** Files that mesh or fail differently depending on whether gmsh reads the upload
  or OCP re-exports it (17 per the calibration); a no-op repair can "fix" them. Scores use the
  delivered STEP.
- **Nondeterminism not measured.** One pass; meshed-before differs by one file from the baseline.
- **Surprises.** rebuild_face never ran (no 2D-stage overlaps); no revealed counts recorded; the
  clean-control sample is a re-sample; 14 `unmeshable` stops vs 5; I did not break these down by forced vs unforced.
- Single platform (darwin-arm64); the 120 s probe budget was hit by multiple rows.
