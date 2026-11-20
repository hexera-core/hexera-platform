# Blocker buckets, v3_full failing (lane F)

Every failing file of a wave goes into exactly ONE bucket: what stops it from being repaired. The
first match wins, in the numbered order below. This is analysis only. Produced by
`devtools/validation/v3_eval/blockers.py` from collect.py's output (rows plus transcripts) and lane
G's clash scan.

```bash
python3 devtools/validation/v3_eval/blockers.py --collect <wave>/collect --out buckets-<wave>.json
python3 devtools/validation/v3_eval/blockers.py --collect <wave4>/collect \
    --compare devtools/validation/v3_eval/supporting/blockers/buckets-wave3-17d5f55e.json
```

## Files

- `laneG-clash.json`: lane G's component-aware surface-mesh clash scan (the PR #495 method) of the
  93 overlap/PLC files.
  - Per file: the clashing face pairs, and whether each face lies in a kernel region (lenient box
    rule) or among the probe-located faces.
  - For the top 3 pairs, the B-rep distance and section-edge count.
  - The input geometry is the same 120-file corpus in every wave, so one scan serves all waves.
- `buckets-wave2-df7e1d7a.json`, `buckets-wave3-17d5f55e.json`: per-file buckets.

## Wave 3 (17d5f55e), 120 files

| # | bucket | files |
|---|---|---|
| 0 | fixed / not failing at baseline | 1 / 2 |
| 1 | real intersection (clash pair B-rep distance ~0), in a region | 15 (+3 gap unmeasured) |
| 2 | gap below mesh size (every pair distance > 0; 0.2-21% of diag/40) | 16 |
| 3 | clash not located | 39 |
| 4 | clash faces outside every region | 12 (+1 not in lane G data) |
| 5 | reviewer reject | 2 |
| 6 | keep-check revert / kept but still unmeshable | 3 |
| 7 | budget (scan_incomplete or time, no regions worked) | 10 |
| 8 | crash | 1 |
| 9 | other mesh failure (timeout, open_loop, no_volume, periodic, other) | 15 |

## Caveats

- "In region" is lane G's region approximation, not the wave's exact region fingerprints.
- Gap vs real reads only the top 3 clash pairs per file.
