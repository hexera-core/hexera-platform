# Responsibility: Brief the snap-grid builder - the staged ECXML model, the placed parts and the region names.
# Boundaries: briefing prose; the driver, not this text, decides what is built.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("An ECXML thermal model is staged as source.ecxml (mesh THIS - its boxes, "
                   "cylinders, plates, fans and vents, placed where the file puts them). "
                   "input.stl is a preview of the placed parts, one named solid per part."),
    workflow=("The build is deterministic: run_mesh places the parts, sizes the snap grid, "
              "writes one region per part and per air space with conformal interfaces, checks "
              "every region with checkMesh and the mesh against the file -> submit_mesh."),
    contract_title="Region + patch contract - the names come from the file",
    contract_guidance=("Every part keeps the region name the fused path gives it; the openings "
                       "(fans, vents) and domain sides keep the file's names. The interfaces "
                       "between regions are written by the mesher - never authored."),
    domain_default="conjugate heat transfer (electronics cooling)",
)
