# Lane C18: does the staged healer need a full layer-1 check per transaction?

FINAL: measured on 837 stored v4 staged-arm OP transactions (parent -> child BREP, 178 files, 187 transactions on
clean controls) and 1,050 of their 1,098 states (48 hit the 3 h container limit; 0 errors), each judged as today (STEP export as `step_for` writes it, read back,
`check_shape`; plus the BREP's own `check_shape`, per-solid rules and per-pair overlap, each timed). Harness:
`st_record.py` / `st_analyse.py` (lane C, VM `lanec18-st-1740`); raw records `gs://dev-crsv3-transfer-224734058693/eval/lane-c18/results/`.

## What layer 1 still costs AFTER A17 (modelled per op: memo hits on every unchanged solid and pair)

| | seconds | share |
|---|---|---|
| today, no memo | 19,347 | |
| after A17 | 8,197 | 100% |
| - Booleans on CHANGED solids / pairs (memo misses) | 4,210 | 51% |
| - non-Boolean rules + soft findings, x2 (read + round trip) | 2,720 | 33% |
| - STEP export + read, x2 | 1,267 | 15% |

## RETIRED: "judge the BREP, export once at the stage boundary"

**Do not re-propose this without new evidence.** Layer 1 on the BREP and layer 1 on the STEP re-read are NOT the same
judgement. On 819 dev states, VALIDITY differs on 8 (0 of 231 clean-control states):

| file | state | BREP says | STEP re-read says (the judgement) |
|---|---|---|---|
| eda22c2f6e7c | `input.orient_and_close.7db98c6c` | brep_invalid | valid |
| eda22c2f6e7c | `split_angle-88c840642b42` | brep_invalid | valid |
| b13ba9c6a96e | `split_continuity-719d6f52fa0b` | self_intersection | valid |
| 9f6e3964cb9b | `input` | valid | self_intersection |
| 9f6e3964cb9b | `split_angle-4896726d38e0.orient_and_close.619cafb4` | valid | self_intersection |
| 9f6e3964cb9b | `v4_tighten_tolerances-2d2fbb95` | valid | self_intersection |
| 9f6e3964cb9b | `v4_tighten_tolerances-2d2fbb95.orient_and_close.8637f6c3` | valid | self_intersection |
| 9f6e3964cb9b | `v4_tighten_tolerances-2d2fbb95.orient_and_close.8637f6c3.orient_and_close.bc211d84` | valid | self_intersection |

And the export is only 15% of what is left after A17 (7% of today): even a safe version would buy little.

## RETIRED: touched-only per transaction (carry the parent's per-solid results, recheck the changed solids)

Not verdict-safe as a per-solid model: layer 1 judges things that are not inside a solid (open_shell on FREE shells,
the whole-shape BRepCheck pass) and the per-solid rules miss them (open_shell alone: 20+ states). And on the changed
solids A17 already does the Booleans, which are the cost.

## BUILT: P1 - a stage that changes no solid reuses the standing state's layer-1 report

- 613 of 837 op transactions (73%) change no solid's geometry_key + tolerances.
- On ALL 613 the STEP-read layer-1 check is IDENTICAL to the standing state's: same findings, ENTITIES included,
  same solid count. 0 exceptions, clean controls included.
- The gate is A17's own per-solid key (`valid_kernel._shape_memo_key`: geometry_key + tolerances + reversed faces),
  called, not copied, plus the face count and the free-shell count (the whole-shape facts an op can change without
  touching a solid). The panel and layer 3 still run on the new file; only layer 1 is not re-checked.
- The 4 transactions where a per-solid BRepCheck flipped under orient_and_close with an unchanged geometry key
  (7ab61ea7d641, 3e7c2a1197c3, 6aa493da5bbf, 6109dea12cc5): A17's key differs on 6109dea12cc5 (re-judged); on the
  other three layer 1's whole-shape answer is identical, so reuse is correct
  (`tests/native/test_native_repair_v4_c18_layer1_reuse.py`).
- Modelled: 8,197 -> 5,683 s after A17 (-31%), an UPPER bound: the numbers are per op and a stage is a chain. The
  row now says, per arm that ran the healer, how many stages were judged and how many reused layer 1
  (`triage.stages.layer1`), the measured companion of the 73%.
- Gate cost: ~1.4 s per big state, against 65-185 s for the check it replaces on the same files.

## Why the staged healer "got worse" on the fast loop (fast-1414 -> fast-1829): it did not

Survivorship. The full-tier doomed cap cut the input verdict 12,117 -> 2,621 s (search_det + search_ai), so files
that used to die in it now REACH the healer, and their real healer cost is measured for the first time. On the 22
rows that completed the healer in BOTH runs, the healer got FASTER: 3,242 -> 2,529 s (-22%). The increase is all
rows the baseline never measured: 32ca3e9a6e9a and bb0fe0d1c8e9 never reached the healer (died in a ~1,150 s input
verdict), and 5accc12fa47d / 7e573047c1a6 had their healer phase CUT by the file wall after 47-84 s.
