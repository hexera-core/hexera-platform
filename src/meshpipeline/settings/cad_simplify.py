# Responsibility: Declare CAD simplification's switches. The first slice only inspects, so these bound the inspection: how long it may take and how large a file it reads.
# Boundaries: settings only; nothing here inspects a file. The kill switch that will separate looking from changing (CAD_SIMPLIFY_ENABLED, the twin of CAD_REPAIR_ENABLED) arrives with the slice that changes geometry, not before: a switch with nothing behind it would read as a promise.
# Collaborates with: pipeline/simplify_inspect.py (reads these), settings/inventory.py (declares them), settings/cad_repair.py (the pattern these mirror).
from __future__ import annotations

from meshpipeline.settings.env import optional_env

#: Seconds inspection may spend on one file before the run goes on without it. The kernel has no
#: interruption point, so on expiry the work is abandoned in its thread, as CAD repair's is.
CAD_SIMPLIFY_INSPECT_TIMEOUT_S: float = float(optional_env("CAD_SIMPLIFY_INSPECT_TIMEOUT_S", "60"))

#: Files larger than this are not inspected, and the report says so rather than calling them clean.
CAD_SIMPLIFY_INSPECT_MAX_BYTES: int = int(optional_env("CAD_SIMPLIFY_INSPECT_MAX_BYTES", str(128 * 1024 * 1024)))
