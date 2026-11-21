# CRS v3 verdict, WAVE 2: code df7e1d7a, CAD_REPAIR_TIMEOUT_S=420

- **Launch:** 11200927, 12× c2-standard-8 in us-central1-a, 96 shards, 96/96 DONE with no relaunches.
- **Image:** `mesh/app@sha256:00ce0156…`, built from 5d496416.
- **Code:** includes 97a9e216 (every tool call answered), the delivered-STEP keep rule and its no-op guard, REVIEW_OBJECTION=live, role/purpose labels on model calls, and stored review verdicts.
- **Sealed by:** `collect.py --commit df7e1d7a… --pull --t6 --out <dir>`, from a checkout at edcff68a. All five bundles pass `repair_corpus.py --verify`.
- **Transcripts** are not committed. They are at `gs://dev-crsv3-transfer-224734058693/eval/supporting/wave2-df7e1d7a/collect/`.

Reported to the owner's rules (2026-11-20). The headline is CAD-repaired files that newly mesh, plus newly broken; tolerant/fTetWild is never counted; the clean rule is "still meshes AND no new defects".

| arm | files fixed | newly broken | crashed (isolated) | clean: still meshes, no new defects | clean changed (info) | cost, failing |
|---|---|---|---|---|---|---|
| t29_deterministic | 1 (0060_00608290) | 0 | 1 (0059_00597937) | 100/100 | 3 | $0.38 |
| v3_full | **0** | 0 | 1 | 100/100 | 1 | $1.18 |
| t19_tournament_off | 0 | 0 | 1 | — | — | $0.91 |

- **Regions concluded / asked:** v3 failing 603/626 (96%), v3 clean 97%, t19_off 96%.
- **Model calls:** 0 provider errors in 4,510 calls.
- **Why v3 missed 0060_00608290 this wave:**
  - Region 0's package was reverted by the area check. That is a rounding artefact: a 6-place display figure compared with strict `>`. It is fixed post-merge in 354c7d56.
  - Region 1 was rejected by the reviewer (objection: faces 89→87). In wave 3 the reviewer accepted the same region, which is reviewer non-determinism and has been routed to lane E.
- **Appendix diagnostic only:** the tier-2b tolerant probe estimates 61-62 failing files per arm.
- **`0055_00556585`** (23.7 MB) took 2,300 s or more in every arm with 420 s set. The timeout does not bound it; this is a product follow-up.
