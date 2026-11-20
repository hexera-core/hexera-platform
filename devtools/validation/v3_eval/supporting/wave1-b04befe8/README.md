# CRS v3 verdict, WAVE 1 (PRELIMINARY): code b04befe8, CAD_REPAIR_TIMEOUT_S=420

**Not the arms of record.** LEAD ruling (2026-11-20): the 97a9e216 unanswered-tool-call defect bit in a few investigator regions, so this wave is preliminary. The arms of record come from wave 2, at lane E's sha, which includes 97a9e216 and lane E's delivered-STEP keep rule.

- **Launch:** 11200820, 12× c2d-standard-8 in us-central1-b, 96 shards, 96/96 DONE with no relaunches.
- **Image:** `mesh/app@sha256:00ce0156…`, built from 5d496416.
- **Sealed by:** `collect.py --commit b04befe8… --pull --t6 --out <this dir>`, from a checkout containing 29f6b8c0, df7e1d7a and the other host-side fixes. All five bundles pass `repair_corpus.py --verify`.
- **Transcripts** (17 MB) are not committed. They are at `gs://dev-crsv3-transfer-224734058693/eval/supporting/wave1-b04befe8/`.

## Headline (results.json)

| arm | newly meshed | newly broken | crashed | model calls | $ total | $/fixed |
|---|---|---|---|---|---|---|
| t29_deterministic | 1 | 0 | 1 | 18 | 0.30 | 0.30 |
| v3_full (T29 v3 and T19 on) | 1 | 0 | 1 | 1978 | 1.05 | 1.05 |
| t19_tournament_off | 0 | 0 | 1 | 1907 | 0.84 | — |
| t6 rules-only (90 files, local) | 0 | 0 | — | n/a | n/a | — |

- **Newly meshed:** the same file in both arms, `0060_00608290_03bd4ca010ce5f1747b235cb_step_000.step`, via `fix_small_faces`.
- **Crashed:** the native crash, now isolated, on `0059_00597937…` (see the RESUME follow-ups).
- **T29 verdict:** not accepted. v3 at 1 is below 4× the deterministic arm's 1.
- **Clean control:** 97/100 byte-unchanged (the minimum over the arms), 0 broken.
  - t29_deterministic kept operations on 3 parts: `00591591…` (sew_faces), `00514684…` (fix_small_faces), `00552206…` (fix_small_faces).
  - v3_full kept an operation on 1 part: `00552206…`.
  - All of them still mesh.

## Regions (investigator arms)

| unit | regions | concluded / asked | runaway | budget_spent | skipped | escalate / mesher_side / package | edits tried / kept |
|---|---|---|---|---|---|---|---|
| v3_full failing | 655 | 623 / 649 (96%) | 3 | 23 | 6 | 538 / 77 / 8 | 265 / 17 |
| t19_off failing | 799 | 584 / 612 (95%) | 7 | 21 | 187 | 492 / 82 / 10 | 333 / 26 |
| v3_full clean | 187 | 160 / 167 (96%) | 3 | 4 | 20 | 149 / 8 / 3 | 144 / 4 |

- **Runaways** are three refused *conclusions* in a row, not tool calls. Mostly it is "conclude package with no kept edit", and twice "verdict names fingerprints that are not defects".
- **Why edits were rolled back:** most often `target_remains`, `declined:not_isolated`, the tolerance cap, and "turned a valid shape into an invalid one".

## Caveats

- **97a9e216 defect.** The model-call telemetry has 14 `invalid_request` calls (9 v3_full, 5 t19_off) out of 4,667. No region ended in a model-error ending, and none of the runaways came from empty replies. The defect's signature is an empty reply that then ends the region `budget_spent` or `runaway`. Regions with that signature are listed in `possibly-affected-97a9e216.json`. It is an upper bound, because the model sometimes really does send an empty reply:
  - v3_full failing: 15 regions in 14 files;
  - v3_full clean: 4 regions in 3 files;
  - t19_off failing: 6 regions in 6 files.
- **Telemetry labels every model call `role=intake`,** so reviewer and investigator spend cannot be split. This is in the RESUME product follow-ups.
- **One part, `0055_00556585…` (23.7 MB), takes 21–36 min in every arm**, and took 2,103 s even under a 120 s `CAD_REPAIR_TIMEOUT_S`. The repair timeout does not bound its wall time.
