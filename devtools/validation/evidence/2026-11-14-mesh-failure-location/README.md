# Mesh failures located on the real-world track (2026-11-14)

**Verdict: acceptance FAILS on the first criterion.** 0 of the 15 previously invisible files are
located (bar: 12). `newly_broken` is 0 in both arms (met). Tuning nothing, these are the real
numbers; the cause is below.

Track `mesh-failure-real`, 80 files (33 OCCT + 47 ABC; manifest
`devtools/validation/corpora/mesh-failure-real.json`), host darwin-arm64. Two arms, run
sequentially, intended-identical NIST-2026-11-13 settings (see Provenance and confound for the actual differences) (`CAD_REPAIR_ENABLED/AUTONOMOUS/FILL_PLANAR_HOLES/
FILL_NONPLANAR_HOLES/FIX_SMALL_FACES=true`, `MAX_OPS=200`, `STALL_LIMIT=50`, `TIMEOUT_S=300`,
`REGION_LOOP=live`, `REGION_ATTEMPTS=6`); `CAD_REPAIR_MESH_PROBE` is `off` in arm A and `live` in arm B, **but the arms also ran at
different engine commits (see Provenance and confound)**. Both scored with `--mesh-probe` (gmsh file probe on the input and on the repaired
file). Bundles: `bundle-off-probe/`, `bundle-live-probe/` (manifest, settings, summary, rows);
rows with logs: `rows-*.json`, `run-*.log`.

## Provenance and confound: the arms ran at different engine commits

| | arm A (probe off) | arm B (probe live) |
|---|---|---|
| `engine_commit` (manifest) | `d567502e` | `9b296a1e` |
| `created_at` (manifest) | 2026-11-15T01:06:46Z | 2026-11-15T05:04:19Z |

The "only the probe differs" reading is therefore not what the bundles show. Arm B was re-run (see
Instrumentation) after the engine had moved on, and `settings.json` differs in more than
`mesh_probe`: arm B's records `split_closed_faces: false` and `fragment_bodies: false`, keys that
did not exist at `d567502e` (so the settings digests differ). Between the two commits the engine
gained the `split_closed_faces` and `fragment_bodies` operations, the model author
(`model_author.py`, `observe.py`), and changes in `session.py`, `rules_author.py` and
`conservative.py` (e.g. the revealed-finding rule, a per-package cap on the proof's probe budget).

Which paths were exercised: both new operations are behind flags that were false in arm B, and the
model author is off in these runs, so none of the new operations or model-directed planning ran.
That is why I expect the confound to be small. I do not claim it is zero: the `session.py` and
`rules_author.py` edits are not all flag-gated (the package-proof cap applies whenever a package is
proved, and the revealed-finding rule applies to any mesh operation, which includes the existing ones
aimed at mesh failures), and I did not isolate their effect. **The A/B deltas (meshed 2 to 4 vs 1 to 3,
timeout counts 7 vs 10, stop reasons) may partly reflect the engine change, not only the probe.**
The within-arm facts (arm B's located counts, `newly_broken`, the `unmeshable` count and the 0/15
result) are unaffected by this, since they are read from arm B alone.

Instrumentation timing, precisely: `timings` entered the working tree before arm A ran, so arm A
rows carry it (42 of 80) and the "Scans and time" sums for arm A are legitimate.
`mesh_located_remaining` entered between arm A and arm B (which is why arm B was re-run), so arm A
rows carry it in 0 of 80 and "located" counts are arm-B-only by construction. The code for both
fields was committed after both runs (`fe254a5d`); arm B's `engine_commit` predates it, and the
fields came from the uncommitted working tree at run time.

## Previously invisible files

Definition: the 15 files whose arm-A row outcome is `nothing_to_repair` (the inspector reported
nothing; all 15 also have no non-mesh defect located):

- bm2_ec_exhaust-A.stp
- bm2_ec_leverrod-A.stp
- 00500350_cf6391333569f6a17439ce66_step_000.step
- 00500875_a97e4a09c926c436c043e3c3_step_028.step
- 00502300_1dcb8ad6a594419908db0fd3_step_002.step
- 00502600_5895f9dd54620e0fdd79706b_step_000.step
- 00502775_5896070a01b2bb0fc88ae0eb_step_071.step
- 00502900_46dbba28aa960b1c0b680bda_step_007.step
- 00503600_58969abbfca3da0f6b696760_step_000.step
- 00503625_3ece9c6bb503a673c7105a4b_step_006.step
- 00506325_589844ae8460820fe9280f95_step_012.step
- 00506550_b5806bb84542de8e2656bc88_step_114.step
- 00506725_067fcf75ee0eae49a8b1f2e1_step_003.step
- 00508150_5898cfe0f3cd940f71b7b650_step_069.step
- 00508775_58991615a878da0f8225af98_step_009.step

"Located" = an arm-B row carries a `mesh_*` defect with a non-empty fingerprint in what the repair
session still held at the end (`mesh_located_remaining`; see Instrumentation).

**Result: 0 / 15.** All 15 are `nothing_to_repair` in arm B too. Cause: the corpus runner (and the
product's own triage, `recommend` returns `mesh_as_is` for a clean inspection) never starts a repair
session for a clean file, and the mesh probe lives inside the session's scan. No scan ran, so no
mesh defect could be located. This is a wiring gap, not a probe failure: the calibration shows these
files fail to mesh. Making the inspector or triage consult the mesh probe is outside this task.

## Everything else, arm B

- Files with a located (fingerprinted) `mesh_*` defect remaining: 12 of 80. Files holding any
  `mesh_*` defect (located or not): 40. Defect kinds: mesh_edge_through_face 19,
  mesh_overlapping_facets 17, mesh_timeout 5, mesh_no_volume 1. Unlocated ones carry no fingerprint
  and are never aimed at.
- Only 43 of 80 rows entered the repair path (35 refused, 4 repaired, 3 no_change, 1 repair_failed; 42 carry `timings`, the repair_failed row has none); 22 were
  `not_attempted:manual_cleanup` and 15 `nothing_to_repair`.

## meshing_summary

| | arm A (probe off) | arm B (probe live) |
|---|---|---|
| meshed before / after | 2 / 4 | 1 / 3 |
| newly_meshed | 2 | 2 |
| newly_broken | **0** | **0** |
| still failing: edge_through_face | 27 | 25 |
| overlapping_facets | 31 | 31 |
| open_loop | 4 | 4 |
| periodic_surface | 3 | 3 |
| timeout | 7 | 10 |
| no_volume / probe_error / unreadable | 1 / 2 / 1 | 1 / 2 / 1 |

Arm A and B differ by one meshed file before and after (2 vs 1, 4 vs 3) and by timeouts (7 vs 10);
see Provenance (engine-commit confound) and "What this does NOT show" on nondeterminism and budget.

## Stop reasons and outcomes

| | arm A | arm B |
|---|---|---|
| outcomes | refused 35, not_attempted:manual_cleanup 22, nothing_to_repair 15, repaired 4, no_change 3, repair_failed 1 | identical |
| stopped_because | regions_exhausted 4, resolved 3 (73 rows none) | **unmeshable 5**, regions_exhausted 2 (73 none) |

The 5 `unmeshable` rows (arm B): no_change 3, repaired 2. In arm A the same files ended
`regions_exhausted`/`resolved`: the new ending replaces the old one where only mesh errors remain.

## Scans and time

| | arm A | arm B |
|---|---|---|
| scans (all) | 1917 | 1736 |
| whole-shape | 49 | 49 |
| scoped | 1868 | 1687 |
| run seconds (sum of timings.total_s) | 1155 | 4858 |
| row seconds (sum, incl. scoring probes) | 1411 | 5185 |

The live probe costs about 3700 s over the 42 rows that carry timings. Scan counts differ because the runs are
not the same path once the probe changes which defects exist.

## Instrumentation (a code change, for review)

Rows did not carry per-defect fingerprints for mesh defects: the row's `before`/`after` come from
`caps.measure`, which never runs the mesh probe. `devtools/validation/repair_corpus.py` now adds
`mesh_located_remaining` (code, entity, fingerprint for each `mesh_*` entry in the region run's
`defects_remaining`) and `timings` to session rows (also refused ones). Covered by
`tests/unit/dev/test_repair_corpus_meshing.py::test_mesh_located_keeps_only_mesh_defects_still_held_with_their_fingerprints`.
Consequence: "located" is measured in what remains at the end, not at the first scan, so a defect
located and then fixed is not counted. A first arm-B run without this field (and so without located
counts) was discarded and re-run; its meshing numbers matched in kind (newly_meshed 2, newly_broken 0,
unmeshable 5) and are not part of this bundle. `run-status.txt` therefore lists two live-probe lines.

## What this does NOT show

- **No new operations.** Existing operations are only aimed at mesh failures; the operations that
  answer these families arrive in Plan 2. Nothing here says the failures can be repaired.
  `newly_meshed` is 2 in both arms, so in this confounded comparison the probe added no repairs (and 2 gains occur with it off); the arms also differ in engine commit, so even that comparison is not clean.
- **gmsh is not the product mesher.** Meshing is scored with gmsh 4.15.2 at diagonal/40 and a 120 s
  budget; a part that meshes here may still fail the engine the product uses, and vice versa.
- **Nondeterminism was not measured, and the arms differ in engine commit.** One pass per arm at
  `d567502e` vs `9b296a1e`. The A/B differences in meshed counts and timeouts could be run-to-run
  noise, the engine change, or the probe; this evidence cannot separate them.
- **Reader effect.** 17 files (calibration README) mesh or fail differently depending on whether
  gmsh reads the upload or OCP reads and re-exports it; two (trj10, abc/00509500) mesh once OCP has
  read them, so a repair that changes nothing already "fixes" them. Scores here use the delivered STEP.
- Also: the 15 invisible files were never scanned (above); located is end-state only; 120 s probe
  budget was hit by multiple rows; one platform.

## Fetching the inputs (no CAD is committed)

See `../2026-11-14-mesh-probe-calibration/README.md` (sources, sha256, extraction). Pass the OCCT and
ABC directories to `repair_corpus.py` as in `bundle-*/manifest.json`.
- Probe-live `refusals_by_reason` counts from before and after commit 8a95d589 are not comparable: a warning pick now also records the mesh `no_operation_claims_it` refusals it previously skipped.
