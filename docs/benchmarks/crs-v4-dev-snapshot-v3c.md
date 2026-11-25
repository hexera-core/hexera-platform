# CRS v4 dev snapshot v3c (PROVISIONAL)

**PROVISIONAL.** 83 dev + 40 clean files. Written by `snap-v1r/analyze.py` from the collected run; every table is computed from the rows.

| | |
|---|---|
| Code | `4c74661ebafc6e50872f6bc4253635b9f79300aa` (resolved by launch.sh from `4c74661ebafc6e50872f6bc4253635b9f79300aa`) |
| Image | `us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:3e8c6fae170342bbb53cf63685c2e49a77f0e6ab042e308a1f038a82e05e3592`, local id `sha256:3807995301c1518c551c7298e308203cbdbc45167833c88dc09dd47b2c0377e3`. Label `org.opencontainers.image.revision` = `1d6ed735f7284609a269ca314c15ff6b0e359d4b`: the image's MESH BUILD (toolchain) is that commit; the RUN commit above is the code bind-mounted at `/w`, and the two may differ |
| VM | `dev-crsv4-snap-v3c`, c2d-standard-56, us-central1-b, reaper max-run 8h → DELETE; created 2026-11-24T06:40:49Z, deleted 2026-11-24T09:02:06Z |
| Splits | dev `/Users/kitts/archductor/snap-runs/v3c-202611240640/splits/dev.json` (sha256 `94e83d177fcd8522…`, 150 files); clean `/Users/kitts/archductor/snap-runs/v3c-202611240640/splits/clean.json` (sha256 `afbb72fe3b6291a6…`, 40 files) |
| Waves | `snap_v2_dev` (26 shards, files [5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 5, 6, 5, 6, 6]), `snap_v2_clean` (2 shards, files [20, 20]); cost-balanced plans (digests `c8c545b18f17`, `2d6d89a3944b`) |
| Flags | `--ops 60` (APPLIED ops; from the commit's evalspec.SHARED_BUDGET.ops), `--file-usd-cap 1.00`, `--route openai:gpt-6-astra`, `--h-mm auto`, `--workers 1`, file wall 1800 s (the commit's default), verdict cache `gs://dev-crsv3-transfer-224734058693/eval/snap-v3c-202611240640/verdict-cache` (fresh) |
| Model caps | dev [1.66, 1.66, 1.66, 1.66, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.66, 1.83, 1.66, 1.83, 1.83], clean [1.66, 1.66]: total **$49.88** |
| Shard exits | clean1=0, clean2=0, dev1=0, dev2=0, post=0 |

## READ FIRST: v3c is the FIRST wave with the AIMED OPS, on the v2 SPLITS. Not a rerun of v2, v3 or v3b.

| | v3c (this wave) | v3b (stopped, superseded) | v3 (stopped, regression) | v2 |
|---|---|---|---|---|
| run commit | `4c74661ebafc6e50872f6bc4253635b9f79300aa` | `e2b25ddf6496f26add2d23f8a10159661f4a1c95` | `323d659f8037b760a32436b6531f38ee99c2b59e` | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` |
| dev / clean split sha256 | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` / `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` | same files | same files | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` / `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` |
| wave ids | `snap-v3c-v2splits-{dev,clean}-<ts>` | `snap-v3b-v2splits-*-202611240606` | `snap-v3-v2splits-*-202611240544` | `snap-v2-*-202611240348` |
| what it can measure | aimed ops: contact family, slivers, rebuild_face / split_angle at named faces | engine before aimed ops (contact candidates all rejected) | crippled search (A10 regression) + body-op ceiling | crash + regression + ceiling |

- **What v3c adds over v3b:** `e23af4b3` (Lane A): the contact row builders now give `remove_duplicate_faces` its params, so contact candidates validate (contacts are 18 of 35 located failures); `rebuild_face` and `split_angle` are aimed at the faces the mesher named. `4c74661e` (Lane D, D18): `unify_same_domain` / `remove_small` aimed at the elongated face.
- Also on this commit: the A10 layer-3 regression fix (`c96fd583`), Lane C11's empty-compound fix (a body op CAN end clean), the rollback_detail crash fix, `located` in every row, the delivered BREP per (file, arm), search traces.
- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): `4c1fc7d5579d` and `e63710eae340`; one defect is double-counted in every dev_v2 rate. **Coincident duplicate faces** in `7e573047c1a6` (faces 108 and 109). The split is unchanged.
- "Measured (file, arm) pairs: this wave vs v2" separates pairs v2 lost to harness crashes from this wave's.
- Image mesh build (toolchain) `1d6ed735`; run commit, bind-mounted, `4c74661ebafc6e50872f6bc4253635b9f79300aa`.
- **STOPPED EARLY - superseded by v4, not wrong.** v3c is the **last pre-anytime baseline**: a wall-stopped search is recorded unmeasured here (v4 delivers and verdicts the best state). It was stopped to release 56 C2D vCPUs that v4, the oracle sweep and the fast loop need.

## STOPPED BEFORE IT FINISHED (2026-11-24T09:00:09Z): stopped EARLY: superseded by v4 (anytime fix, netgen every-root, the aimed ops) and holding quota the live waves need

The wave was stopped before every shard finished. Shards that had not finished were stopped; their rows written so far are kept. **299 (file, arm) pairs were never measured because of the stop** (in progress or not started), reported as `unmeasured: stopped at the wave deadline`. Separately, **53 (file, arm) pairs were excluded BEFORE the stop**; their exact reasons are not on disk (run_arm writes EXCLUDED.json only at shard end), so they are a group of their own. A stopped shard has no PROVENANCE.json and none was written, so the official report refuses those shards; the tables here are computed from the rows.

**The ledger $11.0780 is therefore a FLOOR on what the full wave would cost.** It is 22.2% of the $49.88 wave cap: money did not bound this wave, wall clock did.

| shard | rows kept | excluded before the stop | unmeasured (deadline) | had provenance |
|---|---|---|---|---|
| snap-v3c-v2splits-dev-202611240640/shard-10-of-26 | 13 | 1 | 10 | False |
| snap-v3c-v2splits-dev-202611240640/shard-11-of-26 | 17 | 0 | 7 | False |
| snap-v3c-v2splits-dev-202611240640/shard-12-of-26 | 8 | 2 | 14 | False |
| snap-v3c-v2splits-dev-202611240640/shard-13-of-26 | 2 | 3 | 19 | False |
| snap-v3c-v2splits-dev-202611240640/shard-14-of-26 | 9 | 3 | 12 | False |
| snap-v3c-v2splits-dev-202611240640/shard-15-of-26 | 14 | 1 | 9 | False |
| snap-v3c-v2splits-dev-202611240640/shard-16-of-26 | 4 | 3 | 17 | False |
| snap-v3c-v2splits-dev-202611240640/shard-17-of-26 | 4 | 3 | 17 | False |
| snap-v3c-v2splits-dev-202611240640/shard-18-of-26 | 7 | 3 | 14 | False |
| snap-v3c-v2splits-dev-202611240640/shard-19-of-26 | 8 | 3 | 13 | False |
| snap-v3c-v2splits-dev-202611240640/shard-20-of-26 | 20 | 1 | 3 | False |
| snap-v3c-v2splits-dev-202611240640/shard-21-of-26 | 2 | 7 | 15 | False |
| snap-v3c-v2splits-dev-202611240640/shard-22-of-26 | 9 | 1 | 10 | False |
| snap-v3c-v2splits-dev-202611240640/shard-23-of-26 | 9 | 2 | 13 | False |
| snap-v3c-v2splits-dev-202611240640/shard-24-of-26 | 10 | 1 | 9 | False |
| snap-v3c-v2splits-dev-202611240640/shard-25-of-26 | 5 | 2 | 17 | False |
| snap-v3c-v2splits-dev-202611240640/shard-26-of-26 | 3 | 3 | 18 | False |
| snap-v3c-v2splits-dev-202611240640/shard-3-of-26 | 2 | 0 | 18 | False |
| snap-v3c-v2splits-dev-202611240640/shard-4-of-26 | 8 | 3 | 9 | False |
| snap-v3c-v2splits-dev-202611240640/shard-5-of-26 | 17 | 1 | 6 | False |
| snap-v3c-v2splits-dev-202611240640/shard-6-of-26 | 14 | 1 | 9 | False |
| snap-v3c-v2splits-dev-202611240640/shard-7-of-26 | 13 | 3 | 8 | False |
| snap-v3c-v2splits-dev-202611240640/shard-8-of-26 | 6 | 3 | 15 | False |
| snap-v3c-v2splits-dev-202611240640/shard-9-of-26 | 4 | 3 | 17 | False |

## Measured (file, arm) pairs: this wave vs v2 on the same splits

v2: commit `5c43220a4ce4d993c12af1a602ad0d5000fcda31`, wave ids `snap-v2-dev-202611240348` / `snap-v2-clean-202611240348`. This wave: commit `4c74661ebafc6e50872f6bc4253635b9f79300aa`, wave ids `snap-v3c-v2splits-dev-202611240640` / `snap-v3c-v2splits-clean-202611240640`.

| split | pairs | measured (v2 → this) | harness crash (v2 → this) | file_timeout | deadline stop | other excluded |
|---|---|---|---|---|---|---|
| dev | 600 | 170 → 244 | 0 → 0 | 0 → 0 | 393 → 299 | 37 → 57 |
| clean | 160 | 160 → 160 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |

**Read this before comparing clean counts:** +0 dev pairs measured here were lost to harness crashes in v2 (crash count 0 → 0); a difference in deadline-stopped pairs is wall clock, not repair ability. Gains on pairs v2 never measured are partly the harness fix.

## Official reports

| report | result |
|---|---|
| dev | exit **1**  REFUSED shard-10-of-26: no PROVENANCE.json (shard unfinished) |
| clean | exit **2** snap_v2_clean (clean, normal) - UNRELIABLE ; warnings: staged: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two deterministic a | search_det: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two determinist | search_ai: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two deterministi |


## Per-arm, dev: body-overlap files counted as failures (headline)

> **CAVEAT FOR THIS TABLE:** v3c (`4c74661eb`) is the LAST PRE-ANYTIME BASELINE, stopped early because v4 superseded it, not because it was wrong. On this commit a search that hits its wall is discarded (recorded unmeasured) rather than delivering its best state; v4 (`4a97691a`) carries the anytime fix (B18), netgen every-root judging and the aimed ops. Read these numbers as the engine before the anytime fix.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 83 | 62 | 37 | 26 |
| clean (of conclusive) | 0 | 6 | 12 | 16 |
| repaired (unclean in, clean out) | 0 | 6 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0057_00577690_58b778c39c037a0f70331a6e_step_032.step, …) | 12 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, …) | 16 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 9 | 14 | 17 |
| inconclusive rows | 0 | 0 | 17 | 19 |
| excluded (reason) | out_of_scope 1, deadline stop 65, excluded before the stop (reason not on disk) 1 | out_of_scope 1, deadline stop 70, excluded before the stop (reason not on disk) 17 | out_of_scope 1, deadline stop 77, excluded before the stop (reason not on disk) 18 | out_of_scope 1, deadline stop 87, excluded before the stop (reason not on disk) 17 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 3 (0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, 0053_00533268_58a469534ed19e0ff68bb62f_step_008.step, 0052_00523200_1519f83024d29ad8f5872d22_step_002.step) | 6 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0052_00523208_1519f83024d29ad8f5872d22_step_010.step, 0052_00526908_81cccc4a79fc27bf121c4ec9_step_000.step, 0056_00561477_ca18416aea564660ee2eea7a_step_006.step, …) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step (volume x1.25)) |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 10.0997 |

## Per-arm, dev: deep body-overlap files left out of the denominator (owner ruling)

Files whose INPUT has a body overlap (28), from `triage.overlap` (measured on the input: max_depth_mm = the worst pair's cheaper cut, its method, bound_mm, within_bound). overlap.error: 1; layer-1 body_overlap with no triage.overlap: 0.

- `00564723_d7644cf5268368d63c60876a_step_000.step`: max_depth_mm 9.073 (mean_thickness) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0051_00510314_5899e652a878da0f8226347a_step_000.step`: max_depth_mm 0.08329 (measured) vs bound_mm 0.1, within_bound True → within bound (kept); 3 pair(s)
- `0051_00510565_589a00b4d2bc860f82f79a88_step_002.step`: max_depth_mm 2.266 (measured) vs bound_mm 0.08707, within_bound False → DEEP (left out); 3 pair(s)
- `0051_00510567_589a00b4d2bc860f82f79a88_step_004.step`: max_depth_mm 5.156 (measured) vs bound_mm 0.08916, within_bound False → DEEP (left out); 10 pair(s)
- `0051_00515810_589bdec38006660f7f91d8e2_step_014.step`: max_depth_mm 3.409 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0051_00516238_a001174e1863a81de4ee0f48_step_000.step`: max_depth_mm 1.166 (mean_thickness) vs bound_mm 0.1, within_bound False → DEEP (left out); 9 pair(s)
- `0051_00518960_bb93f0d63e97fc3782a0c6dd_step_051.step`: max_depth_mm 1.15 (measured) vs bound_mm 0.04999, within_bound False → DEEP (left out); 3 pair(s)
- `0052_00520702_589e0c6497e5700f6ce179d1_step_000.step`: max_depth_mm 24.33 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 7 pair(s)
- `0052_00521254_589e42d6c517b70fc5543cc9_step_000.step`: max_depth_mm 0.9 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 3 pair(s)
- `0052_00521989_589ed79ccbc1f90fc89a1c02_step_000.step`: max_depth_mm 0.2493 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step`: max_depth_mm 1.397 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 3 pair(s)
- `0053_00530659_a7f966eb7895d0814869e759_step_000.step`: max_depth_mm 9.013 (mean_thickness) vs bound_mm 0.1, within_bound False → DEEP (left out); 1 pair(s)
- `0053_00536143_58a5896bdeaa381032dc1afc_step_006.step`: max_depth_mm 0.2601 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0053_00536914_31b33d2db5ef5bd00c1c2c55_step_004.step`: max_depth_mm 3.8 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0053_00539618_58a6d600ebf14d0f8e1e94e1_step_008.step`: max_depth_mm 0.1333 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 2 pair(s)
- `0054_00542145_9b4bfe3307eba3d302e360ed_step_004.step`: overlap.error `timeout: v4_overlap_depths exceeded 240.0s; worker killed` (kept)
- `0054_00547086_58aaf7c222115e0f6fe69154_step_014.step`: max_depth_mm 3.409 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0055_00552482_58acdf0594d8a30ff44ef2f8_step_014.step`: max_depth_mm 17.2 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0055_00556546_4c38c761bbc6e5ea418a4223_step_000.step`: max_depth_mm 9.74 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0055_00556932_58ae6c5e9420f7103b1ccecd_step_029.step`: max_depth_mm 2.073 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0055_00557558_58aeccc0d60cfd0fd56386d5_step_002.step`: max_depth_mm 0.05 (measured) vs bound_mm 0.1, within_bound True → within bound (kept); 8 pair(s)
- `0055_00557863_383d8ed7f079f44b9dfecc72_step_000.step`: max_depth_mm 0.2705 (measured) vs bound_mm 0.09289, within_bound False → DEEP (left out); 4 pair(s)
- `0055_00558985_58af4fd6f1160b0f8d7beca0_step_001.step`: max_depth_mm 0.002 (measured) vs bound_mm 0.02361, within_bound True → within bound (kept); 10 pair(s)
- `0056_00569762_58b4bbec8115250f775a5752_step_009.step`: max_depth_mm 1.192 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 1 pair(s)
- `0059_00594882_58bf0c02835d62101bddf8c8_step_069.step`: max_depth_mm 1.499 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 2 pair(s)
- `0060_00608593_58c3f3be25887e0fdaba0489_step_001.step`: max_depth_mm 3.991 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0060_00608912_78e6b79236c0354d748969e2_step_000.step`: max_depth_mm 1.021 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0061_00611396_0b3be1f3a3fc526b378c1f14_step_000.step`: max_depth_mm 3.175 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)

Left out as deep (within_bound false): **24**. Not decidable and kept in: **1** (0054_00542145_9b4bfe3307eba3d302e360ed_step_004.step).

> **CAVEAT FOR THIS TABLE:** v3c (`4c74661eb`) is the LAST PRE-ANYTIME BASELINE, stopped early because v4 superseded it, not because it was wrong. On this commit a search that hits its wall is discarded (recorded unmeasured) rather than delivering its best state; v4 (`4a97691a`) carries the anytime fix (B18), netgen every-root judging and the aimed ops. Read these numbers as the engine before the anytime fix.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 59 | 43 | 31 | 24 |
| clean (of conclusive) | 0 | 6 | 12 | 16 |
| repaired (unclean in, clean out) | 0 | 6 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0057_00577690_58b778c39c037a0f70331a6e_step_032.step, …) | 12 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, …) | 16 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 9 | 13 | 16 |
| inconclusive rows | 0 | 0 | 8 | 12 |
| excluded (reason) | out_of_scope 1, deadline stop 65, excluded before the stop (reason not on disk) 1 | out_of_scope 1, deadline stop 68, excluded before the stop (reason not on disk) 14 | out_of_scope 1, deadline stop 73, excluded before the stop (reason not on disk) 13 | out_of_scope 1, deadline stop 78, excluded before the stop (reason not on disk) 11 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 3 (0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, 0053_00533268_58a469534ed19e0ff68bb62f_step_008.step, 0052_00523200_1519f83024d29ad8f5872d22_step_002.step) | 6 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0052_00523208_1519f83024d29ad8f5872d22_step_010.step, 0052_00526908_81cccc4a79fc27bf121c4ec9_step_000.step, 0056_00561477_ca18416aea564660ee2eea7a_step_006.step, …) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step (volume x1.25)) |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 7.1371 |

Widest version, for reference: every body-overlap input left out (28 files):

> **CAVEAT FOR THIS TABLE:** v3c (`4c74661eb`) is the LAST PRE-ANYTIME BASELINE, stopped early because v4 superseded it, not because it was wrong. On this commit a search that hits its wall is discarded (recorded unmeasured) rather than delivering its best state; v4 (`4a97691a`) carries the anytime fix (B18), netgen every-root judging and the aimed ops. Read these numbers as the engine before the anytime fix.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 55 | 40 | 31 | 24 |
| clean (of conclusive) | 0 | 6 | 12 | 16 |
| repaired (unclean in, clean out) | 0 | 6 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0057_00577690_58b778c39c037a0f70331a6e_step_032.step, …) | 12 (0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, 0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, …) | 16 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step, 0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 9 | 13 | 16 |
| inconclusive rows | 0 | 0 | 6 | 11 |
| excluded (reason) | out_of_scope 1, deadline stop 65, excluded before the stop (reason not on disk) 1 | out_of_scope 1, deadline stop 68, excluded before the stop (reason not on disk) 13 | out_of_scope 1, deadline stop 72, excluded before the stop (reason not on disk) 12 | out_of_scope 1, deadline stop 76, excluded before the stop (reason not on disk) 10 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 3 (0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step, 0053_00533268_58a469534ed19e0ff68bb62f_step_008.step, 0052_00523200_1519f83024d29ad8f5872d22_step_002.step) | 6 (0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0052_00523208_1519f83024d29ad8f5872d22_step_010.step, 0052_00526908_81cccc4a79fc27bf121c4ec9_step_000.step, 0056_00561477_ca18416aea564660ee2eea7a_step_006.step, …) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step (volume x1.25)) |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 7.0073 |

## Clean control

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 40 | 40 | 40 | 40 |
| clean (of conclusive) | 40 | 40 | 40 | 40 |
| repaired (unclean in, clean out) | 0 | 0 | 0 | 0 |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 0 | 0 | 0 |
| inconclusive rows | 0 | 0 | 0 | 0 |
| excluded (reason) | 0 | 0 | 0 | 0 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 0 | 0 |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 0 |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 0.0000 |



## Harness crashes (by name; a crash is not a repair failure)

Dev: none RECORDED. **This is NOT "no crashes": 53 (file, arm) pair(s) were excluded before the wave was stopped and their reasons are not on disk (run_arm writes EXCLUDED.json only when a shard ends), so any of them may be a harness crash** (e.g. a TypeError in run_staged) **or a file-wall timeout; they cannot be told apart from this run.**

Clean control: none.

## Excluded files, each its own group (not in any rate; the ruling on the exclusion rate is pending)

Dev:

**105 file(s), 356 excluded (file, arm) rows** (out_of_scope 4, deadline stop 299, excluded before the stop (reason not on disk) 53). File wall 1800 s.

| file | excluded arms (reason) | measured arms |
|---|---|---|
| `00513166_589af7a315a8541030f8f61d_step_012.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00521046_589e2cfbdd79ff0fc41309a8_step_018.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00522863_64293c310e5d6b9a8b377109_step_019.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00526522_2096e13f381fdeed3052900f_step_070.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00534812_10837490d7714a7adceb88f4_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00538723_2e9dcb57dfa7846a2f2c5fbc_step_010.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00542755_475d3d5812881065217cd6c1_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00542809_58a894c022115e0f6fe41de9_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00566501_3daa0f06888b329e557a6a76_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00567915_58b41f11dbdd800f7e6adfd0_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00569755_58b4bbec8115250f775a5752_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00571995_58b5a4ac33248d103c82ca1d_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00574192_5ee09d41b3e9ec327a3ddc07_step_015.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00576061_58b6fdf8dbdd800f7e712b44_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00593998_58bedf240159270f85d925eb_step_018.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `00595759_3e7304a7ff86e7407d42d324_step_034.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00596083_90f123a7b7e505aa9a785623_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00597851_54911a92f479626781c654b3_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00602867_58c1a27caad5b80f7bffe02d_step_027.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00606468_58c2cd0d85eca50f9755927c_step_144.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00607513_651b26c5198a02ed7451e50b_step_006.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00612282_58c5f88703a00a0f86ffa3f9_step_083.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00615057_1355e20b21640d942232cd85_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00616555_58c7bfe463ebf60f9147b95a_step_064.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `nist_ctc_03_asme1_rc.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_10_asme1_rb.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ctc_04_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_06_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_07_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_08_asme1_ap242-e2.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_stc_06_asme1_ap242-e3.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0051_00510314_5899e652a878da0f8226347a_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0051_00513736_589b1e411529400fd28c70ee_step_034.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0051_00513828_86650477f66607316f890e2b_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0051_00518226_7aa4de49968de523e2091a87_step_001.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00520702_589e0c6497e5700f6ce179d1_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0052_00521254_589e42d6c517b70fc5543cc9_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00521846_589eac5a2ec4e50f8780d04b_step_006.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00521989_589ed79ccbc1f90fc89a1c02_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0052_00526588_ae911af3880ea83f28728e58_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00527796_945276583aeb7d4221f130f8_step_006.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00531292_58a364a1d8d7800fd43939cf_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00532577_58a4246ffb62730f86f554b7_step_003.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00536143_58a5896bdeaa381032dc1afc_step_006.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0053_00536914_31b33d2db5ef5bd00c1c2c55_step_004.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0053_00537355_58a5eaf03522310f74b5131f_step_000.step` | input_only: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | - |
| `0053_00538785_58a65b38deaa381032ddddec_step_009.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0053_00538816_58a65e00deaa381032dde14e_step_015.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0053_00539073_58a66b7522115e0f6fe163e3_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0053_00539088_58a67037ebf14d0f8e1e4852_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00540598_c9c1928f21b45230075b9fde_step_004.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00542145_9b4bfe3307eba3d302e360ed_step_004.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0054_00544196_58a93fda716d73109155df3d_step_090.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00547994_58ab27a87442550f6daa230f_step_079.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0054_00548558_58ab54937442550f6daa7dcd_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0055_00551882_58aca7813522310f74bdd3f8_step_006.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0055_00552482_58acdf0594d8a30ff44ef2f8_step_014.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; search_det: not clean; staged: not clean |
| `0055_00552754_78fab44c55201efeb3082719_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00555903_19170ca5f7030848f6985b79_step_001.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: not clean; staged: not clean |
| `0055_00556546_4c38c761bbc6e5ea418a4223_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0055_00556932_58ae6c5e9420f7103b1ccecd_step_029.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0055_00557558_58aeccc0d60cfd0fd56386d5_step_002.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0055_00557708_58aee364d60cfd0fd563a8ac_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0055_00557863_383d8ed7f079f44b9dfecc72_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: not clean; staged: not clean |
| `0056_00561073_9e2961fdc18d9e5b976c08d0_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: not clean; staged: not clean |
| `0056_00561725_ce82e436f164059a066d0713_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00562266_a20a56b200869b8723d9c310_step_018.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00565489_fc467dbe3e5e9c8b99cbae04_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00566256_f7a9c09734b048d4f4637d6a_step_009.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00567586_aaf14752252a80d40c9e1283_step_005.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00567964_2dc8697785b603ebd064db60_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00569762_58b4bbec8115250f775a5752_step_009.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0057_00570021_58b4c0978115250f775aa494_step_002.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0057_00571912_58b5a0e4b1ea23104135d522_step_004.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0057_00574915_30a8ce52329868badc4fd335_step_014.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0057_00578194_02db97dfc9db7d283d359af0_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0057_00579594_83b3892e48b2778f2d468928_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00592329_58be052560d0ea0f7c80053f_step_006.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00593298_58bea51a3badff0fc550e4d4_step_013.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00594048_58bee1790505910f8018a531_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00594882_58bf0c02835d62101bddf8c8_step_069.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0059_00595893_24ae031415592b9fdd33eb15_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00595948_58bf7c0dacb579101e76c788_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00595987_2c695f24d69fd40a9fb1fcbd_step_003.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00598276_58c0423eb4ee370fd6253b3d_step_037.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00599448_58c07b8e910b980f78da6060_step_003.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00599513_67e4a5106b69d7e77716f579_step_012.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00602103_f0dda3737ed09100b2283db1_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00602875_5de7672422a977f86b14dcca_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00604173_4ec13d006f4db95425937572_step_023.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00608593_58c3f3be25887e0fdaba0489_step_001.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0060_00608682_58c401c28e887f101dd42c22_step_000.step` | input_only: out_of_scope: the input is unbounded (layer 1 unbounded_geometry); it is not a failure and; search_ai: out_of_scope: the input is unbounded (layer 1 unbounded_geometry); it is not a failure and; search_det: out_of_scope: the input is unbounded (layer 1 unbounded_geometry); it is not a failure and; staged: out_of_scope: the input is unbounded (layer 1 unbounded_geometry); it is not a failure and | - |
| `0060_00608912_78e6b79236c0354d748969e2_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0060_00609763_39a057597f72d79c2801cfa6_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00610091_0eac20cb919cf7202186d939_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00610541_78c1575a5d7590cb833fd693_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00611396_0b3be1f3a3fc526b378c1f14_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: not clean; staged: not clean |
| `0061_00618192_58c83c0c03a00a0f8603bd1c_step_031.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00619937_58c915a03045b20fe8b6b4a3_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `trj3_s1-ct-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `trj3_s1-tc-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `trj4_s1-ug-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `trj5_s1-in-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `trj7_b1-ec-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |

Clean control:

None.

## Gaming flags (clean outputs that moved the part: |volume ratio − 1| > 0.25 or bbox ratio < 0.90; STILL counted clean)

Dev:

- **input_only**: 0 of 0 clean outputs flagged
- **staged**: 0 of 6 clean outputs flagged
- **search_det**: 0 of 12 clean outputs flagged
- **search_ai**: 1 of 16 clean outputs flagged: `0052_00523208_1519f83024d29ad8f5872d22_step_010.step` (volume x1.25)

Clean control:

- **input_only**: 0 of 40 clean outputs flagged
- **staged**: 0 of 40 clean outputs flagged
- **search_det**: 0 of 40 clean outputs flagged
- **search_ai**: 0 of 40 clean outputs flagged

## AI marginal (search_ai − search_det, per file)

Paired over **25** files both search arms judged conclusively.
- **search_ai cleaned, search_det did not (3):** 0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step, 0052_00523208_1519f83024d29ad8f5872d22_step_010.step, 0052_00526908_81cccc4a79fc27bf121c4ec9_step_000.step
- **search_det cleaned, search_ai did not (0):** none
- **AI marginal = +3 files.**
- different end panel passes (4): 0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step det 1 / ai 4; 0052_00523208_1519f83024d29ad8f5872d22_step_010.step det 0 / ai 4; 0052_00526908_81cccc4a79fc27bf121c4ec9_step_000.step det 2 / ai 4; 00564723_d7644cf5268368d63c60876a_step_000.step det 1 / ai 2
- the model was paid on 39 of 45 search_ai rows, $10.0997 in rows

## Repeated member timeouts (counted as file failures; lead ruling D5)

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 83 | 10 | 16 | timeout_twice 9, timeout_after_hang 6, after_timeout_crashed 1 |
| staged | 62 | 0 | 0 | - |
| search_det | 37 | 0 | 0 | - |
| search_ai | 26 | 0 | 0 | - |

Clean control:

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 40 | 0 | 0 | - |
| staged | 40 | 0 | 0 | - |
| search_det | 40 | 0 | 0 | - |
| search_ai | 40 | 0 | 0 | - |

## Cheap/full agreement (search ranking tier vs full panel, confirmed states)

| arm | states compared | agree | cheap pass, full fail | cheap fail, full pass | rows without data |
|---|---|---|---|---|---|
| search_det | 199 | 95.5% | 9 | 0 | 0 |
| search_ai | 245 | 96.7% | 8 | 0 | 0 |

## Triage (triage.py, dev): leads

(triage output missing)

Full tables: `reports/triage_dev.md`, `reports/triage_clean.md`.



## Money and time

- **Ledger $11.0780** (route probes are metered in the shard ledgers on this code). `snap-v3c-v2splits-clean-202611240640-s1` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-clean-202611240640-s2` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s1` $0.8534 (10 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s10` $0.7246 (9 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s11` $0.6691 (11 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s12` $0.0633 (2 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s13` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s14` $0.4643 (4 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s15` $0.5981 (9 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s16` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s17` $0.0466 (3 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s18` $0.3141 (5 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s19` $0.6988 (8 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s2` $0.8975 (10 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s20` $0.9871 (16 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s21` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s22` $0.4924 (6 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s23` $0.4683 (6 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s24` $0.4170 (5 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s25` $0.1527 (3 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s26` $0.0704 (2 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s3` $0.0018 (1 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s4` $0.9165 (9 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s5` $1.2475 (10 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s6` $0.5927 (7 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s7` $0.2655 (5 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s8` $0.0567 (2 settles, 0 open); `snap-v3c-v2splits-dev-202611240640-s9` $0.0705 (2 settles, 0 open). Settled calls by kind: {'route_probe': 28, 'repair_v4_propose': 122}.
- **Wave wall 137.1 min** (launch to last shard). Shard minutes: clean1 9.2, clean2 9.1, dev1 73.1, dev2 96.5.
- Per-file arm wall, dev (excluded at the 1800 s wall), median / mean / p90: input_only 873.1 / 938.5 / 1800 s; staged 1800.0 / 1194.6 / 1800 s; search_det 1800.0 / 1345.2 / 1800 s; search_ai 1800.0 / 1422.5 / 1800 s.
- VM ~2.35 h ≈ $6.18.

## Caveats

1. PROVISIONAL split (150 dev files); small and stratified by source, not by defect.
2. The checker's run-to-run behaviour is still under Lane D's investigation; single-file differences are not effects.
3. The image's mesh build is commit `1d6ed735f7284609a269ca314c15ff6b0e359d4b` (its registry label, checked before launch); the run commit is `4c74661ebafc6e50872f6bc4253635b9f79300aa`. The code under test is the run commit, bind-mounted; the image supplies only the toolchain.
4. Rows past the file wall are excluded (288755c2); excluded files are unmeasured, not failures.
5. Overlap depth is triage.overlap, measured on each input; files with overlap.error or no overlap row are listed above.
