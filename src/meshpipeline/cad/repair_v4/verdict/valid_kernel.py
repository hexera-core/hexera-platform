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

OVERLAP_FRACTION = 1e-9  # Common volume above this fraction of the smaller body is overlap
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


def _non_manifold_edges(unit) -> list[str]:
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
            bad.append(f"E{i}")
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


def _overlapping_pairs(solids: list, volumes: list[float]) -> list[dict]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBndLib import BRepBndLib

    boxes = []
    for solid in solids:
        box = Bnd_Box()
        BRepBndLib.Add_s(solid, box)
        boxes.append(box)
    found = []
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            if boxes[i].IsOut(boxes[j]):
                continue
            common = BRepAlgoAPI_Common(solids[i], solids[j])
            if not common.IsDone():
                found.append(_finding("body_overlap", [f"S{i + 1}", f"S{j + 1}"], boolean_failed=True))
                continue
            overlap = _volume(common.Shape())
            smaller = min(abs(volumes[i]), abs(volumes[j]))
            if overlap > OVERLAP_FRACTION * smaller:
                found.append(_finding("body_overlap", [f"S{i + 1}", f"S{j + 1}"], volume=overlap))
    return found


def _soft_findings(shape) -> list[dict]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return []
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    diag = ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5
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


def check_shape(brep_path: str) -> dict:
    """Run every layer 1 rule that needs the kernel on the BREP at `brep_path`."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, brep_path, BRep_Builder()):
        raise ValueError(f"could not read BREP {brep_path}")

    solids = _children(shape, TopAbs_SOLID)
    shells = _free_shells(shape)
    hard: list[dict] = []
    if not solids:
        hard.append(_finding("no_solids", [], free_shells=len(shells)))

    bad = _invalid_entities(shape)
    if bad:
        hard.append(_finding("brep_invalid", bad))

    units = [(f"S{i}", s) for i, s in enumerate(solids, start=1)] + [
        (f"H{i}", s) for i, s in enumerate(shells, start=1)
    ]
    for name, unit in units:
        free = _free_edge_count(unit)
        if free:
            hard.append(_finding("open_shell", [name], free_edges=free))
        non_manifold = _non_manifold_edges(unit)
        if non_manifold:
            hard.append(_finding("non_manifold", [name, *non_manifold]))

    volumes = [_volume(solid) for solid in solids]
    for i, volume in enumerate(volumes, start=1):
        if volume <= 0:
            hard.append(_finding("non_positive_volume", [f"S{i}"], volume=volume))
    for i, solid in enumerate(solids, start=1):
        if _self_intersects(solid):
            hard.append(_finding("self_intersection", [f"S{i}"]))
    hard.extend(_overlapping_pairs(solids, volumes))

    return {"solids": len(solids), "hard": hard, "soft": _soft_findings(shape)}


@register("check_valid")
def _check_valid(payload: dict) -> dict:
    return check_shape(payload["brep"])
