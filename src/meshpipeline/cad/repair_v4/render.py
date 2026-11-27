# Responsibility: Put a picture of a failure site in front of the proposer.
# Owns: `view`.
# Boundaries: no OCCT and no drawing here; the picture is the `render_view` kernel function's.
"""A 512 px PNG of a site, drawn by a kernel worker."""

from __future__ import annotations

import base64
import binascii

from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Workspace

SIZE_PX = 512
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def view(pool: WorkerPool, ws: Workspace, site: Site, *, timeout_s: float = 120.0) -> bytes | None:
    """The site's picture, also stored on `site.view_png`; None (and the field untouched) when the
    kernel could not draw one. A view is optional evidence, so failure is silence, not an error.
    """
    refs = [{"id": e, "type": {"F": "face", "E": "edge", "B": "solid"}[e[0]], "index": ws.indices[e]}
            for e in site.entities if e in ws.indices]
    result = pool.call("render_view", {
        "brep_path": ws.brep_path, "entities": refs, "xyz": [list(p) for p in site.xyz],
        "size": SIZE_PX, "budget_s": min(45.0, timeout_s)}, timeout_s=timeout_s)
    if not result.ok or result.value is None or not result.value.get("png_b64"):
        return None
    try:
        png = base64.b64decode(result.value["png_b64"], validate=True)
    except (binascii.Error, ValueError):
        return None
    if not png.startswith(_PNG_MAGIC):
        return None
    site.view_png = png
    return png
