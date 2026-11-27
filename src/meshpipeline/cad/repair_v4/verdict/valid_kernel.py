# Responsibility: The OCCT half of layer 1 - find what is wrong with one BREP shape.
# Owns: the `check_valid` kernel call and the hard/soft finding rules.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. It judges a
#             shape; reading STEP, the round trip and the verdict's assembly are `valid.py`.
"""Validity findings for one BREP. Hard findings fail the file; soft findings are reported only.

Small edges, small faces and loose tolerances are soft on purpose: when they do real damage it
shows up as a meshing failure in layer 2, and failing them here would put files that mesh fine in
the failing set.
"""

from __future__ import annotations

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.units import UNBOUNDED_DIAGONAL_MM
from meshpipeline.cad.repair_v4.verdict.shape_digest import WorkerMemo, geometry_key

OVERLAP_FRACTION = 1e-9  # Common volume above this fraction of the smaller body is overlap
OVERLAP_THICKNESS_FRACTION = 1e-6  # of the bounding-box diagonal; thinner is contact
MAX_OVERLAP_PAIRS = 2000  # bbox-overlapping body pairs one call will test
SMALL_EDGE_FRACTION = 1e-4  # of the bounding-box diagonal
SMALL_FACE_FRACTION = 1e-8  # of the bounding-box diagonal squared
LARGE_TOLERANCE_FRACTION = 1e-3  # of the bounding-box diagonal


def _finding(code: str, entities: list[str], **detail) -> dict:
    return {"code": code, "entities": entities, "detail": detail}


def _children(shape, kind) -> list:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def _free_shells(shape) -> list:
    """Shells that belong to no solid: a surface model's bodies."""
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    shells = []
    explorer = TopExp_Explorer(shape, TopAbs_SHELL, TopAbs_SOLID)
    while explorer.More():
        shells.append(explorer.Current())
        explorer.Next()
    return shells


def _invalid_entities(shape) -> list[str]:
    from OCP.BRepCheck import BRepCheck_Analyzer, BRepCheck_NoError
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID

    analyzer = BRepCheck_Analyzer(shape)
    bad: list[str] = []
    for letter, kind in (("S", TopAbs_SOLID), ("H", TopAbs_SHELL), ("F", TopAbs_FACE), ("E", TopAbs_EDGE)):
        for index, sub in enumerate(_children(shape, kind), start=1):
            result = analyzer.Result(sub)
            if result is None:
                continue
            statuses = result.Status()
            if any(status != BRepCheck_NoError for status in statuses):
                bad.append(f"{letter}{index}")
    return bad


def _free_edge_count(unit) -> int:
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp

    bounds = ShapeAnalysis_FreeBounds(unit, 1e-7, False, False)
    from OCP.TopTools import TopTools_IndexedMapOfShape

    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(bounds.GetClosedWires(), TopAbs_EDGE, edges)
    TopExp.MapShapes_s(bounds.GetOpenWires(), TopAbs_EDGE, edges)
    return edges.Extent()


def _non_manifold_edges(unit, edge_index=None) -> list[str]:
    """`E<k>` names of `unit`'s non-manifold edges. `k` is the edge's index in `edge_index` (the WHOLE shape's edge
    map) when given: every other layer-1 `E<k>` is a whole-shape index, and `seq.layer1_ids` translates them all by
    that one index. Numbering within the unit (as before v5 step 5) named a different edge on any multi-solid part."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(unit, TopAbs_EDGE, TopAbs_FACE, ancestors)
    bad = []
    for i in range(1, ancestors.Extent() + 1):
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        faces = TopTools_IndexedMapOfShape()
        for face in ancestors.FindFromIndex(i):
            faces.Add(face)  # a seam edge lists its one face twice; the map counts it once
        if faces.Extent() > 2:
            bad.append(f"E{edge_index.FindIndex(edge) if edge_index is not None else i}")
    return bad


def _self_intersects(solid) -> bool:
    from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer

    analyzer = BOPAlgo_ArgumentAnalyzer()
    analyzer.SetShape1(solid)
    analyzer.ArgumentTypeMode = False
    analyzer.SmallEdgeMode = False
    analyzer.RebuildFaceMode = False
    analyzer.TangentMode = False
    analyzer.MergeVertexMode = False
    analyzer.MergeEdgeMode = False
    analyzer.ContinuityMode = False
    analyzer.CurveOnSurfaceMode = False
    analyzer.SelfInterMode = True
    analyzer.Perform()
    return bool(analyzer.HasFaulty())


#: Self-intersection of a solid, keyed on its GEOMETRY plus the tolerances the analyzer reads. `_self_intersects`
#: looks at nothing but the solid, so a solid seen before in this worker - an untouched body of a candidate, a state
#: reached twice, the same part judged by two arms - is not re-analysed. It is most of layer 1's time (Lane A
#: profile: 68% of layers 1+3; Lane C measured check_shape at ~22 s per call, 83% of the healer on one file).
#:
#: A17: the key was `digest(solid)`, which is EXACT but not stable across a STEP round trip - on dev a2807f9a2fef
#: all 3 unchanged solids got new digests after one export and re-read, so the memo almost never hit between
#: judgements. `geometry_key` survives a round trip 290/311 times against the digest's 109/311 (C16). It carries no
#: tolerances and this analysis reads them, so the key adds them: `BRepCheck_Analyzer` with `SelfInterMode` tests
#: faces against each other within their own tolerances, and two shapes identical in geometry but not in tolerance
#: can differ in the answer.
SELF_INTERSECTS = WorkerMemo(4096)
#: Overlap of a PAIR of solids, keyed on both geometries, both tolerance summaries and the fuzzy value the caller
#: derived from the WHOLE shape's diagonal. The stored value is the DECISION, never the finding: a finding carries
#: positional labels (S1, S2) and the same pair can sit at different indices in a later state.
OVERLAP_PAIRS = WorkerMemo(4096)


def _tolerance_key(shape) -> tuple:
    """The tolerances `BRepCheck_Analyzer` and the Booleans actually read, as a key fragment: the max tolerance over
    faces, edges and vertices, each rounded, plus the count of reversed faces (an orientation flip changes no
    geometry and no tolerance, and C16 measured 33 such edits where layer 1's answer was identical - so the count is
    carried to make a change in it a MISS rather than a silent reuse)."""
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX

    tol = ShapeAnalysis_ShapeTolerance()
    maxima = []
    for kind in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
        try:
            maxima.append(round(float(tol.Tolerance(shape, 1, kind)), 12))
        except Exception:  # noqa: BLE001 - an unreadable tolerance must miss, never key as 0.0
            return ("tolerance_unreadable", id(shape))
    reversed_faces = sum(1 for f in _children(shape, TopAbs_FACE) if f.Orientation() == TopAbs_REVERSED)
    return (*maxima, reversed_faces)


def _shape_memo_key(shape) -> tuple:
    """`geometry_key` plus the tolerances: the one key for a memo whose result reads both."""
    return (geometry_key(shape), _tolerance_key(shape))


def _self_intersects_once(solid) -> bool:
    try:
        key = _shape_memo_key(solid)
    except Exception:  # noqa: BLE001 - a key we cannot build is a MISS, never a wrong hit
        return bool(_self_intersects(solid))
    return bool(SELF_INTERSECTS.get_or_compute(key, lambda: _self_intersects(solid)))


def _common(a, b, fuzzy: float):
    """BRepAlgoAPI_Common of two solids, or None if the Boolean did not complete."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    common = BRepAlgoAPI_Common(a, b)
    if common.IsDone():
        return common
    if fuzzy > 0:  # tangent curved contact is where a plain Common most often fails to complete
        common = BRepAlgoAPI_Common()
        from OCP.TopTools import TopTools_ListOfShape

        for setter, shape in ((common.SetArguments, a), (common.SetTools, b)):
            shapes = TopTools_ListOfShape()
            shapes.Append(shape)
            setter(shapes)
        common.SetFuzzyValue(fuzzy)
        common.Build()
        if common.IsDone():
            return common
    return None


def _overlapping_pairs(solids: list, volumes: list[float]) -> list[dict]:
    """Pairs of bodies that really share volume.

    Conformal contact has Common volume ~0, so it never counts; no face-sharing exemption exists
    because one would let identical duplicate bodies pass. Interpenetration thinner than the
    modelling tolerance (1e-6 of the diagonal) is contact, not overlap.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    boxes = []
    whole = Bnd_Box()
    for solid in solids:
        box = Bnd_Box()
        BRepBndLib.Add_s(solid, box)
        whole.Add(box)
        boxes.append(box)
    if whole.IsVoid():
        return []
    xmin, ymin, zmin, xmax, ymax, zmax = whole.Get()
    diag = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5
    thin = OVERLAP_THICKNESS_FRACTION * diag
    candidates = [(i, j) for i in range(len(solids)) for j in range(i + 1, len(solids)) if not boxes[i].IsOut(boxes[j])]
    if len(candidates) > MAX_OVERLAP_PAIRS:
        raise ValueError(f"{len(candidates)} body pairs to test exceeds the cap of {MAX_OVERLAP_PAIRS}")
    found = []
    for i, j in candidates:
        # A17: the Boolean is the expensive half and depends only on the two solids and the fuzzy value, so the
        # DECISION is memoised per pair. The finding is built here, from this state's indices: a stored finding
        # would carry S1/S2 labels that a later state can reorder, which is a wrong answer rather than a slow one.
        decision = _pair_decision(solids[i], solids[j], abs(volumes[i]), abs(volumes[j]), thin)
        if decision is None:
            continue
        found.append(_finding("body_overlap", [f"S{i + 1}", f"S{j + 1}"], **decision))
    return found


def _pair_decision(a, b, va: float, vb: float, thin: float) -> dict | None:
    """Do these two solids really share volume? The finding's KEYWORDS, or None for no overlap.

    Memoised on both geometries, both tolerance summaries and `thin` (which the caller derives from the WHOLE
    shape's diagonal, so the same pair in a differently sized assembly is a different question). Carries no index:
    the caller labels it.
    """
    try:
        key = ("pair", *sorted([_shape_memo_key(a), _shape_memo_key(b)], key=repr),
               round(float(thin), 12), round(float(va), 9), round(float(vb), 9))
    except Exception:  # noqa: BLE001 - no key, no memo; compute it the slow way rather than risk a wrong hit
        return _pair_decision_now(a, b, va, vb, thin)
    return OVERLAP_PAIRS.get_or_compute(key, lambda: _pair_decision_now(a, b, va, vb, thin))


def _pair_decision_now(a, b, va: float, vb: float, thin: float) -> dict | None:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    common = _common(a, b, thin)
    if common is None:
        return {"boolean_failed": True}
    overlap = _volume(common.Shape())
    if overlap <= OVERLAP_FRACTION * min(va, vb):
        return None
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(common.Shape(), surface)
    thickness = 2 * overlap / surface.Mass() if surface.Mass() > 0 else 0.0
    return {"volume": overlap, "thickness": thickness} if thickness > thin else None


def _soft_findings(shape, diag: float | None = None) -> list[dict]:
    """The soft findings, scaled by the shape's diagonal.

    `diag` IS PASSED IN by `check_shape`, measured before anything analysed the shape. It used to be re-measured
    here, which made every soft THRESHOLD depend on what had already run: `BRepCheck_Analyzer` touches the shape in
    place, so a bounding box taken after it differs from one taken before in the last bits. That was invisible
    until A17 memoised the analyzer - skipping it moved a `small_edge` threshold from 0.014493446827583856 to
    ...842 on de9ad40118a4, same edges, 1.4e-17 relative. Measuring once, first, removes the order dependence
    rather than hiding it: the number now cannot depend on which checks ran.
    """
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    if diag is None:
        diag = _bbox_diagonal(shape)
    if diag is None:
        return []
    soft = []
    small_edges = []
    for index, edge in enumerate(_children(shape, TopAbs_EDGE), start=1):
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        if 0 < props.Mass() < SMALL_EDGE_FRACTION * diag:
            small_edges.append(f"E{index}")
    if small_edges:
        soft.append(_finding("small_edge", small_edges, threshold=SMALL_EDGE_FRACTION * diag))
    small_faces = []
    for index, face in enumerate(_children(shape, TopAbs_FACE), start=1):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        if props.Mass() < SMALL_FACE_FRACTION * diag * diag:
            small_faces.append(f"F{index}")
    if small_faces:
        soft.append(_finding("small_face", small_faces, threshold=SMALL_FACE_FRACTION * diag * diag))
    worst = ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1)
    if worst > LARGE_TOLERANCE_FRACTION * diag:
        soft.append(_finding("large_tolerance", [], max_tolerance=worst, threshold=LARGE_TOLERANCE_FRACTION * diag))
    return soft


def _bbox_diagonal(shape) -> float | None:
    """The shape's bounding-box diagonal, or None when the box is void. ONE measurement, taken before any analysis,
    so everything scaled by it agrees."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5


def _unbounded_finding(shape) -> dict | None:
    """`unbounded_geometry` when the shape's bounding box is open or its diagonal is beyond `UNBOUNDED_DIAGONAL_MM`.

    This is decided before anything else is run on the shape: BRepCheck, the Booleans and (later) a mesher all
    take the infinite extent as input, and none of them can say anything useful about it."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    diag = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5
    open_sides = [
        name
        for name, is_open in (
            ("xmin", box.IsOpenXmin()), ("xmax", box.IsOpenXmax()), ("ymin", box.IsOpenYmin()),
            ("ymax", box.IsOpenYmax()), ("zmin", box.IsOpenZmin()), ("zmax", box.IsOpenZmax()),
        )
        if is_open
    ]  # fmt: skip
    if diag == diag and diag <= UNBOUNDED_DIAGONAL_MM and not open_sides:  # `diag == diag`: False for NaN
        return None
    return _finding("unbounded_geometry", [], diagonal_mm=diag, limit_mm=UNBOUNDED_DIAGONAL_MM, open_sides=open_sides)


def check_shape(brep_path: str, *, booleans: bool = True) -> dict:
    """Run every layer 1 rule that needs the kernel on the BREP at `brep_path`. `booleans=False` skips the two
    Boolean rules (self_intersection, body_overlap); `valid` asks for that only where they cannot change a verdict."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, brep_path, BRep_Builder()):
        raise ValueError(f"could not read BREP {brep_path}")

    solids = _children(shape, TopAbs_SOLID)
    # measured BEFORE any analysis touches the shape, and used for every scaled threshold below
    diag = _bbox_diagonal(shape)
    unbounded = _unbounded_finding(shape)
    if unbounded is not None:  # the input is outside what the definition can judge: say so and stop
        return {"solids": len(solids), "hard": [unbounded], "soft": []}
    shells = _free_shells(shape)
    hard: list[dict] = []
    if not solids:
        hard.append(_finding("no_solids", [], free_shells=len(shells)))

    bad = _invalid_entities(shape)
    if bad:
        hard.append(_finding("brep_invalid", bad))

    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    edge_index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edge_index)
    units = [(f"S{i}", s) for i, s in enumerate(solids, start=1)] + [
        (f"H{i}", s) for i, s in enumerate(shells, start=1)
    ]
    for name, unit in units:
        free = _free_edge_count(unit)
        if free:
            hard.append(_finding("open_shell", [name], free_edges=free))
        non_manifold = _non_manifold_edges(unit, edge_index)
        if non_manifold:
            hard.append(_finding("non_manifold", [name, *non_manifold]))

    volumes = [_volume(solid) for solid in solids]
    for i, volume in enumerate(volumes, start=1):
        if volume <= 0:
            hard.append(_finding("non_positive_volume", [f"S{i}"], volume=volume))
    if booleans:
        for i, solid in enumerate(solids, start=1):
            if _self_intersects_once(solid):
                hard.append(_finding("self_intersection", [f"S{i}"]))
        hard.extend(_overlapping_pairs(solids, volumes))

    return {"solids": len(solids), "hard": hard, "soft": _soft_findings(shape, diag)}


@register("check_valid")
def _check_valid(payload: dict) -> dict:
    return check_shape(payload["brep"], booleans=bool(payload.get("booleans", True)))


@register("valid_signed_volumes")
def _valid_signed_volumes(payload: dict) -> dict:
    """Every solid's SIGNED volume, read from the BREP itself (lane C15). Layer 1 judges the STEP re-export, and writing
    and re-reading STEP re-orients an inside-out solid: on dev 0051_00510567 a fused solid of -13.46 mm3 came back as
    +13.46, so layer 1 called the part valid. Only the BREP still shows it."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, payload["brep_path"], BRep_Builder())
    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    return {"volumes": [_volume(solids.FindKey(i)) for i in range(1, solids.Extent() + 1)]}


@register("valid_extent")
def _valid_extent(payload: dict) -> dict:
    """Is the BREP unbounded, by the very rule `check_shape` stops at? A bounding box only, so it is cheap.

    It decides nothing about the file. `valid.check_valid` asks it first so that it can run the round trip
    alongside the shape check without ever writing back an infinite part; the report still comes from
    `check_shape` alone."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, payload["brep"], BRep_Builder()):
        raise ValueError(f"could not read BREP {payload['brep']}")
    finding = _unbounded_finding(shape)
    # the finding's own detail rides along, so a state found unbounded can be REPORTED as layer 1 would report it
    # (`search._judged`, lane C9) without running the rest of check_valid on an infinite shape
    return {"unbounded": finding is not None, "finding": finding}
