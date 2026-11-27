# Responsibility: the facet_crossing_local class's dedicated repairs (CRS v5 round 2, fix G): split a face finely by
#                 its MEASURED turn (any surface type, any angle), and cut a knife edge's thin tip off at a measured
#                 width - each a parent-side `Op` plus its kernel function.
# Owns: `SplitTurn`, `CutKnife` (`OPS`), `op_split_turn` / `op_cut_knife`, `turn_cuts` (the turn plan of one face).
# Boundaries: the parent half never loads OCCT. Parameters (the angle, the width, the reach) come from the measured
#             cause (`repair_v5.facet_local`); the kernel only measures and declines what breaks them. The split
#             family's guard/finish (`split._finish`, `settle_same_parameter`) and gap_split's trimmed iso-curves are
#             reused, not copied. Both ops are `v5_class_only`: v4's proposer and det sweep never see them.
"""Two repairs for facets that cross within a face or across a shared edge.

`split_turn` - THE FINE ANGULAR SPLIT. `split_angle` cannot do it: its schema floors `max_deg` at 30 (the oracle's
ladder was 45/60/90) and its divider, `ShapeUpgrade_ShapeDivideAngle`, only cuts surfaces of revolution - on a closed
B-spline tube it returns "nothing to divide" (fix B, f62ed385ec02). This op samples the face's normal along each
parameter direction (rows across the other one, the largest taken), and cuts the direction into equal-turn pieces of at
most `max_deg` along trimmed iso-curves (`gap_split.iso_edges`) in one `BRepAlgoAPI_Splitter`. It moves no geometry:
the deviation is zero, the volume is the integrator's (measured by the loss guard). A mesher whose element size h is
far above a face's radius puts nodes only at that face's vertices, so a 180-degree half-cylinder meshes flat onto its
twin and a 20-degree lens arc onto its chord; vertices every `max_deg` keep each piece's facets on its own side.

`cut_knife` - THE KNIFE-EDGE CUT. Two faces meeting at a near-zero interior angle are a wedge whose thickness grows
from nothing; below the mesher's tolerance its two sides' facets fold onto each other. The op marches from the shared
edge along the wedge's bisector into the solid, ray-casting the thickness across it, to the first point where it
reaches `width_mm` (the fidelity bound), and splits the solid there with a bounded plane (`BRepAlgoAPI_Splitter`); the
tip piece is dropped. So nothing thicker than `width_mm` is removed, and nothing farther than `max_reach_mm` from the
edge. It declines when the wedge never reaches the width inside the body (the thin region is the whole body: dropping
it is a body loss, the loss guard's to refuse), when the edge is curved, or when the cut would split another body.
Measured: the reach, the thickness at the cut, the removed volume and its share of the body, and `old_to_new` (the
farthest point of the knife edge from the result).
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import check_call, run_kernel_op
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

#: normal samples along a direction, and rows across the other one, for a face's turn
TURN_SAMPLES = 96
TURN_ROWS = 5
#: at most this many pieces per face per direction (a full turn at 10 degrees), and iso-curves per call
MAX_PIECES = 36
MAX_ISO_EDGES = 1200
#: cut_knife: march samples along the bisector, bisection steps on the crossing, the largest wedge angle it cuts
KNIFE_SAMPLES = 96
KNIFE_REFINE = 8
KNIFE_MAX_DEG = 30.0


class _LocalOp:
    name = ""
    kernel_fn = ""
    params_schema: dict = {}
    #: aimed only by v5's facet_crossing_local class menu (parameters from its measured cause): never offered to
    #: v4's proposer (`proposer.offered_ops`) nor swept by its det generator
    v5_class_only = True

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        return run_kernel_op(pool, ws, kernel_fn=self.kernel_fn, op=self.name,
                             payload={"faces": [ws.indices[t] for t in targets], "params": dict(params),
                                      "target_ids": list(targets)},
                             timeout_s=timeout_s)


class SplitTurn(_LocalOp):
    name = "split_turn"
    kernel_fn = "op_split_turn"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "face", "min": 1, "max": 32},
        # any angle (split_angle's floor is 30); 2 degrees is 180 pieces a turn, far past MAX_PIECES already
        "properties": {"max_deg": {"type": "number", "minimum": 2, "maximum": 90}},
        "required": ["max_deg"],
        "additionalProperties": False,
    }


class CutKnife(_LocalOp):
    name = "cut_knife"
    kernel_fn = "op_cut_knife"
    params_schema = {
        "type": "object",
        "x-targets": {"kind": "face", "min": 2, "max": 2},   # the two faces of the knife edge
        "properties": {
            # nothing thicker than this is cut off (the fidelity bound: min(0.1 mm, 1e-3 x diagonal))
            "width_mm": {"type": "number", "minimum": 1e-6, "maximum": 0.1},
            # nothing farther from the edge than this (the cause's: half the element size)
            "max_reach_mm": {"type": "number", "minimum": 1e-6, "maximum": 1000},
        },
        "required": ["width_mm", "max_reach_mm"],
        "additionalProperties": False,
    }


OPS: tuple[Any, ...] = (SplitTurn(), CutKnife())


# --- kernel side: split_turn ---------------------------------------------------------------------------------------


def turn_cuts(face: Any, max_deg: float) -> dict:
    """The parameter values that cut `face` into pieces turning at most `max_deg` each, per direction:
    `{"u": [...], "v": [...], "u_turn": deg, "v_turn": deg}`. The turn along a direction is the normal's accumulated
    angle along `TURN_ROWS` rows across the other direction (the largest row); a row with an undefined normal (a pole)
    is skipped. Cuts too close to one of the face's vertices are dropped (a sliver edge is worse than the fold)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad.repair.gap_split import VERTEX_CLEARANCE, _vertex_parameters

    surface = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    out: dict[str, Any] = {}
    for which in ("u", "v"):
        a0, a1, b0, b1 = (u0, u1, v0, v1) if which == "u" else (v0, v1, u0, u1)
        cum = [0.0] * (TURN_SAMPLES + 1)
        for r in range(TURN_ROWS):
            b = b0 + (b1 - b0) * (r + 0.5) / TURN_ROWS
            row, prev, acc = [0.0], None, 0.0
            for k in range(TURN_SAMPLES + 1):
                a = a0 + (a1 - a0) * k / TURN_SAMPLES
                u, v = (a, b) if which == "u" else (b, a)
                props = BRepLProp_SLProps(surface, u, v, 1, 1e-9)
                if not props.IsNormalDefined():
                    break
                normal = props.Normal()
                if prev is not None:
                    acc += math.degrees(prev.Angle(normal))
                    row.append(acc)
                prev = normal
            if len(row) == TURN_SAMPLES + 1:
                cum = [max(x, y) for x, y in zip(cum, row, strict=True)]
        total = cum[-1]
        pieces = min(MAX_PIECES, max(1, math.ceil(total / max_deg - 1e-9)))
        cuts: list[float] = []
        for j in range(1, pieces):
            target = total * j / pieces
            k = next(i for i in range(1, TURN_SAMPLES + 1) if cum[i] >= target)
            lo, hi = cum[k - 1], cum[k]
            t = (k - 1 + ((target - lo) / (hi - lo) if hi > lo else 0.0)) / TURN_SAMPLES
            cuts.append(a0 + (a1 - a0) * t)
        if cuts:
            span = (a1 - a0) / pieces
            taken = _vertex_parameters(face, which)
            cuts = [c for c in cuts if all(abs(c - t) >= VERTEX_CLEARANCE * span for t in taken)]
        out[which], out[f"{which}_turn"] = cuts, round(total, 3)
    return out


def _turn_split(old: Any, payload: dict, max_deg: float) -> tuple[Any, Any, dict, int] | str:
    """(new shape, history, plan, iso-curves) of splitting the targets at `max_deg`, or why not."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad.repair.gap_split import iso_edges
    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.ops.split import guard, settle_same_parameter

    faces = ws_kernel._maps(old)["face"]
    edges: list[Any] = []
    plan: dict[str, Any] = {}
    for index, sid in zip(payload["faces"], payload["target_ids"], strict=True):
        face = TopoDS.Face_s(faces.FindKey(index))
        cuts = turn_cuts(face, max_deg)
        plan[sid] = {"u_turn": cuts["u_turn"], "v_turn": cuts["v_turn"],
                     "pieces": [len(cuts["u"]) + 1, len(cuts["v"]) + 1]}
        if cuts["u"] or cuts["v"]:
            edges.extend(iso_edges(face, {"u": cuts["u"], "v": cuts["v"]}))
        if len(edges) > MAX_ISO_EDGES:
            return f"too_fine: more than {MAX_ISO_EDGES} iso-curves at {max_deg:g} deg"
    if not edges:
        return f"no_change: every target turns under {max_deg:g} deg in each direction ({plan})"
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(old)
    for edge in edges:
        tools.Append(edge)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.SetToFillHistory(True)
    splitter.SetRunParallel(False)  # identical runs must give identical shapes
    try:
        splitter.Build()
    except Exception as exc:  # noqa: BLE001 - a kernel refusal is a decline
        return f"split_failed: the splitter raised {type(exc).__name__}"
    if not splitter.IsDone():
        return "split_failed: the splitter reported errors"
    new = splitter.Shape()
    settle_same_parameter(old, new)
    why = guard(old, new)
    return why if why is not None else (new, splitter.History(), plan, len(edges))


#: when the split is refused (BRepCheck), the op retries this many divisors FINER only (the sag bound holds): on
#: f62ed385ec02 the closed B-spline tubes split valid at 30 and 20 deg and invalid at 28.7 and 27.7
RETRY_FINER = 2


@register_kernel_fn("op_split_turn")
def _split_turn(payload: dict) -> dict:
    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.ops.split import _finish

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    # the angle used is the asked one rounded DOWN to 90/n (`facet_local.snap_turn`: the pieces of one revolved
    # surface's quarter, half and full faces then meet on common angles)
    first = math.ceil(90.0 / float(params["max_deg"]) - 1e-9)
    tried: list[str] = []
    for n in range(first, first + RETRY_FINER + 1):
        max_deg = 90.0 / n
        got = _turn_split(old, payload, max_deg)
        if not isinstance(got, str):
            break
        tried.append(f"{max_deg:g} deg: {got}")
        if not got.startswith(("invalidated", "split_failed")):
            return {"declined": got}
    else:
        return {"declined": "; ".join(tried)}
    new, history, plan, curves = got
    # a split moves no surface: the deviation is zero by construction (the volume is the loss guard's to measure)
    params["measured"] = {"old_to_new": 0.0, "iso_curves": curves, "used_deg": round(max_deg, 3), "plan": plan,
                          **({"refused_first": tried} if tried else {})}
    return _finish(old, new, history, payload, op="split_turn", params=params, start=start)


# --- kernel side: cut_knife ----------------------------------------------------------------------------------------


def _on_face_dir(face: Any, p: Any, along: Any, probe: float, slack: float) -> Any | None:
    """The unit direction (`along` or its reverse) that runs from `p` INTO `face` (the side whose probe point lies
    on the face, within a quarter probe plus `slack` - the edge's own tolerance), or None when neither does."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    best, gap = None, None
    for sign in (1.0, -1.0):
        q = gp_Pnt(p.X() + sign * probe * along.X(), p.Y() + sign * probe * along.Y(),
                   p.Z() + sign * probe * along.Z())
        dist = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(q).Vertex(), face)
        if dist.IsDone() and (gap is None or dist.Value() < gap):
            best, gap = sign, float(dist.Value())
    if best is None or gap is None or gap > 0.25 * probe + slack:
        return None
    return along.Reversed() if best < 0 else along


def _state(solid: Any, q: Any, tol: float) -> str:
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_OUT

    got = BRepClass3d_SolidClassifier(solid, q, tol).State()
    return "in" if got == TopAbs_IN else "out" if got == TopAbs_OUT else "on"


def _span(solid: Any, q: Any, across: Any, tol: float) -> tuple[float, float] | None:
    """(below, above): the signed parameters of the nearest boundary hits either side of `q` along `across` - the
    wedge's two walls, whether `q` is in material (a solid knife) or in a void (a crack). None without both."""
    from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
    from OCP.gp import gp_Dir, gp_Lin

    inter = BRepIntCurveSurface_Inter()
    inter.Init(solid, gp_Lin(q, gp_Dir(across.X(), across.Y(), across.Z())), tol)
    below, above = None, None
    while inter.More():
        w = float(inter.W())
        if w > 0 and (above is None or w < above):
            above = w
        if w < 0 and (below is None or w > below):
            below = w
        inter.Next()
    return None if below is None or above is None else (below, above)


def _knife_frame(fa: Any, fb: Any, edge: Any, width: float, reach: float) -> dict | str:
    """The knife edge's frame: its midpoint `p`, unit `tangent`, the wedge's `bisector` (into the wedge, between the
    two faces) and `across` (bisector x tangent), its `length` and inside angle - or why there is no knife."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.gp import gp_Ax3, gp_Dir, gp_Vec

    from meshpipeline.cad.repair_v5.quality_kernel import _normal_at

    curve = BRepAdaptor_Curve(edge)
    t0, t1 = curve.FirstParameter(), curve.LastParameter()
    p0, p1, p = curve.Value(t0), curve.Value(t1), curve.Value(0.5 * (t0 + t1))
    chord = gp_Vec(p0, p1)
    if chord.Magnitude() <= 1e-9:
        return "degenerate: the knife edge has no length"
    tangent = chord.Normalized()
    sag = gp_Vec(p0, p).Crossed(tangent).Magnitude()
    if sag > width:
        return f"curved_edge: the knife edge sags {sag:.3g} mm off its chord (over {width:g} mm)"
    na, nb = _normal_at(fa, p), _normal_at(fb, p)
    if na is None or nb is None:
        return "unmeasurable: a normal is undefined at the knife edge"
    probe = max(1e-4, min(0.05 * chord.Magnitude(), 0.1 * reach))
    slack = float(BRep_Tool.Tolerance_s(edge))
    da = _on_face_dir(fa, p, gp_Vec(na).Crossed(tangent).Normalized(), probe, slack)
    db = _on_face_dir(fb, p, gp_Vec(nb).Crossed(tangent).Normalized(), probe, slack)
    if da is None or db is None:
        return "unmeasurable: no in-face direction from the knife edge"
    wedge = math.degrees(da.Angle(db))
    if wedge > KNIFE_MAX_DEG:
        return f"no_knife: the faces meet at {wedge:.3g} deg inside (over {KNIFE_MAX_DEG:g})"
    bis = da.Added(db).Normalized()
    ax = gp_Ax3(p, gp_Dir(bis.X(), bis.Y(), bis.Z()), gp_Dir(tangent.X(), tangent.Y(), tangent.Z()))
    return {"p": p, "tangent": tangent, "bisector": bis, "across": gp_Vec(ax.YDirection()), "ax": ax,
            "length": chord.Magnitude(), "wedge_deg": wedge}


def _march(solid: Any, frame: dict, width: float, reach: float, tol: float) -> dict | str:
    """Walk the bisector from the edge: the wedge is MATERIAL (a solid knife) or a VOID (a crack) by the first
    samples' state; `cut` = the first distance where its walls are `width` apart (bisected), or - a crack only - where
    the crack ends in material (all of it is thin). A solid knife that leaves the body first is a thin BODY."""
    from OCP.gp import gp_Pnt

    p, bis, across = frame["p"], frame["bisector"], frame["across"]

    def at(s: float) -> Any:
        return gp_Pnt(p.X() + s * bis.X(), p.Y() + s * bis.Y(), p.Z() + s * bis.Z())

    step = reach / KNIFE_SAMPLES
    states = [_state(solid, at(k * step), tol) for k in (1, 2, 3)]
    kind = "crack" if states.count("out") > states.count("in") else "solid"
    inside = "out" if kind == "crack" else "in"

    def wide(s: float) -> bool | str:
        if _state(solid, at(s), tol) not in (inside, "on"):
            return "left"
        got = _span(solid, at(s), across, tol)
        if got is None:
            return "open"
        return got[1] - got[0] >= width

    lo, cut, end = 0.0, None, "wide"
    for k in range(1, KNIFE_SAMPLES + 1):
        got = wide(k * step)
        if got in ("left", "open"):
            if kind == "solid":
                return (f"thin_body: the wedge leaves the body {k * step:.4g} mm from the edge still under {width:g} mm "
                        "thick - the thin region is the whole body (a body loss, not a cut)")
            # the crack ends in material, or opens onto the outside at its mouth: all of it is thin, fill all of it
            cut, end = k * step, str(got)
            break
        if got:
            cut = k * step
            break
        lo = k * step
    if cut is None:
        last = _span(solid, at(reach), across, tol)
        return (f"too_long: the {kind} wedge is under {width:g} mm thick for the whole {reach:.4g} mm reach (at its "
                f"end: {'no wall on one side' if last is None else f'{last[1] - last[0]:.4g} mm'}; states {states})")
    hi = cut
    for _ in range(KNIFE_REFINE):
        mid = 0.5 * (lo + hi)
        if wide(mid) is False:
            lo = mid
        else:
            hi = mid
    span = _span(solid, at(lo), across, tol) or (-width, width)
    # a crack open at its mouth is filled to its last walled point (never past it into the outside)
    return {"kind": kind, "cut": lo if end == "open" else hi, "span": span, "thickness": span[1] - span[0],
            "end": end}


@register_kernel_fn("op_cut_knife")
def _cut_knife(payload: dict) -> dict:  # noqa: PLR0911 - one decline per measured reason
    """Cut a solid knife's thin tip off, or fill a crack's thin end, out to where the wedge is `width_mm` thick."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRepTools import BRepTools_ReShape
    from OCP.gp import gp_Ax2, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.ops.quality import _compound, _deviation, _edge_samples, _sub, _valid
    from meshpipeline.cad.repair_v4.ops.shells import _entities, _history_from_lookup, _record
    from meshpipeline.cad.repair_v5.quality_kernel import _ancestors

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    params = dict(payload["params"])
    width, reach, tol = float(params["width_mm"]), float(params["max_reach_mm"]), 1e-7
    maps = ws_kernel._maps(old)
    fa, fb = (TopoDS.Face_s(maps["face"].FindKey(i)) for i in payload["faces"])
    b_edges = TopTools_IndexedMapOfShape()
    for e in _sub(fb, TopAbs_EDGE):
        b_edges.Add(e)
    shared = [TopoDS.Edge_s(e) for e in _sub(fa, TopAbs_EDGE) if b_edges.Contains(e)]
    if not shared:
        return {"declined": "not_adjacent: the two faces share no edge"}
    owners = _ancestors(old, TopAbs_FACE, TopAbs_SOLID)
    solids: list[Any] = []
    for s in (owners.FindFromKey(fa) if owners.Contains(fa) else []):
        if not any(s.IsSame(t) for t in solids):
            solids.append(s)
    if len(solids) != 1:
        return {"declined": f"no_body: the knife's faces bound {len(solids)} solids (one is needed)"}
    solid = TopoDS.Solid_s(solids[0])

    def measure(shape: Any, volume: bool) -> float:
        g = GProp_GProps()
        (BRepGProp.VolumeProperties_s if volume else BRepGProp.LinearProperties_s)(shape, g)
        return abs(float(g.Mass()))

    edge = max(shared, key=lambda e: measure(e, False))
    frame = _knife_frame(fa, fb, edge, width, reach)
    if isinstance(frame, str):
        return {"declined": frame}
    walk = _march(solid, frame, width, reach, tol)
    if isinstance(walk, str):
        return {"declined": walk}
    # THE TOOL: a box over the wedge from just behind the edge to the cut, across its walls there plus a margin
    p, ax, cut, (lo, hi) = frame["p"], frame["ax"], walk["cut"], walk["span"]
    # a cut reaches a margin past the edge's ends (into the air beside a solid knife); a fill stops at them (past
    # them it would add material outside the body's side walls)
    margin = width
    half = 0.5 * frame["length"] + (width if walk["kind"] == "solid" else 0.0)
    x, y, z = ax.XDirection(), ax.YDirection(), ax.Direction()
    corner = gp_Pnt(*(c - half * xx + (lo - margin) * yy - margin * zz for c, xx, yy, zz in
                      zip((p.X(), p.Y(), p.Z()), (x.X(), x.Y(), x.Z()), (y.X(), y.Y(), y.Z()), (z.X(), z.Y(), z.Z()),
                          strict=True)))
    box = BRepPrimAPI_MakeBox(gp_Ax2(corner, z, x), 2.0 * half, hi - lo + 2.0 * margin, cut + margin).Shape()
    try:
        boolean = (BRepAlgoAPI_Cut if walk["kind"] == "solid" else BRepAlgoAPI_Fuse)(solid, box)
    except Exception as exc:  # noqa: BLE001 - a kernel refusal is a decline
        return {"declined": f"boolean_failed: the Boolean raised {type(exc).__name__}"}
    if not boolean.IsDone():
        return {"declined": f"boolean_failed: the {'cut' if walk['kind'] == 'solid' else 'fill'} reported errors"}
    # the fill's box faces lying on the solid's own walls are merged back (no new seams on the walls)
    unify = ShapeUpgrade_UnifySameDomain(boolean.Shape(), True, True, False)
    unify.Build()
    pieces = _entities(unify.Shape(), "solid")
    if len(pieces) != 1:
        return {"declined": f"split_failed: the {walk['kind']} left {len(pieces)} solids where one was"}
    reshape = BRepTools_ReShape()
    reshape.Replace(solid, pieces[0])
    new = reshape.Apply(old)
    if len(_entities(new, "solid")) != len(_entities(old, "solid")):
        return {"declined": "split_failed: the solid count changed"}
    if _valid(old) and not _valid(new):
        return {"declined": "invalidated: the result is not BRepCheck-valid"}
    kept = TopTools_IndexedMapOfShape()
    for kind in ("face", "edge", "solid"):
        for sub in _entities(new, kind):
            kept.Add(sub)
    b_hist, u_hist = boolean.History(), unify.History()

    def lookup(sub: Any) -> Any:
        if kept.Contains(sub):
            return sub
        if sub.IsSame(solid):
            return pieces[0]
        def through(history: Any, s: Any) -> list[Any]:
            if history.IsRemoved(s):
                return []
            return list(history.Modified(s)) or [s]

        got: list[Any] = []
        for m in through(b_hist, sub):
            later = through(u_hist, m)
            got.extend(n for n in later if kept.Contains(n) and not any(n.IsSame(g) for g in got))
        if not got:
            return None
        return got[0] if len(got) == 1 else _compound(got)

    history = _history_from_lookup(old, lookup)
    changed = measure(pieces[0], True) - measure(solid, True)
    body = measure(solid, True)
    dev = _deviation(_edge_samples(edge), _compound(_sub(new, TopAbs_FACE)))
    params["measured"] = {"old_to_new": dev, "knife": walk["kind"], "reach_mm": round(cut, 6),
                          "thickness_at_cut_mm": round(walk["thickness"], 6), "wedge_deg": round(frame["wedge_deg"], 5),
                          "volume_change_mm3": changed, "volume_change_share_of_body": changed / body if body else None,
                          "edge_mm": round(frame["length"], 6), "end": walk["end"]}
    return _record(old, new, history, {**payload, "ids": payload.get("ids") or {}}, "cut_knife", params, start,
                   declare_removed=False)


__all__ = ["OPS", "CutKnife", "SplitTurn", "turn_cuts"]
