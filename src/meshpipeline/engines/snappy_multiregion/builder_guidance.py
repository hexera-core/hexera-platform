# Responsibility: Brief the multi-region builder - the staged assembly, the region split and the patch mapping.
# Boundaries: briefing prose; the driver, not this text, decides what a tool actually does.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("A multi-solid CAD assembly is staged as geometry.step (mesh THIS - one "
                   "closed solid per region: the fluid plus each solid). input.stl is a "
                   "tessellated preview of the whole assembly."),
    workflow=("Build the mesh per your system prompt: geometry_report (lists the assembly's "
              "solids - volumes, bounding boxes, centroids) -> assign each solid to a REGION "
              "(fluid or solid) -> configure_mesh (regions + per-region strategy) -> run_mesh "
              "(meshes the background, then splitMeshRegions cuts the coupled regions) -> "
              "submit_mesh. You do NOT write dicts by hand - configure_mesh writes them."),
    contract_title="Region + patch contract (user-confirmed) - use these EXACT names",
    contract_guidance=("Map each assembly solid onto a REGION name and type (fluid | solid) "
                       "from the brief; map the external boundary faces (inlet/outlet on the "
                       "fluid, the external-BC faces on the solids) onto the contracted patch "
                       "names. The fluid<->solid INTERFACE patches are created automatically "
                       "by the region split - do not author them."),
    domain_default="multi-region coupled simulation",
)
