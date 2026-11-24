# CRS v4 dev snapshot v2 (PROVISIONAL)

**PROVISIONAL.** 0 dev + 0 clean files. Written by `snap-v1r/analyze.py` from the collected run; every table is computed from the rows.

| | |
|---|---|
| Code | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` (resolved by launch.sh from `5c43220a`) |
| Image | `us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:3e8c6fae170342bbb53cf63685c2e49a77f0e6ab042e308a1f038a82e05e3592`, local id `?`. Label `org.opencontainers.image.revision` = `1d6ed735f7284609a269ca314c15ff6b0e359d4b`: the image's MESH BUILD (toolchain) is that commit; the RUN commit above is the code bind-mounted at `/w`, and the two may differ |
| VM | `dev-crsv4-snap-v2`, c2d-standard-56, us-central1-b, reaper max-run 5h → DELETE; created 2026-11-24T03:42:29Z, deleted 2026-11-24T03:46:57Z |
| Splits | dev `/Users/kitts/archductor/snap-runs/v2-202611240342/splits/dev.json` (sha256 `94e83d177fcd8522…`, 150 files); clean `/Users/kitts/archductor/snap-runs/v2-202611240342/splits/clean.json` (sha256 `afbb72fe3b6291a6…`, 40 files) |
| Waves | `snap_v2_dev` (26 shards, files [5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6]), `snap_v2_clean` (2 shards, files [20, 20]); cost-balanced plans (digests `12011f540d33`, `46a95b256eb5`) |
| Flags | `--ops 24` (APPLIED ops), `--file-usd-cap 1.00`, `--route openai:gpt-6-astra`, `--h-mm auto`, `--workers 1`, file wall 1800 s (the commit's default), verdict cache `gs://dev-crsv3-transfer-224734058693/eval/snap-v2-202611240342/verdict-cache` (fresh) |
| Model caps | dev [1.66, 1.66, 1.66, 1.66, 1.66, 1.66, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83, 1.83], clean [1.66, 1.66]: total **$49.88** |
| Shard exits |  |

## READ FIRST: this is the FIRST snapshot under the NEW goal (owner decision 2026-11-24)

- **Clean = layer 1 VALID + layer 2 full PANEL.** Faithfulness (layer 3) is still computed and reported, in the rows "clean but NOT faithful" below, but it NEVER gates clean. The geometry/fidelity caps are deleted (B12). Clean outputs that moved the part (|volume ratio − 1| > 0.25 or bbox ratio < 0.90) are FLAGGED as gaming, named per arm, and still counted clean.
- **Not comparable with v1 or v1r.** Those used the OLD clean (valid + panel + faithful). On this commit the deterministic arm also proposes new op rows: defeature (D14, ported from Raymond), fuse, `cut_by` at any depth, and `trim_unbounded` (C7). search_det and staged numbers measure a different generator, not v1's.
- **Splits:** `devtools/validation/corpora/v4/provisional_v2/PROVISIONAL_dev_v2.json` (150 failing) and `PROVISIONAL_clean_v2.json` (40 clean), at the run commit. These are PROVISIONAL, drawn from the partial reprobe-800 re-probe; the real splits come from the full re-probe.
- **Cache:** VERDICT_CODE_VERSION .4, a fresh prefix. Every verdict was meshed cold.
- **Image vs code:** the image's mesh build (toolchain) is `1d6ed735`; the run commit, bind-mounted, is `5c43220a`.

## Official reports

| report | result |
|---|---|
| dev | exit **missing**   |
| clean | exit **missing**   |


## Per-arm, dev: body-overlap files counted as failures (headline)

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 0 | 0 | 0 | 0 |
| clean (of conclusive) | 0 | 0 | 0 | 0 |
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

## Per-arm, dev: deep body-overlap files left out of the denominator (owner ruling)

Files whose INPUT has a body overlap (0), from `triage.overlap` (measured on the input: max_depth_mm = the worst pair's cheaper cut, its method, bound_mm, within_bound). overlap.error: 0; layer-1 body_overlap with no triage.overlap: 0.

- none

Left out as deep (within_bound false): **0**. Not decidable and kept in: **0**.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 0 | 0 | 0 | 0 |
| clean (of conclusive) | 0 | 0 | 0 | 0 |
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

Widest version, for reference: every body-overlap input left out (0 files):

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 0 | 0 | 0 | 0 |
| clean (of conclusive) | 0 | 0 | 0 | 0 |
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

## Clean control

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 0 | 0 | 0 | 0 |
| clean (of conclusive) | 0 | 0 | 0 | 0 |
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



## Excluded files, each its own group (not in any rate; the ruling on the exclusion rate is pending)

Dev:

None.

Clean control:

None.

## Gaming flags (clean outputs that moved the part: |volume ratio − 1| > 0.25 or bbox ratio < 0.90; STILL counted clean)

Dev:

- **input_only**: 0 of 0 clean outputs flagged
- **staged**: 0 of 0 clean outputs flagged
- **search_det**: 0 of 0 clean outputs flagged
- **search_ai**: 0 of 0 clean outputs flagged

Clean control:

- **input_only**: 0 of 0 clean outputs flagged
- **staged**: 0 of 0 clean outputs flagged
- **search_det**: 0 of 0 clean outputs flagged
- **search_ai**: 0 of 0 clean outputs flagged

## AI marginal (search_ai − search_det, per file)

Paired over **0** files both search arms judged conclusively.
- **search_ai cleaned, search_det did not (0):** none
- **search_det cleaned, search_ai did not (0):** none
- **AI marginal = +0 files.**
- different end panel passes (0): none
- the model was paid on 0 of 0 search_ai rows, $0.0000 in rows

## Repeated member timeouts (counted as file failures; lead ruling D5)

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 0 | 0 | 0 | - |
| staged | 0 | 0 | 0 | - |
| search_det | 0 | 0 | 0 | - |
| search_ai | 0 | 0 | 0 | - |

Clean control:

| arm | conclusive | files with a repeated member timeout | members | causes |
|---|---|---|---|---|
| input_only | 0 | 0 | 0 | - |
| staged | 0 | 0 | 0 | - |
| search_det | 0 | 0 | 0 | - |
| search_ai | 0 | 0 | 0 | - |

## Cheap/full agreement (search ranking tier vs full panel, confirmed states)

| arm | states compared | agree | cheap pass, full fail | cheap fail, full pass | rows without data |
|---|---|---|---|---|---|
| search_det | 0 | - | 0 | 0 | 0 |
| search_ai | 0 | - | 0 | 0 | 0 |

## Triage (triage.py, dev): leads

(triage output missing)

Full tables: `reports/triage_dev.md`, `reports/triage_clean.md`.

## Money and time

- **Ledger $0.0000** (route probes are metered in the shard ledgers on this code). . Settled calls by kind: {}.
- **Wave wall ? min** (launch to last shard). Shard minutes: .
- Per-file arm wall, dev (excluded at the 1800 s wall), median / mean / p90: .
- VM ~0.07 h ≈ $0.18.

## Caveats

1. PROVISIONAL split (150 dev files); small and stratified by source, not by defect.
2. The checker's run-to-run behaviour is still under Lane D's investigation; single-file differences are not effects.
3. The image's mesh build is commit `1d6ed735f7284609a269ca314c15ff6b0e359d4b` (its registry label, checked before launch); the run commit is `5c43220a4ce4d993c12af1a602ad0d5000fcda31`. The code under test is the run commit, bind-mounted; the image supplies only the toolchain.
4. Rows past the file wall are excluded (288755c2); excluded files are unmeasured, not failures.
5. Overlap depth is triage.overlap, measured on each input; files with overlap.error or no overlap row are listed above.
