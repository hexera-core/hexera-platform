# Responsibility: Brief the snappyHexMesh builder - the staged surface, the tool sequence and the patch mapping.
# Boundaries: briefing prose; the driver, not this text, decides what a tool actually does.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("A surface STL of the body has been placed in the workspace "
                   "as input.stl."),
    workflow=("Build the mesh per your system prompt: geometry_report -> measure_scales "
              "-> configure_mesh (choose a STRATEGY) -> run_mesh (iterate on feedback) -> "
              "submit_mesh. You do NOT write dicts by hand - configure_mesh writes them."),
    contract_title="Patch contract (user-confirmed) - use these EXACT names",
    contract_guidance=("Map the body surface to the wall patch; map the external boundary to "
                       "the farfield patch. Pass these names to prepare_surface and set them "
                       "in the meshDict's renameBoundary."),
    domain_default="external CFD",
)
