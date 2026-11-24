# Responsibility: The kernel side of workspace identity - name a shape's entities by fingerprint and
#                 turn a BRepTools_History into a JSON record keyed by those fingerprints.
# Owns: `ws_index`, `history_record` (for op kernel functions to call), and test-only ops.
# Boundaries: runs INSIDE a worker; OCP is imported lazily, so the parent never loads it. The
#             record's meaning to the parent is workspace.py's `apply_history`.
"""Fingerprint entities and serialise BRepTools_History for the workspace."""

from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.settings.cad_repair import kernel_test_fns_enabled

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


def part_diagonal(payload: dict, shape: Any) -> float:
    """The size an op's RELATIVE thresholds are made against: the lineage's ORIGINAL diagonal (`part_diag_mm`, which
    every op payload carries from `Workspace.part_diag_mm`), and `diagonal_of(shape)` only when a caller sent none.

    Lane C13, measured on dev 263d5e2d5bab: `diagonal_of` is plain `BRepBndLib.Add`, which bounds a B-spline face by
    its CONTROL NET. A located defeature made 7 B-spline faces whose poles sit far outside them: the state's plain
    diagonal went 441 -> 1237 mm (2.8x) while its optimal box is 446.5 mm, its volume unchanged and the shape valid.
    A threshold sized from the CURRENT shape inherits that, and pole distance has no ceiling. At the measured 2.8x the
    effect is small (the zero-volume eps 1.89 mm3 instead of 0.089; tighten_tolerances' target and the proximity split
    size 2.8x looser; the fidelity bounds are capped at 0.1 mm on any part over 100 mm), so this is a consistency fix:
    a threshold belongs to the part as it arrived, measured once (plain and optimal agree within 0-3% on 40 of 42 dev
    overlap inputs), and must not drift from state to state along a lineage. (An earlier "78x / 34.66 m" figure was
    retracted by Lane A: it was inferred from a D18 sliver width, never measured.)"""
    from meshpipeline.cad.repair_v4.units import UNBOUNDED_DIAGONAL_MM

    given = payload.get("part_diag_mm")
    # An UNBOUNDED input's diagonal (~3.5e100 mm, `trim_unbounded`'s case) sizes nothing: every threshold relative to
    # it is infinite, and the zero-volume eps would drop every solid of the trimmed part. Such a lineage is sized from
    # the shape in hand, which the trim has bounded.
    if given is not None and 0.0 < float(given) < UNBOUNDED_DIAGONAL_MM:
        return float(given)
    return diagonal_of(shape)


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


def shape_signature(shape: Any) -> list:
    """The state an op can change that no entity fingerprint sees, as a short JSON-safe list.

    Entity fingerprints are centroid-and-box of each face, edge and solid, so they are blind to
    ORIENTATION (a solid and its inside-out twin have identical boxes), to the SHELL/COMPOUND layout
    above the entities, and to whether a shell is closed. The search dedupes on them, and
    `orient_and_close` changes exactly those things. Contents, in order:

    * the top shape's TopAbs type;
    * the counts of compounds, compsolids, solids and shells, and how many shells are closed;
    * per SHELL (those inside solids and bare ones alike), its SIGNED volume to six significant figures,
      sorted. Reversing a solid or shell negates it and reversing one face of it changes it by twice that
      face's contribution, so both are seen; the same geometry reached by two routes agrees to ~1e-12,
      far inside the rounding.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_COMPSOLID, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    def indexed(kind: Any) -> Any:
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, m)
        return m

    solids, shells = indexed(TopAbs_SOLID), indexed(TopAbs_SHELL)
    closed = sum(1 for i in range(1, shells.Extent() + 1) if BRep_Tool.IsClosed_s(shells.FindKey(i)))
    volumes: list[str] = []
    for i in range(1, shells.Extent() + 1):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shells.FindKey(i), props)
        volumes.append(f"{props.Mass():.6g}")
    return [int(shape.ShapeType()), indexed(TopAbs_COMPOUND).Extent(), indexed(TopAbs_COMPSOLID).Extent(),
            solids.Extent(), shells.Extent(), closed, *sorted(volumes)]


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


#: Bumped whenever the record's shape changes; `apply_history` refuses any other version.
RECORD_VERSION = 1


class IndexInstabilityError(RuntimeError):
    """The BREP written for the new shape reads back with a different indexed-map order or content,
    so indices recorded from the in-memory shape would bind ids to the wrong geometry."""


def _verify_round_trip(new: Any, new_path: str, quantum: float) -> list[dict]:
    """Re-read `new_path` and require the same entity at every (kind, index) as in memory.

    Identity is carried by kernel index, and the NEXT op reads this file, so the order of the
    re-read shape's indexed maps must equal the in-memory one. Fingerprints, which identity does
    not rest on, are exactly the cheap per-index check of that assumption."""
    in_memory = entities_of(new, quantum)
    reread = entities_of(read_brep(new_path), quantum)
    if in_memory != reread:
        raise IndexInstabilityError(
            f"{new_path!r} does not read back with the in-memory entity order "
            f"({len(in_memory)} entities in memory, {len(reread)} on disk); refusing to hand out "
            "indices that would bind ids to the wrong geometry")
    return in_memory


def history_record(old: Any, new: Any, history: Any, *, op: str, params: dict, old_path: str,
                   new_path: str, quantum: float, seconds: float, declared_added: list[str] | None = None,
                   declared_removed: list[str] | None = None) -> dict:
    """The record `workspace.apply_history` consumes (version RECORD_VERSION). `old_path` MUST be the
    BREP `old` was read from (the workspace's `brep_path`) and `new_path` must already hold `new`;
    it is re-read and checked entity-for-entity (IndexInstabilityError otherwise).

    CONTRACT FOR OPS: `now` and the child indices come from `FindIndex` of the OLD sub-shape in the
    NEW shape, which needs them to be the SAME TShape (IsSame). An op that copies or relocates
    untouched geometry (BRepBuilderAPI_Transform, a deep copy) makes every entity look vanished and
    `apply_history` retires all ids; such an op must report its own mapping through `history`.

    Entities are matched by KERNEL INDEX, not by fingerprint: for each OLD entity, `now` is its own index in the new shape (0 if absent),
    `removed` is history.IsRemoved, and `modified`/`generated` are child indices in the new shape.
    `declared_added`/`declared_removed` are caller-supplied claims, passed through unchecked."""
    entities = _verify_round_trip(new, new_path, quantum)
    new_maps = _maps(new)
    rows: list[dict] = []
    for kind, indexed in _maps(old).items():
        for i in range(1, indexed.Extent() + 1):
            sub = indexed.FindKey(i)
            rows.append({
                "type": kind, "index": i, "fp": fingerprint(kind, sub, quantum, i),
                "now": int(new_maps[kind].FindIndex(sub)),
                "removed": bool(history.IsRemoved(sub)),
                "modified": _children(history.Modified(sub), new_maps),
                "generated": _children(history.Generated(sub), new_maps),
            })
    return {
        "version": RECORD_VERSION, "op": op, "params": params, "old_path": old_path,
        "brep_path": new_path, "seconds": seconds,
        "declared_added": list(declared_added or []),
        "declared_removed": list(declared_removed or []),
        "entities": entities, "history": rows, "signature": shape_signature(new),
    }


@register("ws_index")
def _ws_index(payload: dict) -> dict:
    """Name every face, edge and solid of a BREP. Picks the fingerprint quantum if none is given."""
    from meshpipeline.cad.repair.identity import quantum_for

    shape = read_brep(payload["brep_path"])
    diag = diagonal_of(shape)
    quantum = float(payload.get("quantum") or quantum_for(diag))
    return {"quantum": quantum, "entities": entities_of(shape, quantum), "signature": shape_signature(shape),
            "diag_mm": diag}


@register("ws_diagonal")
def _ws_diagonal(payload: dict) -> dict:
    """The bounding-box diagonal of a BREP, in the BREP's own unit (millimetres: every workspace is)."""
    return {"diag_mm": diagonal_of(read_brep(payload["brep_path"]))}


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
                              old_path=payload["brep_path"], params={"p1": payload["p1"], "p2": payload["p2"]},
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
                              old_path=payload["brep_path"], params={"corner": payload["corner"], "size": payload["size"]},
                              new_path=payload["out_path"], quantum=payload["quantum"],
                              seconds=time.perf_counter() - start)

    @register("ws_record_for_wrong_file")
    def _wrong_file(payload: dict) -> dict:
        """Test op: build a record for `new` but leave a DIFFERENT shape at new_path, to prove the
        round-trip guard refuses it."""
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepTools import BRepTools_History
        from OCP.gp import gp_Pnt

        old = read_brep(payload["brep_path"])
        new = BRepAlgoAPI_Cut(old, BRepPrimAPI_MakeBox(gp_Pnt(-1, -1, 5), 4, 12, 6).Shape()).Shape()
        write_brep(old, payload["out_path"])  # not `new`
        return history_record(old, new, BRepTools_History(), op="x", params={},
                              old_path=payload["brep_path"], new_path=payload["out_path"],
                              quantum=payload["quantum"], seconds=0.0)


if kernel_test_fns_enabled():
    _register_test_fns()
