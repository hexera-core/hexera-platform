# t29_deterministic @ 0ff8db35, 120 s: SUPPORTING DATA, not the arm of record

The rules-only arm (investigator and tournament off, reviewer live), run on the 120-file failing sample and the 100-part clean control. Code was 0ff8db350582. The image was `mesh/app@sha256:00ce0156…`, built from 5d496416. The machines were GCE c2d/c2-standard-8, one shard per vCPU. Launches: 11200547/11200548, of which 4 shards survived; 11200639, the other 24; and 11200655, the shard 15 rerun.

The arm of record runs at lane A's fixed commit with `CAD_REPAIR_TIMEOUT_S=420` for every arm (LEAD ruling, 22:55 PT, 2026-11-19).

| | failing | clean |
|---|---|---|
| files with a row | 119 of 120 (1 crash) | 100 of 100 |
| meshed before → after | 2 → 2 | 100 → 100 |
| newly meshing | **0** | 0 |
| newly broken | **0** | **0** |
| clean parts with no kept operation (byte-unchanged) | n/a | **100 / 100** |
| endings | unmeshable 74, attempted_none_held 34, scan_incomplete 7, nothing_to_repair 4 | nothing_to_repair 92, attempted_none_held 7, not_attempted 1 |
| model calls / $ | 0 / $0.00 | 0 / $0.00 |
| VM $ (shard-share of hourly) | $0.12 | $0.01 |

Caveats:
- `CAD_REPAIR_TIMEOUT_S` was unset, so runs got the 120 s default; shared dev ships 420.
- **Crash.** Failing shard 15/21 segfaults (exit 139) on both attempts, on `abc/0059_00597937_2e928f752b8806de5a85c14b_step_000.step` (sha256 `fc4e4a440ffd…`). It crashes right after OCC `FixShape … Geom_RectangularTrimmedSurface::U parameters out of range`. The process dies, so that file has no row; the shard's other 5 streamed rows are included. That shard's VM cost is not counted because it has no PROVENANCE. Per-file crash isolation has been routed to lane C as a blocker for the AI wave.
- `collect.py` was not used to seal this, because it needs every arm at one commit. `summary.json` and the bundles come from collect.py's own `arm_report`, `clean_untouched` and `seal`, called by a lane-B scratch script. Both bundles pass `repair_corpus.py --verify` (100 and 119 rows).
- Raw shard outputs: `gs://dev-crsv3-transfer-224734058693/eval/t29_deterministic-0ff8db350582/`.
