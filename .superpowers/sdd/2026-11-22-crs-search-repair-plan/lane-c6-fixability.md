# Lane C6: fixability breakdown (DROPPED by the owner, 2026-11-24): a record only

The owner's decision of 2026-11-24 removes the fidelity gate from "clean", so the question this job asked (what is
fixable *within fidelity*) no longer decides anything. Stopped before classification. What was measured is kept here.

**Measured** (`lane-c6/measure.jsonl`): the 30 dev_v1r files (`provisional_v2/PROVISIONAL_dev_v1r.json`, af3e15b9).
- Run on jig, one container, no meshing.
- B-rep facts only: solid and face counts, layer-1 hard and soft findings.
- Body overlap: every pair measured both ways by `body_kernel._cut_depth`, with the cheaper cut, the method and the
  fidelity bound.
- Free loops: their spans against the 5% fill cap.

**What the measurements say** (for whoever picks this up under the new goal):
- **Body overlap is in 10 of 30 files, and NO pair in any of them is within the old bound.** The worst pair's cheaper
  cut ranges from 0.099 mm (occt/trj3, bound 0.0585) to 37.2 mm (0613121, a contained or duplicate body:
  mean_thickness). The overlaps elsewhere are 0.44 to 23.4 mm. Under the new goal these are candidates for
  fuse / cut_by.
- **Open shells** in 4 files:
  - nist_ftc_07: 2 loops of about 7.2 mm, fillable under the 5% cap.
  - bug32314: 1 loop of 17 mm, fillable.
  - 0606843: 1 loop of 502 mm, over the cap.
  - 0528484: 2 loops of 423 and 446 mm, over the cap.

  With the caps removed, all are fill candidates.
- **0608014** (brep_invalid) has no free loop at all: its open_loop is a mesher-side 1D-loop failure, not a hole.
- **Small edges or faces** (`small_edge` / `small_face`) are present in 17 of 30 files. The flag threshold (1e-4 x
  diagonal) is below the old bound, so removing them was always admissible.
- **Panel-only failures with no layer-1 finding:** 13 files, the `low_quality`, HXT and no_volume cases. Their
  fixability was the open question and stays unanswered.
- **00611852** is labelled clean under the engine form (its raw-STEP verdict differs). It is not a failure in dev_v1r.
