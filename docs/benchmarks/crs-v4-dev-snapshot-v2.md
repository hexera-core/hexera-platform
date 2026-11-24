# CRS v4 dev snapshot v2 (PROVISIONAL)

**PROVISIONAL.** 61 dev + 40 clean files. Written by `snap-v1r/analyze.py` from the collected run; every table is computed from the rows.

| | |
|---|---|
| Code | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` (resolved by launch.sh from `5c43220a`) |
| Image | `us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:3e8c6fae170342bbb53cf63685c2e49a77f0e6ab042e308a1f038a82e05e3592`, local id `?`. Label `org.opencontainers.image.revision` = `1d6ed735f7284609a269ca314c15ff6b0e359d4b`: the image's MESH BUILD (toolchain) is that commit; the RUN commit above is the code bind-mounted at `/w`, and the two may differ |
| VM | `dev-crsv4-snap-v2`, c2d-standard-56, us-central1-b, reaper max-run 5h → DELETE; created 2026-11-24T03:48:48Z, deleted 2026-11-24T05:42:09Z |
| Splits | dev `/Users/kitts/archductor/snap-runs/v2-202611240348/splits/dev.json` (sha256 `94e83d177fcd8522…`, 150 files); clean `/Users/kitts/archductor/snap-runs/v2-202611240348/splits/clean.json` (sha256 `afbb72fe3b6291a6…`, 40 files) |
| Waves | `snap_v2_dev` (26 shards, files [5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6]), `snap_v2_clean` (2 shards, files [20, 20]); cost-balanced plans (digests `12011f540d33`, `46a95b256eb5`) |
| Flags | `--ops 24` (APPLIED ops; from ?), `--file-usd-cap 1.00`, `--route openai:gpt-6-astra`, `--h-mm auto`, `--workers 1`, file wall 1800 s (the commit's default), verdict cache `gs://dev-crsv3-transfer-224734058693/eval/snap-v2-202611240348/verdict-cache` (fresh) |
| Model caps | dev [1.66, 1.66, 1.66, 1.66, 1.66, 1.66, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83], clean [1.66, 1.66]: total **$49.88** |
| Shard exits | clean1=0, clean2=0, post=0 |

## READ FIRST: this is the FIRST snapshot under the NEW goal (owner decision 2026-11-24)

- **Clean = layer 1 VALID + layer 2 full PANEL.** Faithfulness (layer 3) is still computed and reported, in the rows "clean but NOT faithful" below, but it NEVER gates clean. The geometry/fidelity caps are deleted (B12). Clean outputs that moved the part (|volume ratio − 1| > 0.25 or bbox ratio < 0.90) are FLAGGED as gaming, named per arm, and still counted clean.
- **Not comparable with v1 or v1r.** Those used the OLD clean (valid + panel + faithful). On this commit the deterministic arm also proposes new op rows: defeature (D14, ported from Raymond), fuse, `cut_by` at any depth, and `trim_unbounded` (C7). search_det and staged numbers measure a different generator, not v1's.
- **Splits:** `devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json` (150 failing) and `PROVISIONAL_clean_v2.json` (40 clean), at the run commit. These are PROVISIONAL, drawn from the partial reprobe-800 re-probe; the real splits come from the full re-probe.
- **Cache:** VERDICT_CODE_VERSION .4, a fresh prefix. Every verdict was meshed cold.
- **Image vs code:** the image's mesh build (toolchain) is `1d6ed735`; the run commit, bind-mounted, is `5c43220a`.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): `4c1fc7d5579d` and `e63710eae340` are the same part (477 faces, diagonal 437.112 mm, identical face 9). One defect is double-counted in every dev_v2 rate, including this wave's. The split was not changed.
- **Coincident duplicate faces** (Lane A, A11): in `7e573047c1a6` faces 108 and 109 are coincident duplicates (identical area 9032.0789 mm2, aspect 230:1); a mesher naming both is naming two copies of one face.
- **This wave ran WITH the rollback_detail crash** (fixed after `5c43220a`): every (file, arm) that raised (reason `error: ...`, e.g. a TypeError in run_staged) is listed by name under "Harness crashes": unmeasured, not a failed repair.

## STOPPED BEFORE IT FINISHED (2026-11-24T05:39:35Z): stopped EARLY on coordinator order to launch v3 on the gmsh_prod location fixes; 446 of 600 dev rows would need 5-7 h, past the 08:18:48Z guard

The wave was stopped before every shard finished. Shards that had not finished were stopped; their rows written so far are kept. **393 (file, arm) pairs were never measured because of the stop** (in progress or not started), reported as `unmeasured: stopped at the wave deadline`. Separately, **37 (file, arm) pairs were excluded BEFORE the stop**; their exact reasons are not on disk (run_arm writes EXCLUDED.json only at shard end), so they are a group of their own. A stopped shard has no PROVENANCE.json and none was written, so the official report refuses those shards; the tables here are computed from the rows.

**The ledger $4.2278 is therefore a FLOOR on what the full wave would cost.** It is 8.5% of the $49.88 wave cap: money did not bound this wave, wall clock did.

| shard | rows kept | excluded before the stop | unmeasured (deadline) | had provenance |
|---|---|---|---|---|
| snap-v2-dev-202611240348/shard-1-of-26 | 6 | 5 | 9 | False |
| snap-v2-dev-202611240348/shard-10-of-26 | 2 | 1 | 21 | False |
| snap-v2-dev-202611240348/shard-11-of-26 | 4 | 3 | 17 | False |
| snap-v2-dev-202611240348/shard-12-of-26 | 1 | 2 | 21 | False |
| snap-v2-dev-202611240348/shard-13-of-26 | 14 | 0 | 10 | False |
| snap-v2-dev-202611240348/shard-14-of-26 | 3 | 0 | 21 | False |
| snap-v2-dev-202611240348/shard-15-of-26 | 1 | 2 | 21 | False |
| snap-v2-dev-202611240348/shard-16-of-26 | 5 | 3 | 16 | False |
| snap-v2-dev-202611240348/shard-17-of-26 | 2 | 3 | 19 | False |
| snap-v2-dev-202611240348/shard-18-of-26 | 1 | 2 | 21 | False |
| snap-v2-dev-202611240348/shard-19-of-26 | 3 | 0 | 21 | False |
| snap-v2-dev-202611240348/shard-2-of-26 | 7 | 1 | 12 | False |
| snap-v2-dev-202611240348/shard-20-of-26 | 3 | 3 | 18 | False |
| snap-v2-dev-202611240348/shard-21-of-26 | 18 | 0 | 6 | False |
| snap-v2-dev-202611240348/shard-22-of-26 | 10 | 0 | 14 | False |
| snap-v2-dev-202611240348/shard-23-of-26 | 7 | 2 | 15 | False |
| snap-v2-dev-202611240348/shard-24-of-26 | 7 | 2 | 15 | False |
| snap-v2-dev-202611240348/shard-25-of-26 | 7 | 0 | 17 | False |
| snap-v2-dev-202611240348/shard-26-of-26 | 1 | 2 | 21 | False |
| snap-v2-dev-202611240348/shard-3-of-26 | 13 | 0 | 7 | False |
| snap-v2-dev-202611240348/shard-4-of-26 | 13 | 0 | 7 | False |
| snap-v2-dev-202611240348/shard-5-of-26 | 17 | 0 | 3 | False |
| snap-v2-dev-202611240348/shard-6-of-26 | 1 | 2 | 17 | False |
| snap-v2-dev-202611240348/shard-7-of-26 | 13 | 1 | 10 | False |
| snap-v2-dev-202611240348/shard-8-of-26 | 7 | 1 | 16 | False |
| snap-v2-dev-202611240348/shard-9-of-26 | 4 | 2 | 18 | False |


## Official reports

| report | result |
|---|---|
| dev | exit **1**  REFUSED shard-1-of-26: no PROVENANCE.json (shard unfinished) |
| clean | exit **2** snap_v2_clean (clean, normal) - UNRELIABLE ; warnings: staged: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two deterministic a | search_det: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two determinist | search_ai: every conclusive file has the same outcome (40/40 clean): a constant verdict looks like a broken harness, not a result - but this is ALSO what a real result looks like when two deterministi |


## Per-arm, dev: body-overlap files counted as failures (headline)

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 61 | 45 | 16 | 15 |
| clean (of conclusive) | 0 | 6 | 7 | 8 |
| repaired (unclean in, clean out) | 0 | 6 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 7 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 8 (00542755_475d3d5812881065217cd6c1_step_002.step, 0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 7 | 10 | 10 |
| inconclusive rows | 0 | 0 | 19 | 14 |
| excluded (reason) | deadline stop 88, excluded before the stop (reason not on disk) 1 | deadline stop 94, excluded before the stop (reason not on disk) 11 | excluded before the stop (reason not on disk) 15, deadline stop 100 | deadline stop 111, excluded before the stop (reason not on disk) 10 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 0 |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 3.2882 |

## Per-arm, dev: deep body-overlap files left out of the denominator (owner ruling)

Files whose INPUT has a body overlap (21), from `triage.overlap` (measured on the input: max_depth_mm = the worst pair's cheaper cut, its method, bound_mm, within_bound). overlap.error: 0; layer-1 body_overlap with no triage.overlap: 0.

- `0051_00510314_5899e652a878da0f8226347a_step_000.step`: max_depth_mm 0.08329 (measured) vs bound_mm 0.1, within_bound True → within bound (kept); 3 pair(s)
- `0051_00510565_589a00b4d2bc860f82f79a88_step_002.step`: max_depth_mm 2.266 (measured) vs bound_mm 0.08707, within_bound False → DEEP (left out); 3 pair(s)
- `0051_00510567_589a00b4d2bc860f82f79a88_step_004.step`: max_depth_mm 5.156 (measured) vs bound_mm 0.08916, within_bound False → DEEP (left out); 10 pair(s)
- `0051_00515810_589bdec38006660f7f91d8e2_step_014.step`: max_depth_mm 3.409 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0051_00518960_bb93f0d63e97fc3782a0c6dd_step_051.step`: max_depth_mm 1.15 (measured) vs bound_mm 0.04999, within_bound False → DEEP (left out); 3 pair(s)
- `0052_00520702_589e0c6497e5700f6ce179d1_step_000.step`: max_depth_mm 24.33 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 7 pair(s)
- `0052_00521254_589e42d6c517b70fc5543cc9_step_000.step`: max_depth_mm 0.9 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 3 pair(s)
- `0052_00521989_589ed79ccbc1f90fc89a1c02_step_000.step`: max_depth_mm 0.2493 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step`: max_depth_mm 1.397 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 3 pair(s)
- `0053_00530659_a7f966eb7895d0814869e759_step_000.step`: max_depth_mm 9.013 (mean_thickness) vs bound_mm 0.1, within_bound False → DEEP (left out); 1 pair(s)
- `0053_00538816_58a65e00deaa381032dde14e_step_015.step`: max_depth_mm 9.022 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 3 pair(s)
- `0054_00544196_58a93fda716d73109155df3d_step_090.step`: max_depth_mm 0.2694 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 1 pair(s)
- `0054_00547086_58aaf7c222115e0f6fe69154_step_014.step`: max_depth_mm 3.409 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0055_00552482_58acdf0594d8a30ff44ef2f8_step_014.step`: max_depth_mm 17.2 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0055_00556546_4c38c761bbc6e5ea418a4223_step_000.step`: max_depth_mm 9.74 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 6 pair(s)
- `0055_00558985_58af4fd6f1160b0f8d7beca0_step_001.step`: max_depth_mm 0.002 (measured) vs bound_mm 0.02361, within_bound True → within bound (kept); 10 pair(s)
- `0056_00569762_58b4bbec8115250f775a5752_step_009.step`: max_depth_mm 1.192 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 1 pair(s)
- `0057_00574915_30a8ce52329868badc4fd335_step_014.step`: max_depth_mm 0.4583 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 2 pair(s)
- `0060_00608593_58c3f3be25887e0fdaba0489_step_001.step`: max_depth_mm 3.991 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0060_00608912_78e6b79236c0354d748969e2_step_000.step`: max_depth_mm 1.021 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)
- `0061_00610091_0eac20cb919cf7202186d939_step_000.step`: max_depth_mm 1 (measured) vs bound_mm 0.1, within_bound False → DEEP (left out); 10 pair(s)

Left out as deep (within_bound false): **19**. Not decidable and kept in: **0**.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 42 | 30 | 14 | 14 |
| clean (of conclusive) | 0 | 6 | 7 | 8 |
| repaired (unclean in, clean out) | 0 | 6 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 7 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 8 (00542755_475d3d5812881065217cd6c1_step_002.step, 0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 7 | 10 | 10 |
| inconclusive rows | 0 | 0 | 12 | 10 |
| excluded (reason) | deadline stop 88, excluded before the stop (reason not on disk) 1 | deadline stop 93, excluded before the stop (reason not on disk) 8 | excluded before the stop (reason not on disk) 11, deadline stop 94 | deadline stop 101, excluded before the stop (reason not on disk) 6 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 0 |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 2.8272 |

Widest version, for reference: every body-overlap input left out (21 files):

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 40 | 28 | 14 | 14 |
| clean (of conclusive) | 0 | 6 | 7 | 8 |
| repaired (unclean in, clean out) | 0 | 6 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 7 (0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, 0052_00524615_58a08791cbc1f90fc89aefc2_step_005.step, …) | 8 (00542755_475d3d5812881065217cd6c1_step_002.step, 0056_00564820_9f99023025ad12ef276d1bf9_step_002.step, 0054_00540309_4ef8a7619ffd2eb17e6eb519_step_000.step, 0060_00604173_4ec13d006f4db95425937572_step_023.step, …) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 7 | 10 | 10 |
| inconclusive rows | 0 | 0 | 10 | 9 |
| excluded (reason) | deadline stop 88, excluded before the stop (reason not on disk) 1 | deadline stop 93, excluded before the stop (reason not on disk) 8 | excluded before the stop (reason not on disk) 11, deadline stop 94 | deadline stop 101, excluded before the stop (reason not on disk) 5 |
| clean but NOT faithful (reported, not gating) | 0 | 0 | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) | 1 (0052_00523208_1519f83024d29ad8f5872d22_step_010.step) |
| clean and gaming-flagged (still clean) | 0 | 0 | 0 | 0 |
| out_clean ≠ valid+panel (should be 0) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 2.6398 |

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

Dev: none RECORDED. **This is NOT "no crashes": 37 (file, arm) pair(s) were excluded before the wave was stopped and their reasons are not on disk (run_arm writes EXCLUDED.json only when a shard ends), so any of them may be a harness crash** (e.g. a TypeError in run_staged) **or a file-wall timeout; they cannot be told apart from this run.**

Clean control: none.

## Excluded files, each its own group (not in any rate; the ruling on the exclusion rate is pending)

Dev:

**121 file(s), 430 excluded (file, arm) rows** (excluded before the stop (reason not on disk) 37, deadline stop 393). File wall 1800 s.

| file | excluded arms (reason) | measured arms |
|---|---|---|
| `00513166_589af7a315a8541030f8f61d_step_012.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00518460_595bacda86770fa5dda409b8_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00521046_589e2cfbdd79ff0fc41309a8_step_018.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00522863_64293c310e5d6b9a8b377109_step_019.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00526522_2096e13f381fdeed3052900f_step_070.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00534812_10837490d7714a7adceb88f4_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00538723_2e9dcb57dfa7846a2f2c5fbc_step_010.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00542809_58a894c022115e0f6fe41de9_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00551787_cf931dcb8717801dea5fd095_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00564723_d7644cf5268368d63c60876a_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00566501_3daa0f06888b329e557a6a76_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00567915_58b41f11dbdd800f7e6adfd0_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00569755_58b4bbec8115250f775a5752_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00571995_58b5a4ac33248d103c82ca1d_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00574192_5ee09d41b3e9ec327a3ddc07_step_015.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `00576061_58b6fdf8dbdd800f7e712b44_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00593998_58bedf240159270f85d925eb_step_018.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00595759_3e7304a7ff86e7407d42d324_step_034.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00596083_90f123a7b7e505aa9a785623_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00597851_54911a92f479626781c654b3_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00602867_58c1a27caad5b80f7bffe02d_step_027.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00606468_58c2cd0d85eca50f9755927c_step_144.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00607513_651b26c5198a02ed7451e50b_step_006.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00612282_58c5f88703a00a0f86ffa3f9_step_083.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00615057_1355e20b21640d942232cd85_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `00616555_58c7bfe463ebf60f9147b95a_step_064.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ctc_03_asme1_rc.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_10_asme1_rb.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ctc_04_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_06_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_07_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ctc_04_asme1_ap203.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_08_asme1_ap242-e2.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_stc_06_asme1_ap242-e3.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0051_00510314_5899e652a878da0f8226347a_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0051_00513736_589b1e411529400fd28c70ee_step_034.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0051_00513828_86650477f66607316f890e2b_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0051_00515810_589bdec38006660f7f91d8e2_step_014.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0051_00516114_24c77a4e4477f9a84d0a86f3_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0051_00516238_a001174e1863a81de4ee0f48_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0051_00518226_7aa4de49968de523e2091a87_step_001.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00520702_589e0c6497e5700f6ce179d1_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0052_00521254_589e42d6c517b70fc5543cc9_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00521846_589eac5a2ec4e50f8780d04b_step_006.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0052_00521989_589ed79ccbc1f90fc89a1c02_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00523200_1519f83024d29ad8f5872d22_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; staged: not clean |
| `0052_00526588_ae911af3880ea83f28728e58_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0052_00527796_945276583aeb7d4221f130f8_step_006.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00530659_a7f966eb7895d0814869e759_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0053_00531292_58a364a1d8d7800fd43939cf_step_000.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00532577_58a4246ffb62730f86f554b7_step_003.step` | search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0053_00533268_58a469534ed19e0ff68bb62f_step_008.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0053_00536143_58a5896bdeaa381032dc1afc_step_006.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0053_00536914_31b33d2db5ef5bd00c1c2c55_step_004.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0053_00537355_58a5eaf03522310f74b5131f_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0053_00538785_58a65b38deaa381032ddddec_step_009.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0053_00538816_58a65e00deaa381032dde14e_step_015.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0053_00539088_58a67037ebf14d0f8e1e4852_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0053_00539618_58a6d600ebf14d0f8e1e94e1_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00542145_9b4bfe3307eba3d302e360ed_step_004.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00545731_58aa1a11baa7e90f79546cff_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0054_00547086_58aaf7c222115e0f6fe69154_step_014.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0054_00547994_58ab27a87442550f6daa230f_step_079.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0054_00548558_58ab54937442550f6daa7dcd_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00551882_58aca7813522310f74bdd3f8_step_006.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0055_00552482_58acdf0594d8a30ff44ef2f8_step_014.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: inconclusive (unmeasured search); staged: not clean |
| `0055_00552754_78fab44c55201efeb3082719_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00554260_58ada6478105ed0f83971f1e_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00555512_58adf189df48ba0f834d4e71_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00555903_19170ca5f7030848f6985b79_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00556546_4c38c761bbc6e5ea418a4223_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0055_00556932_58ae6c5e9420f7103b1ccecd_step_029.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00557558_58aeccc0d60cfd0fd56386d5_step_002.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00557708_58aee364d60cfd0fd563a8ac_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0055_00557863_383d8ed7f079f44b9dfecc72_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00561073_9e2961fdc18d9e5b976c08d0_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00561725_ce82e436f164059a066d0713_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00565245_d96abb3842cf81ccbe5b1c09_step_009.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00565489_fc467dbe3e5e9c8b99cbae04_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00566187_58b33579367d56104c5e8230_step_002.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; search_det: not clean; staged: not clean |
| `0056_00566256_f7a9c09734b048d4f4637d6a_step_009.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00567586_aaf14752252a80d40c9e1283_step_005.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0056_00569762_58b4bbec8115250f775a5752_step_009.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0056_00569919_58b4bffcdbdd800f7e6d3ddc_step_004.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0057_00570021_58b4c0978115250f775aa494_step_002.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0057_00570936_f868f0f84bb5ce9c3926e742_step_020.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0057_00571912_58b5a0e4b1ea23104135d522_step_004.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0057_00578194_02db97dfc9db7d283d359af0_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0057_00578564_58b7efcfda98a4102b812175_step_008.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0057_00579594_83b3892e48b2778f2d468928_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00592329_58be052560d0ea0f7c80053f_step_006.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00593298_58bea51a3badff0fc550e4d4_step_013.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00593651_58bec7caafe56e0fcddaeff0_step_031.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00594048_58bee1790505910f8018a531_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00594882_58bf0c02835d62101bddf8c8_step_069.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00595893_24ae031415592b9fdd33eb15_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00595987_2c695f24d69fd40a9fb1fcbd_step_003.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00599448_58c07b8e910b980f78da6060_step_003.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0059_00599513_67e4a5106b69d7e77716f579_step_012.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00602103_f0dda3737ed09100b2283db1_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00602875_5de7672422a977f86b14dcca_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00608466_3a923e805e65f093905f2748_step_005.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00608593_58c3f3be25887e0fdaba0489_step_001.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean |
| `0060_00608682_58c401c28e887f101dd42c22_step_000.step` | input_only: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_ai: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; search_det: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on; staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | - |
| `0060_00608912_78e6b79236c0354d748969e2_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0060_00608988_58c430cff3b7fe0f7f17333c_step_010.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00609763_39a057597f72d79c2801cfa6_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00609783_3df4aeffe01eb64ef9a65e8f_step_019.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00610091_0eac20cb919cf7202186d939_step_000.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: excluded before the stop; the exact reason is NOT on disk (run_arm writes EXCLUDED.json on | input_only: not clean |
| `0061_00610541_78c1575a5d7590cb833fd693_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00611396_0b3be1f3a3fc526b378c1f14_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00618192_58c83c0c03a00a0f8603bd1c_step_031.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00618651_d89a52b42c828ded4e4cae62_step_028.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0061_00619937_58c915a03045b20fe8b6b4a3_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `bm2_ec_leverrod-A.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
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
- **search_det**: 0 of 7 clean outputs flagged
- **search_ai**: 0 of 8 clean outputs flagged

Clean control:

- **input_only**: 0 of 40 clean outputs flagged
- **staged**: 0 of 40 clean outputs flagged
- **search_det**: 0 of 40 clean outputs flagged
- **search_ai**: 0 of 40 clean outputs flagged

## AI marginal (search_ai − search_det, per file)

Paired over **14** files both search arms judged conclusively.
- **search_ai cleaned, search_det did not (0):** none
- **search_det cleaned, search_ai did not (0):** none
- **AI marginal = +0 files.**
- different end panel passes (1): 0059_00598276_58c0423eb4ee370fd6253b3d_step_037.step det 2 / ai 0
- the model was paid on 23 of 29 search_ai rows, $3.2882 in rows

## Repeated member timeouts (counted as file failures; lead ruling D5)

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 61 | 8 | 14 | after_timeout_crashed 1, timeout_twice 7, timeout_after_hang 6 |
| staged | 45 | 0 | 0 | - |
| search_det | 16 | 0 | 0 | - |
| search_ai | 15 | 0 | 0 | - |

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
| search_det | 112 | 99.1% | 1 | 0 | 0 |
| search_ai | 102 | 100.0% | 0 | 0 | 0 |

## Triage (triage.py, dev): leads

(triage output missing)

Full tables: `reports/triage_dev.md`, `reports/triage_clean.md`.

## Money and time

- **Ledger $4.2278** (route probes are metered in the shard ledgers on this code). `snap-v2-clean-202611240348-s1` $0.0018 (1 settles, 0 open); `snap-v2-clean-202611240348-s2` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s1` $0.1291 (3 settles, 0 open); `snap-v2-dev-202611240348-s10` $0.0650 (2 settles, 0 open); `snap-v2-dev-202611240348-s11` $0.0604 (2 settles, 0 open); `snap-v2-dev-202611240348-s12` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s13` $0.3315 (5 settles, 0 open); `snap-v2-dev-202611240348-s14` $0.1426 (3 settles, 0 open); `snap-v2-dev-202611240348-s15` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s16` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s17` $0.0692 (2 settles, 0 open); `snap-v2-dev-202611240348-s18` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s19` $0.1409 (3 settles, 0 open); `snap-v2-dev-202611240348-s2` $0.1125 (3 settles, 0 open); `snap-v2-dev-202611240348-s20` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s21` $0.5321 (8 settles, 0 open); `snap-v2-dev-202611240348-s22` $0.1238 (3 settles, 0 open); `snap-v2-dev-202611240348-s23` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s24` $0.0654 (2 settles, 0 open); `snap-v2-dev-202611240348-s25` $0.1860 (3 settles, 0 open); `snap-v2-dev-202611240348-s26` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s3` $0.6069 (7 settles, 0 open); `snap-v2-dev-202611240348-s4` $0.3344 (6 settles, 0 open); `snap-v2-dev-202611240348-s5` $0.5868 (8 settles, 0 open); `snap-v2-dev-202611240348-s6` $0.0018 (1 settles, 0 open); `snap-v2-dev-202611240348-s7` $0.3555 (6 settles, 0 open); `snap-v2-dev-202611240348-s8` $0.3659 (7 settles, 0 open); `snap-v2-dev-202611240348-s9` $0.0018 (1 settles, 0 open). Settled calls by kind: {'route_probe': 28, 'repair_v4_propose': 56}.
- **Wave wall 109.0 min** (launch to last shard). Shard minutes: clean1 8.5, clean2 9.6.
- Per-file arm wall, dev (excluded at the 1800 s wall), median / mean / p90: input_only 1800.0 / 1173.1 / 1800 s; staged 1800.0 / 1365.3 / 1800 s; search_det 1800.0 / 1547.8 / 1800 s; search_ai 1800.0 / 1571.1 / 1800 s.
- VM ~1.88 h ≈ $4.94.

## Caveats

1. PROVISIONAL split (150 dev files); small and stratified by source, not by defect.
2. The checker's run-to-run behaviour is still under Lane D's investigation; single-file differences are not effects.
3. The image's mesh build is commit `1d6ed735f7284609a269ca314c15ff6b0e359d4b` (its registry label, checked before launch); the run commit is `5c43220a4ce4d993c12af1a602ad0d5000fcda31`. The code under test is the run commit, bind-mounted; the image supplies only the toolchain.
4. Rows past the file wall are excluded (288755c2); excluded files are unmeasured, not failures.
5. Overlap depth is triage.overlap, measured on each input; files with overlap.error or no overlap row are listed above.
