# Responsibility: The one place the repair engine's shared numeric limits live, so that the op that enforces a limit,
#                 the judge that re-checks it and the benchmark injector that must stay inside it cannot drift apart.
# Boundaries: stdlib only. No kernel, no op, no OCP - importable from the hermetic tier and from devtools.
from __future__ import annotations

import math

#: A REPORTING threshold, no longer a cap: `fill_loop` used to refuse a loop whose bounding-box diagonal exceeded this
#: fraction of the part's diagonal; the owner decision of 2026-11-24 removed that gate (ops/faces.py fills any loop,
#: the det sweep proposes any loop). It is still read by verdict/faithful_kernel.py, which REPORTS `fill_too_large` in
#: layer 3 (layer 3 gates nothing), and by devtools/validation/v4_eval/inject.py, whose `face_delete` strata are
#: benchmark design. Do not retype it anywhere.
MAX_FILL_SPAN_RATIO = 0.05


#: THE CHECKER'S PER-CALL WALL, seconds: the limit on ONE kernel call of the verdict's layer 1 / layer 3
#: (`check_valid`, `read_step_xcaf`, `valid_extent`, the layer-3 comparison). A call that overruns it kills its
#: worker and the file becomes `CheckerUnavailable` - EXCLUDED from the counts, never a finding about the CAD.
#: Read by verdict/valid.py (`CALL_TIMEOUT_S`, re-exported to verdict/faithful.py) and verdict/memo.py
#: (`READ_TIMEOUT_S`). Do not retype it anywhere; the per-FILE wall that bounds the SUM of a file's calls is
#: devtools/validation/v4_eval/evalspec.py's DEFAULT_FILE_TIMEOUT_S and must stay larger than this.
#:
#: 300.0 until 2026-11-23 (task 55). RAISED to 900.0 on the measured timeout distribution of the re-probe
#: corpus, because at 300 s the kills alone excluded more of the pool than collect.py's 2% limit allows:
#:   - Lane D, 59 diagonal-stratified files at commit 5eefb130 (corpora/v4/measure_lane_d_20261123/files.jsonl):
#:     2 of 59 files (pool-weighted by file size: 2.7%) were excluded by THIS wall, both with a whole-file wall
#:     of 620 s and 662 s, so ~320-360 s of each file was spent outside check_valid.
#:   - Task 43, an independent 86-file sample: 3 of 86 excluded by this wall, same shape.
#:   - The whole-file wall distribution projected over the 10,887-file pool (project_reprobe.py's size model,
#:     log wall = -1.263 + 0.360 log bytes, residuals resampled) has p50 30 s, p90 309 s, p95 604 s, p98 1602 s.
#: 900 s is above the p95 of a WHOLE file's wall: one layer-1 call may now take as long as 95% of complete files
#: do. It is not a round number chosen for generosity - it is the largest value that still leaves the per-file
#: wall (1800 s, itself set by the VM's 8 h --max-run-duration) room for the 362 s of measured non-check_valid
#: work plus a second long call. The measurement is right-censored AT the old wall (a killed call reports ">300 s",
#: never its true length), so what fraction this still excludes is a projection, not a measurement: ~0.9%
#: pool-weighted on the lognormal tail, 0% if every victim was merely slow. The real wave measures it.
CHECKER_CALL_TIMEOUT_S = 900.0


def fill_span_allowed(span: float, diagonal: float) -> bool:
    """True when a loop spanning `span` (same unit as `diagonal`) is within the fill cap. The op's refusal is the
    negation of this, so `span > MAX_FILL_SPAN_RATIO * diagonal` is written exactly once."""
    return span <= MAX_FILL_SPAN_RATIO * diagonal


#: CRS v5 step 4 (owner decision 2026-11-24, "large parts get more time"): THE size-scaled budget rule. One factor,
#: applied by the caller to the file wall, the search wall and the per-mesher timeout alike (`budget_scale`).
#: CALIBRATED on the step-1 paired baseline (run v4-202611250141, search_ai at 32f7f6f47, 150 dev files, max 4,887
#: faces): one full check's median wall is 4.5 s under 100 faces, 24 s at 100-1k and 189 s at 1k-10k; search_ai's
#: timeout rate is 12% / 12% / 60% and the 1k-10k bucket cleaned 0 of 15. So the scale starts at 300 faces (the
#: 100-1k bucket's median wall, 697 s, already exceeds the 600 s search wall) and reaches the 3x cap at 3,000 faces,
#: where one full check costs about 3 min. Log-linear between, clamped to [1.0, BUDGET_SCALE_MAX]; the only place
#: to change it. Re-fit once a v5 wave records `part_faces` / `finish` per row.
BUDGET_SCALE_FLOOR_FACES = 300
BUDGET_SCALE_CEIL_FACES = 3_000
BUDGET_SCALE_MAX = 3.0


def budget_scale(faces: int | None, solids: int | None = None) -> float:
    """The budget multiplier for a part of `faces` B-rep faces, in [1.0, BUDGET_SCALE_MAX], non-decreasing in
    `faces`. An unknown size (None, or not a positive count) gets 1.0: a part is never given extra time it was not
    measured to need. `solids` is accepted and recorded by callers but does not move the provisional rule (the
    face count already grows with the solid count; the baseline wave decides whether it earns its own term)."""
    del solids
    if faces is None or not faces > BUDGET_SCALE_FLOOR_FACES:
        return 1.0
    span = math.log(BUDGET_SCALE_CEIL_FACES / BUDGET_SCALE_FLOOR_FACES)
    frac = math.log(faces / BUDGET_SCALE_FLOOR_FACES) / span
    return round(min(BUDGET_SCALE_MAX, 1.0 + (BUDGET_SCALE_MAX - 1.0) * frac), 4)
