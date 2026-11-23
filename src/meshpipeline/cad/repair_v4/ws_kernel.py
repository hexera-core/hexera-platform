# Responsibility: The kernel side of workspace identity - name a shape's entities by fingerprint and
#                 turn a BRepTools_History into a JSON record keyed by those fingerprints.
# Owns: `ws_index`, `history_record` (for op kernel functions to call), and test-only ops.
# Boundaries: runs INSIDE a worker; OCP is imported lazily, so the parent never loads it. The
#             record's meaning to the parent is workspace.py's `apply_history`.
"""Fingerprint entities and serialise BRepTools_History for the workspace."""

from __future__ import annotations

import os
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

KINDS = ("face", "edge", "solid")


def _topabs() -> dict[str, Any]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID

    return {"face": TopAbs_FACE, "edge": TopAbs_EDGE, "solid": TopAbs_SOLID}


def read_brep(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, path, BRep_Builder()):
        raise ValueError(f"cannot read BREP {path!r}")
    return shape


def write_brep(shape: Any, path: str) -> None:
    from OCP.BRepTools import BRepTools

    if not BRepTools.Write_s(shape, path):
        raise ValueError(f"cannot write BREP {path!r}")


def diagonal_of(shape: Any) -> float:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return 0.0
    x0, y0, z0, x1, y1, z1 = box.Get()
    return float(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5)


def fingerprint(kind: str, sub: Any, quantum: float, index: int) -> str:
    """`identity.fingerprint_of` over the localiser's own box. INFORMATIONAL: identity is carried by
    kernel index (see `history_record`), never by this name, because fingerprints can collide. An
    entity with no measurable extent gets `kind:#index`, valid for this shape only."""
    from meshpipeline.cad.repair.identity import fingerprint_of
    from meshpipeline.cad.repair.localize import _centroid_and_box

    return fingerprint_of(kind, _centroid_and_box(sub), quantum=quantum) or f"{kind}:#{index}"


def _maps(shape: Any) -> dict[str, Any]:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out: dict[str, Any] = {}
    for kind, topabs in _topabs().items():
        indexed = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, topabs, indexed)
        out[kind] = indexed
    return out


def entities_of(shape: Any, quantum: float) -> list[dict]:
    """Every face, edge and solid with its 1-based index in the shape's per-kind indexed map."""
    out: list[dict] = []
    for kind, indexed in _maps(shape).items():
        for i in range(1, indexed.Extent() + 1):
            out.append({"type": kind, "index": i,
                        "fp": fingerprint(kind, indexed.FindKey(i), quantum, i)})
    return out


def _kind_of(sub: Any) -> str | None:
    for kind, value in _topabs().items():
        if sub.ShapeType() == value:
            return kind
    return None


def _children(shapes: Any, new_maps: dict[str, Any]) -> list[dict]:
    """The listed shapes as `{type, index}` in the NEW shape. A shape the new shape does not hold
    (index 0) is not an identity the workspace can use, so it is left out."""
    out: list[dict] = []
    for sub in shapes:
        kind = _kind_of(sub)
        if kind is None:
            continue
        index = new_maps[kind].FindIndex(sub)
        if index > 0:
            out.append({"type": kind, "index": index})
    return out


def history_record(old: Any, new: Any, history: Any, *, op: str, params: dict, new_path: str,
                   quantum: float, seconds: float, declared_added: list[str] | None = None,
                   declared_removed: list[str] | None = None) -> dict:
    """The record `workspace.apply_history` consumes. Entities are matched by KERNEL INDEX, not by
    fingerprint: for each OLD entity, `now` is its own index in the new shape (0 if absent),
    `removed` is history.IsRemoved, and `modified`/`generated` are child indices in the new shape.
    `declared_added`/`declared_removed` are caller-supplied claims, passed through unchecked."""
    new_maps = _maps(new)
    rows: list[dict] = []
    for kind, indexed in _maps(old).items():
        for i in range(1, indexed.Extent() + 1):
            sub = indexed.FindKey(i)
            rows.append({
                "type": kind, "index": i,
                "now": int(new_maps[kind].FindIndex(sub)),
                "removed": bool(history.IsRemoved(sub)),
                "modified": _children(history.Modified(sub), new_maps),
                "generated": _children(history.Generated(sub), new_maps),
            })
    return {
        "op": op, "params": params, "brep_path": new_path, "seconds": seconds,
        "declared_added": list(declared_added or []),
        "declared_removed": list(declared_removed or []),
        "entities": entities_of(new, quantum), "history": rows,
    }


@register("ws_index")
def _ws_index(payload: dict) -> dict:
    """Name every face, edge and solid of a BREP. Picks the fingerprint quantum if none is given."""
    from meshpipeline.cad.repair.identity import quantum_for

    shape = read_brep(payload["brep_path"])
    quantum = float(payload.get("quantum") or quantum_for(diagonal_of(shape)))
    return {"quantum": quantum, "entities": entities_of(shape, quantum)}


def _register_test_fns() -> None:
    @register("ws_face_ids_at_z")
    def _faces_at_z(payload: dict) -> dict:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedMapOfShape

        shape = read_brep(payload["brep_path"])
        indexed = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, _topabs()["face"], indexed)
        found: list[str] = []
        indices: list[int] = []
        for i in range(1, indexed.Extent() + 1):
            box = Bnd_Box()
            BRepBndLib.Add_s(indexed.FindKey(i), box)
            _, _, z0, _, _, z1 = box.Get()
            if abs(z0 - payload["z"]) < 1e-6 and abs(z1 - payload["z"]) < 1e-6:
                found.append(fingerprint("face", indexed.FindKey(i), payload["quantum"], i))
                indices.append(i)
        return {"fingerprints": found, "indices": indices}

    @register("ws_split_face_by_edge")
    def _split_face_by_edge(payload: dict) -> dict:
        """Test op: split the solid's faces with one straight edge lying on a face, keeping the
        BRepTools_History the Splitter fills. Real ops (Tasks 11-15) return the same record."""
        import time

        from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
        from OCP.gp import gp_Pnt
        from OCP.TopTools import TopTools_ListOfShape

        start = time.perf_counter()
        old = read_brep(payload["brep_path"])
        args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        args.Append(old)
        tools.Append(BRepBuilderAPI_MakeEdge(gp_Pnt(*payload["p1"]), gp_Pnt(*payload["p2"])).Edge())
        splitter = BRepAlgoAPI_Splitter()
        splitter.SetArguments(args)
        splitter.SetTools(tools)
        splitter.SetToFillHistory(True)
        splitter.Build()
        if not splitter.IsDone():
            raise RuntimeError("split failed")
        new = splitter.Shape()
        write_brep(new, payload["out_path"])
        return history_record(old, new, splitter.History(), op="split_face_by_edge",
                              params={"p1": payload["p1"], "p2": payload["p2"]},
                              new_path=payload["out_path"], quantum=payload["quantum"],
                              seconds=time.perf_counter() - start)

    @register("ws_cut_box")
    def _cut_box(payload: dict) -> dict:
        """Test op: cut an axis-aligned box out of the part, keeping the Cut's BRepTools_History.
        Faces the box only trims are Modified 1:1 with a changed bounding box."""
        import time

        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        from OCP.TopTools import TopTools_ListOfShape

        start = time.perf_counter()
        old = read_brep(payload["brep_path"])
        tool = BRepPrimAPI_MakeBox(gp_Pnt(*payload["corner"]), *payload["size"]).Shape()
        args, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        args.Append(old)
        tools.Append(tool)
        cut = BRepAlgoAPI_Cut()
        cut.SetArguments(args)
        cut.SetTools(tools)
        cut.SetToFillHistory(True)
        cut.Build()
        if not cut.IsDone():
            raise RuntimeError("cut failed")
        new = cut.Shape()
        write_brep(new, payload["out_path"])
        return history_record(old, new, cut.History(), op="cut_box",
                              params={"corner": payload["corner"], "size": payload["size"]},
                              new_path=payload["out_path"], quantum=payload["quantum"],
                              seconds=time.perf_counter() - start)


if os.environ.get("HEXERA_TEST_KERNEL_FNS") == "1":
    _register_test_fns()
