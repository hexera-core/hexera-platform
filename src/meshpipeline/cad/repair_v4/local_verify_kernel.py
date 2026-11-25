# Responsibility: the kernel half of search_seq's local verification (lane C17): layer 1's rules on a CHOSEN set of
#                 solids, overlap against their box-neighbours, point location, and the touched solids exported alone.
# Owns: the kernel functions `local_check`, `locate_points`.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. Layer 1's rule implementations are
#             `valid_kernel`'s own (reused, never copied), so a local finding means what a full one means.
"""Every index here is 1-based in `TopExp.MapShapes` order of the BREP read, exactly as layer 1 names `S3` and as
`Workspace.indices` records it. The overlap thinness threshold is a fraction of the WHOLE part's diagonal (passed in
as `diag_mm`, else measured on the whole shape), never of the chosen solids' box, so a local overlap verdict is the
one the full check would give on the same pair."""

from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, path, BRep_Builder()):
        raise ValueError(f"could not read BREP {path}")
    return shape


def _box(shape: Any) -> Any:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box


def _diag(box: Any) -> float:
    if box.IsVoid():
        return 0.0
    x0, y0, z0, x1, y1, z1 = box.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def _owners(shape: Any, solids: list, faces: list[int]) -> set[int]:
    """1-based solid indices that contain any of the 1-based face indices."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    if not faces:
        return set()
    all_faces = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, all_faces)
    wanted = [all_faces.FindKey(i) for i in faces if 1 <= i <= all_faces.Extent()]
    owners: set[int] = set()
    for s_index, solid in enumerate(solids, start=1):
        own = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(solid, TopAbs_FACE, own)
        if any(own.Contains(face) for face in wanted):
            owners.add(s_index)
    return owners


def _solid_findings(solid: Any) -> list[str]:
    """Layer 1's per-solid hard codes on one solid, by `valid_kernel`'s own rules."""
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    codes: list[str] = []
    if not BRepCheck_Analyzer(solid).IsValid():
        codes.append("brep_invalid")
    if vk._free_edge_count(solid):
        codes.append("open_shell")
    if vk._non_manifold_edges(solid):
        codes.append("non_manifold")
    if vk._volume(solid) <= 0:
        codes.append("non_positive_volume")
    if vk._self_intersects_once(solid):
        codes.append("self_intersection")
    return codes


def _overlap(a: Any, b: Any, va: float, vb: float, thin: float) -> dict | None:
    """`valid_kernel._overlapping_pairs`' rule for ONE pair: the overlap detail, or None when the two only touch."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    common = vk._common(a, b, thin)
    if common is None:
        return {"boolean_failed": True}
    overlap = vk._volume(common.Shape())
    if overlap <= vk.OVERLAP_FRACTION * min(abs(va), abs(vb)):
        return None
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(common.Shape(), surface)
    thickness = 2 * overlap / surface.Mass() if surface.Mass() > 0 else 0.0
    return {"volume": overlap, "thickness": thickness} if thickness > thin else None


@register("local_check")
def _local_check(payload: dict) -> dict:
    """Layer 1 on the chosen solids of `payload['brep']`.

    payload: `solids` (1-based), `faces` (1-based; their owning solids are added), `neighbours` (bool: also test
    overlap against every solid whose box meets a chosen one), `diag_mm` (the whole part's diagonal; measured when
    absent), `rules` (False: only the region, no rule runs), `export` (a BREP path: the chosen solids, neighbours excluded, are written there as one compound).

    Returns `solids` (the chosen 1-based indices, sorted), `neighbours` (the added ones), `codes` ({index: [codes]}
    for the chosen solids), `overlaps` ([{pair: [i, j], ...detail}] among chosen + neighbours, at least one side
    chosen), `tested_pairs`, `n_solids`.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    shape = _read(payload["brep"])
    solids = vk._children(shape, TopAbs_SOLID)
    n = len(solids)
    chosen = {i for i in payload.get("solids", []) if 1 <= i <= n} | _owners(shape, solids, payload.get("faces", []))
    diag = float(payload.get("diag_mm") or 0.0) or _diag(_box(shape))
    thin = vk.OVERLAP_THICKNESS_FRACTION * diag
    boxes = {i: _box(solids[i - 1]) for i in range(1, n + 1)}
    neighbours: set[int] = set()
    if payload.get("neighbours"):
        neighbours = {j for j in range(1, n + 1) if j not in chosen
                      and any(not boxes[i].IsOut(boxes[j]) for i in chosen)}
    group = sorted(chosen | neighbours)
    if payload.get("rules") is False:  # only the region (`local_verify.touched_bodies`)
        return {"solids": sorted(chosen), "neighbours": sorted(neighbours), "codes": {}, "overlaps": [],
                "tested_pairs": [], "n_solids": n}
    volumes = {i: vk._volume(solids[i - 1]) for i in group}
    codes = {i: _solid_findings(solids[i - 1]) for i in sorted(chosen)}
    tested, overlaps = [], []
    for x, i in enumerate(group):
        for j in group[x + 1:]:
            if (i not in chosen and j not in chosen) or boxes[i].IsOut(boxes[j]):
                continue
            tested.append([i, j])
            found = _overlap(solids[i - 1], solids[j - 1], volumes[i], volumes[j], thin)
            if found is not None:
                overlaps.append({"pair": [i, j], **found})
    if payload.get("export"):
        compound = TopoDS_Compound()
        builder = BRep_Builder()
        builder.MakeCompound(compound)
        for i in sorted(chosen):
            builder.Add(compound, solids[i - 1])
        BRepTools.Write_s(compound, payload["export"])
    return {"solids": sorted(chosen), "neighbours": sorted(neighbours), "codes": {str(i): c for i, c in codes.items()},
            "overlaps": overlaps, "tested_pairs": tested, "n_solids": n}


@register("locate_points")
def _locate_points(payload: dict) -> dict:
    """For each point in `payload['xyz']`, the 1-based indices of the solids within `payload['tol_mm']` of it (on or
    inside). Used to re-locate a defect whose entities the ops' history no longer names."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_SOLID

    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    shape = _read(payload["brep"])
    solids = vk._children(shape, TopAbs_SOLID)
    tol = float(payload.get("tol_mm", 1e-3))
    found: set[int] = set()
    for xyz in payload.get("xyz", []):
        point = gp_Pnt(*map(float, xyz))
        vertex = BRepBuilderAPI_MakeVertex(point).Vertex()
        for i, solid in enumerate(solids, start=1):
            if i in found:
                continue
            state = BRepClass3d_SolidClassifier(solid, point, tol).State()
            if state in (TopAbs_IN, TopAbs_ON):
                found.add(i)
                continue
            dist = BRepExtrema_DistShapeShape(vertex, solid)
            if dist.IsDone() and dist.Value() <= tol:
                found.add(i)
    return {"solids": sorted(found)}
