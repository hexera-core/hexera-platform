# Responsibility: Brief a Netgen attempt - the staged geometry, who plans it and who names the patches.
# Boundaries: briefing prose; the deterministic driver, not this text, decides what runs.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("The fluid boundary has been staged in the workspace as fluid_boundary.stl: one "
                   "named solid per patch - the wall and every declared opening."),
    workflow=("Netgen is driven by its deterministic plan (engines/netgen/plan.py): the cell size at "
              "every wall point from the measured passage, prism layers on the wall, then the native "
              "ladder. No model authors this case."),
    contract_title="Patch contract (user-confirmed) - the mesh will expose these",
    contract_guidance=("Every staged solid becomes the patch of the same name; the walls are typed "
                       "wall. You do not name patches yourself."),
    domain_default="internal CFD",
)
