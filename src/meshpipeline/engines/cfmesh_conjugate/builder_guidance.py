# Responsibility: Brief the conjugate builder - the staged part, the coolant cut out of it, and the two region names.
# Boundaries: briefing prose; the driver, not this text, decides what is built.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("The part is staged as its own CAD (geometry.step or the format it arrived "
                   "in) - mesh THAT. input.stl is a preview of the part's outside skin; the "
                   "coolant is not in it, because the coolant is the space inside the part and "
                   "is cut out of the CAD by the build."),
    workflow=("The build is deterministic: run_mesh cuts the coolant out of the part, sizes the "
              "cells from its narrowest passage, meshes the whole block once, tags the coolant's "
              "cells and splits the mesh in two, checks both regions and compares the meshed "
              "coolant against the CAD -> submit_mesh."),
    contract_title="Region + patch contract - two regions, and the names you approved",
    contract_guidance=("The two regions are always called fluid (the coolant) and solid (the "
                       "metal), and the interface between them is written by the split - never "
                       "authored. The declared inlet and outlet name the block faces the coolant "
                       "actually reaches; which faces those are is measured from the passage, "
                       "not chosen."),
    domain_default="conjugate heat transfer (a cooled solid)",
)

__all__ = ["BRIEFING"]
