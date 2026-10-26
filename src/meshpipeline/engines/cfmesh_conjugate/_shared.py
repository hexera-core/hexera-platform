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
        # OPTIONAL, and that is a real state rather than a shortfall - the same reading every
        # other bundle takes. A volume export is a review aid: foam_exec.export_volume_vtk returns
        # None rather than failing a mesh over one, and base.py drops the region obligations when
        # the manifest publishes no volume, with its own comment recording why - demanding one
        # "ended the review before the model was asked (EXPECTED_TARGET_MISSING) on a mesh that
        # passed every gate". Required here would re-introduce exactly that.
        #
        # So the floor is made real the other way round: finalize publishes the per-region volume
        # on every run, and then the region slices materialise because the volume is THERE, not
        # because the declaration insisted on it.
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
