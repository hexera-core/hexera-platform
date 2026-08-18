# Responsibility: Render the views a reviewer needs to judge a cfMesh mesh.
# Boundaries: it renders artifacts the review session already resolved and confined.
# Collaborates with: sandbox/render_adapter.py and sandbox/review_session.py.
from __future__ import annotations

from collections.abc import Mapping

from meshpipeline.contracts.review_evidence import (
    RenderContext,
    ResolvedArtifact,
    ReviewRenderSession,
)
from meshpipeline.sandbox.render_adapter import (
    BackendRenderSession,
    build_backend,
    expand_patch_targets,
    expand_region_targets,
)


class CfmeshReviewRenderer:

    def required_artifacts(self):
        from meshpipeline.engines.cfmesh._shared import _RENDER_ARTIFACTS
        return _RENDER_ARTIFACTS

    def open(self, context: RenderContext,
             artifacts: Mapping[str, ResolvedArtifact]) -> ReviewRenderSession:
        backend, inputs, regions = build_backend(context, artifacts)

        # THE FLOOR IS THE SPEC'S, expanded against what actually loaded. The wildcard targets
        # (`patch:*`, `region:*`) say every patch and every declared slice must be looked at;
        # this turns them into the concrete list for THIS mesh. Reading `required` from the
        # spec - rather than deciding it here - is what keeps the policy in the declaration.
        from meshpipeline.engines.cfmesh._shared import _INSPECTION_TARGETS
        declared = {t.target_id: t for t in _INSPECTION_TARGETS}
        patch_rule = declared.get("patch:*")
        region_rule = declared.get("region:*")

        # From the LOADED SCENE, never from the manifest: a manifest may name patches no geometry
        # reaches, and requiring those made the coverage floor unsatisfiable.
        patch_targets = expand_patch_targets(
            backend.patch_names(),
            required=bool(patch_rule and patch_rule.required),
            purpose=(patch_rule.purpose if patch_rule else "inspect the patch directly"),
        )
        region_targets = expand_region_targets(
            regions,
            required=bool(region_rule and region_rule.required),
            purpose=(region_rule.purpose if region_rule else "reveal internal structure"),
            volume_available=inputs.has_volume,
        )
        return BackendRenderSession(
            backend, inputs=inputs, regions=regions,
            patch_targets=patch_targets, region_targets=region_targets,
            opening_purpose="the wrapped boundary as delivered, before the reviewer moves anything",
        )


RENDERER = CfmeshReviewRenderer()
