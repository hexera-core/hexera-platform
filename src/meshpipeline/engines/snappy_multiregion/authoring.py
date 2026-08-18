# Responsibility: Own the multi-region palette the builder authors through, and reject anything outside it.
# Boundaries: the model chooses declared values.
from __future__ import annotations

from meshpipeline.engines.base import Diagnostic

AUTHORING_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "configure_mesh",
        "description": (
            "Render a VALID, budget-clamped MULTI-REGION snappyHexMesh multi-region case FOR you from a "
            "high-level STRATEGY - you never hand-write the dicts. You MUST supply `regions`: map "
            "each solid from geometry_report (by its integer index) onto a named region tagged "
            "'fluid' or 'solid' (a coupled case needs >=1 fluid and >=1 solid). The fluid-solid "
            "interfaces are created automatically by the region split. Call this, then run_mesh; to "
            "iterate, change a strategy field and call again. All lengths in METRES."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "regions": {
                    "type": "array",
                    "description": ("REQUIRED. One entry per region: "
                                    "{name, type:'fluid'|'solid', solids:[<geometry_report indices>]}. "
                                    "Every assembly solid must be assigned to exactly one region."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "region name (OpenFOAM-safe, e.g. 'fluid','plate')"},
                            "type": {"type": "string", "description": "'fluid' or 'solid'"},
                            "solids": {"type": "array", "items": {"type": "integer"},
                                       "description": "geometry_report solid indices in this region"},
                        },
                        "required": ["name", "type", "solids"],
                    },
                },
                "surface_level": {"type": "array", "items": {"type": "integer"}, "description": "[min,max] surface refinement (clamped to budget)"},
                "interface_refinement": {"type": "integer", "description": "extra refinement levels at fluid-solid interfaces (0-2)"},
                "n_layers": {"type": "integer", "description": "prism layers on the FLUID-side walls (0=none)"},
                "first_layer_rel": {"type": "number", "description": "outer layer thickness vs local cell (default 0.35)"},
                "quality": {"type": "string", "description": "'balanced' (default) or 'strict' (checkMesh skew<4)"},
                "max_cells": {"type": "integer", "description": "cell budget for the background mesh (default 6e6)"},
                "region_refinement": {"type": "object", "description": "per-region [min,max] surface-level OVERRIDE, e.g. {\"fasteners\": [5, 6]} - the scale-aware handle for multi-scale assemblies: refine the region holding tiny solids locally (see geometry_report per_solid_scale.needed_level) instead of raising the global surface_level"},
            },
            "required": ["regions"],
        },
    },
}

_STRATEGY = {"regions", "surface_level", "interface_refinement", "n_layers",
             "first_layer_rel", "quality", "max_cells", "region_refinement"}
_PREAMBLE = {"geometry_file", "wall_patch", "feature_angle", "strategy", "fluid_topology"}
_KNOWN = _STRATEGY | _PREAMBLE
_QUALITY = {"balanced", "strict"}
_REGION_TYPES = {"fluid", "solid"}
# snappy (single-region) knobs a confused model might send - name them so the redirect helps
_SNAPPY_ONLY = {"domain_margin", "feature_level"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_regions(regions) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    if not isinstance(regions, list) or not regions:
        return [Diagnostic("error", "regions", "regions must be a non-empty array of "
                                               "{name, type:'fluid'|'solid', solids:[int]}")]
    names: list[str] = []
    solids_seen: dict[int, str] = {}
    n_fluid = n_solid = 0
    for i, r in enumerate(regions):
        if not isinstance(r, dict):
            d.append(Diagnostic("error", f"regions[{i}]", "each region must be an object"))
            continue
        name = str(r.get("name", "")).strip()
        rtype = str(r.get("type", "")).strip().lower()
        solids = r.get("solids")
        if not name:
            d.append(Diagnostic("error", f"regions[{i}].name", "region name is required"))
        elif name in names:
            d.append(Diagnostic("error", f"regions[{i}].name", f"duplicate region name {name!r}"))
        else:
            names.append(name)
        if rtype not in _REGION_TYPES:
            d.append(Diagnostic("error", f"regions[{i}].type", f"type must be one of {sorted(_REGION_TYPES)}"))
        else:
            n_fluid += rtype == "fluid"
            n_solid += rtype == "solid"
        if not (isinstance(solids, list) and solids and all(_int(s) for s in solids)):
            d.append(Diagnostic("error", f"regions[{i}].solids", "solids must be a non-empty array of integer "
                                                                 "solid indices (from geometry_report)"))
        else:
            for s in solids:
                if s in solids_seen:
                    d.append(Diagnostic("error", f"regions[{i}].solids",
                                        f"solid {s} already assigned to region {solids_seen[s]!r} - "
                                        "each solid belongs to exactly one region"))
                else:
                    solids_seen[s] = name
    if n_fluid == 0:
        d.append(Diagnostic("error", "regions", "a coupled case needs at least one 'fluid' region"))
    if n_solid == 0:
        d.append(Diagnostic("error", "regions", "a coupled case needs at least one 'solid' region "
                                                "(otherwise use snappy, not snappy_multiregion)"))
    return d


def validate(strategy: dict) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    if not isinstance(strategy, dict):
        return [Diagnostic("error", "", "configure_mesh args must be an object")]
    for k in sorted(set(strategy) - _KNOWN):
        if k in _SNAPPY_ONLY:
            d.append(Diagnostic("error", k, f"{k!r} is a single-region snappy knob - snappy_multiregion derives the "
                                            f"domain from the assembly; use the multi-region palette ({sorted(_STRATEGY)})"))
        else:
            d.append(Diagnostic("error", k, f"unknown snappy_multiregion strategy key {k!r} - allowed: {sorted(_STRATEGY)}"))
    # regions is required and is the multi-region-specific contract
    if "regions" not in strategy:
        d.append(Diagnostic("error", "regions", "regions is required - map each assembly solid onto a "
                                                "named fluid/solid region"))
    else:
        d.extend(_validate_regions(strategy["regions"]))
    if "n_layers" in strategy and (not _int(strategy["n_layers"]) or strategy["n_layers"] < 0):
        d.append(Diagnostic("error", "n_layers", "n_layers must be an integer >= 0"))
    if "interface_refinement" in strategy and (not _int(strategy["interface_refinement"])
                                               or not 0 <= strategy["interface_refinement"] <= 3):
        d.append(Diagnostic("error", "interface_refinement", "interface_refinement must be an integer in 0..3"))
    if "max_cells" in strategy and (not _int(strategy["max_cells"]) or strategy["max_cells"] <= 0):
        d.append(Diagnostic("error", "max_cells", "max_cells must be a positive integer"))
    if "first_layer_rel" in strategy and not (_num(strategy["first_layer_rel"]) and strategy["first_layer_rel"] > 0):
        d.append(Diagnostic("error", "first_layer_rel", "first_layer_rel must be a positive number"))
    if "quality" in strategy and strategy["quality"] not in _QUALITY:
        d.append(Diagnostic("error", "quality", f"quality must be one of {sorted(_QUALITY)}"))
    if "region_refinement" in strategy:
        rr = strategy["region_refinement"]
        _declared = {str(r.get("name", "")).strip() for r in strategy.get("regions") or []
                     if isinstance(r, dict)}
        if not isinstance(rr, dict):
            d.append(Diagnostic("error", "region_refinement",
                                "region_refinement must be an object {region_name: [min,max]}"))
        else:
            for rn, lv in rr.items():
                if _declared and rn not in _declared:
                    d.append(Diagnostic("error", f"region_refinement.{rn}",
                                        f"unknown region {rn!r} - keys must be declared region names"))
                if not (isinstance(lv, list) and len(lv) == 2
                        and all(_int(x) and 0 <= x <= 7 for x in lv)) or (lv[1] < lv[0]
                        if isinstance(lv, list) and len(lv) == 2 and all(_int(x) for x in lv) else False):
                    d.append(Diagnostic("error", f"region_refinement.{rn}",
                                        "levels must be [min,max], integers 0..7, max >= min"))
    if "surface_level" in strategy:
        sl = strategy["surface_level"]
        if not (isinstance(sl, list) and len(sl) == 2 and all(_int(x) and x >= 0 for x in sl)):
            d.append(Diagnostic("error", "surface_level", "surface_level must be [min,max], two integers >= 0"))
        elif sl[1] < sl[0]:
            d.append(Diagnostic("error", "surface_level[1]", "surface_level max must be >= min"))
    return d


#: ENGINE-OWNED mesh-detail mapping. A split mesh multiplies work across regions, so the deltas are
#: deliberately conservative. Classification: level_delta / interface_refinement / n_layers = soft
#: recommendations; the cell budget stays bounded by clamp_cell_budget + CELL_HARD_LIMIT.
_FIDELITY_MULTIREGION = {
    "draft":    {"level_delta": -1, "interface_refinement": 0, "n_layers": 0},
    "standard": {"level_delta":  0, "interface_refinement": 1, "n_layers": 3},
    "max":      {"level_delta": +1, "interface_refinement": 2, "n_layers": 5},
}


def recommend(analysis: dict, *, fidelity: str = "standard") -> dict:
    from meshpipeline.cad.analysis import recommend_refinement
    _f = _FIDELITY_MULTIREGION.get(str(fidelity or "standard"), _FIDELITY_MULTIREGION["standard"])
    r = recommend_refinement(analysis)
    return {
        "mesh_detail_preference": str(fidelity or "standard"),
        "surface_level": [max(0, int(v) + _f["level_delta"]) for v in r["surface_level"]],
        "interface_refinement": _f["interface_refinement"],
        "n_layers": _f["n_layers"],
        "budget_capped": r["budget_capped"],
        "note": ("Assign every solid from geometry_report to a fluid or solid region first. Use "
                 "surface_level for the background refinement; interface_refinement adds levels at "
                 "the fluid-solid interfaces where the coupling gradient concentrates. Keep it within "
                 "the cell budget - a finer background mesh multiplied across regions explodes fast."),
    }
