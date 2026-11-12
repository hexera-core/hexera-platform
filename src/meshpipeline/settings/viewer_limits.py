# Responsibility: Declare the three limits that decide how much of a delivered mesh the viewer is
# handed - how many polygons one drawn surface carries, how many faces a detail ball returns, and
# how many a deep-zoom window returns before it refuses.
# Boundaries: settings only. render/polymesh_budget.py spends the first, render/detail_window.py
# the other two. Nothing here decides WHAT is drawn or how it is reduced.
"""THREE LIMITS THAT WERE READ STRAIGHT OUT OF THE ENVIRONMENT, AND NOW ARE NOT.

They arrived with the semiconductor viewer as `int(os.environ.get("HEXERA_...") or N)` at module
scope in `render/polymesh_budget.py` and `render/detail_window.py`. That works and is invisible:
`devtools/quality/config_inventory.py --full` certifies that every setting this product reads is
in the catalogue and is read through this package, and those three were three undeclared reads and
three direct bypasses. A setting nobody can find is a setting nobody can set, and the first person
to need one of these is whoever is told a window "does not fit" on a machine that could carry it.

THE NAMES KEEP THEIR `HEXERA_` PREFIX. They are already documented in the viewer's own notes and
in the measurements recorded beside each default, and renaming them would quietly strand anyone
who has set one. The prefix is the only one in the catalogue, which is a small inconsistency and
the honest trade.

Every default is unchanged from the measurement that set it, and each measurement is recorded
beside the value rather than here.
"""
from __future__ import annotations

from meshpipeline.settings.env import optional_env

#: How many triangles one drawn boundary surface may carry. Measured 2026-11-07, one actor at 1:1:
#: 1,904,636 faces / 3,810,902 triangles drew in 0.30 ms of CPU per frame at 55.2 fps on Intel UHD
#: via ANGLE/D3D11, and the binary path carried the same payload in 574 ms against base64's
#: 1,379 ms. What actually binds is VRAM, about 92 B per delivered face, so a whole 36 M-face
#: boundary would be ~3.3 GB and is out of reach - but several million is not. The default is the
#: one every existing page was built against, so nothing moves until a measured build raises it.
DRAWN_MAX_TRIANGLES: int = int(optional_env("HEXERA_DRAWN_MAX_TRIANGLES", "800000"))

#: How many faces a detail ball returns. A detail view is a few cell widths across, so this is a
#: backstop for a radius asked carelessly, not a budget anyone should meet.
DETAIL_MAX_FACES: int = int(optional_env("HEXERA_DETAIL_MAX_FACES", "150000"))

#: How many faces a deep-zoom window returns before it refuses to send any. Measured on the
#: delivered plate: a view 4 mm across holds 1,372,166 delivered faces, which is where a cell
#: first reaches 5 px - so this sits just above what the first legible view actually costs, and a
#: view any wider is one the exact surface should be drawing instead.
WINDOW_MAX_FACES: int = int(optional_env("HEXERA_WINDOW_MAX_FACES", "1500000"))

__all__ = ["DETAIL_MAX_FACES", "DRAWN_MAX_TRIANGLES", "WINDOW_MAX_FACES"]
