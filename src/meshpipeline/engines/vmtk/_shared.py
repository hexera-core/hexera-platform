# Responsibility: Declare the evidence a VMTK review needs: the wall, the submitted lumen, the centerlines, the caps.
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
        purpose="the delivered mesh boundary - the wall the solver will see",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.lumen",
        allowed_formats=(ArtifactFormat.VTK_VTP,),
        purpose="the SUBMITTED lumen - the truth a smoothed-away wall is compared against",
        required=False,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.centerlines",
        allowed_formats=(ArtifactFormat.VTK_VTP,),
        purpose="branch topology: a lost or distorted branch shows against its centerline",
        required=False,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(target_id="opening:*", kind=TargetKind.OPENING,
                     label="every inlet/outlet cap",
                     purpose="a malformed cap is a boundary condition the solver cannot apply",
                     required=False),
    InspectionTarget(target_id="branch:*", kind=TargetKind.BRANCH,
                     label="every centerline branch",
                     purpose="region_count catches a branch that VANISHED, not one that is "
                             "present and distorted",
                     required=False),
    InspectionTarget(target_id="layer_region:*", kind=TargetKind.LAYER_REGION,
                     label="near-wall layer coverage along the lumen",
                     purpose="a locally collapsed layer averages away in a global coverage "
                             "percentage",
                     required=False),
)
