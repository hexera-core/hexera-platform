# CRS v4 dev snapshot v1r (PROVISIONAL)

**PROVISIONAL.** 24 dev + 9 clean files. Written by `snap-v1r/analyze.py` from the collected run; every table is computed from the rows.

| | |
|---|---|
| Code | `a9dae3bb79dde62157a13dfc1b0ab4b8b756e40c` (resolved by launch.sh from `a9dae3bb`) |
| Image | `us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:3e8c6fae170342bbb53cf63685c2e49a77f0e6ab042e308a1f038a82e05e3592`, local id `sha256:3807995301c1518c551c7298e308203cbdbc45167833c88dc09dd47b2c0377e3`. Label `org.opencontainers.image.revision` = `1d6ed735f7284609a269ca314c15ff6b0e359d4b`: the image's MESH BUILD (toolchain) is that commit; the RUN commit above is the code bind-mounted at `/w`, and the two may differ |
| VM | `dev-crsv4-snap-v1r`, c2d-standard-16, us-central1-b, reaper max-run 6h → DELETE; created 2026-11-24T02:38:08Z, deleted 2026-11-24T05:41:17Z |
| Splits | dev `/Users/kitts/archductor/snap-runs/v1r-202611240237/splits/dev.json` (sha256 `63fe30dfc59a7965…`, 29 files); clean `/Users/kitts/archductor/snap-runs/v1r-202611240237/splits/clean.json` (sha256 `de83039bea2ae13c…`, 9 files) |
| Waves | `snap_v1r_dev` (6 shards, files [1, 1, 1, 8, 9, 9]), `snap_v1r_clean` (1 shards, files [9]); cost-balanced plans (digests `3abd202cdeb6`, `2f0124bf7b98`) |
| Flags | `--ops 24` (APPLIED ops), `--file-usd-cap 1.00`, `--route openai:gpt-6-astra`, `--h-mm auto`, `--workers 1`, file wall 1800 s (the commit's default), verdict cache `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/verdict-cache` (fresh) |
| Model caps | dev [1.0, 1.0, 1.0, 3.29, 3.62, 3.62], clean [1.4]: total **$14.93** |
| Shard exits | clean1=0, dev1=0, dev2=0, dev3=0, dev4=0, dev6=0, post=0 |

## Splits, rulings and relabels (read before the tables)

- **Splits:** `PROVISIONAL_dev_v1r.json` / `PROVISIONAL_clean_v1r.json` from `sdd/reprobe-800` @ `af3e15b9` (sha256 `184c22d0b251bf71…` / `c3d42a2683496af4…`, 30 + 8 files), each file's row replaced by its ENGINE-FORM re-probe row; membership is v1's. Then one ruled move (below), giving the run's splits: 29 dev + 9 clean (sha256 in the header table).
- **Moved, dev → clean (coordinator ruling at GO): `00611852/00611852_673cbf4282be88b3374992cb_step_008.step`.** In the engine form it is CLEAN (v1r label `clean`, panel 4/4, no hard finding; v1's original-form label was `panel/mesher`, gmsh_alt failing at min SICN 0.0042). A clean file does not belong in a dev split of failing files, so it is now a clean control, carrying its own v1r entry unchanged (nothing invented). It is reported apart below. **v1 counted it as "clean" in every arm; that was never a repair, and its absence from v1r's dev clean count is not a regression.** It was also v1's whole REFUSED cause.
- **Relabel, not engine change** (the same bytes judged in the engine's form instead of the original STEP; nothing in the repair engine changed these):
  - verdict changes: `0057_00573880_417ab441d502eafedde01127_step_005` self_intersection → panel failure (panel/mesher); `0052_00528484_58a256af264a260f6f931a1c_step_003` open_shell → valid_other;
  - member-status-only changes (label unchanged, a panel member's status changed): `0053_00533660_…_014` (crashed→fail ×2), `0051_00519727_…_014` (fail→crashed), `occt/bug32314_corrupted.stp` (fail→crashed), `0060_00606843_…_000` (fail→crashed);
  - coarse-kind-only changes (panel/mesher → panel/geometry or panel/quality, same member statuses): 11 files — `00526988`, `00601268`, `00550171`, `00614327`, `00519158`, `00615999`, `00547929` (→ geometry); `00611083`, `00614908`, `nist_ctc_01_asme1_rd`, `nist_ctc_01_asme1_ap203` (→ quality).
- **v1 vs v1r is not like-for-like:** different labels (engine form), different split membership (29/9 vs 30/8), different code (a9dae3bb: D9's AI re-prompt, B8b's adaptive cheap tier, C5's locations, A6's coded declines, A7's wall/dedupe fixes, B11's throttle), a 1800 s file wall (v1: 900 s) and a $1.00 per-file AI ceiling (v1: $0.50).
- **Image vs code:** the image's mesh build (toolchain) is commit `1d6ed735`; the run commit, bind-mounted, is `a9dae3bb`.

## STOPPED BEFORE IT FINISHED (2026-11-24T05:38:55Z): stopped EARLY on lead order: dev5's search arm is 74+ min past its 1800 s file wall on a 1.1M-entity state and the wall is not cutting it; v1r's remaining pairs are not worth 2.5 h of wall clock

The wave was stopped before every shard finished. Shards that had not finished were stopped; their rows written so far are kept. **22 (file, arm) pairs were never measured because of the stop** (in progress or not started), reported as `unmeasured: stopped at the wave deadline`. Separately, **0 (file, arm) pairs were excluded BEFORE the stop**; their exact reasons are not on disk (run_arm writes EXCLUDED.json only at shard end), so they are a group of their own. A stopped shard has no PROVENANCE.json and none was written, so the official report refuses those shards; the tables here are computed from the rows.

**The ledger $5.0623 is therefore a FLOOR on what the full wave would cost.** It is 33.9% of the $14.93 wave cap: money did not bound this wave, wall clock did.

| shard | rows kept | excluded before the stop | unmeasured (deadline) | had provenance |
|---|---|---|---|---|
| snap-v1r-dev-202611240237/shard-5-of-6 | 14 | 0 | 22 | False |

## Official reports

| report | result |
|---|---|
| dev | exit **1**  REFUSED shard-5-of-6: no PROVENANCE.json (shard unfinished) |
| clean | exit **0** snap_v1r_clean (clean, normal) - OK  |


## Per-arm, dev: body-overlap files counted as failures (headline)

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 24 | 22 | 15 | 14 |
| clean (of conclusive) | 0 | 0 | 2 | 2 |
| repaired (unclean in, clean out) | 0 | 0 | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 1 | 3 | 3 |
| inconclusive rows | 0 | 0 | 5 | 6 |
| excluded (reason) | deadline stop 5 | file_timeout 2, deadline stop 5 | file_timeout 3, deadline stop 6 | file_timeout 3, deadline stop 6 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 4.8897 |

## Per-arm, dev: deep body-overlap files left out of the denominator (owner ruling)

Files whose INPUT carries `body_overlap` (9). The depth and bound come from a body op's `exceeds_fidelity` decline in the row triage, so only a file the search tried to cut has a measured depth:

- `00613121_58c67ddf032617103d59c101_step_003.step`: depth 37.2 mm vs bound 0.1 mm → DEEP (left out)
- `0051_00519727_589dc8aa5abe5e0fe4ada930_step_014.step`: depth 3 mm vs bound 0.1 mm → DEEP (left out)
- `0052_00521884_f7d1e4b9658237600ec82613_step_012.step`: depth 0.6 mm vs bound 0.0843 mm → DEEP (left out)
- `0052_00528484_58a256af264a260f6f931a1c_step_003.step`: depth never measured: no body op ran on it (kept)
- `0053_00533660_58a4821bab78c2106dbc0ab6_step_014.step`: depth never measured: no body op ran on it (kept)
- `0053_00535066_bb0bc8e7914fdea894ad2ec6_step_009.step`: depth 6.64 mm vs bound 0.1 mm → DEEP (left out)
- `0054_00541957_51229e00ebc25e88ce807b9e_step_000.step`: depth 2 mm vs bound 0.1 mm → DEEP (left out)
- `0057_00574245_58b65964d2d73c0f795da35a_step_017.step`: depth never measured: no body op ran on it (kept)
- `trj3_s1-db-214.stp`: depth 0.0992 mm vs bound 0.0585 mm → DEEP (left out)

Left out as deep: **6**. Depth unknown and kept in: **3** (0052_00528484_58a256af264a260f6f931a1c_step_003.step, 0053_00533660_58a4821bab78c2106dbc0ab6_step_014.step, 0057_00574245_58b65964d2d73c0f795da35a_step_017.step).

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 18 | 16 | 10 | 8 |
| clean (of conclusive) | 0 | 0 | 2 | 2 |
| repaired (unclean in, clean out) | 0 | 0 | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 1 | 3 | 3 |
| inconclusive rows | 0 | 0 | 4 | 6 |
| excluded (reason) | deadline stop 5 | file_timeout 2, deadline stop 5 | file_timeout 3, deadline stop 6 | file_timeout 3, deadline stop 6 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 3.5174 |

Widest version, for reference: every body-overlap input left out (9 files):

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 15 | 13 | 10 | 8 |
| clean (of conclusive) | 0 | 0 | 2 | 2 |
| repaired (unclean in, clean out) | 0 | 0 | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) | 2 (00619793_973db45bf9076901a50908bc_step_000.step, 00614908_a8619514c95972d6221f4629_step_000.step) |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 1 | 3 | 3 |
| inconclusive rows | 0 | 0 | 2 | 4 |
| excluded (reason) | deadline stop 5 | file_timeout 2, deadline stop 5 | file_timeout 2, deadline stop 6 | file_timeout 2, deadline stop 6 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 3.0672 |

## Clean control

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 9 | 9 | 9 | 9 |
| clean (of conclusive) | 9 | 9 | 9 | 9 |
| repaired (unclean in, clean out) | 0 | 0 | 0 | 0 |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 0 | 0 | 0 |
| more panel passes than input | 0 | 0 | 0 | 0 |
| inconclusive rows | 0 | 0 | 0 | 0 |
| excluded (reason) | 0 | 0 | 0 | 0 |
| model $ (rows) | 0.0000 | 0.0000 | 0.0000 | 0.0000 |


### Moved file `00611852_673cbf4282be88b3374992cb_step_008.step` (reported apart: not a repair)

| arm | in clean | out clean | panel in→out | inconclusive | model $ |
|---|---|---|---|---|---|
| input_only | True | True | 4→4 | False | 0.0000 |
| staged | True | True | 4→4 | False | 0.0000 |
| search_det | True | True | 4→4 | False | 0.0000 |
| search_ai | True | True | 4→4 | False | 0.0000 |


## Excluded files, each its own group (not in any rate; the ruling on the exclusion rate is pending)

Dev:

**9 file(s), 30 excluded (file, arm) rows** (file_timeout 8, deadline stop 22). File wall 1800 s.

| file | excluded arms (reason) | measured arms |
|---|---|---|
| `nist_ctc_01_asme1_rd.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `nist_ftc_07_asme1_ap242-e2.stp` | search_ai: file_timeout: file wall clock 1800s exceeded; search_det: file_timeout: file wall clock 1800s exceeded; staged: file_timeout: file wall clock 1800s exceeded | input_only: not clean |
| `0052_00528484_58a256af264a260f6f931a1c_step_003.step` | search_ai: file_timeout: file wall clock 1800s exceeded; search_det: file_timeout: file wall clock 1800s exceeded | input_only: not clean; staged: not clean |
| `0057_00570551_59995fb3cd27b48fe0957ae2_step_007.step` | search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | input_only: not clean; staged: not clean |
| `0060_00601268_01579e613800b230c1716e80_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00604433_58c21d4ea0c27b0f87b161a9_step_000.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `0060_00608014_3fedd89a1d6b0c6f1c34e1b7_step_001.step` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |
| `bug32314_corrupted.stp` | search_ai: file_timeout: file wall clock 1800s exceeded; search_det: file_timeout: file wall clock 1800s exceeded; staged: file_timeout: file wall clock 1800s exceeded | input_only: not clean |
| `trj4_s1-tu-214.stp` | input_only: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_ai: unmeasured: stopped at the wave deadline (this arm was in progress or never started); search_det: unmeasured: stopped at the wave deadline (this arm was in progress or never started); staged: unmeasured: stopped at the wave deadline (this arm was in progress or never started) | - |

Clean control:

None.

## AI marginal (search_ai − search_det, per file)

Paired over **13** files both search arms judged conclusively.
- **search_ai cleaned, search_det did not (0):** none
- **search_det cleaned, search_ai did not (0):** none
- **AI marginal = +0 files.**
- different end panel passes (0): none
- the model was paid on 20 of 20 search_ai rows, $4.8897 in rows

## Repeated member timeouts (counted as file failures; lead ruling D5)

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 24 | 2 | 2 | timeout_twice 1, after_timeout_crashed 1 |
| staged | 22 | 0 | 0 | - |
| search_det | 15 | 0 | 0 | - |
| search_ai | 14 | 0 | 0 | - |

Clean control:

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 9 | 0 | 0 | - |
| staged | 9 | 0 | 0 | - |
| search_det | 9 | 0 | 0 | - |
| search_ai | 9 | 0 | 0 | - |

## Cheap/full agreement (search ranking tier vs full panel, confirmed states)

| arm | states compared | agree | cheap pass, full fail | cheap fail, full pass | rows without data |
|---|---|---|---|---|---|
| search_det | 45 | 100.0% | 0 | 0 | 0 |
| search_ai | 67 | 100.0% | 0 | 0 | 0 |

## Triage (triage.py, dev): leads

(triage output missing)

Full tables: `reports/triage_dev.md`, `reports/triage_clean.md`.

## Money and time

- **Ledger $5.0623** (route probes are metered in the shard ledgers on this code). `snap-v1r-clean-202611240237-s1` $0.0018 (1 settles, 0 open); `snap-v1r-dev-202611240237-s1` $0.0018 (1 settles, 0 open); `snap-v1r-dev-202611240237-s2` $0.1620 (2 settles, 0 open); `snap-v1r-dev-202611240237-s3` $0.0018 (1 settles, 0 open); `snap-v1r-dev-202611240237-s4` $1.7989 (25 settles, 0 open); `snap-v1r-dev-202611240237-s5` $0.6130 (13 settles, 0 open); `snap-v1r-dev-202611240237-s6` $2.4831 (30 settles, 0 open). Settled calls by kind: {'route_probe': 7, 'repair_v4_propose': 66}.
- **Wave wall 179.2 min** (launch to last shard). Shard minutes: clean1 6.6, dev1 105.0, dev2 105.6, dev3 100.0, dev4 133.5, dev6 172.8.
- Per-file arm wall, dev (excluded at the 1800 s wall), median / mean / p90: input_only 64.3 / 406.9 / 1800 s; staged 128.6 / 678.4 / 1800 s; search_det 661.5 / 854.6 / 1800 s; search_ai 793.5 / 887.3 / 1800 s.
- VM ~3.05 h ≈ $2.29.

## Caveats

1. PROVISIONAL split (29 dev files); small and stratified by source, not by defect.
2. The checker's run-to-run behaviour is still under Lane D's investigation; single-file differences are not effects.
3. The image's mesh build is commit `1d6ed735f7284609a269ca314c15ff6b0e359d4b` (its registry label, checked before launch); the run commit is `a9dae3bb79dde62157a13dfc1b0ab4b8b756e40c`. The code under test is the run commit, bind-mounted; the image supplies only the toolchain.
4. Rows past the file wall are excluded (288755c2); excluded files are unmeasured, not failures.
5. Overlap depth exists only where a body op ran; see the list above for files whose depth was never measured.
