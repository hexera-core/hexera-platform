# Responsibility: Declare the evidence a multi-region review needs, including the slices where an interface shows.
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
        purpose="the delivered boundary surface across all regions",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.volume",
        allowed_formats=(ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP),
        purpose="internal volume inspection and sectional review - the conformal interface between regions is an internal surface",
        # OPTIONAL, and that is a real state, not a shortfall: a valid job may ship no volume
        # export, and its surface review must still open and proceed. What is NOT ordinary is a
        # volume that was declared and is unsafe or malformed - that is refused, never quietly
        # folded into absence.
        required=False,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(target_id="patch:*", kind=TargetKind.PATCH,
                     label="every renderable boundary patch",
                     purpose="per-region boundaries must be the ones the case declares",
                     required=True),
    InspectionTarget(target_id="region:*", kind=TargetKind.REGION,
                     label="every declared internal inspection slice",
                     purpose="a conformal interface and a merged pair of regions look "
                             "identical in aggregate metrics; the slice is where they differ",
                     required=True),
)
