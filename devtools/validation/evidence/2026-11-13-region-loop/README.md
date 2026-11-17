# Region-scoped repair driver: baseline comparison (2026-11-13)

Evidence for merging the region-scoped CAD repair driver. The driver is selected by
`CAD_REPAIR_REGION_LOOP`, which **defaults to `off`**: nothing here is live for a customer until a
deployment sets it. `cad/repair/loop.py`, the operation-at-a-time baseline, is retained unchanged
so a difference between the two paths stays attributable to the driver rather than to an accident
(`docs/research/public-benchmarks.md`).

Design: `docs/superpowers/specs/2026-11-11-scoped-llm-cad-repair-design.md`.
Plan: `docs/superpowers/plans/2026-11-11-region-loop-foundation.md`.

Engine commit `bcae4f66`, hardware class `darwin-arm64`. Both arms ran the same bytes with the same
settings; only `CAD_REPAIR_REGION_LOOP` differs.

```
CAD_REPAIR_FILL_PLANAR_HOLES=true  CAD_REPAIR_FILL_NONPLANAR_HOLES=true
CAD_REPAIR_FIX_SMALL_FACES=true    CAD_REPAIR_MAX_OPS=200
CAD_REPAIR_TIMEOUT_S=300           CAD_REPAIR_STALL_LIMIT=50
CAD_REPAIR_REGION_ATTEMPTS=6       CAD_REPAIR_REGION_LOOP=off|live
```

## Corpora

| corpus | files | provenance | redistribution |
|---|---|---|---|
| NIST MBE PMI CTC/FTC | 33 | `nist.gov/system/files/documents/noindex/2024/06/19/NIST-PMI-STEP-Files.zip`, zip sha256 `8fa78429e6d8d9b0d7681d223b6aa9ec98c3772185c55b1a0e3679b21c181911` | redistributable |
| generated defect campaign | 50 | `hexera-core/hexera-cad-defect-generation`, campaign `c_20261016T102018Z_cdae3abe40` | internal |
| clean references (control) | 40 of 316 | same repo, `cad-samples/`, sampled across all 22 families | internal |

Per-input sha256 for all 33 NIST files is in the manifests. No CAD files are committed here.

## NIST: repair parity

Sealed bundles, `repair_corpus.py --bundle`. Outcome vectors are identical.

| | baseline (`off`) | region driver (`live`) |
|---|---|---|
| rows | 33 | 33 |
| outcomes | repaired 7, refused 15, nothing_to_repair 4, not_attempted 6, no_change 1 | **identical** |
| `open_shell` defects repaired | 36 | **36** |
| routes | conservative_repair 23, manual_cleanup 6, mesh_as_is 4 | identical |
| paired-rim refusal records | 22 | 20 |
| loop stopped | stalled 4, resolved 2, no_candidate 2 | regions_exhausted 5, resolved 2, time_budget 1 |

**The 22-vs-20 refusal gap is duplicate records, not a safety difference.** All records come from
`nist_ftc_10_asme1_ap242-e2.stp`. Stripping the `#N` collision suffix that `identity.py` assigns,
both arms refuse the same **13 distinct rims**; the set difference is empty in both directions. The
baseline rescans the whole shape every iteration, so a rim re-spelled after its colliding sibling is
repaired earns a second record under `RunHistory.refuse`'s `(defect, reason)` key. The region driver
takes two whole-shape scans by design and records each rim once.

## Generated campaign: cost

50 defective parts, both drivers, same settings.

| | baseline | region driver |
|---|---|---|
| whole-shape re-inspections | **566** | **54** |
| worst case on one part | 45 | **2** |
| scoped re-inspections | 0 | 802 |
| wall-clock | 184 s | **154 s** |
| per-part verdict | — | 41 better, 9 tied, **0 worse** |

Whole-shape re-inspection is the expensive kind; scoped scans are cheap enough that total time falls
rather than moving. That is the architecture's claim, measured.

## Clean references: no-op control

40 clean parts across all 22 families.

| | baseline | region driver |
|---|---|---|
| operations kept | **0** | **0** |
| parts mutated (faces, solids or volume) | **0** | **0** |
| whole-shape scans | 104 | 40 |

A repair that acts on geometry with nothing to fix is damage with no defect to excuse it, and
neither defect counts nor `stopped_because` can see it. This control can.

## What this evidence does NOT show

Stated plainly so a reviewer does not infer it.

- **No repair improvement.** Parity with the baseline on NIST, not advantage.
- **Cost inverts on fragmented geometry.** `nist_ctc_02_asme1_ap242-e2.stp` spends its full 300 s
  against the baseline's 96.7 s; `nist_ctc_04_asme1_ap242-e1.stp` takes 43.9 s against 5.9 s.
  `decompose` yields 103 regions on that 637-face part, and each region may take
  `CAD_REPAIR_REGION_ATTEMPTS` packages. The cost win holds where defects cluster and inverts where
  they scatter. Both numbers belong in any published claim.
- **`resolved` is not a repair metric on these corpora.** Across a sliver-precision sweep it moved
  17 -> 39 while kept operations fell 32 -> 12 and defects left rose 371 -> 396: the highest
  `resolved` count was the setting that repaired least. It measures how little was attempted.
- **The generated campaign cannot demonstrate repair.** Its 60 injected defects are 26 thin
  walls/ligaments, 23 debris/boss/flash/slot, and the rest pin holes, voids and obstructions - none
  of which this catalogue repairs. One expected repair reads "Restore the local solid-region
  ligament to the paired reference shape." The three implemented topology pilots
  (`missing_face`, `duplicate_face`, `tolerance_inflation`) are offered at an 8% single-edit rate and
  drew zero cases in 50.

## Known defects found by this comparison, and fixed

Each was found by the corpora above, not by unit tests.

| finding | commit |
|---|---|
| region walk was spatial, so an error at region 47 of 103 was never reached before the clock expired | `709476bd` |
| planner refusals were never reported on the region path, leaving one arm of the comparison blank | `e50dc5ee` |
| paired-rim guard saw one region's defects, so two rims of one passage in different regions were both patched - a sealed through-hole | `bcae4f66` |

## Files

`nist-{off,live}-summary.json`, `-manifest.json`, `-settings.json`, and the verbatim run logs.
Row JSON is not committed: `/tmp/bundles/final-{off,live}.json`.
