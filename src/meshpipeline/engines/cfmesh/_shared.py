# Responsibility: Declare the evidence a cfMesh review needs: the artifacts that must render and the targets to walk.
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
        purpose="the delivered boundary surface - what the reviewer walks",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.volume",
        allowed_formats=(ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP),
        purpose="internal volume inspection and sectional review - cut through the fill to see layer collapse and internal refinement",
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
                     purpose="surface fidelity and patch correctness are per-patch judgements",
                     required=True),
    InspectionTarget(target_id="region:*", kind=TargetKind.REGION,
                     label="every declared internal inspection slice",
                     purpose="boundary-layer quality and local defects are internal",
                     required=True),
)
