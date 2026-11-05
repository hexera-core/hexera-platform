# Responsibility: Own the Gmsh configuration palette the builder authors through, and reject anything outside it.
# Boundaries: the model chooses declared values.
from __future__ import annotations

#: tier → recommended size factor multiplier (>1 coarsens, <1 refines) and a curvature hint.
#: Classification: size_factor_multiplier = soft recommendation; curvature_hint = soft
#: recommendation; element_order = UNSUPPORTED/no-op by deliberate policy (see module docstring).
_FIDELITY_GMSH = {
    "draft":    {"size_factor_multiplier": 1.6, "curvature_hint": 0},
    "standard": {"size_factor_multiplier": 1.0, "curvature_hint": 12},
    "max":      {"size_factor_multiplier": 0.7, "curvature_hint": 24},
}


def recommend(analysis: dict, *, fidelity: str = "standard") -> dict:
    from meshpipeline.cad.analysis import recommend_refinement
    f = _FIDELITY_GMSH.get(str(fidelity or "standard"), _FIDELITY_GMSH["standard"])
    r = recommend_refinement(analysis)
    base = float(r["base_cell"]) * f["size_factor_multiplier"]
    return {
        "mesh_detail_preference": str(fidelity or "standard"),
        "target_element_size_m": round(base, 6),
        "curvature_points_per_2pi_hint": f["curvature_hint"],
        "element_order": "unchanged",     # NEVER derived from the detail preference
        "note": ("gmsh sizes from a factor of the part diagonal: set size.value so the largest "
                 "element is about target_element_size_m. The detail preference only shifts that "
                 "recommendation - it is not an element-count promise, it cannot raise the element "
                 "ceiling, and it never changes element_order (that is the user's analysis choice)."),
    }
