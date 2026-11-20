# Responsibility: Declare CAD simplification's switches - whether geometry may be DEFEATURED at all, the error budget a kept simplification must stay inside, and the clocks that bound inspection and the attempt.
# Boundaries: settings only; nothing here inspects, defeatures or solves anything. Whether a particular simplification is acceptable is decided by cad/simplify/simplify.py and cad/simplify/verify.py measuring their own result against these.
# Collaborates with: pipeline/simplify_inspect.py and pipeline/simplify_attempt.py (read these), settings/inventory.py (declares them), settings/cad_repair.py (the pattern these mirror).
from __future__ import annotations

from meshpipeline.settings.env import bool_env, optional_env

#: Seconds inspection may spend on one file before the run goes on without it. The kernel has no
#: interruption point, so on expiry the work is abandoned in its thread, as CAD repair's is.
CAD_SIMPLIFY_INSPECT_TIMEOUT_S: float = float(optional_env("CAD_SIMPLIFY_INSPECT_TIMEOUT_S", "60"))

#: Files larger than this are not inspected, and the report says so rather than calling them clean.
CAD_SIMPLIFY_INSPECT_MAX_BYTES: int = int(optional_env("CAD_SIMPLIFY_INSPECT_MAX_BYTES", str(128 * 1024 * 1024)))

#: OFF unless a deployment turns it on, and the ONE switch that separates looking from changing -
#: the twin of CAD_REPAIR_ENABLED. Inspection runs regardless (pipeline/simplify_inspect.py); this
#: says whether a simplification may write new geometry at all. A kill switch, not a tuning knob:
#: with it off, an attempt is refused before any kernel call (cad/simplify/simplify.py) and again
#: in the node (pipeline/simplify_attempt.py), so a deployment that has not opted in cannot
#: defeature a customer's CAD by any path.
CAD_SIMPLIFY_ENABLED: bool = bool_env("CAD_SIMPLIFY_ENABLED", "false")

#: THE ERROR BUDGET: the largest relative change of the canonical load case's compliance a kept
#: simplification may spend, summed over the features it removes. 0.001 is 0.1%. For FEA the sum
#: of member errors was measured to bound the package's own error conservatively
#: (docs/simplification/DEVLOG.md), which is why the budget is a ledger and not a per-feature test.
CAD_SIMPLIFY_BUDGET: float = float(optional_env("CAD_SIMPLIFY_BUDGET", "0.001"))

#: THE SCREEN MARGIN: a feature is kept only when its one-driver local estimate reads within this
#: fraction of the budget. Measured on nist_ftc_09 with one shared driver solve: 0.5 made no false
#: accept where 0.8 made one; a borderline feature is rejected rather than given its own driver.
CAD_SIMPLIFY_SCREEN_MARGIN: float = float(optional_env("CAD_SIMPLIFY_SCREEN_MARGIN", "0.5"))

#: Seconds the run will WAIT for a simplification - defeaturing, the driver solve and every local
#: check - before going on with the geometry it already had. As CAD_REPAIR_TIMEOUT_S: the kernel
#: cannot be interrupted, so its thread is abandoned; the solver children are bounded by the same
#: deadline and killed with it.
CAD_SIMPLIFY_TIMEOUT_S: float = float(optional_env("CAD_SIMPLIFY_TIMEOUT_S", "600"))

#: The most features one attempt removes and verifies, the costliest first (the inspection's
#: estimated surface cells). Each costs two small local solves.
CAD_SIMPLIFY_MAX_FEATURES: int = int(optional_env("CAD_SIMPLIFY_MAX_FEATURES", "24"))

#: THE VOLUME CEILING (T0), as a fraction of the part's volume. Filling holes adds material; a
#: package that moved more than this is refused before anything is solved, because a change that
#: large is a different part, whatever a stiffness reading says.
CAD_SIMPLIFY_MAX_VOLUME_CHANGE: float = float(optional_env("CAD_SIMPLIFY_MAX_VOLUME_CHANGE", "0.05"))

#: The largest mesh, in 10-node tets, the verification solves - the driver or any local piece.
#: CalculiX's direct solver on one thread is the product's solver, and its memory grows past the
#: worker's well before its time does (SPOOLES at 460k tets peaked at 29.6 GB on the bench). A
#: part whose driver would be larger is not verified, so nothing is removed from it.
CAD_SIMPLIFY_MAX_SOLVE_TETS: int = int(optional_env("CAD_SIMPLIFY_MAX_SOLVE_TETS", "200000"))
