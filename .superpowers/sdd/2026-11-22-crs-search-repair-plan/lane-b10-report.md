# Lane B10 report (PARKED at 24 of 30 files): the staged healer under three cheap tiers

**Parked, not finished**, because the owner's 2026-11-24 goal change (B12) took priority. **24 of the 30** v1 dev files were measured. The remaining 6 are the slowest (per-file cap 3000 s); they keep running on VM `dev-crsv4-lane-b10` (c2d-standard-8, `dev-crsv4` labels), which uploads `gs://dev-crsv3-transfer-224734058693/eval/lane-b10/` on completion and self-deletes at 04:37 UTC at the latest (`--max-run-duration 3h`). Code: B8b's tree (0fa39fd5 + `cheap_members`). Read-only measurement; nothing was changed.

Per file, `run_staged` was run three times, each followed by `confirm_staged` with the full panel:
- **pre_b8:** layer 1 + gmsh_default;
- **b8_static:** + gmsh_prod (the tip after B8);
- **b8b_adaptive:** + every member the input fails (the tip after B8b).

The per-stage kept/rolled-back sets come from each run's own transactions (`lane-b10/files/*.json`, `analysis_24of30.txt`).

## Results (24 files, all three tiers measured on every one, 0 errors)

| | pre_b8 | b8_static | b8b_adaptive |
|---|---|---|---|
| final clean (old definition, faithful included) | 2/24 | 2/24 | 2/24 |
| full-panel-pass regressions after confirmation | 0 | 0 | 0 |
| **confirm_staged fell back** | **0 files** | **0 files** | **0 files** |
| mean wall per file (staged + confirmation) | 90 s | 80 s | 104 s |
| condition: kept / rolled back | 8 / 7 | 10 / 5 | 10 / 5 |
| simplify: kept / rolled back | 8 / 3 | 11 / 0 | 11 / 0 |

**Transactions that changed between tiers.**
- **pre_b8 → b8_static: 1 newly rolled back** (`condition`, kept before). **6 newly kept**: `condition` x3, `simplify` x3. With gmsh_prod meshed, a stage that costs gmsh_default some min-SICN but helps gmsh_prod is no longer rejected on gmsh_default's number alone, so the cheap tier turned out *less* restrictive here, not more.
- **b8_static → b8b_adaptive: no transaction differs** on these 24 files, at +24 s mean wall. That matches B8b's finding that on v1 inputs every gmsh_alt/Netgen failure co-occurs with a gmsh_default or gmsh_prod failure.

**confirm_staged** never had to fall back on these 24 dev files under any tier. The regression it exists for (00614313) is a clean-control file, not in this set.

**Healer gains (same for all tiers):**
- `00614908`: 3 → clean.
- `0051_00519158`: 0 → 1 panel pass.
- `0060_00601268`: 0 → 1.

**Note.** "Clean" here is the definition before the 2026-11-24 owner decision (faithfulness gated). Under the new definition (valid + panel), the counts may differ; the per-file JSONs carry the full panel and validity, so they can be recomputed.
