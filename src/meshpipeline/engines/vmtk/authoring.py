# Responsibility: Own the VMTK configuration palette the builder authors through, and reject anything outside it.
# Boundaries: the model chooses declared values.
from __future__ import annotations

import math

from meshpipeline.engines.base import Diagnostic

AUTHORING_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "configure_mesh",
        "description": (
            "Render a VALID, budget-clamped vmtk meshing spec FOR you from a high-level STRATEGY "
            "- you never hand-write a vmtk pype. The pipeline is fixed (centerlines -> "
            "radius-adaptive surface remesh -> tetrahedral volume mesh, optional boundary layers); "
            "you choose the STRATEGY. Patch names come from the contract automatically. Call this, "
            "then run_mesh; to iterate, change a strategy field and call again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edge_length_factor": {
                    "type": "number",
                    "description": ("target edge length as a FRACTION OF THE LOCAL RADIUS "
                                    "(greater than 0, at most 1.0; 0.3 is the default and vmtk's own "
                                    "published example). Smaller = finer everywhere, scaled to each "
                                    "branch's radius. This is vmtk's defining knob."),
                },
                "source_ids": {"type": "array", "items": {"type": "integer"},
                               "description": ("OPEN-PROFILE ids (from geometry_report) that seed the "
                                               "centerlines at the INLET end. Use with target_ids.")},
                "target_ids": {"type": "array", "items": {"type": "integer"},
                               "description": "open-profile ids at the OUTLET end(s)"},
                "source_points": {"type": "array", "items": {"type": "number"},
                                  "description": ("explicit inlet seed coordinates [x,y,z,...]. Use these "
                                                  "when the lumen is CLOSED (no open profiles to select).")},
                "target_points": {"type": "array", "items": {"type": "number"},
                                  "description": "explicit outlet seed coordinates [x,y,z,...]"},
                "boundary_layers": {"type": "integer", "description": "near-wall prism layers inside the lumen wall (0=none)"},
                "boundary_layer_thickness_factor": {"type": "number", "description": "total layer thickness vs local radius (default 0.2)"},
                "cap_openings": {"type": "boolean", "description": "cap the open profiles at the lumen ends into inlet/outlet patches (default true; set false when the lumen is already closed)"},
                "remesh_surface": {"type": "boolean", "description": "radius-adaptive surface remesh before the volume fill (default true)"},
                "max_cells": {"type": "integer", "description": "cell budget (default 4e6)"},
                "min_edge_length": {"type": "number",
                                    "description": "ENGINE-STAGED (metres): floor on the radius-adaptive cell size"},
                "max_edge_length": {"type": "number",
                                    "description": "ENGINE-STAGED (metres): ceiling on the radius-adaptive cell size"},
            },
            "required": [],
        },
    },
}

_STRATEGY = {"edge_length_factor", "boundary_layers", "boundary_layer_thickness_factor",
             "cap_openings", "remesh_surface", "max_cells",
             "source_ids", "target_ids", "source_points", "target_points",
             "min_edge_length", "max_edge_length", "sizing_array", "generator_remesh"}
_PREAMBLE = {"geometry_file", "wall_patch", "strategy", "wall_layers"}
_KNOWN = _STRATEGY | _PREAMBLE
# knobs from the OpenFOAM engines a confused model might send - name them so the redirect helps
_FOAM_ONLY = {"surface_level", "feature_level", "domain_margin", "n_layers", "max_cell_factor",
              "first_layer_rel", "wall_cell", "quality", "regions"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(strategy: dict) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    if not isinstance(strategy, dict):
        return [Diagnostic("error", "", "configure_mesh args must be an object")]
    for k in sorted(set(strategy) - _KNOWN):
        if k in _FOAM_ONLY:
            d.append(Diagnostic("error", k, f"{k!r} is an OpenFOAM-mesher knob, not vmtk - vmtk sizes "
                                            f"cells from the centerline radius; use the vmtk palette "
                                            f"({sorted(_STRATEGY)})"))
        else:
            d.append(Diagnostic("error", k, f"unknown vmtk strategy key {k!r} - allowed: {sorted(_STRATEGY)}"))
    if "edge_length_factor" in strategy:
        e = strategy["edge_length_factor"]
        # WHAT VMTK ITSELF REQUIRES is a positive number - `-edgelengthfactor` is a multiplier on
        # the local radius, and upstream's own published example uses 0.3. So the hard rules here
        # are finite and greater than zero, and nothing narrower is presented as invalid input.
        # A very small factor is legal and expensive rather than wrong: cell count grows roughly
        # as 1/e^3, and the run is already bounded by `max_cells` and the engine's cell budget.
        # It is reported as a warning so an expert can still ask for it. A factor above 1.0 means
        # edges longer than the local vessel radius, which cannot resolve the lumen at all.
        if not _num(e) or not math.isfinite(float(e)) or float(e) <= 0:
            d.append(Diagnostic("error", "edge_length_factor",
                                "edge_length_factor must be a finite number greater than zero - "
                                "it multiplies the local centerline radius"))
        elif float(e) > 1.0:
            d.append(Diagnostic("error", "edge_length_factor",
                                "edge_length_factor must not exceed 1.0 - an edge longer than the "
                                "local radius cannot resolve the lumen"))
        elif float(e) < 0.05:
            d.append(Diagnostic("warning", "edge_length_factor",
                                f"edge_length_factor {e} is very fine; cell count grows roughly as "
                                "1/factor^3 and the run may reach its cell budget"))
    if "boundary_layers" in strategy and (not _int(strategy["boundary_layers"])
                                          or not 0 <= strategy["boundary_layers"] <= 10):
        d.append(Diagnostic("error", "boundary_layers", "boundary_layers must be an integer in 0..10"))
    if "boundary_layer_thickness_factor" in strategy:
        t = strategy["boundary_layer_thickness_factor"]
        if not (_num(t) and 0.0 < t < 1.0):
            d.append(Diagnostic("error", "boundary_layer_thickness_factor",
                                "boundary_layer_thickness_factor must be a number in (0,1) - a fraction of the local radius"))
        elif float(t) < 0.05:
            # the first sweep job web-searched its way to 0.018: the layer tets came out with a
            # scaled Jacobian of 0.001 - slivers a solver will feel; the default resolves the wall
            d.append(Diagnostic("warning", "boundary_layer_thickness_factor",
                                f"boundary_layer_thickness_factor {t} is very thin - below 0.05 the "
                                "layer tets are slivers (scaled Jacobian ~0.001). Keep the default "
                                "0.2 unless the brief asks for a specific first-cell height."))
    for b in ("cap_openings", "remesh_surface", "generator_remesh"):
        # ECHO WHAT ARRIVED. A live run sent the STRING "false", read the bare
        # "must be true or false" as a validator bug, retried the same string three times,
        # then capitulated to `true` - the wrong answer for a closed lumen. A diagnostic
        # that cannot be distinguished from a bug in the tool is not a diagnostic.
        if b in strategy and not isinstance(strategy[b], bool):
            got = strategy[b]
            d.append(Diagnostic("error", b, (
                f"{b} must be the JSON boolean true or false - received {got!r} "
                f"({type(got).__name__}). Send {b}: false, not \"false\".")))
    if "max_cells" in strategy and (not _int(strategy["max_cells"]) or strategy["max_cells"] <= 0):
        d.append(Diagnostic("error", "max_cells", "max_cells must be a positive integer"))
    for k in ("min_edge_length", "max_edge_length"):
        v = strategy.get(k)
        if v is not None and (not _num(v) or not math.isfinite(float(v)) or float(v) <= 0):
            d.append(Diagnostic("error", k, f"{k} must be a positive length in metres (or omitted "
                                            f"to keep the engine-staged value) - received {v!r}"))
    sa = strategy.get("sizing_array")
    if sa is not None and not isinstance(sa, str):
        d.append(Diagnostic("error", "sizing_array",
                            "sizing_array is the engine-staged point array name (a string); "
                            "leave it out to keep the staged value"))
    lo, hi = strategy.get("min_edge_length"), strategy.get("max_edge_length")
    if (isinstance(lo, (int, float)) and isinstance(hi, (int, float))
            and float(lo) >= float(hi) > 0):
        d.append(Diagnostic("error", "min_edge_length",
                            "min_edge_length must be smaller than max_edge_length"))
    d.extend(_validate_seeding(strategy))
    return d


def _validate_seeding(strategy: dict) -> list[Diagnostic]:
    d: list[Diagnostic] = []
    pts = [k for k in ("source_points", "target_points") if strategy.get(k)]
    ids = [k for k in ("source_ids", "target_ids") if strategy.get(k)]
    for k in ("source_ids", "target_ids"):
        v = strategy.get(k)
        if v is not None and not (isinstance(v, list) and all(_int(x) for x in v)):
            d.append(Diagnostic("error", k, f"{k} must be an array of integer open-profile ids"))
    for k in ("source_points", "target_points"):
        v = strategy.get(k)
        if v is not None and not (isinstance(v, list) and all(_num(x) for x in v)):
            d.append(Diagnostic("error", k, f"{k} must be a flat array of coordinates [x,y,z,...]"))
        elif v and len(v) % 3 != 0:
            d.append(Diagnostic("error", k, f"{k} must hold whole (x,y,z) triples - got {len(v)} numbers"))
    if len(pts) == 1:
        d.append(Diagnostic("error", "source_points",
                            "supply BOTH source_points and target_points (inlet and outlet seeds)"))
    if len(ids) == 1:
        d.append(Diagnostic("error", "source_ids",
                            "supply BOTH source_ids and target_ids (inlet and outlet open-profile ids)"))
    if not pts and not ids:
        # a WARNING, not a refusal: a CAD body with declared ports has its seeds staged by the
        # engine (geometry_report: staged_ports) and configure_mesh fills them in; when nothing
        # was staged configure_mesh itself refuses with vmtk_seeds_required
        d.append(Diagnostic("warning", "source_points",
                            "no centerline seeds given - they are taken from the engine-staged ports "
                            "(geometry_report lists them as staged_ports). If none were staged, give "
                            "source_points+target_points (any lumen, including a CLOSED one) or "
                            "source_ids+target_ids (open-profile ids from geometry_report); vmtk's "
                            "interactive seeding cannot run headless."))
    return d


#: ENGINE-OWNED mesh-detail mapping. vmtk sizes from the CENTERLINE RADIUS, so the tier moves the
#: relative edge-length factor (LOWER = finer) and the layer count. Classification: all soft
#: recommendations; vmtk's own gate still rejects a mesh above CELL_HARD_LIMIT.
_FIDELITY_VMTK = {
    "draft":    {"edge_length_factor": 0.5, "boundary_layers": 0},
    "standard": {"edge_length_factor": 0.3, "boundary_layers": 3},
    "max":      {"edge_length_factor": 0.2, "boundary_layers": 5},
}


def recommend(analysis: dict, *, fidelity: str = "standard") -> dict:
    # NOTE: `analysis` is geometry.analysis.analyze_surface() output - it measures scales
    # (diag, min_feature, thin_gap, …) and does NOT report whether the lumen is closed.
    # Closedness is reported by geometry_report (engines.vmtk.vmtk_runner.inspect_stl), so
    # the BUILDER makes that call; recommend() must not silently guess it.
    _f = _FIDELITY_VMTK.get(str(fidelity or "standard"), _FIDELITY_VMTK["standard"])
    return {
        "mesh_detail_preference": str(fidelity or "standard"),
        "edge_length_factor": _f["edge_length_factor"],
        "boundary_layers": _f["boundary_layers"],
        "boundary_layer_thickness_factor": 0.2,
        "cap_openings": True,
        "remesh_surface": True,
        "note": ("vmtk sizes cells from the CENTERLINE RADIUS, so give it a factor, not a length: "
                 "edge_length_factor≈0.3 puts roughly 6-7 cells across every branch diameter, "
                 "narrow or wide. Lower it to refine everywhere; do not try to set an absolute "
                 "cell size. Centerline seeding must be EXPLICIT (no interactive picking). These "
                 "layer/capping values assume an OPEN lumen: if geometry_report reports "
                 "closed=true, set cap_openings=false (nothing to cap) and start from "
                 "boundary_layers=0, then raise layers only if the mesh stays valid."),
    }
