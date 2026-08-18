# Responsibility: Own the snappyHexMesh palette the builder authors through, and reject anything outside it.
# Boundaries: the model chooses declared values.
from __future__ import annotations

from meshpipeline.engines.base import Diagnostic

AUTHORING_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "configure_mesh",
        "description": (
            "Render a VALID, budget-clamped snappyHexMesh case (system/blockMeshDict + "
            "snappyHexMeshDict) FOR you from a high-level STRATEGY - you never hand-write the "
            "dicts. Patch names/types come from the contract automatically. Call this, then "
            "run_mesh; to iterate, change a strategy field and call again. All lengths in METRES."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain_margin": {"type": "object", "description": "far-field margins as multiples of body length L: {up,down,side,vert}"},
                "n_layers": {"type": "integer", "description": "near-wall prism layers (0=none)"},
                "first_layer_rel": {"type": "number", "description": "outer layer thickness vs local cell (default 0.35)"},
                "quality": {"type": "string", "description": "'balanced' (default) or 'strict' (checkMesh skew<4) - use 'strict' when max_skewness>4"},
                "surface_level": {"type": "array", "items": {"type": "integer"}, "description": "[min,max] surface refinement (clamped to budget)"},
                "feature_level": {"type": "integer", "description": "sharp-edge eMesh level (clamped to budget)"},
                "max_cells": {"type": "integer", "description": "cell budget (default 8e6)"},
            },
            "required": [],
        },
    },
}

_STRATEGY = {"domain_margin", "n_layers", "first_layer_rel", "quality",
             "surface_level", "feature_level", "max_cells"}
_PREAMBLE = {"geometry_file", "domain_min", "domain_max", "wall_patch",
             "farfield_patch", "feature_angle", "strategy"}
_KNOWN = _STRATEGY | _PREAMBLE
_MARGIN_KEYS = {"up", "down", "side", "vert"}
_QUALITY = {"balanced", "strict"}
# cfMesh knobs a confused model might send to snappy - name them so the redirect helps
_CFMESH_ONLY = {"wall_cell", "max_cell_factor", "thickness_ratio", "first_layer_thickness", "features"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(strategy: dict) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    if not isinstance(strategy, dict):
        return [Diagnostic("error", "", "configure_mesh args must be an object")]
    for k in sorted(set(strategy) - _KNOWN):
        if k in _CFMESH_ONLY:
            d.append(Diagnostic("error", k, f"{k!r} is a cfMesh knob, not snappy - use the snappy "
                                             f"palette ({sorted(_STRATEGY)})"))
        else:
            d.append(Diagnostic("error", k, f"unknown snappy strategy key {k!r} - allowed: {sorted(_STRATEGY)}"))
    if "n_layers" in strategy and (not _int(strategy["n_layers"]) or strategy["n_layers"] < 0):
        d.append(Diagnostic("error", "n_layers", "n_layers must be an integer >= 0"))
    if "feature_level" in strategy and (not _int(strategy["feature_level"]) or strategy["feature_level"] < 0):
        d.append(Diagnostic("error", "feature_level", "feature_level must be an integer >= 0"))
    if "max_cells" in strategy and (not _int(strategy["max_cells"]) or strategy["max_cells"] <= 0):
        d.append(Diagnostic("error", "max_cells", "max_cells must be a positive integer"))
    if "first_layer_rel" in strategy and not (_num(strategy["first_layer_rel"]) and strategy["first_layer_rel"] > 0):
        d.append(Diagnostic("error", "first_layer_rel", "first_layer_rel must be a positive number"))
    if "quality" in strategy and strategy["quality"] not in _QUALITY:
        d.append(Diagnostic("error", "quality", f"quality must be one of {sorted(_QUALITY)}"))
    if "surface_level" in strategy:
        sl = strategy["surface_level"]
        if not (isinstance(sl, list) and len(sl) == 2 and all(_int(x) and x >= 0 for x in sl)):
            d.append(Diagnostic("error", "surface_level", "surface_level must be [min,max], two integers >= 0"))
        elif sl[1] < sl[0]:
            d.append(Diagnostic("error", "surface_level[1]", "surface_level max must be >= min"))
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
    return d


#: ENGINE-OWNED mesh-detail mapping. snappy reads LEVELS, so the tier is a level DELTA (each level
#: is roughly an 8x cell multiplier, so the range is deliberately narrow) plus a layer hint.
#: Classification: level_delta = soft recommendation; n_layers_hint = soft recommendation. Levels are
#: floored at 0 and the resulting cell count is still bounded by clamp_cell_budget/CELL_HARD_LIMIT.
_FIDELITY_LEVELS = {
    "draft":    {"level_delta": -1, "n_layers_hint": 0},
    "standard": {"level_delta":  0, "n_layers_hint": 3},
    "max":      {"level_delta": +1, "n_layers_hint": 5},
}


def recommend(analysis: dict, *, fidelity: str = "standard") -> dict:
    from meshpipeline.cad.analysis import recommend_refinement
    _f = _FIDELITY_LEVELS.get(str(fidelity or "standard"), _FIDELITY_LEVELS["standard"])
    _d = _f["level_delta"]
    r = recommend_refinement(analysis)
    return {
        "mesh_detail_preference": str(fidelity or "standard"),
        "n_layers_hint": _f["n_layers_hint"],
        "surface_level": [max(0, int(v) + _d) for v in r["surface_level"]],
        "feature_level": max(0, int(r["feature_level"]) + _d),   # USE THIS - fits the budget
        "feature_level_true": r["feature_level_true"],  # what full resolution wants
        "budget_capped": r["budget_capped"],
        "distance_bands": r["distance_bands"],
        "resolve_feature_angle": r["resolve_feature_angle"],
        "note": ("Use feature_level (it fits the cell budget). Do NOT exceed it - a higher "
                 "level explodes the cell count and the run fails. feature_level_true is only "
                 "what full resolution would want; the budget caps it on purpose."),
    }
