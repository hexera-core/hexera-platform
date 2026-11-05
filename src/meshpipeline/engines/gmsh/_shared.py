# Responsibility: Declare the evidence a Gmsh review needs: the mesh that must render and the named groups to check.
# Boundaries: requirements only - the spec publishes them and the review renderer enforces them.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    InspectionTarget,
    RenderArtifactRequirement,
    TargetKind,
)

_RENDER_ARTIFACTS = (
    RenderArtifactRequirement(
        artifact_key="mesh_paths.surface",
        allowed_formats=(ArtifactFormat.GMSH_MSH,),
        purpose="the delivered FEA mesh - physical groups are named inside it",
        required=True,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(target_id="group:*", kind=TargetKind.GROUP,
                     label="every named physical group",
                     purpose="a passing Jacobian says nothing about whether the load, "
                             "restraint and contact groups landed on the intended surfaces",
                     required=False),
)
