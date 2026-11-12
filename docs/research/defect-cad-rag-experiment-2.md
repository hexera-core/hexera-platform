# Experiment 2: calibrated abstention and attempt ordering from sweep records

**Date:** 2026-10-29 · **Code:** `labs/defect_cad_rag/policy.py` · **Follows:**
`defect-cad-rag-experiment-1.md` (direction approved by the user: outcome prediction first).

Offline evaluation on the experiment-1 sweep records (1 999 judged rules-op attempts, 63
families, OCCT + defectgen, precapped files excluded). No new kernel runs: the sweep measured
each attempt's outcome and wall time, so skip and ordering policies replay against real numbers.
Leave-one-family-out throughout; the skip threshold is chosen on training families only
(cross-fitted by family within train), at a 95% target keep recall. Raw output
`/tmp/dcr-policy.json`.

## Skip policy: "don't attempt what retrieval says will roll back"

Rule: predict P(kept) for the rules-chosen operation with the hand-v2 kNN; skip the attempt when
the raw score falls under the training-family threshold.

| code | attempts | skipped | keeps lost | keep recall | attempt-seconds saved |
|---|---:|---:|---:|---:|---:|
| all | 1999 | **49.1%** | 10 / 184 | 0.946 | **62.7%** (1 551 of 2 473 s) |
| self_intersection | 464 | 100% | 0 / 0 | — | 100% |
| invalid_brep | 29 | 17.2% | 0 / 0 | — | 97.3% |
| open_shell | 437 | 0% | 0 / 143 | 1.000 | 0% |
| small_face | 351 | 27.1% | 2 / 23 | 0.913 | 53.2% |
| small_edge | 718 | 58.1% | **8 / 18** | 0.556 | 61.4% |

Held-out keep recall 0.946 against the 0.95 target — the training-family threshold transfers.
Half the attempt budget is recoverable at essentially full yield, and the recovered time is
concentrated exactly where experiment 1 said retrieval cannot help selection: the hopeless codes.
Brier 0.065.

**The one failure is small_edge** (8 of its 18 keeps skipped): with so few positives the global
threshold treats the whole code as near-hopeless. Design consequence: the live skip rule needs a
per-code guard — no skipping in a code whose training keeps are below a floor (~30), unless the
code has zero keeps anywhere (self_intersection, invalid_brep, where skipping all is exactly the
contraindication policy the review asked for).

**Calibration gotcha** (kept as a test): choosing the threshold on *binned calibrated*
probabilities ties keeps and hopeless attempts inside one bin and the recall constraint then
forbids every skip — the first run skipped nothing. The threshold must bind on the raw continuous
score; the calibrator is for reporting and ordering only.

## Ordering policy: best-first attempts under the real budgets

Replay per file under `CAD_REPAIR_MAX_OPS=6` and 120 s of measured attempt time: scan order vs
descending predicted P(kept).

- keeps achieved in the first round: scan **67**, ranked **84** (+25%), of 184 reachable without
  budgets — the budgets bind hard on defect-heavy files, which is where ordering pays;
- 8 of 220 files improved, 2 worsened, the rest unchanged (few files have enough judged attempts
  for order to matter — but those are precisely the thousands-of-defects files the user cares
  about).

Limit: attempts are replayed as independent (the sweep's snapshot semantics). This models the
first pass of a session, not reactive replanning after a kept operation changes the shape.

## Conclusions

1. The first shadow metric for `CAD_REPAIR_RAG=shadow` is now concrete: log predicted P(kept) per
   proposed attempt and report held-out keep recall vs seconds saved. The numbers to beat:
   49% attempts / 63% seconds at 0.946 recall.
2. Live behaviour 1 = attempt ordering (never changes what is judged, only when); live
   behaviour 2 = skipping with the per-code positive-count guard. Both leave caps, rollback and
   the progress rule untouched.
3. small_edge needs labels before it needs policy: grow its action-outcome base (more OCCT
   sampling beyond 24/file, or targeted defectgen) before letting the skip rule touch it.
