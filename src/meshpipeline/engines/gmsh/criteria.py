# Responsibility: Declare the measurable bars and review axes a Gmsh mesh is judged against.
# Boundaries: this engine's contribution only.
from __future__ import annotations

from meshpipeline.contracts.review_evidence import (
    MetricRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    TargetKind,
)
from meshpipeline.engines.review_types import Criterion, ReviewAxis

_GMSH_DOC = "https://gmsh.info/doc/texinfo/gmsh.html"

CRITERIA_ROWS: tuple[Criterion, ...] = (
    Criterion(
            key="timed_out", label="Build completes within compute budget",
            op="==", threshold=False, gating=True,
            rationale=(
                "A mesh too fine to build within the budget cannot be reproduced "
                "or iterated; the fix is coarser sizing, not more time."),
            evidence_url=_GMSH_DOC,
        ),
        Criterion(
            key="rc", label="Gmsh driver exits cleanly", op="==", threshold=0, gating=True,
            rationale=(
                "A nonzero exit means Gmsh aborted mid-generation - the deck on "
                "disk is absent or partial, not a usable FEA mesh."),
            evidence_url=_GMSH_DOC,
        ),
        Criterion(
            key="fatal", label="No degenerate elements", op="empty", threshold=None, gating=True,
            rationale=(
                "Zero/negative-quality elements make the stiffness matrix "
                "singular or ill-conditioned; no FEA solver can assemble over them."),
            evidence_url=_GMSH_DOC,
        ),
        Criterion(
            key="min_sicn", label="Worst element clears the SICN quality floor",
            op=">", threshold=0.1, gating=True,
            rationale=(
                "SICN (signed inverse condition number) is Gmsh's standard "
                "element-quality measure (1 = ideal, 0 = degenerate); elements "
                "below ~0.1 are near-degenerate and corrupt FEA conditioning."),
            evidence_url=_GMSH_DOC,
        ),
        Criterion(
            key="sicn_low_fraction", label="Low-quality elements are localized",
            op="<=", threshold=0.02, gating=False,
            rationale=(
                "A small localized set of low-SICN elements near sharp features "
                "is normal; a widespread fraction means the sizing strategy is "
                "wrong for this geometry. Advisory."),
            evidence_url=_GMSH_DOC,
        ),
)

# The SEMANTIC review layer - mesh-class concerns a metric report cannot settle by
# threshold alone (they need the group summary, the brief, and quality interpretation).
# The gmsh review is HYBRID: measured metrics carry element quality, and the render lane
# serves exactly two visual evidence classes - the iso overview and named-group isolation
# (toggle_patch). There are NO interior slices on this engine (its deliverable deck is not
# sliceable), so no axis here may demand region/slice evidence: every `requires` below must
# stay satisfiable by the opening view, a group inspection, or a metric finalize measures.
REVIEW_AXES: tuple[ReviewAxis, ...] = (
    ReviewAxis(
        name="group_completeness", validation_axis="conformance",
        guidance=("Check that every contracted region group is present in the deck and "
                  "that no boundary surface was left unassigned - the mechanical "
                  "completeness of the named-group decomposition."),
        concern="A named group you need for boundary conditions is missing from the deck",
        failure_signals=("a requested group missing from the region summary",
                         "boundary surfaces left unassigned or lumped into the default group",
                         "a group present but with no faces"),
        evidence=("groups", "region_summary", "brief"),
        evidence_url=_GMSH_DOC,
        # A named group's PLACEMENT - did the load face land on the right surface - is a spatial
        # fact no summary count can prove. Hybrid: the opening view plus inspection of the group
        # targets. (TargetKind.GROUP, never patch - this is Gmsh's target vocabulary.)
        requires=(RenderViewRequirement("iso"),
                  RenderTargetRequirement(TargetKind.GROUP, "group:*")),
    ),
    ReviewAxis(
        name="element_order_appropriateness", validation_axis="conformance",
        guidance=("Check the delivered element order matches what the user asked for - "
                  "linear where they wanted linear, quadratic where they wanted curvature "
                  "captured. Judge against the brief, not a default preference."),
        concern="The element order does not suit the analysis you asked for",
        failure_signals=("user asked for second-order elements but the deck is first-order",
                         "curved geometry meshed with straight-sided elements against the request"),
        evidence=("element_order", "brief"),
        evidence_url=_GMSH_DOC,
    ),
    ReviewAxis(
        name="element_quality_distribution", validation_axis="quality",
        guidance=("Interpret the element-quality evidence: decide whether low-quality "
                  "elements are LOCALIZED to sharp features (normal) or WIDESPREAD (a "
                  "sizing problem the builder should fix), beyond the pass/fail floor the "
                  "criteria already checked."),
        concern="Distorted elements will degrade the accuracy of the solution",
        failure_signals=("low-quality elements spread across the body, not just at features",
                         "quality degradation that tracks the sizing strategy rather than geometry"),
        # "render" (the iso overview this axis requires), not the retired "render_not_available"
        # token: that stale hint told the reviewer no render existed while the axis's own
        # `requires` demanded one - a contradiction that invited hunting for other visual tools.
        evidence=("quality_metrics", "sicn_low_fraction", "render"),
        evidence_url=_GMSH_DOC,
        # The pass/fail floor is a metric; whether low-quality elements are LOCALISED or WIDESPREAD
        # is read from the mesh - metric plus overview render.
        requires=(MetricRequirement("sicn_low_fraction"),
                  RenderViewRequirement("iso")),
    ),
    ReviewAxis(
        name="deck_conformance", validation_axis="conformance",
        guidance=("Check the exported deck is complete and internally consistent with the "
                  "declared groups and requested format - a usable FEA input, not a partial "
                  "or malformed file."),
        concern="The deck will not load cleanly into the solver it was written for",
        failure_signals=("deck truncated or missing sections",
                         "groups present in the mesh but absent from the exported deck"),
        evidence=("deliverable", "groups"),
        evidence_url=_GMSH_DOC,
    ),
)
