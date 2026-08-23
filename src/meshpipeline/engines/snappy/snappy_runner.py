# Responsibility: Drive a snappyHexMesh run end to end: background mesh, feature extraction, snap, layers, finalize.
# Owns: the stage sequence and the runtime surface other layers reach through this module.
# Boundaries: stage order is load-bearing - layers are meaningless before a successful snap.
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

from meshpipeline.cad.cad_tessellate import (  # noqa: F401
    tessellate_internal,
    tessellate_to_stl,
)
from meshpipeline.cad.stl_io import (  # noqa: F401
    _write_solid,
    drop_degenerate,
    inspect_stl,
    mirror_y,
    read_stl_solids,
    read_stl_triangles,
)

# Shared, engine-agnostic helpers imported from their canonical homes (not from
# cfmesh_runner - these were never cfMesh-specific). Re-exported (F401) so the
# snappy engine adapter in engines.runtime can getattr them off this module.
from meshpipeline.engines.domain_extent_gate import (
    extent_gate_for_request as check_domain_extents,  # noqa: F401  (engine-adapter seam; cross-engine-neutral case-level gate)
)
from meshpipeline.engines.manifest import (  # noqa: F401
    _patch_face_counts,
    write_manifest,
)
from meshpipeline.engines.snappy.finalize import finalize  # noqa: F401  (engine-adapter seam)
from meshpipeline.engines.snappy.foam_exec import (  # noqa: F401
    _DEFAULT_BASHRC,
    _foam_env,
    check_mesh,
    export_volume_vtk,
    scan_case_dicts,
)

# The native phase. Authoring decides what to mesh; `native` runs the mesher and reports
# facts. Re-exported names below are the engine's runtime surface, resolved through the
# bundle adapter.
from meshpipeline.engines.snappy.native import (  # noqa: F401
    _foam_version,
    _run_snappy_local,
    _snappy_result,
    run_snappy,
)
from meshpipeline.engines.snappy.solvability import check_solvability  # noqa: F401  (engine-adapter seam)

# The snappyHexMesh mechanics this engine shares with snappy_multiregion. Both are CALLED
# here - `_write_case_skeleton` by the case renderers, `parse_layer_coverage` by the local
# run - so neither is a re-export; each engine's `finalize` probes the latter by attribute.
# `parse_layer_coverage` is part of this engine's DECLARED RUNTIME SURFACE, not a
# convenience: `finalize` asks `hasattr(runner, "parse_layer_coverage")` to decide
# whether this engine can report near-wall layer coverage - capability by declaration,
# never an engine-name check. The bundle adapter resolves attributes off this module, so
# the name has to be here even though the native phase is what calls it.
from meshpipeline.engines.snappy_hexmesh import (  # noqa: F401
    _write_case_skeleton,
    parse_layer_coverage,
)
from meshpipeline.render.review_artifacts import (  # noqa: F401
    build_review_msh,
)

logger = logging.getLogger(__name__)

_HDR = ("FoamFile{{ version 2.0; format ascii; class {cls}; object {obj}; }}\n")


# #
# case skeleton  (boilerplate snappy needs; the AI never authors these)
# #
def _feature_extract_dict(surf_file: str, feature_angle: float) -> str:
    return (_HDR.format(cls="dictionary", obj="surfaceFeatureExtractDict")
            + f"{surf_file}\n{{ extractionMethod extractFromSurface;\n"
            f"  extractFromSurfaceCoeffs {{ includedAngle {feature_angle:g}; }}\n"
            "  writeObj yes; }\n")


# #
# surface prep  (snappy variant - triSurface + skeleton; AI writes the dicts)
# #
def prepare_surface(workspace, *, geometry_file: str,
                    domain_min=None, domain_max=None, wall_patch: str = "body",
                    farfield_patch: str = "farfield", feature_angle: float = 30.0,
                    mirror_y_half: bool = False, reference_length_m: float | None = None,
                    bashrc: str = _DEFAULT_BASHRC) -> dict:
    ws = Path(workspace)
    body_stl = ws / geometry_file
    if body_stl.suffix.lower() != ".stl":
        raise ValueError(f"input must be a surface STL, got '{body_stl.suffix}' "
                         f"(export STL from CAD upstream - OpenFOAM has no CAD import)")
    # REGIONS the input already distinguishes. read_stl_triangles returns every triangle with the
    # solid boundaries dissolved, which is the right read for a body meshed as one wall and the
    # wrong one for a surface whose parts are named: the names are gone before snappyHexMesh, which
    # can carry them, is ever asked. Read solids first and keep them when there is more than one.
    solids = {n: drop_degenerate(v) for n, v in read_stl_solids(body_stl).items()}
    regions = {n: v for n, v in solids.items() if v} if len(solids) > 1 else {}
    body = drop_degenerate(read_stl_triangles(body_stl))
    if mirror_y_half:
        body = body + mirror_y(body)
        regions = {n: v + mirror_y(v) for n, v in regions.items()}
    bb_min = [min(v[i] for t in body for v in t) for i in range(3)]
    bb_max = [max(v[i] for t in body for v in t) for i in range(3)]

    surf_name = re.sub(r"[^A-Za-z0-9_]", "_", wall_patch) or "body"
    surf_file = f"{surf_name}.stl"
    tri = ws / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    with (tri / surf_file).open("w") as fh:
        if regions:
            # ONE file, several named solids: the form snappyHexMesh reads region-wise. Written in
            # the source's own order so the patches come back in the order the CAD declared them.
            for name, tris in regions.items():
                _write_solid(fh, re.sub(r"[^A-Za-z0-9_]", "_", name) or name, tris)
        else:
            _write_solid(fh, surf_name, body)

    _write_case_skeleton(ws)
    (ws / "system" / "surfaceFeatureExtractDict").write_text(
        _feature_extract_dict(surf_file, feature_angle))

    if domain_min is not None and domain_max is not None:
        # Parity with cfMesh's geom_box.json so the A1 domain-extent gate measures the
        # box the Builder REQUESTED, not snappy's refined mesh bounds.
        # The ruler travels WITH the box it sized. The margins are in body-lengths, but the
        # user's request is in THEIR unit (a chord, a MAC); judging the one with the other
        # rejected a correct CRM domain twice - "requested 20c, mesh has 3.2c" where 3.2
        # body-lengths WAS 20 MAC. Null means the user quoted no reference, and the gate falls
        # back to the body length - the old behaviour exactly.
        (ws / "geom_box.json").write_text(json.dumps({
            "domain_min": [float(v) for v in domain_min],
            "domain_max": [float(v) for v in domain_max],
            "reference_length_m": (float(reference_length_m)
                                   if reference_length_m else None),
        }))

    return {"surface_file": f"constant/triSurface/{surf_file}",
            "surface_name": surf_name, "feature_file": f"{surf_name}.eMesh",
            "surface_regions": [re.sub(r"[^A-Za-z0-9_]", "_", n) or n for n in regions],
            "body_bbox": [bb_min, bb_max]}


# #
def surface_capture_reference(workspace, patch_types: dict | None = None):
    ws = Path(workspace)
    wall = next((n for n, t in (patch_types or {}).items() if t == "wall"), None)
    if not wall:
        return None
    vtps = sorted(ws.glob(f"VTK/*/boundary/{wall}.vtp"))
    ref = ws / "input.stl"
    return (vtps[0], ref) if vtps and ref.exists() else None


# DECLARED reviewer-surface hooks (consumed by the shared finalize instead of engine-name
# branches): snappy stages the BODY as a named triSurface (constant/triSurface/<wall>.stl) -
# internal flow has several (wall/inlet/outlet), external one - while the far-field / symmetry
# patches live only in the blockMesh boundary, not the triSurface.
review_surface_is_body_only = True   # → finalize also declares the mesh's blockMesh patches


def review_geometry_stls(workspace, internal_flow: bool = False) -> list:
    ws = Path(workspace)
    tri = sorted((ws / "constant" / "triSurface").glob("*.stl"))
    if internal_flow:
        return tri
    return [tri[0]] if tri else [ws / "geom.stl"]


# #
# run the snappy sequence  (dispatched to Cloud Run - no local fallback)
# #


# #
# deterministic dict RENDERER
# #
# The builder picks STRATEGY (domain size, layer count, optional level tweaks); this writes
# VALID, budget-clamped, leak-resistant blockMeshDict + snappyHexMeshDict. The model never
# hand-writes snappy syntax - which was the failure mode (broken braces, level-11 explosions,
# locationInMesh inside the body). It therefore CANNOT emit a malformed dict, exceed the cell
# budget, or mis-place the carve point. All sizing derives from the measured geometry, so this
# generalises across parts with no per-geometry constants.
# The six hex faces of the blockMesh box, keyed by (axis, side) - the vertex ordering matches
# the V[] list built in render_snappy_case. One of these becomes the symmetryPlane for a
# half-model; the rest stay farfield.
_BOX_FACES: dict[tuple[int, str], str] = {
    (0, "min"): "(0 4 7 3)", (0, "max"): "(1 2 6 5)",   # x-min / x-max
    (1, "min"): "(0 1 5 4)", (1, "max"): "(2 3 7 6)",   # y-min / y-max
    (2, "min"): "(0 3 2 1)", (2, "max"): "(4 5 6 7)",   # z-min / z-max
}
_ALL_BOX_FACES = ["(0 3 2 1)", "(4 5 6 7)", "(0 1 5 4)", "(2 3 7 6)", "(1 2 6 5)", "(0 4 7 3)"]


def detect_symmetry_plane(analysis: dict, sym_name: str, *, tol_frac: float = 0.005,
                          min_cap_frac: float = 0.005) -> dict | None:
    if not sym_name:
        return None
    bmin, bmax = analysis["bbox_min"], analysis["bbox_max"]
    tol = tol_frac * (float(analysis["L"]) or 1.0)
    caps = analysis.get("axis_caps")
    # A bounding box that merely TOUCHES zero is not a cut plane. A NACA 0012 sitting at
    # x = 0.0011 with L = 2 passes abs(lo) < tol on the X axis and was declared a half-model cut
    # on its own leading edge - which clamped the domain to x >= 0 and left the case with no
    # upstream far field at all. The mesh came out geometrically clean and aerodynamically
    # useless. A real cut face is FLAT and carries area, so require the end cap to exist.
    def _has_cap(ax: int, side: str) -> bool:
        if not caps:
            return True                    # no measurement available - preserve prior behaviour
        return float(caps[ax].get(side, 0.0)) >= min_cap_frac

    for ax in range(3):
        lo, hi = float(bmin[ax]), float(bmax[ax])
        if abs(lo) < tol < hi and _has_cap(ax, "min"):    # sits on the plane at 0, extends positive
            return {"axis": ax, "pos": 0.0, "side": "min", "name": sym_name}
        if abs(hi) < tol and lo < -tol and _has_cap(ax, "max"):  # on the plane at 0, extends negative
            return {"axis": ax, "pos": 0.0, "side": "max", "name": sym_name}
    return None                            # straddles the centreline - no half-model plane


def detect_slab_symmetry(analysis: dict, lo_name: str, hi_name: str, *,
                         min_cap_frac: float = 0.002) -> dict | None:
    """The 2.5D case: an extruded section with a symmetry patch on EACH spanwise end.

    detect_symmetry_plane above answers a different question - "is this half a body, cut on a
    plane?" - and can only ever name ONE face. A wing section swept along its span is not a half
    model: it is a whole body whose two end caps are both symmetry boundaries. Asking for that
    used to silently produce a mesh with the second patch empty, which the manifest contract
    then rejected after the mesh had already been paid for.

    The signal is the flat end caps measured in analysis["axis_caps"]: an extrusion has real
    surface area lying ON both bbox planes of its sweep axis, normals along that axis. A curved
    closed body has neither cap, and a half-model has exactly one - so requiring BOTH separates
    this case from the one above without guessing from the bounding box.
    """
    caps = analysis.get("axis_caps")
    if not caps or not lo_name or not hi_name:
        return None
    best, best_score = None, 0.0
    for ax in range(3):
        lo_f, hi_f = float(caps[ax].get("min", 0.0)), float(caps[ax].get("max", 0.0))
        # Weight by how EQUAL the two caps are. A swept section ends in two identical faces;
        # anything else that happens to present flat area at both bbox planes does not, so this
        # separates the real sweep axis from a coincidence without needing a second measurement.
        balance = 1.0 - abs(lo_f - hi_f) / max(lo_f, hi_f, 1e-30)
        score = min(lo_f, hi_f) * balance
        if lo_f >= min_cap_frac and hi_f >= min_cap_frac and score > best_score:
            best, best_score = ax, score
    if best is None:
        return None
    return {"slab": True, "axis": best,
            "pos_lo": float(analysis["bbox_min"][best]),
            "pos_hi": float(analysis["bbox_max"][best]),
            "lo_name": lo_name, "hi_name": hi_name,
            "cap_frac": best_score}


def _extent_of(analysis: dict) -> list[float]:
    ext = analysis.get("extent")
    if ext:
        return [float(v) for v in ext]
    lo, hi = analysis["bbox_min"], analysis["bbox_max"]
    return [float(hi[i]) - float(lo[i]) for i in range(3)]


def axis_roles(analysis: dict, symmetry: dict | None = None) -> tuple[int, int, int] | None:
    """(streamwise, vertical, spanwise) axis indices, or None when they cannot be told apart.

    The margins below are named for a body's flow directions, but were applied by fixed index -
    axis 0 up/down, axis 1 side, axis 2 vert. That is a guess about how the CAD happens to be
    oriented. A NACA 0012 exported with chord on X, THICKNESS on Y and span on Z gets the
    "side" margin applied to its vertical direction and the "vert" margin applied to its span,
    where the slab clamp then discards it: a 20-chord brief produced 2 chords of clearance above
    and below the aerofoil. The mesh is clean and the lift computed on it is wrong, which is the
    expensive kind of wrong.

    Only answerable when the spanwise axis is known independently - which is exactly what a
    declared symmetry gives us. Without it, return None and leave the historical mapping alone
    rather than guess: for a bare body, the thinnest axis is as likely to be a wing's thickness
    as its span.
    """
    if not symmetry:
        return None
    span = int(symmetry["axis"])
    # bbox is the authority; "extent" is a convenience analyze_surface happens to publish and not
    # every caller carries it. Deriving keeps this readable from any analysis that can describe a
    # box at all, which is the only thing it needs to answer the question.
    ext = _extent_of(analysis)
    rest = [i for i in range(3) if i != span]
    # Of the two non-span axes, the longer is the streamwise one - chord exceeds thickness on any
    # body worth wrapping in a far field.
    stream = max(rest, key=lambda i: float(ext[i]))
    vert = rest[0] if rest[1] == stream else rest[1]
    return stream, vert, span


def domain_from_strategy(analysis: dict, strategy: dict | None = None,
                         symmetry: dict | None = None) -> tuple[list, list]:
    bmin, bmax, L = analysis["bbox_min"], analysis["bbox_max"], analysis["L"]
    m = (strategy or {}).get("domain_margin") or {}
    up, dn = float(m.get("up", 2.0)), float(m.get("down", 4.0))
    side, vert = float(m.get("side", 2.0)), float(m.get("vert", 2.0))

    roles = axis_roles(analysis, symmetry)
    if roles is None:
        dmin = [bmin[0] - up * L, bmin[1] - side * L, bmin[2] - vert * L]
        dmax = [bmax[0] + dn * L, bmax[1] + side * L, bmax[2] + vert * L]
    else:
        # REFERENCE LENGTH is the STREAMWISE extent, not the largest bbox dimension. Far-field
        # distances are quoted in chords by every text and every reviewer, and the planner is
        # briefed in "body-lengths L" meaning exactly that. With L = max(extent), a 2 m span on a
        # 1 m chord silently doubled every margin: a 20-chord brief became 40 chords upstream and
        # 60 downstream - paid for in background cells - while the direction that actually needed
        # the room got the smallest multiplier.
        i_s, i_v, i_p = roles
        # A brief cannot bind a model. The planner is told which direction each key names, and it
        # still emitted vert=0 while reasoning "vert: 0 (spanwise, no padding)" - it had the key
        # meaning the span. That put the far-field boundary flat on the aerofoil's suction and
        # pressure surfaces: 809k cells, 29,657 of them skewed, and a lift number that would have
        # been meaningless had anything downstream accepted it. SPANWISE may legitimately be zero
        # for a slab; the direction a lifting body develops its pressure field in may not.
        _VERT_FLOOR = 2.0
        if vert < _VERT_FLOOR:
            logger.warning("domain_margin vert=%.3g is below the %.3g-body-length floor for external "
                           "flow - raising it. A zero here puts the far-field on the body's own "
                           "surface; only the SPANWISE margin may be zero.", vert, _VERT_FLOOR)
            vert = _VERT_FLOOR
        ref = float(_extent_of(analysis)[i_s]) or L
        dmin, dmax = list(bmin), list(bmax)
        dmin[i_s], dmax[i_s] = bmin[i_s] - up * ref,   bmax[i_s] + dn * ref
        dmin[i_v], dmax[i_v] = bmin[i_v] - vert * ref, bmax[i_v] + vert * ref
        dmin[i_p], dmax[i_p] = bmin[i_p] - side * ref, bmax[i_p] + side * ref
    if symmetry and symmetry.get("slab"):
        # Both ends of the sweep axis ARE the domain: a 2.5D slab is not padded spanwise, or the
        # symmetry planes would sit out in the far field with fluid between them and the body.
        ax = symmetry["axis"]
        dmin[ax], dmax[ax] = symmetry["pos_lo"], symmetry["pos_hi"]
    elif symmetry:
        ax = symmetry["axis"]
        if symmetry["side"] == "min":
            dmin[ax] = symmetry["pos"]
        else:
            dmax[ax] = symmetry["pos"]
    return dmin, dmax


def render_snappy_case(workspace, *, surface_name: str, feature_file: str, analysis: dict,
                       recommendation: dict, domain_min, domain_max,
                       strategy: dict | None = None, dimensionality: str = "3D",
                       symmetry: dict | None = None,
                       surface_regions: list | None = None) -> dict:
    ws = Path(workspace)
    strategy = strategy or {}
    rec = recommendation
    base = rec["base_cell"]
    if (dimensionality or "3D").upper() == "2D":
        # UPSTREAM TRUTH: snappyHexMesh is a 3D mesher. The former pseudo-2D workflow
        # (thin slab + extrudeMesh collapse) was removed - real 2D Cartesian meshing is
        # cfMesh cartesian2DMesh. Admission rejects snappy+2D; this guard is the backstop.
        raise ValueError("snappyHexMesh is a 3D engine in this system - 2D was removed; "
                         "use cfmesh (cartesian2DMesh) for 2D Cartesian meshes")

    domain_min, domain_max = list(domain_min), list(domain_max)
    ext = [float(domain_max[i] - domain_min[i]) for i in range(3)]

    # coarse background grid over the WHOLE domain (snappy refines down); clamp so a large
    # far-field box never explodes the background cell count.
    #
    # The clamp used to be a flat per-axis cap of 80, which spent the budget in the wrong
    # currency. Background cells grow LINEARLY with div; the `deficit` compensation below grows
    # the refinement level, and cells grow EXPONENTIALLY with level (8x per level in 3D). On a
    # 20-chord far field the flat cap forced base_actual = 1.025 m against an intended 0.0833 m -
    # 12x coarse - which bought a 37k-cell background and paid for it with FOUR extra refinement
    # levels. That is a ~4000x multiplier on the wall shell to save 500k background cells, and it
    # OOM-killed snappy at both 8 GiB and 16 GiB.
    #
    # So budget the background directly and let each axis resolve as finely as that budget allows.
    _BG_MAX_DIV = 256          # per-axis sanity bound
    _BG_CELL_BUDGET = 600_000  # total background cells before refinement
    div = [max(12, min(_BG_MAX_DIV, int(round(ext[i] / base)))) for i in range(3)]
    # Trim the coarsest-paying axis until the whole grid fits the budget. The floor of 12 is a
    # blockMesh sanity minimum, so a thin slab's span axis is exempt from the trimming.
    for _ in range(256):
        if div[0] * div[1] * div[2] <= _BG_CELL_BUDGET:
            break
        k = max(range(3), key=lambda i: div[i])
        if div[k] <= 12:
            break
        div[k] = max(12, int(div[k] * 0.92))

    # DOMAIN-DECOUPLED RESOLUTION. A refinement LEVEL is relative to the background base cell,
    # but the clamp above (needed so a large far-field doesn't explode the background) makes the
    # ACTUAL base cell (ext/div) coarser than the recommender assumed. Uncompensated, a big domain
    # silently coarsens the wall until cells are larger than the feature (the aircraft failure:
    # 0.125 m cells on a 0.05 m wing). Bump every level by the clamp DEFICIT so the ABSOLUTE cell
    # size the recommender intended is held regardless of domain size. deficit=0 when the clamp
    # doesn't bite (small domains - e.g. an airfoil - are unchanged). snappy's maxGlobalCells is
    # the hard budget backstop and the distance bands are already in absolute units, so this
    # cannot explode the count - the fine cells stay a thin shell on the wall.
    base_actual = max(ext[i] / max(div[i], 1) for i in range(3))
    # floor, not ceil - the same correction render_internal_case already carries at the matching
    # line. div is an integer count, so ext/div lands slightly above the requested base cell for
    # pure rounding reasons; ceil turns that rounding into a whole extra refinement level, and a
    # level is an 8x cell multiplier in 3D. Only a genuine doubling of the base cell should cost
    # a level.
    deficit = max(0, int(math.floor(math.log2(max(base_actual / max(base, 1e-30), 1.0)) + 1e-9)))
    _HARD_MAX_LEVEL = 10

    # levels - the surface level is FLOORED at the body-sealing level the recommender computed
    # (going coarser leaks the carve → 0 wall faces) and CEILINGed at the budget-afford level
    # (going finer explodes the count). The strategy may move WITHIN [floor, afford], never below.
    # Each level is shifted up by `deficit` to undo the clamp's coarsening (see above).
    floor = min(_HARD_MAX_LEVEL, int(rec["surface_level"][0]) + deficit)
    ceil_ = min(_HARD_MAX_LEVEL, max(floor, int(rec.get("afford_level", floor)) + deficit))
    so = strategy.get("surface_level")
    lvl = floor if not so else max(floor, min(int(so[-1]) + deficit, ceil_))
    smin = smax = lvl
    flevel = int(strategy.get("feature_level", rec["feature_level"]))
    flevel = min(_HARD_MAX_LEVEL, max(smax, min(flevel, int(rec["feature_level"])) + deficit))
    bands = rec["distance_bands"]
    if deficit:
        logger.info("render_snappy_case: clamp deficit=%d → levels bumped (base_assumed=%.4g "
                    "base_actual=%.4g surf=%d feat=%d) to hold absolute wall resolution",
                    deficit, base, base_actual, smax, flevel)
    ang = rec["resolve_feature_angle"]
    max_cells = int(strategy.get("max_cells", 8_000_000))

    # locationInMesh - a far-field corner, GUARANTEED in the fluid (body is centred with margin).
    loc = [domain_min[i] + 0.02 * ext[i] for i in range(3)]

    def vf(p) -> str:
        return f"({p[0]:.6g} {p[1]:.6g} {p[2]:.6g})"

    V = [(domain_min[0], domain_min[1], domain_min[2]), (domain_max[0], domain_min[1], domain_min[2]),
         (domain_max[0], domain_max[1], domain_min[2]), (domain_min[0], domain_max[1], domain_min[2]),
         (domain_min[0], domain_min[1], domain_max[2]), (domain_max[0], domain_min[1], domain_max[2]),
         (domain_max[0], domain_max[1], domain_max[2]), (domain_min[0], domain_max[1], domain_max[2])]
    if symmetry and symmetry.get("slab"):
        # 2.5D slab: BOTH faces of the sweep axis are symmetryPlanes, each under the name the
        # user declared for it. The other four are farfield. Emitting only one of these is what
        # left the second declared patch with zero faces.
        _lo = _BOX_FACES[(symmetry["axis"], "min")]
        _hi = _BOX_FACES[(symmetry["axis"], "max")]
        _ff = "".join(f for f in _ALL_BOX_FACES if f not in (_lo, _hi))
        _boundary = (f"boundary (farfield {{ type patch; faces ({_ff}); }} "
                     f"{symmetry['lo_name']} {{ type symmetryPlane; faces ({_lo}); }} "
                     f"{symmetry['hi_name']} {{ type symmetryPlane; faces ({_hi}); }});")
    elif symmetry:
        # half-model: one box face is the symmetryPlane (named as the user declared); the
        # remaining five are farfield.
        _sym_face = _BOX_FACES[(symmetry["axis"], symmetry["side"])]
        _ff = "".join(f for f in _ALL_BOX_FACES if f != _sym_face)
        _boundary = (f"boundary (farfield {{ type patch; faces ({_ff}); }} "
                     f"{symmetry['name']} {{ type symmetryPlane; faces ({_sym_face}); }});")
    else:
        _boundary = ("boundary (farfield { type patch; faces "
                     "((0 3 2 1)(4 5 6 7)(0 1 5 4)(2 3 7 6)(1 2 6 5)(0 4 7 3)); });")
    (ws / "system" / "blockMeshDict").write_text(
        _HDR.format(cls="dictionary", obj="blockMeshDict")
        + "scale 1; vertices (" + "".join(vf(p) for p in V)
        + f");\nblocks (hex (0 1 2 3 4 5 6 7) ({div[0]} {div[1]} {div[2]}) simpleGrading (1 1 1)); edges ();\n"
        + _boundary + "\nmergePatchPairs ();\n")


    # layers - survival settings baked in (nRelaxedIter + relaxed quality + tet veto off), so
    # layers don't roll back to 0% on sharp edges. The builder only chooses count/thickness.
    n_layers = max(0, int(strategy.get("n_layers", 3)))
    first_rel = float(strategy.get("first_layer_rel", 0.35))
    (b0d, b0l), (b1d, b1l) = bands
    near_band_level = min(int(b0l) + deficit, smax + 2)   # bumped with the surface (see deficit above)

    # quality vs coverage: "balanced" buys layer coverage with relaxed cell quality (some
    # high-skew cells); "strict" enforces a real tet quality + tighter relaxed bounds so
    # checkMesh passes (skew < 4), trading a few % coverage. The builder flips this on high skew.
    if strategy.get("quality") == "strict":
        min_tet, relaxed_no, n_relaxed, medial = "1e-13", 65, 6, 0.3
    else:
        min_tet, relaxed_no, n_relaxed, medial = "-1e30", 75, 20, 0.5
    # REGION-WISE DECLARATION. Named solids in the surface become named patches: snappyHexMesh
    # emits <surface>_<region> for each, so the layer entry becomes a pattern covering them all.
    # With no regions every fragment is empty and the dict is exactly what it has always been.
    _names = [str(r) for r in (surface_regions or []) if str(r).strip()]
    _geo_regions = (" regions { " + " ".join(f"{r} {{ name {r}; }}" for r in _names) + " }"
                    if _names else "")
    _ref_regions = (" regions { "
                    + " ".join(f"{r} {{ level ({smin} {smax}); patchInfo {{ type wall; }} }}"
                               for r in _names) + " }"
                    if _names else "")
    # ONE ENTRY PER REGION, by the patch's real name. snappyHexMesh names a region patch after the
    # region itself, not <surface>_<region>: a pattern built on the surface name matches nothing,
    # and OpenFOAM says so in the log and then adds no layers at all - a wall-resolved case would
    # come back silently without its boundary layer.
    _layers = (" ".join(f"{r} {{ nSurfaceLayers {n_layers}; }}" for r in _names)
               if _names else f"{surface_name} {{ nSurfaceLayers {n_layers}; }}")
    (ws / "system" / "snappyHexMeshDict").write_text(
        _HDR.format(cls="dictionary", obj="snappyHexMeshDict") + f"""
castellatedMesh true; snap true; addLayers {'true' if n_layers > 0 else 'false'};
geometry {{ {surface_name}.stl {{ type triSurfaceMesh; name {surface_name};{_geo_regions} }} }}
castellatedMeshControls {{ maxLocalCells {max_cells}; maxGlobalCells {max_cells}; minRefinementCells 10;
  maxLoadUnbalance 0.10; nCellsBetweenLevels 3; features ( {{ file "{feature_file}"; level {flevel}; }} );
  refinementSurfaces {{ {surface_name} {{ level ({smin} {smax});{_ref_regions} }} }} resolveFeatureAngle {ang:.0f};
  refinementRegions {{ {surface_name} {{ mode distance; levels (({b0d:.6g} {near_band_level}) ({b1d:.6g} {max(1, smin - 1)})); }} }}
  locationInMesh {vf(loc)}; allowFreeStandingZoneFaces true; }}
snapControls {{ nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 8; nFeatureSnapIter 15;
  implicitFeatureSnap false; explicitFeatureSnap true; multiRegionFeatureSnap false; }}
addLayersControls {{ relativeSizes true; layers {{ {_layers} }}
  expansionRatio 1.2; finalLayerThickness {first_rel:g}; minThickness 0.05; nGrow 0; featureAngle 130;
  slipFeatureAngle 30; nRelaxIter 8; nSmoothSurfaceNormals 2; nSmoothNormals 3; nSmoothThickness 10;
  maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio {medial}; minMedialAxisAngle 90;
  nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter {n_relaxed}; }}
meshQualityControls {{ maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;
  minVol 1e-13; minTetQuality {min_tet}; minArea -1; minTwist 0.02; minDeterminant 0.001;
  minFaceWeight 0.02; minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;
  relaxed {{ maxNonOrtho {relaxed_no}; maxInternalSkewness 4; }} }}
mergeTolerance 1e-6; debug 0;
""")
    return {"divisions": div, "surface_level": [smin, smax], "feature_level": flevel,
            "location_in_mesh": [round(x, 4) for x in loc], "max_cells": max_cells,
            "n_layers": n_layers, "domain_min": [round(x, 3) for x in domain_min],
            "domain_max": [round(x, 3) for x in domain_max]}


def configure_mesh(workspace, *, geometry_file: str, strategy: dict, wall_patch: str,
                   contract_patches: list, args: dict, cell_budget: int,
                   surface=None) -> dict:
    from meshpipeline.cad.analysis import analyze_surface, recommend_refinement
    from meshpipeline.cad.prepared_surface import require_metre_surface
    analysis = analyze_surface(require_metre_surface(surface, workspace, geometry_file))
    rec = recommend_refinement(analysis, max_cells=int(strategy.get("max_cells", 8_000_000)))
    if args.get("domain_min") and args.get("domain_max"):
        dmin, dmax = args["domain_min"], args["domain_max"]
    else:
        dmin, dmax = domain_from_strategy(analysis, strategy)
    prep = prepare_surface(
        workspace, geometry_file=geometry_file, domain_min=dmin, domain_max=dmax,
        wall_patch=wall_patch, farfield_patch=args.get("farfield_patch", "farfield"),
        feature_angle=float(args.get("feature_angle", 150)))
    summary = render_snappy_case(
        workspace, surface_name=prep["surface_name"], feature_file=prep["feature_file"],
        analysis=analysis, recommendation=rec, domain_min=dmin, domain_max=dmax,
        strategy=strategy, dimensionality=args.get("dimensionality", "3D"))
    return {"success": True, "wrote": ["system/blockMeshDict", "system/snappyHexMeshDict"],
            **summary,
            "next": "Dicts written (valid + clamped to the budget). Call run_mesh NOW to build "
                    "the mesh. Reconfigure ONLY in response to a concrete run_mesh failure."}


# #
# INTERNAL-FLOW surface prep + renderer  (inverted topology: mesh the CAVITY)
# #
def prepare_surface_internal(workspace, *, surfaces_src: dict, feature_angle: float = 30.0,
                             bashrc: str = _DEFAULT_BASHRC) -> dict:
    ws = Path(workspace)
    tri = ws / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    names, feats, body = {}, {}, ""
    for patch, src in surfaces_src.items():
        tris = drop_degenerate(read_stl_triangles(Path(src)))
        if not tris:
            raise ValueError(f"internal surface '{patch}' tessellated to zero triangles")
        name = re.sub(r"[^A-Za-z0-9_]", "_", patch) or patch
        with (tri / f"{name}.stl").open("w") as fh:
            _write_solid(fh, name, tris)
        names[patch] = name
        feats[patch] = f"{name}.eMesh"
        body += (f'{name}.stl {{ extractionMethod extractFromSurface; '
                 f'extractFromSurfaceCoeffs {{ includedAngle {feature_angle:g}; }} '
                 f'writeObj yes; }}\n')
    _write_case_skeleton(ws)
    (ws / "system" / "surfaceFeatureExtractDict").write_text(
        _HDR.format(cls="dictionary", obj="surfaceFeatureExtractDict") + body)
    return {"names": names, "features": feats}


def render_internal_case(workspace, *, names: dict, features: dict, interior_point,
                         bbox_min, bbox_max, base_cell: float, surface_level: int,
                         feature_level: int, n_layers: int, first_layer_rel: float = 0.3,
                         max_cells: int = 8_000_000, quality: str = "balanced") -> dict:
    ws = Path(workspace)
    ext = [float(bbox_max[i] - bbox_min[i]) for i in range(3)]
    maxext = max(ext)
    pad = max(2.0 * base_cell, 0.03 * maxext)
    dmin = [bbox_min[i] - pad for i in range(3)]
    dmax = [bbox_max[i] + pad for i in range(3)]
    dext = [dmax[i] - dmin[i] for i in range(3)]

    div = [max(8, min(120, int(round(dext[i] / base_cell)))) for i in range(3)]
    # domain-decoupled resolution: if the background clamp coarsened the base cell, bump levels
    # so the ABSOLUTE wall cell size is held (same rationale as render_snappy_case).
    base_actual = max(dext[i] / max(div[i], 1) for i in range(3))
    # floor (not ceil): a refinement LEVEL is a factor of 2, so bump only when the clamp genuinely
    # DOUBLED the base cell. ceil would add a phantom +1 level from mere div-rounding (base_actual
    # marginally > base_cell), needlessly doubling wall resolution on every internal build.
    deficit = max(0, int(math.floor(math.log2(max(base_actual / max(base_cell, 1e-30), 1.0)) + 1e-9)))
    _HARD_MAX_LEVEL = 10

    smin = smax = min(_HARD_MAX_LEVEL, int(surface_level) + deficit)
    flevel = min(_HARD_MAX_LEVEL, max(smax, int(feature_level) + deficit))
    port_level = max(1, smin - 1)                 # ports resolved, one below the wall
    near_dist = max(3.0 * base_cell, 0.08 * maxext)
    near_level = min(_HARD_MAX_LEVEL, smax + 1)
    max_cells = int(max_cells)
    n_layers = max(0, int(n_layers))
    first_rel = float(first_layer_rel)
    if deficit:
        logger.info("render_internal_case: clamp deficit=%d → levels bumped (base_actual=%.4g "
                    "surf=%d feat=%d) to hold absolute wall resolution", deficit, base_actual,
                    smax, flevel)

    def vf(p) -> str:
        return f"({p[0]:.6g} {p[1]:.6g} {p[2]:.6g})"

    V = [(dmin[0], dmin[1], dmin[2]), (dmax[0], dmin[1], dmin[2]),
         (dmax[0], dmax[1], dmin[2]), (dmin[0], dmax[1], dmin[2]),
         (dmin[0], dmin[1], dmax[2]), (dmax[0], dmin[1], dmax[2]),
         (dmax[0], dmax[1], dmax[2]), (dmin[0], dmax[1], dmax[2])]
    (ws / "system" / "blockMeshDict").write_text(
        _HDR.format(cls="dictionary", obj="blockMeshDict")
        + "scale 1; vertices (" + "".join(vf(p) for p in V)
        + f");\nblocks (hex (0 1 2 3 4 5 6 7) ({div[0]} {div[1]} {div[2]}) simpleGrading (1 1 1)); edges ();\n"
        "boundary (outer { type patch; faces "
        "((0 3 2 1)(4 5 6 7)(0 1 5 4)(2 3 7 6)(1 2 6 5)(0 4 7 3)); });\nmergePatchPairs ();\n")

    geom = "".join(f"{names[p]}.stl {{ type triSurfaceMesh; name {names[p]}; }} "
                   for p in names)
    feat_entries = "".join(f'{{ file "{features[p]}"; level {flevel}; }} ' for p in names)
    wall = names["wall"]
    refine_surfs = (f"{wall} {{ level ({smin} {smax}); patchInfo {{ type wall; }} }} "
                    + "".join(f"{names[p]} {{ level ({port_level} {port_level}); "
                              f"patchInfo {{ type patch; }} }} "
                              for p in names if p != "wall"))

    if quality == "strict":
        min_tet, relaxed_no, n_relaxed, medial = "1e-13", 65, 6, 0.3
    else:
        min_tet, relaxed_no, n_relaxed, medial = "-1e30", 75, 20, 0.5
    (ws / "system" / "snappyHexMeshDict").write_text(
        _HDR.format(cls="dictionary", obj="snappyHexMeshDict") + f"""
castellatedMesh true; snap true; addLayers {'true' if n_layers > 0 else 'false'};
geometry {{ {geom} }}
castellatedMeshControls {{ maxLocalCells {max_cells}; maxGlobalCells {max_cells}; minRefinementCells 10;
  maxLoadUnbalance 0.10; nCellsBetweenLevels 3; features ( {feat_entries} );
  refinementSurfaces {{ {refine_surfs} }} resolveFeatureAngle 30;
  refinementRegions {{ {wall} {{ mode distance; levels (({near_dist:.6g} {near_level})); }} }}
  locationInMesh {vf(interior_point)}; allowFreeStandingZoneFaces true; }}
snapControls {{ nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 8; nFeatureSnapIter 15;
  implicitFeatureSnap false; explicitFeatureSnap true; multiRegionFeatureSnap false; }}
addLayersControls {{ relativeSizes true; layers {{ {wall} {{ nSurfaceLayers {n_layers}; }} }}
  expansionRatio 1.2; finalLayerThickness {first_rel:g}; minThickness 0.05; nGrow 0; featureAngle 130;
  slipFeatureAngle 30; nRelaxIter 8; nSmoothSurfaceNormals 2; nSmoothNormals 3; nSmoothThickness 10;
  maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio {medial}; minMedialAxisAngle 90;
  nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter {n_relaxed}; }}
meshQualityControls {{ maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;
  minVol 1e-13; minTetQuality {min_tet}; minArea -1; minTwist 0.02; minDeterminant 0.001;
  minFaceWeight 0.02; minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;
  relaxed {{ maxNonOrtho {relaxed_no}; maxInternalSkewness 4; }} }}
mergeTolerance 1e-6; debug 0;
""")
    return {"divisions": div, "surface_level": [smin, smax], "feature_level": flevel,
            "location_in_mesh": [round(x, 5) for x in interior_point], "max_cells": max_cells,
            "n_layers": n_layers, "domain_min": [round(x, 4) for x in dmin],
            "domain_max": [round(x, 4) for x in dmax], "patches": list(names.values())}
