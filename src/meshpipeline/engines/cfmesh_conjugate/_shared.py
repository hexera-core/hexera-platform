# Responsibility: Declare the evidence a conjugate review needs: the artifacts that must render and the targets to walk.
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
        purpose="the two regions' delivered boundaries, each under its own region name - the metal "
                "box and the wetted channel wall",
        required=True,
    ),
    RenderArtifactRequirement(
        artifact_key="mesh_paths.volume",
        allowed_formats=(ArtifactFormat.VTK_VTU, ArtifactFormat.VTK_VTP),
        purpose="the coolant region's cells - a conformal split and one merged block look alike in "
                "every number checkMesh prints, and the slice through the coolant is where they "
                "differ",
        # OPTIONAL AS AN ARTIFACT, REQUIRED AS EVIDENCE, and those are two different
        # statements. Optional here means the RESOLVER does not raise EVIDENCE_MISSING on it:
        # foam_exec.export_volume_vtk returns None rather than failing a mesh over an export, and
        # raising at resolution "ended the review before the model was asked
        # (EXPECTED_TARGET_MISSING) on a mesh that passed every gate".
        #
        # What it used to ALSO mean, and no longer does, is that an absent volume waived the
        # region obligation. base.py waived it for every engine off the manifest string, so a
        # conjugate run whose fluid export went missing was reviewed on its outside and could
        # pass - the same shape as job 575f76e6, reached a different way. This bundle now
        # declares `review_requires_the_interior=True` (spec.py), so the obligation stands, the
        # session cannot offer a region target without a volume, and the review says the evidence
        # is missing. The mesh is still delivered; the review is inconclusive, which is honest.
        required=False,
    ),
)

_INSPECTION_TARGETS = (
    InspectionTarget(target_id="patch:*", kind=TargetKind.PATCH,
                     label="every renderable boundary patch",
                     purpose="the coolant surface and the metal surface must each be the one the "
                             "part declares, and the ports must be where the user said",
                     required=True),
    InspectionTarget(target_id="region:*", kind=TargetKind.REGION,
                     label="every declared internal inspection slice",
                     purpose="a conformal split and a single merged block print the same numbers; "
                             "a slice through the coolant is where the two differ",
                     required=True),
)
