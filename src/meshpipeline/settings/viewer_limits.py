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

Each default is recorded beside the value together with the measurement that set it. Two of the
three are unchanged from the day they arrived; DRAWN_MAX_TRIANGLES was raised on 2026-11-19 and
carries both the figure it was raised to and the arithmetic that chose it.
"""
from __future__ import annotations

from meshpipeline.settings.env import optional_env

#: How many triangles one drawn boundary surface may carry. Measured 2026-11-07, one actor at 1:1:
#: 1,904,636 faces / 3,810,902 triangles drew in 0.30 ms of CPU per frame at 55.2 fps on Intel UHD
#: via ANGLE/D3D11, and the binary path carried the same payload in 574 ms against base64's
#: 1,379 ms. What actually binds is VRAM, about 92 B per drawn POLYGON - the same figure out of
#: both of that day's measurements, 174.4 MB / 1,904,636 = 91.6 B and 1,396 MB / 15.2 M = 91.8 B -
#: so a whole 36 M-face boundary would be ~3.3 GB and is out of reach, but several million
#: triangles is not.
#:
#: RAISED FROM 800,000 TO 3,000,000 ON 2026-11-19. That comment used to end "the default is
#: unchanged so no existing page moves; a build that has been measured sets
#: HEXERA_DRAWN_MAX_TRIANGLES", and nobody ever did - so the measurement sat in the repo for
#: twelve days while the product served the lossy route to the one part that most needed the
#: other one. What 800,000 cost, measured on the delivered cold plate (job 6dd6703c, 283,429,764
#: cells, coolant passages 98.2422 um wide with 0.500 mm of metal between them): the payload
#: carried `reduced`, 17,070,364 delivered faces drawn as 524,274 triangles at a cluster size of
#: 567.2 um. That is 5.8x COARSER THAN THE CHANNEL. Every drawn triangle spanned about six
#: passages and the vertex merge collapsed straight across them, which is the diagonal hatching
#: and the torn edges in the screenshot that started this.
#:
#: THE ARITHMETIC, all of it measured on that job and none of it assumed.
#:   * The lossless surface was already being BUILT and then thrown away. `exact_surface` takes
#:     its 17,070,364 delivered faces to 1,059,668 polygons - fluid 16,584,838 -> 854,403 (19.4:1)
#:     and solid 485,526 -> 205,265 (2.4:1) - at an area error of 2.17e-16 with NO VERTEX MOVED.
#:   * In TRIANGLES, counted off the poly stream rather than assumed: 2,164,434. The polygon-size
#:     distribution is what makes that 2.0426 per polygon and not 2 - 95.908% quads, 3.677%
#:     pentagons, 0.307% hexagons, 0.090% triangles, 0.018% 7-gons and three 8-gons. Taking "two
#:     per polygon" would have under-counted by 45,098 triangles.
#:   * The poor-cell overlays ride ON TOP of this budget and are not counted against it
#:     (cfmesh_conjugate.viewer adds them after `fit_to_budget`, on purpose, so they are never
#:     merged). On this job that is 13 sets, 423,102 polygons and 859,079 triangles.
#:   * So the binding sum is in POLYGONS, because VRAM is: B/2.0426 + 423,102 <= 1,904,636, the
#:     polygon count that was measured drawing at 55.2 fps. B <= 3,026,120.
#: 3,000,000 is that, rounded down. At it the whole page - mesh plus every overlay switched on -
#: is 1,891,848 polygons and 174.05 MB of VBO, against the 174.4 MB measured at 55.2 fps and
#: 0.30 ms of CPU per frame. The exact surface fits with 27.9% of the budget to spare.
#:
#: AND IT DOES NOT COST THE PAGE ITS WEBGL CONTEXT, which is the other half of the same record:
#: edges on a 1,904,636-polygon actor took the context away on 2026-11-08, and that was not a
#: slow frame, it was the context going. It cannot happen here, and the reason is that this
#: change moves the page AWAY from that failure rather than towards it. viewer.js caps edges per
#: actor at 750,000 polygons; the exact route puts fluid/fluid_to_solid at 851,763, so its edges
#: go off automatically. Counted off the two served payloads:
#:
#:     largest actor                         336,687  ->   851,763 polygons
#:     largest actor WITH EDGES              336,687  ->   212,284 polygons
#:     line segments, every surface shown  3,280,745  -> 2,541,165
#:     line segments, as the page opens    2,725,904  ->    29,350
#:
#: The one actor that grew past the cap is the one that stops drawing edges, so every edged
#: actor on the page is smaller than the largest edged actor was before it. Verified in a real
#: browser against this payload on 2026-11-19: isContextLost false, getError 0, no page errors.
DRAWN_MAX_TRIANGLES: int = int(optional_env("HEXERA_DRAWN_MAX_TRIANGLES", "3000000"))

#: How many faces a detail ball returns. A detail view is a few cell widths across, so this is a
#: backstop for a radius asked carelessly, not a budget anyone should meet.
DETAIL_MAX_FACES: int = int(optional_env("HEXERA_DETAIL_MAX_FACES", "150000"))

#: How many faces a deep-zoom window returns before it refuses to send any. Measured on the
#: delivered plate: a view 4 mm across holds 1,372,166 delivered faces, which is where a cell
#: first reaches 5 px - so this sits just above what the first legible view actually costs, and a
#: view any wider is one the exact surface should be drawing instead.
WINDOW_MAX_FACES: int = int(optional_env("HEXERA_WINDOW_MAX_FACES", "1500000"))

__all__ = ["DETAIL_MAX_FACES", "DRAWN_MAX_TRIANGLES", "WINDOW_MAX_FACES"]
