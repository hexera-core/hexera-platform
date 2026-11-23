# Responsibility: The one place the repair engine's shared numeric limits live, so that the op that enforces a limit,
#                 the judge that re-checks it and the benchmark injector that must stay inside it cannot drift apart.
# Boundaries: stdlib only. No kernel, no op, no OCP - importable from the hermetic tier and from devtools.
from __future__ import annotations

#: `fill_loop` refuses a loop whose bounding-box diagonal exceeds this fraction of the part's bounding-box
#: diagonal (design §7 decision 2: fills only for loops <= 5% of the diagonal). Read by ops/faces.py (the op),
#: verdict/faithful_kernel.py (the judge's `fill_too_large`) and devtools/validation/v4_eval/inject.py
#: (`face_delete` only deletes faces the op could put back). Do not retype it anywhere.
MAX_FILL_SPAN_RATIO = 0.05


def fill_span_allowed(span: float, diagonal: float) -> bool:
    """True when a loop spanning `span` (same unit as `diagonal`) is within the fill cap. The op's refusal is the
    negation of this, so `span > MAX_FILL_SPAN_RATIO * diagonal` is written exactly once."""
    return span <= MAX_FILL_SPAN_RATIO * diagonal
