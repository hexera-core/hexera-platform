# Responsibility: Answer the builder's questions about the geometry it is meshing.
# Boundaries: read-only measurement and reporting; it modifies no geometry.
from __future__ import annotations

import logging

from meshpipeline.agents.builder.tool_context import BuilderToolContext

#: THE staged surface every engine analyses. A DERIVED workspace artefact - written upstream by the
#: engine's own tessellator - not the source geometry and not an identity. It carries no unit,
#: which is precisely why a tool that measures it must also hold the interpretation.
STAGED_SURFACE = "input.stl"

logger = logging.getLogger(__name__)


# These carry the typed context rather than rediscovering geometry from files, because
# `input.stl` names no unit: without it a millimetre duct and a metre duct are the same file.
from meshpipeline.agents.builder.tools.workspace import _confined

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "geometry_report",
            "description": (
                "Inspect the staged geometry and return the ENGINE's geometry report, in "
                "METRES (surface meshers: bounding box, extents, thinnest axis, triangle "
                "count; CAD-solid meshers: solids plus per-surface tags/areas/centroids). "
                "Call this FIRST, before sizing the mesh."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "measure_scales",
            "description": (
                "Measure the body's intrinsic length scales - overall size, thinnest "
                "feature/gap, curvature - and get refinement sizing DERIVED from them (target "
                "sizes/levels phrased in the selected engine's own palette). Call "
                "before sizing refinement so you resolve fine features (trailing edges, slat/flap "
                "gaps) without one uniform cell size that either explodes the count or "
                "under-resolves. The numbers come from THIS body, not a guess."
            ),
            "parameters": {
                "type": "object",
                "properties": {"geometry_file": {"type": "string", "description": "body STL (default input.stl)"}},
                "required": [],
            },
        },
    },
]

ACTIONS: dict[str, str] = {
    "geometry_report": "Inspecting the geometry",
    "measure_scales":  "Measuring the body's length scales",
}

def geometry_report(ctx: BuilderToolContext) -> dict:
    # Input is an STL surface (CAD is tessellated to input.stl upstream); the ENGINE's own
    # inspect_stl reports it in the engine's vocabulary. The context travels with it so the
    # engine reports PHYSICAL facts rather than whatever the file's numbers happen to be.
    from meshpipeline.engines.runtime import get_engine
    ctx.require_geometry()
    report = get_engine(ctx.engine).inspect_stl(ctx.workspace, context=ctx)
    import meshpipeline.settings.runtime as rtcfg
    return _bounded_report(report, rtcfg.MAX_TOOL_OUTPUT_CHARS)


def _serialized_len(report: dict) -> int:
    import json
    return len(json.dumps(report))


#: Headroom under the tool cap. The registry's `_serialized` measures its OWN json.dumps of the
#: result against the cap; this module aims a comfortable margin below it so an innocent
#: serializer difference (key added downstream, separator change) can never turn "fits by one
#: char" back into the over-cap notice this function exists to prevent.
_CAP_MARGIN = 512


def _bounded_report(report: dict, cap: int) -> dict:
    """Degrade an oversized geometry report DETERMINISTICALLY instead of losing it whole.

    `geometry_report` takes no arguments, so the model cannot make the engine "return a smaller
    result" - yet the registry's output cap replaces any oversized result with exactly that
    instruction. On a 57-face pump volute the full report (16821 chars) exceeded the 16000-char
    cap on every call; the builder never learned one surface tag, burned every round of two
    attempts on STEP archaeology, and the job died at finalize with "no mesh.inp"
    (job d0fc1033-c2dc-474a-affc-dcc316237668, 2026-08-28). This function is the tool-side
    guarantee that the report ALWAYS arrives, shedding detail in declared steps:

      1. fits - returned untouched (the overwhelmingly common case);
      2. a solid model's curve table is dropped (3D groups bind surface tags, never curves);
      3. per-surface dicts are compacted to arrays (same tags, same numbers, ~half the chars);
      4. last resort: smallest-area surfaces are elided, loudly counted - never silently.

    Every step stamps what it did, so the model reads a degraded report as degraded rather
    than as the geometry's whole truth.
    """
    if not isinstance(report, dict) or _serialized_len(report) <= cap - _CAP_MARGIN:
        return report
    budget = cap - _CAP_MARGIN
    out = dict(report)

    curves = out.get("curves")
    if isinstance(curves, list) and curves and out.get("volumes"):
        # A SOLID model's groups bind surface tags; its curve table informs nothing the
        # builder can author with, and on real volutes it was most of the report.
        out["curves"] = []
        out["curves_omitted"] = len(curves)
        if _serialized_len(out) <= budget:
            return out

    surfaces = out.get("surfaces")
    if isinstance(surfaces, list) and surfaces and all(
            isinstance(s, dict) and "tag" in s for s in surfaces):
        compact = [[s.get("tag"), s.get("area")] + list(s.get("centroid") or [])
                   for s in surfaces]
        out["surfaces"] = compact
        out["surfaces_format"] = "[tag, area, cx, cy, cz]"
        if _serialized_len(out) <= budget:
            return out

        # LAST RESORT - elide the smallest faces, keeping every large one, and say so. The
        # note matters: an elided tag still exists in the engine, and a group listing "every
        # surface" from this report would silently miss it. (The keys are total even for a
        # row missing its number - two Nones must sort, not raise.)
        def _by_area(row: list) -> tuple:
            return (row[1] is None, row[1] or 0.0)

        def _by_tag(row: list) -> tuple:
            return (row[0] is None, row[0] or 0)

        by_area = sorted(out["surfaces"], key=_by_area)
        total = len(by_area)
        while len(by_area) > 1 and _serialized_len(out) > budget:
            by_area.pop(0)
            out["surfaces"] = sorted(by_area, key=_by_tag)
            out["surfaces_omitted"] = total - len(by_area)
            out["surfaces_note"] = (
                f"the {total - len(by_area)} SMALLEST-area surfaces were elided to fit the "
                "tool output cap - their tags still exist in the engine; do not treat the "
                "listed tags as the complete surface set")
    return out


def measure_scales(ctx: BuilderToolContext, args: dict) -> dict:
    workspace, engine, mesh_fidelity = ctx.workspace, ctx.engine, ctx.mesh_fidelity
    from meshpipeline.cad.analysis import analyze_surface
    # Measuring is a PHYSICAL question: an extent is meaningless without a unit, so this refuses
    # rather than reporting numbers whose scale nobody has established.
    ctx.require_geometry()
    gf = args.get("geometry_file", STAGED_SURFACE)
    stl = _confined(workspace, gf)
    if stl is None:
        return {"error": f"Path escape attempt blocked: {gf}"}
    if not stl.exists():
        return {"error": f"{gf} not found (CAD is tessellated to {STAGED_SURFACE} upstream)."}
    try:
        from meshpipeline.cad.staging import staged_surface
        a = analyze_surface(staged_surface(ctx.geometry, stl))
        # ENGINE-AGNOSTIC measured scales. The recommended STRATEGY (sizes vs levels) is
        # phrased by the ENGINE's own palette - the builder holds no mesh vocabulary.
        from meshpipeline.engines.registry import get_spec
        _out = {
            "scale_m": {"diag": round(a["diag"], 4),
                        "extent": [round(x, 4) for x in a["extent"]]},
            "min_feature_m": round(a["min_feature"], 6),
            "thin_gap_m": round(a["thin_gap"], 6),
            "n_triangles": a["n_triangles"],
        }
        _spec = get_spec(engine)
        _rec = _spec.recommend_authoring(a, fidelity=mesh_fidelity)
        if _rec:
            _out["recommended_strategy"] = _rec
        # INPUT CONTRACT (measured axes): flag geometry this engine physically cannot mesh
        # before the builder wastes rounds on it - the mirror of the output validation.
        #   * thinness (min_thickness_ratio) - inert while declared 0.0
        #   * self-intersection - computed only when the engine's contract requires a
        #     fillable (non-self-intersecting) surface, since the ~1s test is pure waste for a
        #     wrap-then-fill engine that tolerates a dirty surface (snappy/cfMesh).
        _ic = _spec.input_contract
        if _ic is not None and _ic.require_no_self_intersection:
            from meshpipeline.cad.surface_checks import self_intersects
            a["self_intersecting"] = self_intersects(stl)
            _out["self_intersecting"] = a["self_intersecting"]
        _unsuitable = _spec.geometry_unsuitable(a)
        if _unsuitable:
            _out["suitability_warning"] = _unsuitable
        return _out
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
