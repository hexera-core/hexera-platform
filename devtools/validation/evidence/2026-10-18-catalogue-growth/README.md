# Catalogue growth: corpus floor gate (2026-10-18)

Corpus: OCCT dataset 7.8.0 step directory (332 files, 331 rows). Settings: `CAD_REPAIR_ENABLED=true`,
`CAD_REPAIR_FILL_PLANAR_HOLES=true`, `CAD_REPAIR_MAX_DEVIATION_RATIO=0.001`, `CAD_REPAIR_MAX_TOLERANCE_MM=0.1`,
`--loop`, planner off, review off. Baseline has the new flags off; growth adds
`CAD_REPAIR_FILL_NONPLANAR_HOLES=true CAD_REPAIR_HEAL_SELF_INTERSECTIONS=true`.

Files: `catalogue-baseline.log` (cbc7862), `catalogue-growth-r2.log` (after 679ad96 + 254e918), `catalogue-gate-r2.txt`
(gate output, verbatim). Both logs are committed in full (under 200 KB). The row JSON is not committed:
`/tmp/planner-evidence/catalogue-baseline.json`, `/tmp/planner-evidence/catalogue-growth-r2.json`
(first run: `catalogue-growth.json` / `.log`). No corpus STEP files are committed.

## Before / after

| | baseline (flags off) | growth r2 (flags on) |
|---|---|---|
| repaired defects | 430 | 430 |
| by kind | open_shell 414, small_edge 16 | open_shell 414, small_edge 16 |
| outcomes | nothing_to_repair 143, refused 115, not_attempted 57 (manual_cleanup 43, ask_customer 14), repaired 16 | identical |
| loop stopped | stalled 16 | stalled 15, resolved 1 |
| sew_faces kept/rolled_back/declined | 14 / 81 / 4 | 14 / 81 / 4 |
| fix_small_edges | 2 / 62 / 0 | 2 / 57 / 0 |
| fix_shape_global | 0 / 62 / 0 | 0 / 57 / 0 |
| fill_planar_hole | 0 / 30 / 23 | 0 / 30 / 23 |
| fill_nonplanar_hole | never exercised | never exercised |
| heal_self_intersections | never exercised | proposed, declined 10, kept 0 |
| paired_rim refusals | 369 | 369 |
| heal corroboration | corroborated 0 / kept 0, unknown 0 | corroborated 0 / kept 0, unknown 0 |
| cap violations | 0 | 0 |

Aggregate is exactly 430, so the floor held; no "strictly more" claim is made. Every baseline-kept file is kept again,
no paired_rim file kept a fill, and all 16 promoted files passed the final check.

Heal declines (10): multi_body 4; not_isolated, 22 faces 2; not_isolated, 6 faces 2; not_isolated, analysis shows no crossing
among the targets 2. The fill_nonplanar_hole op was never proposed: the rules planner stalls (sew, planar decline) first.

## The first run failed (318 < 430)

The first flags-on run (`catalogue-growth.json`) repaired 318 defects, 112 short. The face scan ran after every operation and
its findings demoted runs: 19 files newly carrying a face-level self_intersection were routed to manual_cleanup, and on
trj4_k1_geo-pe-203 and -214 (56 defects each) the sew was rolled back as new_defect because the sewn result showed a
crossing the baseline never measured. The gate stopped there and nothing was committed. Commits 679ad96 and 254e918 made
the face scan run only for a session's first scan and for the re-inspection that judges a heal; every other operation is
kept or rolled back on the status walk alone, and a crossing only the analyzer measured is carried in the report as
analyzer_only without moving inspection status, triage route or loop ending. The r2 run then matched the baseline outcome
for outcome.
