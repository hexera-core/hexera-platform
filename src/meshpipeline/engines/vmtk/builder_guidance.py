# Responsibility: Brief the VMTK builder - the staged lumen surface, the tool sequence and the capped openings.
# Boundaries: briefing prose; the driver, not this text, decides what a tool actually does.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("The lumen SURFACE is staged as lumen.vtp (mesh the volume INSIDE it); "
                   "input.stl is a tessellated preview of the same surface."),
    workflow=("Build the mesh per your system prompt: geometry_report (surface bounds + the "
              "open profiles at the lumen ends) -> measure_scales -> configure_mesh (choose a "
              "STRATEGY: edge length relative to the local radius, boundary layers, capping) -> "
              "run_mesh (centerlines -> radius-adaptive remesh -> tet volume mesh) -> "
              "submit_mesh. You do NOT write vmtk pypes by hand - configure_mesh writes the spec."),
    contract_title="Patch contract (user-confirmed) - use these EXACT names",
    contract_guidance=("Map the lumen wall onto the wall patch; map each CAPPED open profile at "
                       "the ends of the lumen onto the contracted inlet/outlet patch names "
                       "(geometry_report lists the open profiles with their centroids and areas "
                       "so you can tell which is which)."),
    domain_default="internal flow through a lumen",
)
