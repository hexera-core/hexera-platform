# Body-pair census: touch or overlap

Date: 2026-11-15. Command:

    python devtools/validation/body_pairs.py --manifest devtools/validation/corpora/mesh-failure-real.json \
      --roots ~/archductor/mesh-fail-corpus/occt ~/archductor/mesh-fail-corpus/abc --json run.json

Targets: manifest files with `solids > 1` and a `PLC` gmsh error (23). A pair whose bounding boxes meet is measured with `BRepAlgoAPI_Common`; volume > 1e-6 mm3 is overlapping, else touching. Pair cap 40, deadline 120 s per file. Raw data: `run.json`.

## Headline

**11 of 23 files have at least one overlapping pair; 12 are touch-only** (one of those, abc/00509150, has no box-meeting pair at all: its 4 bodies are disjoint). 3 files hit the 40-pair cap and are incomplete (their counts are lower bounds); all 3 already show overlap, so the headline split does not depend on them, but their touching counts are not final.

Overlap is therefore common: a `fragment_bodies` op that assumes touching bodies would be wrong on roughly half the PLC-failure multi-solid files and must decline on overlap.

## Per file

| file | solids | overlapping | touching | pairs checked | complete |
|---|---|---|---|---|---|
| occt/bm2_ec_exhaust-A.stp | 6 (read 6) | 2 | 1 | 3 | yes |
| occt/bug33261.stp | 24 (read 41) | 5 | 35 | 40 | NO (cap) |
| occt/trj3_s1-ac-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj3_s1-db-214.stp | 11 (read 11) | 1 | 15 | 16 | yes |
| occt/trj3_s1-md-214.stp | 11 (read 11) | 7 | 9 | 16 | yes |
| occt/trj3_s1-pe-203.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj3_s1-tc-214.stp | 11 (read 11) | 3 | 15 | 18 | yes |
| occt/trj3_s1-ug-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj4_s1-pe-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj4_s1-ug-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj4_s1-tu-214.stp | 11 (read 11) | 3 | 15 | 18 | yes |
| occt/trj5_s1-ac-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj5_s1-in-214.stp | 11 (read 11) | 1 | 15 | 16 | yes |
| occt/trj5_s1-ug-214.stp | 11 (read 11) | 0 | 18 | 18 | yes |
| occt/trj6_pm4-ec-203.stp | 32 (read 32) | 3 | 37 | 40 | NO (cap) |
| abc/00500875_a97e4a09c926c436c043e3c3_step_028.step | 5 (read 5) | 0 | 4 | 4 | yes |
| abc/00502475_5895e91efca3da0f6b68639a_step_003.step | 5 (read 5) | 1 | 2 | 3 | yes |
| abc/00502200_5895d152ed2c0c0fe9ef029c_step_093.step | 178 (read 178) | 3 | 7 | 10 | yes |
| abc/00503325_58965842fca3da0f6b68cb76_step_006.step | 35 (read 35) | 15 | 25 | 40 | NO (cap) |
| abc/00504025_5896ef7054620e0fdd79f59d_step_085.step | 2 (read 2) | 0 | 1 | 1 | yes |
| abc/00506275_541a2ce632275fed8964367a_step_000.step | 2 (read 2) | 0 | 1 | 1 | yes |
| abc/00503600_58969abbfca3da0f6b696760_step_000.step | 2 (read 2) | 0 | 1 | 1 | yes |
| abc/00509150_cb7c199fecd1c9d080129028_step_009.step | 4 (read 4) | 0 | 0 | 0 | yes |

Note: occt/bug33261.stp reads 41 solids against the manifest's 24 (the manifest count comes from a different reader pass); the census uses the solids OCP reads.
