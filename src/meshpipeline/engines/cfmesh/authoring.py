# Responsibility: Own the cfMesh configuration palette the builder authors through, and reject anything outside it.
# Boundaries: the model chooses declared values.
from __future__ import annotations

from meshpipeline.engines.base import Diagnostic

AUTHORING_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "configure_mesh",
        "description": (
            "Render a VALID, budget-clamped cfMesh case (system/meshDict) FOR you from a "
            "high-level STRATEGY - you never hand-write meshDict. Patch names/types come from "
            "the contract automatically. Call this, then run_mesh; to iterate, change a strategy "
            "field and call again. All lengths in METRES."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain_margin": {"type": "object", "description": "far-field margins as multiples of body length L: {up,down,side,vert}"},
                "n_layers": {"type": "integer", "description": "near-wall boundary layers (0=none)"},
                "max_cell_factor": {"type": "number", "description": "background coarseness = largest_extent/factor (higher=finer; default 40, clamped to budget)"},
                "wall_cell": {"type": "number", "description": "wall-patch cell size in metres (default L/20)"},
                "thickness_ratio": {"type": "number", "description": "boundary-layer growth ratio (default 1.2)"},
                "first_layer_thickness": {"type": "number", "description": "first boundary-layer height in metres (optional)"},
                "features": {"type": "array", "description": "volume refinements (objectRefinements), each {name,type:box|sphere|cone|line,cellSize,+geometry}", "items": {"type": "object"}},
            },
            "required": [],
        },
    },
}

# Strategy knobs + the preamble-handled args (geometry/patch/domain) the tool reads.
_STRATEGY = {"domain_margin", "n_layers", "max_cell_factor", "wall_cell",
             "thickness_ratio", "first_layer_thickness", "features"}
_PREAMBLE = {"geometry_file", "domain_min", "domain_max", "wall_patch",
             "farfield_patch", "feature_angle", "mirror_y_half", "strategy"}
_KNOWN = _STRATEGY | _PREAMBLE
_MARGIN_KEYS = {"up", "down", "side", "vert"}
_FEATURE_TYPES = {"box", "sphere", "cone", "line"}
# snappy knobs a confused model might send to cfMesh - name them so the redirect helps
_SNAPPY_ONLY = {"surface_level", "feature_level", "first_layer_rel", "quality", "max_cells"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(strategy: dict) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    if not isinstance(strategy, dict):
        return [Diagnostic("error", "", "configure_mesh args must be an object")]
    for k in sorted(set(strategy) - _KNOWN):
        if k in _SNAPPY_ONLY:
            d.append(Diagnostic("error", k, f"{k!r} is a snappy knob, not cfMesh - use the cfMesh "
                                             f"palette ({sorted(_STRATEGY)})"))
        else:
            d.append(Diagnostic("error", k, f"unknown cfMesh strategy key {k!r} - allowed: {sorted(_STRATEGY)}"))
    if "n_layers" in strategy and (not isinstance(strategy["n_layers"], int)
                                   or isinstance(strategy["n_layers"], bool) or strategy["n_layers"] < 0):
        d.append(Diagnostic("error", "n_layers", "n_layers must be an integer >= 0"))
    for k in ("max_cell_factor", "wall_cell", "first_layer_thickness"):
        if k in strategy and not (_num(strategy[k]) and strategy[k] > 0):
            d.append(Diagnostic("error", k, f"{k} must be a positive number"))
    if "thickness_ratio" in strategy and not (_num(strategy["thickness_ratio"]) and strategy["thickness_ratio"] >= 1.0):
        d.append(Diagnostic("error", "thickness_ratio", "thickness_ratio must be a number >= 1.0"))
    if "domain_margin" in strategy:
        dm = strategy["domain_margin"]
        if not isinstance(dm, dict):
            d.append(Diagnostic("error", "domain_margin", "domain_margin must be an object {up,down,side,vert}"))
        else:
            for mk in sorted(set(dm) - _MARGIN_KEYS):
                d.append(Diagnostic("error", f"domain_margin.{mk}", f"unknown margin {mk!r} - only {sorted(_MARGIN_KEYS)}"))
            for mk, mv in dm.items():
                if mk in _MARGIN_KEYS and not (_num(mv) and mv >= 0):
                    d.append(Diagnostic("error", f"domain_margin.{mk}", f"{mk} margin must be a number >= 0"))
    if "features" in strategy:
        feats = strategy["features"]
        if not isinstance(feats, list):
            d.append(Diagnostic("error", "features", "features must be a list of refinement objects"))
        else:
            for i, f in enumerate(feats):
                if not isinstance(f, dict):
                    d.append(Diagnostic("error", f"features[{i}]", "each feature must be an object {name,type,cellSize,…}"))
                    continue
                if str(f.get("type", "")).lower() not in _FEATURE_TYPES:
                    d.append(Diagnostic("error", f"features[{i}].type", f"type must be one of {sorted(_FEATURE_TYPES)}"))
                if not (_num(f.get("cellSize")) and f.get("cellSize", 0) > 0):
                    d.append(Diagnostic("error", f"features[{i}].cellSize", "cellSize must be a positive number"))
    return d


#: ENGINE-OWNED mesh-detail mapping. cfMesh reads SIZES, so the tier is a size multiplier:
#: >1 coarsens, <1 refines. Every value is a SOFT RECOMMENDATION shown to the builder - the
#: executor's cell-budget gate and CELL_HARD_LIMIT remain the authority, and no tier is a promised
#: cell count. Classification: size_factor = soft recommendation; n_layers_hint = soft recommendation.
_FIDELITY_SIZES = {
    "draft":    {"size_factor": 1.6, "n_layers_hint": 0},
    "standard": {"size_factor": 1.0, "n_layers_hint": 2},
    "max":      {"size_factor": 0.7, "n_layers_hint": 4},
}


def recommend(analysis: dict, *, fidelity: str = "standard") -> dict:
    from meshpipeline.cad.analysis import recommend_refinement
    _f = _FIDELITY_SIZES.get(str(fidelity or "standard"), _FIDELITY_SIZES["standard"])
    r = recommend_refinement(analysis)
    base = r["base_cell"] * _f["size_factor"]
    feature = base / (2 ** r["feature_level"])
    return {"mesh_detail_preference": str(fidelity or "standard"),
            "n_layers_hint": _f["n_layers_hint"],
            "cell_sizes_m": {"base": round(base, 6), "feature": round(feature, 6)},
            "note": ("Set the background coarseness (max_cell_factor) so the largest cell ≈ "
                     "base, and add localRefinement/features at ≈ feature size for edges and "
                     "thin regions.")}
