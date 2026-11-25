# Responsibility: The one place the repair engine's shared numeric limits live, so that the op that enforces a limit,
#                 the judge that re-checks it and the benchmark injector that must stay inside it cannot drift apart.
# Boundaries: stdlib only. No kernel, no op, no OCP - importable from the hermetic tier and from devtools.
from __future__ import annotations

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
