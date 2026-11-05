# Responsibility: Brief the Gmsh builder - the staged CAD solid, the tool sequence and how groups are mapped.
# Boundaries: briefing prose; the driver, not this text, decides what a tool actually does.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("The CAD SOLID is staged as geometry.step (mesh THIS; input.stl is a "
                   "tessellated preview)."),
    workflow=("Build the mesh per your system prompt: geometry_report -> write "
              "gmsh_spec.json -> run_mesh (iterate on feedback) -> submit_mesh. "
              "You do NOT write mesh files or Gmsh code - the driver renders the spec."),
    contract_title="Region contract (user-confirmed) - use these EXACT names",
    contract_guidance=("Each contracted name becomes a NAMED GROUP in gmsh_spec.json: map "
                       "surface_tags from geometry_report (areas + centroids identify the "
                       "faces) onto every contracted name. Unassigned faces fall into "
                       "default_group."),
    domain_default="structural FEA",
)
