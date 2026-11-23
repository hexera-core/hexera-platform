# CRS v3 verdict, WAVE 3: code 17d5f55e, CAD_REPAIR_TIMEOUT_S=420, aggressive clone

- **Launch:** 11201006, 12× c2d-standard-8 in us-central1-b, 96 shards, 96/96 DONE with no relaunches.
- **Image:** `mesh/app@sha256:00ce0156…`, built from 5d496416.
- **Code:** everything in wave 2, plus the owner's aggressive-clone policy (`CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS=live`, clone gates freed, mesh-located regions walked first, +4 tier turns), lane E's review-safety, cure and tournament fixes, and the tolerant production-rung diagnostic (`CAD_REPAIR_TOLERANT_PROBE=live`).
- **Sealed by:** `collect.py --commit 17d5f55e… --pull --t6 --out <dir>`, from a checkout at edcff68a. All five bundles pass `repair_corpus.py --verify`.
- **Transcripts** are not committed. They are at `gs://dev-crsv3-transfer-224734058693/eval/supporting/wave3-17d5f55e/collect/`.

| arm | files fixed | newly broken | crashed (isolated) | clean: still meshes, no new defects | clean changed (info) | cost, failing |
|---|---|---|---|---|---|---|
| t29_deterministic | 1 (0060_00608290) | 0 | 1 (0059_00597937) | 100/100 | 4 | $0.34 |
| v3_full | 1 (0060_00608290) | 0 | 1 | 100/100 | 1 | $1.31 |
| t19_tournament_off | 0 | 0 | 1 | — | — | $1.10 |

- **Regions concluded / asked:** v3 failing 548/602 (91%), t19_off 564/614 (92%), v3 clean 96%. Both failing arms are down from about 96% in wave 2.
  - The cause is regions running out of tool calls: 17 in v3 and 22 in t19_off, against 1 and 3 in wave 2. Edits tried roughly doubled, and the +4 tier turns do not raise the tool-call cap.
- **Runaways:** v3 has 11 (1.7%), 3 of them "same call asked 3 times".
- **Conclusions:** the split moves toward mesher_side (v3 142, t19_off 181).
- **Model calls:** 0 provider errors in 5,224 calls.
- **Appendix diagnostic only:** the tolerant production rung delivered 65 (v3) and 68 (t19_off) failing files. This is not a product fallback and not counted.
