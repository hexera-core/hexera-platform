# Responsibility: Declare the evidence a snap-grid (placed-parts) review needs, including the slices where an interface shows.
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
        purpose="every placed part's boundary, each under its own region name",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.volume",
        allowed_formats=(ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP),
        purpose="internal volume inspection - the interfaces between parts are internal surfaces",
        # OPTIONAL, as for the other multi-region engine: a valid job may ship no volume export.
        required=False,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(target_id="patch:*", kind=TargetKind.PATCH,
                     label="every renderable boundary patch",
                     purpose="each part and each opening must be the one the file declares",
                     required=True),
    InspectionTarget(target_id="region:*", kind=TargetKind.REGION,
                     label="every declared internal inspection slice",
                     purpose="a part kept as its own region and two parts merged into one look "
                             "identical in aggregate metrics; the slice is where they differ",
                     required=True),
)
