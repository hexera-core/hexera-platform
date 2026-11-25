# Lane D15 (+ D16 evidence): what the det sweep proposes per expansion, and what binds

Probe: `search_det` through the real arm path (`arms.run_file`: engine form, staged healer, confirm, search), budget
60 ops / 600 s wall, `k_propose` 12, `--h-mm auto`, workers 1, no model, code 9766d1b90 (D14 merged, BEFORE the D16
reorder). The only change is an instrumented `DetGenerator` that logs every row before the k cut and returns
`full[:k]`, so the search is unchanged. 30 dev_v1r files, one c2d-standard-16 VM (14 files at once, 1 vCPU each),
40-min per-file kill. Evidence: `lane-d15/` (probe, analysis scripts, per-file rows+traces, outputs).

## Coverage: 20 of 30 measured
- 20 searched (2 clean at start or in search), 4 crashed in the staged healer, 6 killed at 40 min.
- **4 crashed**: `stages.rollback_detail` formatted `min_sicn` None (a member fail -> fail whose SICN vanished).
  Fixed in b320e979e. This took down staged AND both search arms on those files; it is on the tip, so v2 rows may
  carry it.
- **6 killed at 40 min** (idx 8, 15, 18, 23, 25, 26: the largest parts, up to 289k STEP entities). With a 600 s
  search wall a file still ran > 2400 s; which phase (staged or the first expansion) is not in the logs.

## What binds
| stop | files |
|---|---|
| wall | 8 |
| patience | 8 |
| clean | 2 |
| exhausted | 2 |
| ops | **0** (max spent 45 / 60) |

- **The wall is soft.** It is checked once per expansion; an expansion runs every candidate up to k. Overshoot up to
  +1406 s (idx 10: ONE 8-candidate expansion, 2006 s, ~250 s per candidate, ended clean); idx 27 1751 s total.
- **Cost per op**: every det candidate that RUNS is charged exactly 1 op, declines included (212 of 402 candidate
  events applied 0 ops and were charged 1). Det never applies more than 1. A defeature expansion costs one op per
  defeature row: 1 whole-part + up to 3 per site (D16: located, recognised-at-site, site faces).
- **k binds expansion 1, not the wall.** 15 of 18 files with an expansion had own + defeature rows <= 12 in their
  first expansion (both reached). 3 exceed k: idx 5 (19 rows), 19 (14), 21 (23). After the D16 reorder their own rows
  (17 / 13 / 22) fill k and defeature drops out. Reaching both on idx 21 needs k >= 23; that expansion would take
  ~63 + 23 x 49 s = ~1190 s.
- **The wall binds depth.** The 8 wall-stopped files' expansions take 449-735 s; a SECOND expansion needs ~1200 s,
  i.e. <= +600 s on ~40% of files (~+38% on the 585 s mean search wall). No evidence yet that depth 2 cleans more.

## Rows per expansion
52 expansions, median 8.5 rows, max 23; 6 over k, 39 rows cut. See `final_analyse.txt` for per-op counts.

| site kind | sites x expansions | mean rows | max | rows past k |
|---|---|---|---|---|
| overlap | 6 | 9.0 | 14 | 11 / 54 |
| proximity | 25 | 7.7 | 11 | 26 / 193 |
| self_overlap | 8 | 6.5 | 10 | 0 / 52 |
| invalid | 14 | 3.5 | 7 | 1 / 49 |
| intersection | 14 | 2.3 | 8 | 0 / 32 |
| open | 12 | 2.0 | 7 | 0 / 24 |

`trim_unbounded` never fires on dev: it needs an `unbounded` site, and unbounded inputs are excluded from the splits.

## Did the new ops help (calibrated rank: parent rank vs the child's rank)
Lane B13 compares a cheap-tier child score with a full-tier parent score, which leans to "worse". On the rank the
search itself uses:

| op | better | equal | worse | worse on |
|---|---|---|---|---|
| defeature, whole recognised set | 3 | 1 | 18 | valid 14, panel 2, dev 1, churn 1 |
| defeature, site faces (D14) | 4 | 0 | 5 | sicn 2, valid 1, panel 1, dev 1 |
| fuse | 3 | 0 | 1 | dev 1 |
| cut_by | 1 | 1 | 3 | dev 3 |

Whole-part defeature makes the part INVALID 14 times in 22: that one is real, not tier bias. D16 moves it last.

## D16 (d9c905dbc) in one line
Site-specific rows for all sites first, then defeature (located -> recognised-at-site -> site faces), then the whole
recognised set; `located` = the faces a mesher's own located failure named (`graph.mesher_faces`), at any kind but
open/unbounded. Not yet measured on a wave.
