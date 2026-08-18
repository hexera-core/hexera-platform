# Responsibility: Brief the cfMesh builder - the staged geometry, the tool sequence and who names the patches.
# Boundaries: briefing prose; the driver, not this text, decides what a tool actually does.
from __future__ import annotations

from meshpipeline.engines.base import Briefing

BRIEFING = Briefing(
    geometry_line=("A surface STL of the body has been placed in the workspace "
                   "as input.stl."),
    workflow=("Build the mesh per your system prompt: geometry_report -> measure_scales "
              "-> configure_mesh (choose a STRATEGY) -> run_mesh (iterate on feedback) -> "
              "submit_mesh. You do NOT hand-write the meshDict - configure_mesh renders it."),
    contract_title="Patch contract (user-confirmed) - the mesh will expose these",
    contract_guidance=("configure_mesh renders renameBoundary from this contract automatically "
                       "- the produced patches carry these EXACT names and types. You do not "
                       "name patches yourself."),
    domain_default="external CFD",
)
