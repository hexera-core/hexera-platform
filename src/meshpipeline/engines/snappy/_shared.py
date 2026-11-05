# Responsibility: Declare the evidence a snappyHexMesh review needs: the artifacts to render and the targets to walk.
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
        purpose="the delivered boundary surface - what the reviewer actually walks",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.volume",
        allowed_formats=(ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP),
        purpose="internal volume inspection and sectional review - the boundary layers and refinement regions are only judgeable in section",
        # OPTIONAL, and that is a real state, not a shortfall: a valid job may ship no volume
        # export, and its surface review must still open and proceed. What is NOT ordinary is a
        # volume that was declared and is unsafe or malformed - that is refused, never quietly
        # folded into absence.
        required=False,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(
        target_id="patch:*", kind=TargetKind.PATCH,
        label="every renderable boundary patch",
        purpose="a patch nobody isolated is a patch nobody checked for capture or layers",
        required=True,
    ),
    InspectionTarget(
        target_id="region:*", kind=TargetKind.REGION,
        label="every declared internal inspection slice",
        purpose="internal cell quality and layer collapse are invisible from outside",
        required=True,
    ),
)
