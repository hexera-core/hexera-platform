# Responsibility: Read an FEA assembly (a CAD file of several solid bodies): which part is which (Part_k and the CAD's own name for it), which parts touch (shared faces: contact candidates) and which pass into each other (interference), and stage it for the mesher - one conformal solid where the parts are bonded, or the parts side by side where they only touch.
# Boundaries: geometry only, deterministic, no model call, no persistence. OpenCASCADE's general fuse (BOPAlgo_Builder) decides what touches and what overlaps, and the same fuse is what the bonded mesh is built on, so what the stage shows is what the mesh joins. It plans no size and meshes nothing.
# Collaborates with: cad/fea_measure.py (the per-face record it renames Part_k_Face_j), application/geometry_check.py (the stage's record), engines/fea_driver.py (stages the assembly for the mesher), engines/gmsh/fea_adapter.py (meshes it), engines/fea_assembly_deck.py (writes the parts and their pairs).
"""THE FEA ASSEMBLY.

Each solid body of the file is a part, Part_1..Part_N in OpenCASCADE's order (TopExp.MapShapes),
named from the CAD product where the file names it ("Valve body", "TornilloM3"), else by its
number. Its faces are Part_k_Face_j: the j-th face of the k-th solid, in the solid's own order -
so a face keeps its name whatever the other parts are.

THE GENERAL FUSE cuts every part against every other (OpenCASCADE's BOPAlgo_Builder, the General
Fuse algorithm; gmsh's fragment is the same operation). In its result:

* a face that is the image of faces of TWO parts is a shared face: the parts TOUCH there (an
  interface - in a bonded mesh the parts share its nodes; with contact it is a contact pair);
* a solid that is the image of TWO parts is where they PASS INTO each other (interference): a
  bonded mesh makes it one piece of material, counted with the lower-numbered part, and says so.

A record (record()) for the stage and the plan:

    {"version": 1, "parts": [{"id": "Part_1", "name": "Valve body", "faces": 37,
                              "centroid_mm": [..], "box_mm": [dx, dy, dz], "volume_share": 0.68}],
     "interfaces": [{"id": "Interface_1", "kind": "touching" | "overlap", "parts": ["Part_1", "Part_2"],
                     "faces": {"Part_1": ["Part_1_Face_3"], "Part_2": ["Part_2_Face_1"]},
                     "at_mm": [..], "area_mm2": .., "overlap_share": ..}],
     "free_parts": ["Part_4"], "fused": true, "notes": [..], "seconds": ..}
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

VERSION = 1
#: Where the record sits in the FEA measurement record (cad/fea_measure, facts["fea_measure"]).
FACT_KEY = "assembly"
#: THE CONNECTION: how the parts are joined (the stage's one question for an assembly).
BONDED, CONTACT = "bonded", "contact"
CONNECTIONS = (BONDED, CONTACT)
#: The pairs a mesh writes: bonded and conformal (shared nodes), bonded but not conformal (a tie:
#: the fuse could not be made), or contact (separate meshes, a contact pair).
PAIR_BONDED, PAIR_TIE, PAIR_CONTACT = "bonded", "tie", "contact"
#: TIE FIRST: a bonded assembly whose parts pass into each other in this many separate pieces is staged
#: apart and tied from the start, not fused. Each piece is a shared solid the fuse cuts out, with its own
#: intersection curves, and the fused mesh then runs for hours or comes back invalid there. Measured on
#: 27 bonded assemblies (25 Oct, the same fuse the mesh is built on): every assembly whose fused mesh was
#: valid had at most 7 pieces (chrono 3, lp_channel_leg 3, fc_folding_table 4, ts_twist_block 7);
#: kart_chassis (9: the fused fill failed), kart_roll_cage (10: invalid on the joints), lp_lptl_support
#: (26: past the 230-minute wall) and ender3_idler (36 pieces from ONE pair of parts: 239 minutes, then
#: gmsh crashed) did not. Neither the number of overlapping pairs (ender3_idler has one) nor how much
#: the parts overlap (chrono by a third, and it fuses in minutes) separated them; the pieces did.
#: Fewer pieces that still fail are tied after the first pass (engines/fea_driver, PLAN-fea-17).
TIE_FIRST_PIECES = 8
#: A part is WHOLLY INSIDE another when this share of it overlaps the other.
WHOLLY_INSIDE = 0.999
#: The general fuse treats surfaces this close (a share of the assembly's size) as touching: CAD
#: assemblies are often drawn with faces a few nanometres apart.
FUZZY_SHARE = 1e-6
#: The general fuse runs in millimetres (fuse(), docs/RULES.md)
FUSE_IN_MM = "PLAN-fea-90"
#: OpenCASCADE's Precision::Confusion: points closer than this, in the shape's own unit, are one point
_OCC_CONFUSION = 1e-7
#: Names a CAD exporter gives a body when nobody named it: no better than Part_k.
_GENERIC_BODY = re.compile(
    r"^\s*(\d+|solid|body|part|compound|component|shape|assembly|product|unnamed|noname|default|"
    r"open cascade.*|solidworks.*|feature|brep|mesh|item|object)\s*[#<(\[_\-\s]*\d*\s*[>)\]]?\s*$",
    re.IGNORECASE)


_REFERENCE = re.compile(r"^\s*(=>)?\s*\[[0-9:]+\]\s*$")


def part_id(p: int) -> str:
    return f"Part_{int(p)}"


def face_name(p: int, j: int) -> str:
    """The name of the j-th face of part p, in every file (a deck name: letters, digits and _)."""
    return f"Part_{int(p)}_Face_{int(j)}"


def part_of_face(name: str) -> int | None:
    m = re.match(r"^Part_(\d+)_Face_\d+$", str(name))
    return int(m.group(1)) if m else None


def readable_body_name(name: str | None) -> str:
    """The CAD's own name for a body, or "" when it is an exporter's generic one ("SOLID")."""
    n = " ".join(str(name or "").split())
    # an XCAF reference ("=>[0:1:1:2]") is the reader's label for an unnamed instance
    return "" if not n or _GENERIC_BODY.match(n) or _REFERENCE.match(n) else n[:80]


# #
# shapes
# #

def solids(shape) -> list:
    """The solid bodies of a shape, in OpenCASCADE's order (TopExp.MapShapes): Part_1..Part_N."""
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, m)
    return [TopoDS.Solid_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _map(shape, kind):
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def compound(parts: list):
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in parts:
        b.Add(c, s)
    return c


def _props(shape, volume: bool) -> tuple[float, np.ndarray]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    (BRepGProp.VolumeProperties_s if volume else BRepGProp.SurfaceProperties_s)(shape, g)
    c = g.CentreOfMass()
    return abs(float(g.Mass())), np.array([c.X(), c.Y(), c.Z()])


def _box(shape) -> tuple[np.ndarray, np.ndarray]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def global_face_names(shape) -> list[str]:
    """Part_k_Face_j for every face of `shape` in its own MapShapes order (the order cad/fea_measure
    numbers Face_1..Face_N in): the face's part and its place in that part. A face two solids of
    the file already share keeps its first part's name."""
    from OCP.TopAbs import TopAbs_FACE

    fmap = _map(shape, TopAbs_FACE)
    names: list[str | None] = [None] * fmap.Extent()
    for p, sol in enumerate(solids(shape), start=1):
        for j, f in enumerate(_faces(sol), start=1):
            k = fmap.FindIndex(f)
            if k > 0 and names[k - 1] is None:
                names[k - 1] = face_name(p, j)
    # a face in no solid (a loose sheet beside the bodies) keeps a name of its own
    return [n or f"Loose_Face_{k}" for k, n in enumerate(names, start=1)]


# #
# the general fuse
# #

@dataclass
class Fused:
    """The general fuse of the parts: its shape, and for each of its faces and solids (MapShapes
    order, 0-based) where it came from - (part, face) and parts."""
    shape: object = None
    face_origins: list[list[tuple[int, int]]] = field(default_factory=list)
    solid_parts: list[list[int]] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


def _scaled(shape, k: float):
    """`shape` scaled by `k` about the origin: a new shape, its sub-shapes in the same order."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    tr = gp_Trsf()
    tr.SetScale(gp_Pnt(0.0, 0.0, 0.0), float(k))
    return BRepBuilderAPI_Transform(shape, tr, True).Shape()


def fuse(parts: list, *, fuzzy: float = 0.0, mm_per_unit: float = 1.0) -> Fused:
    """OpenCASCADE's General Fuse of the solids (non-destructive, one thread: the same parts give
    the same result), with each result face's and solid's origins.

    PLAN-fea-90: THE FUSE RUNS IN MILLIMETRES. `mm_per_unit` is the parts' unit (the staging
    writes metres: 1000); parts in another unit are scaled to millimetres for the fuse and the
    result scaled back, its faces and solids in the same order. OpenCASCADE's own tolerances are
    absolute (1e-7), fitted to CAD drawn in millimetres: the same 406 laser-cutter parts fused in
    metres grew past 26 GB over 33 minutes - the lab's 32 GiB job was killed there twice - and in
    millimetres took 3.2 GB and 2 minutes (o_ox_mc_laser, lab 27 Oct)."""
    k = float(mm_per_unit or 1.0)
    if abs(k - 1.0) > 1e-12:
        # never finer than the fuse in the parts' own unit: OpenCASCADE confuses points closer than 1e-7 in
        # any unit, so the metre fuse merged edges 1e-7 m (1e-4 mm) apart; scaled, that floor goes with it
        done = fuse([_scaled(s, k) for s in parts], fuzzy=max(float(fuzzy), _OCC_CONFUSION) * k)
        if done.shape is not None and not done.error:
            done.shape = _scaled(done.shape, 1.0 / k)
        return done
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID

    t0 = time.time()
    b = BOPAlgo_Builder()
    for s in parts:
        b.AddArgument(s)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    if fuzzy > 0:
        b.SetFuzzyValue(float(fuzzy))
    try:
        b.Perform()
    except Exception as exc:  # noqa: BLE001 - said as the fuse's failure; the caller falls back
        return Fused(error=f"the parts could not be fused ({type(exc).__name__}: {exc})",
                     seconds=round(time.time() - t0, 2))
    if b.HasErrors():
        return Fused(error="the parts could not be fused (OpenCASCADE's general fuse reported an error)",
                     seconds=round(time.time() - t0, 2))
    res = b.Shape()
    rf, rs = _map(res, TopAbs_FACE), _map(res, TopAbs_SOLID)
    f_orig: list[list[tuple[int, int]]] = [[] for _ in range(rf.Extent())]
    s_orig: list[list[int]] = [[] for _ in range(rs.Extent())]
    for p, sol in enumerate(parts, start=1):
        for j, f in enumerate(_faces(sol), start=1):
            for im in _images(b, f):
                k = rf.FindIndex(im)
                if k > 0 and (p, j) not in f_orig[k - 1]:
                    f_orig[k - 1].append((p, j))
        for im in _images(b, sol):
            k = rs.FindIndex(im)
            if k > 0 and p not in s_orig[k - 1]:
                s_orig[k - 1].append(p)
    return Fused(shape=res, face_origins=[sorted(o) for o in f_orig], solid_parts=[sorted(o) for o in s_orig],
                 seconds=round(time.time() - t0, 2))


def _images(builder, s) -> list:
    mods = list(builder.Modified(s))
    if mods:
        return mods
    return [] if builder.IsDeleted(s) else [s]


# #
# the record
# #

def interfaces_of(fused: Fused, volumes: list[float], *, scale: float = 1.0) -> list[dict]:
    """The interfaces the fuse found (see the module text): every pair of parts that touch (a shared
    face) or pass into each other (a shared solid), with the faces of each part there, where it is
    and how big. Lengths in the shapes' unit times `scale`."""
    mm = float(scale)
    vols = [(float(v), None) for v in volumes]
    rfaces = _faces(fused.shape)
    rsol = solids(fused.shape)
    # TOUCHING: result faces that are images of two parts' faces
    # (every pair of the parts that share one: three bodies drawn on one face are three pairs)
    by_pair: dict[tuple[int, int], list[int]] = {}
    for k, orig in enumerate(fused.face_origins):
        for pair in combinations(sorted({p for p, _j in orig}), 2):
            by_pair.setdefault(pair, []).append(k)
    # OVERLAP: result solids that are images of two parts (or more: every pair of them)
    over: dict[tuple[int, int], list[int]] = {}
    for k, owners in enumerate(fused.solid_parts):
        for pair in combinations(sorted(owners), 2):
            over.setdefault(pair, []).append(k)
    interfaces: list[dict[str, Any]] = []
    for pair in sorted(set(by_pair) | set(over)):
        faces: dict[str, list[str]] = {part_id(p): [] for p in pair}
        if pair in over:
            pieces = [rsol[k] for k in over[pair]]
            vol = sum(_props(s, True)[0] for s in pieces)
            centre = sum(_props(s, True)[0] * _props(s, True)[1] for s in pieces) / max(vol, 1e-300)
            # the faces of either part that bound the shared piece: the pair's contact surfaces
            # when the parts are meshed apart
            bound = {_index_of(rfaces, f) for s in pieces for f in _faces(s)}
            for k in sorted(i for i in bound if i is not None):
                for p, j in fused.face_origins[k]:
                    if p in pair and face_name(p, j) not in faces[part_id(p)]:
                        faces[part_id(p)].append(face_name(p, j))
            smaller = min(vols[pair[0] - 1][0], vols[pair[1] - 1][0]) or 1.0
            interfaces.append({"kind": "overlap", "parts": [part_id(p) for p in pair], "faces": faces,
                               "at_mm": _pt(centre * mm), "overlap_share": round(vol / smaller, 6),
                               "area_mm2": None})
            continue
        ks = by_pair[pair]
        area = 0.0
        acc = np.zeros(3)
        for k in ks:
            a, c = _props(rfaces[k], False)
            area += a
            acc += a * c
            for p, j in fused.face_origins[k]:
                if p in pair and face_name(p, j) not in faces[part_id(p)]:
                    faces[part_id(p)].append(face_name(p, j))
        interfaces.append({"kind": "touching", "parts": [part_id(p) for p in pair], "faces": faces,
                           "at_mm": _pt(acc / max(area, 1e-300) * mm), "area_mm2": _r(area * mm * mm)})
    for i, itf in enumerate(interfaces, start=1):
        itf["id"] = f"Interface_{i}"
        for v in itf["faces"].values():
            v.sort(key=_face_sort)
    return interfaces


def pairwise_interfaces(parts: list, volumes: list[float], *, fuzzy: float, scale: float = 1.0) -> list[dict]:
    """The interfaces found PAIR BY PAIR, for when the fuse of every part at once fails: each two
    parts whose boxes meet are fused on their own (a pair the kernel cannot fuse either is left
    out). Same records as interfaces_of, numbered afresh."""
    boxes = [_box(s) for s in parts]
    pad = 2.0 * float(fuzzy)
    out: list[dict[str, Any]] = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            (a0, a1), (b0, b1) = boxes[i], boxes[j]
            if np.any(a1 + pad < b0) or np.any(b1 + pad < a0):
                continue
            pair = fuse([parts[i], parts[j]], fuzzy=fuzzy, mm_per_unit=scale)
            if pair.error:
                continue
            for itf in interfaces_of(pair, [volumes[i], volumes[j]], scale=scale):
                rename = {"Part_1": part_id(i + 1), "Part_2": part_id(j + 1)}
                itf["parts"] = [rename[x] for x in itf["parts"]]
                itf["faces"] = {rename[k]: [n.replace(f"{k}_", f"{rename[k]}_", 1) for n in v]
                                for k, v in itf["faces"].items()}
                out.append(itf)
    for k, itf in enumerate(out, start=1):
        itf["id"] = f"Interface_{k}"
    return out


#: A GAP JOINT'S REACH in the solver (*TIE POSITION TOLERANCE, CONTA174 pinball, BCTABLE ERROR): the
#: widest gap measured on its faces, and this share more, so every node across it is tied.
GAP_REACH_MARGIN = 0.25
#: The stage's reading of the joints (record(), in the naming step the user waits on) measures for at
#: most this long; the run's staging (stage()) has cad/fea_joins.BUDGET_S.
RECORD_MEASURE_S = 150.0


def joined_interfaces(parts: list, fused: Fused, volumes: list[float], *, diag: float, mm_per_unit: float,
                      scale: float, gap_joins: bool = True, budget_s: float | None = None) -> tuple[list[dict], dict]:
    """PLAN-fea-18: the interfaces the stage shows and the mesh joins, and how they were found.

    The fuse's interfaces (cad/fea_assembly.interfaces_of, or pair by pair when the fuse failed) are
    checked against the parts' surfaces (cad/fea_joins.check). When the surfaces agree, the fuse's
    interfaces stand exactly as they were, and every pair of parts that faces each other across a
    small gap (cad/fea_joins.gap_limit_mm) is added after them, joined there - unless `gap_joins` is
    off - and every touch the fuse missed (a pin in an exact hole whose two cylinders it did not
    merge, a wire or a fin resting on a face along a line) is added too, marked "added", tied there.
    When the surfaces contradict what the fuse joined (a part "inside" another it does not reach, a
    joint the surfaces do not have, a joint only through a face that crosses its own part), the
    measured joints replace the fuse's: `info["trusted"]` is False, and a bonded mesh is meshed apart
    and tied.

    `info`: {"trusted", "findings", "skins" (Part_k -> its faces that cross their own part), "gaps"
    (the gap interfaces' ids), "added" (the missed touches' ids), "complete", "seconds"}."""
    from meshpipeline.cad import fea_joins as J

    tol = FUZZY_SHARE * diag
    if fused.error:
        fuse_itf = pairwise_interfaces(parts, volumes, fuzzy=tol, scale=scale)
    else:
        fuse_itf = interfaces_of(fused, volumes, scale=scale)
    # NOTHING OPENCASCADE THROWS HERE FAILS THE STAGING: a pair it cannot measure is that pair unmeasured
    # (the fuse's answer stands there, said); a measurement that fails outright leaves the fuse's answer whole
    try:
        measured = J.measure(parts, tol=tol, mm_per_unit=mm_per_unit,
                             budget_s=J.BUDGET_S if budget_s is None else float(budget_s))
        findings = J.check(fuse_itf, parts, measured, tol=tol, mm_per_unit=mm_per_unit)
    except Exception as exc:  # noqa: BLE001 - said as a note; the fuse's answer stands
        logger.warning("fea assembly: the parts' surfaces could not be measured (%s: %s)", type(exc).__name__, exc)
        measured = J.Measured(tol=tol, complete=False)
        measured.unmeasured[(-1, -1)] = f"{type(exc).__name__}: {exc}"[:200]
        findings = []
    kinds = (J.TOUCHING, J.GAP, J.OVERLAP) if gap_joins else (J.TOUCHING, J.OVERLAP)
    # a touch the fuse missed is an omission, not a wrong answer: the fuse stands, the touch is added
    wrong = [f for f in findings if f["kind"] != "missed"]
    if wrong:
        interfaces = J.interfaces(measured, parts, volumes, scale=scale, kinds=kinds)
        # a pair the surfaces could not be measured on keeps the fuse's answer
        keep = [dict(i) for i in fuse_itf
                if tuple(sorted(int(x.split("_")[1]) - 1 for x in i["parts"])) in measured.unmeasured]
        for k, itf in enumerate(keep, start=len(interfaces) + 1):
            itf["id"] = f"Interface_{k}"
            interfaces.append(itf)
    else:
        said = {tuple(sorted(i["parts"], key=lambda x: int(x.split("_")[1]))) for i in fuse_itf}
        extra = [i for i in J.interfaces(measured, parts, volumes, scale=scale,
                                         kinds=(J.TOUCHING, J.GAP) if gap_joins else (J.TOUCHING,))
                 if tuple(i["parts"]) not in said]
        interfaces = list(fuse_itf)
        for k, itf in enumerate(extra, start=len(interfaces) + 1):
            itf["id"] = f"Interface_{k}"
            if itf["kind"] == J.TOUCHING:
                itf["added"] = True
            interfaces.append(itf)
    for itf in interfaces:
        if itf.get("kind") == J.GAP or itf.get("line"):
            # a line contact's reach is the gap limit's (gap_max), so the solver ties the band along the line
            itf["reach_mm"] = _r(float(itf.get("gap_max_mm") or itf.get("gap_mm") or 0.0) * (1.0 + GAP_REACH_MARGIN)
                                 + tol * scale)
    info = {"trusted": not wrong, "findings": findings,
            "added": [i["id"] for i in interfaces if i.get("added")],
            "skins": {part_id(p): [face_name(p, j) for j in js] for p, js in sorted(measured.skins.items())},
            "gaps": [i["id"] for i in interfaces if i.get("kind") == J.GAP],
            "complete": measured.complete, "seconds": measured.seconds,
            "unmeasured": [{"parts": ([part_id(i + 1), part_id(j + 1)] if i >= 0 else []), "why": why}
                           for (i, j), why in sorted(measured.unmeasured.items())]}
    return interfaces, info


def joins_record(info: dict, interfaces: list[dict]) -> dict | None:
    """What the record and the sidecar say of how the joints were found - None when there is nothing
    to say (the fuse stood, no gap was joined, no skin found, the measurement finished), so an
    assembly the new measurement changes nothing on is recorded exactly as before."""
    if info["trusted"] and not info["gaps"] and not info["skins"] and info["complete"] and not info["findings"] \
            and not info.get("unmeasured"):
        return None
    gap_faces = sorted({f for i in interfaces if i.get("kind") == "gap" or i.get("line")
                        for v in (i.get("faces") or {}).values() for f in v}, key=_face_sort)
    return {"found_by": "fuse" if info["trusted"] else "surfaces", "findings": info["findings"],
            "skins": info["skins"], "skin_faces": sorted({f for v in info["skins"].values() for f in v}, key=_face_sort),
            "gaps": info["gaps"], "gap_faces": gap_faces, "added": info.get("added") or [],
            "unmeasured": info.get("unmeasured") or [],
            "complete": info["complete"],
            "seconds": info["seconds"]}


def joins_notes(info: dict, interfaces: list[dict], *, unit: str = "mm", scale_to_mm: float = 1.0) -> list[str]:
    """The joints' finding in plain words, for the record and the sidecar."""
    notes = []
    for u in info.get("unmeasured") or []:
        why = str(u.get("why") or "").split(":", 1)[0]
        if u.get("parts"):
            notes.append(f"Where {u['parts'][0]} and {u['parts'][1]} meet could not be measured on their surfaces "
                         f"(OpenCASCADE: {why}): the fuse's answer stands for that pair.")
        else:
            notes.append(f"The parts' surfaces could not be measured (OpenCASCADE: {why}): where they meet is as "
                         "the fuse found it.")
    if not info["trusted"]:
        first = info["findings"][0]["words"]
        more = f" ({len(info['findings'])} such answers)" if len(info["findings"]) > 1 else ""
        notes.append("Where the parts meet was measured on their surfaces, pair by pair: the fuse of all the parts "
                     f"at once gave answers their surfaces contradict - {first}{more}.")
    added = [i for i in interfaces if i.get("added")]
    if added:
        words = "; ".join(f"{i['parts'][0]} and {i['parts'][1]}" for i in added[:6])
        notes.append(f"{len(added)} place(s) where parts touch were found on their surfaces that the fuse did not "
                     f"join (two faces it did not merge, or parts meeting along a line): {words}"
                     f"{' and more' if len(added) > 6 else ''} - they are tied there.")
    for pid, faces in info["skins"].items():
        notes.append(f"{pid} has faces that cross or lie on its own other faces ({_list(faces[:6])}"
                     f"{' and more' if len(faces) > 6 else ''}): a surface of no thickness there, so they join "
                     "no part and size no gap.")
    lines = [i for i in interfaces if i.get("line")]
    if lines:
        words = "; ".join(f"{i['parts'][0]} and {i['parts'][1]}" for i in lines[:6])
        notes.append(f"{len(lines)} pair(s) of parts meet only along a line, an edge or at a point (no faces facing "
                     f"each other): {words}{' and more' if len(lines) > 6 else ''} - joined there, the solver tying "
                     "what lies within the gap limit of the contact.")
    gaps = [i for i in interfaces if i.get("kind") == "gap" and not i.get("line")]
    if gaps:
        words = "; ".join(f"{i['parts'][0]} and {i['parts'][1]} {_mm(float(i['gap_mm']) * scale_to_mm)} apart"
                          for i in gaps[:6])
        notes.append(f"{len(gaps)} pair(s) of parts face each other across a small gap and are joined there, as a "
                     f"weld or a thread the CAD does not draw (up to 0.5 mm, less on small parts): {words}"
                     f"{' and more' if len(gaps) > 6 else ''}.")
    if not info["complete"] and all(u.get("parts") for u in info.get("unmeasured") or []):
        notes.append("The parts' surfaces were not all measured in the time set for it: where they meet is as the "
                     "fuse found it.")
    return notes


def _mm(v: float) -> str:
    return f"{v:.2g} mm" if v < 1 else f"{v:.3g} mm"


def record(shape, *, mm_per_unit: float, names: list[str] | None = None) -> dict:
    """What the assembly is (see the module text), in millimetres."""
    t0 = time.time()
    mm = float(mm_per_unit)
    parts = solids(shape)
    lo, hi = _box(shape)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    vols = [_props(s, True) for s in parts]
    total = sum(v for v, _c in vols) or 1.0
    out_parts = []
    for p, (s, (v, c)) in enumerate(zip(parts, vols, strict=True), start=1):
        a, b = _box(s)
        name = readable_body_name((names or [])[p - 1] if names and p - 1 < len(names) else "")
        out_parts.append({"id": part_id(p), "name": name, "faces": len(_faces(s)),
                          "centroid_mm": _pt(c * mm), "box_mm": _pt((b - a) * mm),
                          "volume_share": round(v / total, 6)})
    notes: list[str] = []
    fused = fuse(parts, fuzzy=FUZZY_SHARE * diag, mm_per_unit=mm)
    rec: dict[str, Any] = {"version": VERSION, "parts": out_parts, "interfaces": [], "free_parts": [], "fused": not fused.error,
           "notes": notes}
    if fused.error:
        # THE PARTS PAIR BY PAIR: what touches is still found; a bonded mesh then ties them
        notes.append(f"{fused.error} all at once: where they touch was found pair by pair, and a bonded mesh "
                     "ties the parts there instead of sharing nodes.")
    # PLAN-fea-18: the fuse's answer, checked on the parts' surfaces, and the small-gap joins
    interfaces, info = joined_interfaces(parts, fused, [v for v, _c in vols], diag=diag, mm_per_unit=mm, scale=mm,
                                         budget_s=RECORD_MEASURE_S)
    rec["interfaces"] = interfaces
    said = joins_record(info, interfaces)
    if said is not None:
        said["notes"] = joins_notes(info, interfaces)
        rec["joins"] = said
        notes.extend(said["notes"])
    joined = {p for itf in interfaces for p in itf["parts"]}
    rec["free_parts"] = [p["id"] for p in out_parts if p["id"] not in joined]
    if rec["free_parts"]:
        one = len(rec["free_parts"]) == 1
        notes.append(f"{_list(rec['free_parts'])} touch{'es' if one else ''} no other part: in the mesh "
                     f"{'it is a separate piece' if one else 'each is a separate piece'}, held only by the supports "
                     "you set in your solver.")
    n_over = sum(1 for i in interfaces if i["kind"] == "overlap")
    if n_over:
        notes.append(f"{n_over} pair(s) of parts pass into each other (interference).")
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def _gap_fields(itf: dict) -> dict:
    """A small-gap pair's measured gap and its reach in the solver, in the staged unit (metres)."""
    # the staged interfaces are read at scale 1: their *_mm lengths are in the staged unit
    return {"gap": _r(float(itf.get("gap_mm") or 0.0)), "gap_max": _r(float(itf.get("gap_max_mm") or 0.0)),
            "reach": _r(float(itf.get("reach_mm") or 0.0))}


def _index_of(faces: list, f) -> int | None:
    for i, g in enumerate(faces):
        if g.IsSame(f):
            return i
    return None


def _face_sort(name: str) -> tuple[int, int]:
    m = re.match(r"^Part_(\d+)_Face_(\d+)$", name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _list(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else ", ".join(ids[:-1]) + " and " + ids[-1]


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _pt(p) -> list[float]:
    return [_r(x) for x in np.asarray(p, dtype=float)]


# #
# staging for the mesher
# #

#: The staged assembly the mesher reads (OpenCASCADE B-rep, metres), and what each of its faces
#: and solids is (SIDECAR).
STAGED = "assembly.brep"
SIDECAR = "assembly.json"


def stage(shape, connection: str, out_dir: Path | str, *, names: list[str] | None = None,
          tie: bool = False, gap_joins: bool = True, mm_per_unit: float = 1000.0) -> dict:
    """Write STAGED and SIDECAR for `connection` into `out_dir`, and return the sidecar.

    BONDED: the general fuse itself - the parts share every face where they touch, so the mesh is
    conformal there (shared nodes); where they pass into each other the shared piece is one piece
    of material, counted with the lower-numbered part. Should the fuse fail, the parts are staged
    side by side and every touching pair is TIED instead (bonded, not conformal) - never a stop;
    `tie` asks for that directly (a fused assembly the mesher could not build as one).
    CONTACT: the parts side by side, each meshed on its own; every pair the fuse found touching
    (or overlapping) is a contact pair.
    PLAN-fea-18: the fuse is checked on the parts' surfaces first (joined_interfaces). Parts that face
    each other across a small gap are joined there - tied when bonded, a contact pair with contact -
    each pair with its measured gap and its reach (`gap_joins` off: not joined); a fuse the surfaces
    contradict is set aside, and the measured joints are tied (bonded) or paired (contact).
    `mm_per_unit`: the staged shape's unit (the staging writes metres).

    The sidecar says, for every face and solid of STAGED in MapShapes order (gmsh's tag order for
    the file it reads), its names (Part_p_Face_j; a shared face has one per part) and its parts,
    with its area / volume and centre in metres for the mesher to check that order against."""
    from OCP.BRepTools import BRepTools

    out = Path(out_dir)
    parts = solids(shape)
    lo, hi = _box(shape)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    vols = [_props(s, True)[0] for s in parts]
    notes: list[str] = []
    # MESH-8: EACH PART AS IT IS MESHED - its zero-thickness skins left out (cad/zero_skins), every face of
    # what is meshed still named for the CAD face it came from; a part with none is the part itself
    opened = [open_skins(s) for s in parts]
    mesh_parts = [o.solid for o in opened]
    skins = skin_record(opened)
    notes.extend(skin_notes(skins))
    fused = fuse(mesh_parts, fuzzy=FUZZY_SHARE * diag, mm_per_unit=mm_per_unit)
    fused.face_origins = [[(p, opened[p - 1].origin[j - 1]) for p, j in o] for o in fused.face_origins]
    mode = PAIR_BONDED if connection == BONDED else PAIR_CONTACT
    if fused.error and connection == BONDED:
        mode = PAIR_TIE
        notes.append(f"{fused.error}, so the parts are meshed apart and tied where they touch (bonded, "
                     "but their nodes are not shared there).")
    elif tie and connection == BONDED:
        # the mesher could not build the fused parts as one (the driver's way on, PLAN-fea-17)
        mode = PAIR_TIE
        notes.append("The parts could not be meshed as one where they meet, so each part is meshed on its own and "
                     "tied there (bonded, but their nodes are not shared).")
    interfaces, info = joined_interfaces(parts, fused, vols, diag=diag, mm_per_unit=mm_per_unit, scale=1.0,
                                         gap_joins=gap_joins)
    if not info["trusted"] and mode == PAIR_BONDED:
        # THE FUSE IS WRONG HERE (PLAN-fea-18): its one piece cannot be meshed as the parts are, so each
        # part is meshed on its own and tied where its surfaces meet the others
        mode = PAIR_TIE
    notes.extend(joins_notes(info, interfaces, scale_to_mm=mm_per_unit))
    n_over = sum(1 for i in interfaces if i["kind"] == "overlap")
    n_pieces = 0 if fused.error or not info["trusted"] else sum(1 for o in fused.solid_parts if len(o) > 1)
    tie_first = mode == PAIR_BONDED and n_pieces >= TIE_FIRST_PIECES
    if tie_first:
        mode = PAIR_TIE
        notes.append(f"The parts pass into each other in {n_pieces} places ({n_over} pair(s) of parts), too many to "
                     "mesh as one piece reliably, so each part is meshed on its own and tied where they meet "
                     "(bonded, but their nodes are not shared).")
    # PLAN-fea-17: PARTS THAT MEET TANGENTIALLY (tangent_meets()): fused, they leave a crack of no angle between
    # their outer faces that no mesher can fill, so a bonded assembly with one is meshed apart and tied from the
    # start - never a fused pass that can only fail first (job bf863215, 27 Oct: each bolt's head fillet runs
    # tangentially onto its washer, and the fused fill failed "PLC Error: A segment and a facet intersect" on
    # every subset of the parts that held a bolt and its washer)
    grazed = tangent_meets(fused) if mode == PAIR_BONDED and not fused.error else []
    if grazed:
        mode = PAIR_TIE
        notes.append(f"{_list([' with '.join(part_id(x) for x in pq) for pq in grazed])} meet tangentially - a "
                     "round of one runs onto the other's face at no angle - and parts fused there leave a crack no "
                     "mesher can fill, so each part is meshed on its own and tied where they meet (bonded, but "
                     "their nodes are not shared).")
    if mode == PAIR_BONDED:
        staged = fused.shape
        face_names = [[face_name(p, j) for p, j in o] for o in fused.face_origins]
        vol_parts = [list(o) for o in fused.solid_parts]
    else:
        # MESHED APART: every part a copy of its own, so a face two solids of the file already share
        # (a valid multi-solid B-rep can) becomes one face on each side, each named for its part
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

        staged = compound([BRepBuilderAPI_Copy(s).Shape() for s in mesh_parts])
        face_names = [[_origin_name(n, opened)] for n in global_face_names(staged)]
        vol_parts = [[p] for p in range(1, len(parts) + 1)]
    out.mkdir(parents=True, exist_ok=True)
    BRepTools.Write_s(staged, str(out / STAGED))
    surfaces = []
    for k, f in enumerate(_faces(staged)):
        a, c = _props(f, False)
        surfaces.append({"names": face_names[k] if k < len(face_names) else [], "area_m2": _r(a),
                         "centroid_m": _pt(c)})
    volumes = []
    for k, s in enumerate(solids(staged)):
        v, c = _props(s, True)
        volumes.append({"parts": vol_parts[k] if k < len(vol_parts) else [], "volume_m3": _r(v),
                        "centroid_m": _pt(c)})
    pairs = []
    unpaired: list[tuple[str, str]] = []
    for itf in interfaces:
        p, q = (int(x.split("_")[1]) for x in itf["parts"])
        gap = _gap_fields(itf) if itf.get("kind") == "gap" or itf.get("line") else {}
        if mode == PAIR_BONDED and (gap or itf.get("added")):
            # A SMALL-GAP JOINT in a fused mesh: the two sides are not one surface, so they are tied
            pairs.append({"id": itf["id"], "kind": PAIR_TIE, "parts": [p, q], "overlap": False,
                          "faces": {str(p): itf["faces"].get(part_id(p)) or [],
                                    str(q): itf["faces"].get(part_id(q)) or []}, **gap})
            continue
        if mode == PAIR_BONDED:
            # the faces both parts share: one surface per side, where the bond's stress is read
            shared = [k for k, o in enumerate(fused.face_origins) if {p, q} <= {a for a, _j in o}]
            if itf["kind"] == "overlap" or not shared:
                continue
            pairs.append({"id": itf["id"], "kind": PAIR_BONDED, "parts": [p, q], "surfaces": shared})
        else:
            fp, fq = itf["faces"].get(part_id(p)) or [], itf["faces"].get(part_id(q)) or []
            if not fp or not fq:
                # ONE SIDE HAS NO FACE THERE: a part wholly inside the other meets it only inside its
                # material, so there is no surface pair to write - said in words, never dropped quietly
                inner, outer = (part_id(p), part_id(q)) if fp else (part_id(q), part_id(p))
                unpaired.append((inner, outer))
                continue
            pairs.append({"id": itf["id"], "kind": PAIR_TIE if mode == PAIR_TIE else PAIR_CONTACT,
                          "parts": [p, q], "overlap": itf["kind"] == "overlap",
                          "faces": {str(p): fp, str(q): fq}, **gap})
    joined = {p for i in interfaces for p in i["parts"]}
    free = [part_id(p) for p in range(1, len(parts) + 1) if part_id(p) not in joined]
    if free:
        one = len(free) == 1
        notes.append(f"{_list(free)} touch{'es' if one else ''} no other part: in the mesh "
                     f"{'it is a separate piece' if one else 'each is a separate piece'}, held only by the supports "
                     "you set in your solver.")
    over = [i for i in interfaces if i["kind"] == "overlap"]
    # A PART WHOLLY INSIDE ANOTHER (a body drawn twice, a pin modelled inside its boss): bonded, it is
    # all one piece of material with the other, so it has no elements of its own
    inside = [i for i in over if float(i.get("overlap_share") or 0.0) >= WHOLLY_INSIDE]
    if inside and mode == PAIR_BONDED:
        for i in inside:
            lo_id, hi_id = i["parts"]                     # the shared piece counts with lo_id
            v_lo, v_hi = (vols[int(x.split("_")[1]) - 1] for x in (lo_id, hi_id))
            if v_hi <= v_lo:
                notes.append(f"{hi_id} lies wholly inside {lo_id} (drawn twice?): bonded, it is one piece of "
                             f"material with {lo_id}, so it has no elements of its own.")
            else:
                notes.append(f"{lo_id} lies wholly inside {hi_id}: bonded, they are one piece of material there.")
    if over and mode == PAIR_BONDED:
        notes.append(f"{len(over)} pair(s) of parts pass into each other: where they do, the mesh is one piece "
                     "of material, counted with the lower-numbered part (" + "; ".join(
                         f"{i['parts'][0]} and {i['parts'][1]}, {100 * float(i['overlap_share']):.3g}% of the "
                         "smaller part" for i in over) + ").")
    elif over and mode == PAIR_TIE:
        notes.append(f"{len(over)} pair(s) of parts pass into each other: tied, each keeps its own elements "
                     "there, so the shared piece is counted in both parts (its stiffness and mass twice) - fix "
                     "the CAD if that piece matters.")
    elif over:
        notes.append(f"{len(over)} pair(s) of parts pass into each other: they are meshed apart as drawn, so "
                     "the solver starts with them overlapping there - let it resolve the overlap at the first "
                     "increment (Abaqus/CalculiX: ADJUST; Ansys: initial penetration), or fix the CAD.")
    joined_how = "tied" if mode == PAIR_TIE else "a contact pair"
    for inner, outer in unpaired:
        notes.append(f"{inner} lies wholly inside {outer}, so they have no surface between them and are not "
                     f"joined by {joined_how}: in the mesh they overlap, unconnected. Choose Bonded to make them "
                     "one piece, or remove the extra body from the CAD.")
    sidecar = {"version": VERSION, "connection": connection, "mode": mode,
               "parts": [{"id": part_id(p), "name": readable_body_name((names or [""] * len(parts))[p - 1]
                                                                        if names and p - 1 < len(names) else "")}
                         for p in range(1, len(parts) + 1)],
               "surfaces": surfaces, "volumes": volumes, "pairs": pairs, "interfaces": interfaces,
               "unpaired": [{"inner": a, "outer": b} for a, b in unpaired],
               "notes": notes, "fuse_seconds": fused.seconds,
               # PLAN-fea-90: the unit the general fuse ran in (millimetres, whatever the staging's)
               "fuse_unit": {"rule": FUSE_IN_MM, "unit": "mm", "scaled_by": float(mm_per_unit)},
               "tie_first": ({"overlap_pieces": n_pieces, "overlap_pairs": n_over, "at": TIE_FIRST_PIECES}
                             if tie_first else None)}
    if grazed:
        sidecar["tangent_meets"] = [list(pq) for pq in grazed]   # absent when none: the sidecar is as before
    said = joins_record(info, interfaces)
    if said is not None:
        said["notes"] = joins_notes(info, interfaces, scale_to_mm=mm_per_unit)
    if not gap_joins:
        sidecar["gap_joins"] = False
    if said is not None:
        # HOW THE JOINTS WERE FOUND, and the faces no gap sizing should read: the faces a small-gap
        # joint joins (the gap is joined, not meshed across) and the skins (no thickness)
        sidecar["joins"] = said
    if skins:
        sidecar["skins"] = skins          # MESH-8 (absent when there is none: the sidecar is as before)
    (out / SIDECAR).write_text(json.dumps(sidecar, indent=1))
    return sidecar


#: PLAN-fea-17: two parts MEET TANGENTIALLY where an outer face of each meets the other's at an edge of the fuse
#: with their outward normals within this cosine of opposite (about 5 degrees): a crack of no angle between them.
TANGENT_COS = -0.996


def tangent_meets(fused: Fused) -> list[tuple[int, int]]:
    """The pairs of parts (Part numbers, sorted) whose outer faces meet tangentially in the fuse: at an edge
    shared by faces of solids of different parts that bound no other solid, outward normals (at the edge's
    middle) within TANGENT_COS of opposite. Never raises: what cannot be read is no crack."""
    try:
        return _tangent_meets(fused)
    except Exception:  # noqa: BLE001 - no crack found is the fuse as before
        return []


def _tangent_meets(fused: Fused) -> list[tuple[int, int]]:
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    emap = _map(fused.shape, TopAbs_EDGE)
    fmap = _map(fused.shape, TopAbs_FACE)
    uses: dict[int, list] = {}
    bounding: dict[int, int] = {}
    for k, sol in enumerate(solids(fused.shape)):
        ex = TopExp_Explorer(sol, TopAbs_FACE)
        while ex.More():
            f = TopoDS.Face_s(ex.Current())
            fi = fmap.FindIndex(f)
            bounding[fi] = bounding.get(fi, 0) + 1
            e2 = TopExp_Explorer(f, TopAbs_EDGE)
            while e2.More():
                uses.setdefault(emap.FindIndex(e2.Current()), []).append((k, fi, f))
                e2.Next()
            ex.Next()
    out: set[tuple[int, int]] = set()
    for ei, lst in uses.items():
        outer = [(k, f) for k, fi, f in lst if bounding.get(fi) == 1 and k < len(fused.solid_parts)]
        if len({tuple(fused.solid_parts[k]) for k, _f in outer}) < 2:
            continue
        e = TopoDS.Edge_s(emap.FindKey(ei))
        if BRep_Tool.Degenerated_s(e):
            continue
        c = BRepAdaptor_Curve(e)
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        normals = []
        for k, f in outer:
            uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(f)).ValueOfUV(p, 1e-7)
            pr = BRepLProp_SLProps(BRepAdaptor_Surface(f), uv.X(), uv.Y(), 1, 1e-9)
            if pr.IsNormalDefined():
                sign = -1.0 if f.Orientation() == TopAbs_REVERSED else 1.0
                normals.append((tuple(fused.solid_parts[k]), sign * np.array(pr.Normal().Coord())))
        for a in range(len(normals)):
            for b in range(a + 1, len(normals)):
                pa, pb = normals[a][0], normals[b][0]
                if pa != pb and len(pa) == 1 and len(pb) == 1 and float(normals[a][1] @ normals[b][1]) < TANGENT_COS:
                    out.add((min(pa[0], pb[0]), max(pa[0], pb[0])))
    return sorted(out)


def open_skins(solid):
    """cad/zero_skins.open_skins (imported here, where every part is staged)."""
    from meshpipeline.cad import zero_skins
    return zero_skins.open_skins(solid)


def skin_record(opened: list) -> list[dict]:
    """MESH-8: the skins each part was meshed without, named: [{"part", "faces", "on"}]."""
    out = []
    for p, o in enumerate(opened, start=1):
        if o.skins:
            out.append({"part": part_id(p), "faces": [face_name(p, k.skin) for k in o.skins],
                        "on": sorted({face_name(p, k.on) for k in o.skins}, key=_face_sort)})
        elif o.note:
            out.append({"part": part_id(p), "faces": [], "on": [], "not_opened": o.note})
    return out


def skin_notes(skins: list[dict]) -> list[str]:
    """MESH-8 in plain words, one line a part."""
    out = []
    for sk in skins:
        if sk.get("faces"):
            n = len(sk["faces"])
            out.append(f"{sk['part']} has {n} skin{'s' if n != 1 else ''} of no thickness in its CAD "
                       f"({_list(sk['faces'])}, lying on {_list(sk['on'])}, the other side out, with no material "
                       f"between): {'they are' if n != 1 else 'it is'} left out of the mesh, so the opening "
                       f"{'they' if n != 1 else 'it'} closed goes through. Nothing with material was changed, and "
                       "the CAD file is untouched.")
    return out


def _origin_name(name: str, opened: list) -> str:
    """Part_p_Face_j of a part as meshed -> the CAD face it came from."""
    p = part_of_face(name)
    try:
        j = int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return name
    if p is None or not (0 < p <= len(opened)) or not (0 < j <= len(opened[p - 1].origin)):
        return name
    return face_name(p, opened[p - 1].origin[j - 1])


# #
# the measurement record, renamed per part
# #

def rename_record(rec: dict, names: list[str]) -> dict:
    """cad/fea_measure's record with Face_k (the k-th face of the whole file) renamed to its part's
    Part_p_Face_j (names[k-1]) everywhere it is said: the faces, the hot spots, the thinnest wall,
    the cusps and the micro details."""
    def nm(f: str) -> str:
        try:
            k = int(str(f).rsplit("_", 1)[-1])
        except ValueError:
            return f
        return names[k - 1] if str(f).startswith("Face_") and 0 < k <= len(names) else f

    out = dict(rec)
    out["faces"] = {nm(k): v for k, v in (rec.get("faces") or {}).items()}
    out["hot_spots"] = [{**h, "faces": [nm(f) for f in h.get("faces") or []]} for h in rec.get("hot_spots") or []]
    if isinstance(rec.get("thickness"), dict) and rec["thickness"].get("face"):
        out["thickness"] = {**rec["thickness"], "face": nm(rec["thickness"]["face"])}
    for key in ("cusps", "micro", "fold_back", "suspects", "coincident", "bores"):
        if rec.get(key):
            out[key] = [{**c, "faces": [nm(f) for f in c.get("faces") or []],
                         **({"outer_faces": [nm(f) for f in c["outer_faces"]]} if c.get("outer_faces") else {})}
                        for c in rec[key]]
    out["face_names"] = "Part_k_Face_j"
    return out


def measure_assembly(shape, rec: dict, *, source: Path | str | None, mm_per_unit: float) -> dict:
    """cad/fea_measure's record of a file of several bodies, made an assembly's: every face renamed
    Part_k_Face_j, and the assembly itself (record()) under FACT_KEY - its parts named from the CAD
    where it names them. Never fails the measurement: an assembly that cannot be read says why."""
    names: list[str] = []
    if source:
        try:
            names = match_body_names(read_body_names(source), solids(shape))
        except Exception as exc:  # noqa: BLE001 - a courtesy: every part is still Part_k
            logger.info("fea assembly: the CAD's part names were not read (%s)", exc)
    out = rename_record(rec, global_face_names(shape))
    try:
        out[FACT_KEY] = record(shape, mm_per_unit=mm_per_unit, names=names)
    except Exception as exc:  # noqa: BLE001 - the stage still asks how the parts are joined
        logger.warning("fea assembly: the parts' interfaces were not found (%s)", exc)
        out[FACT_KEY] = {"version": VERSION, "parts": [], "interfaces": [], "free_parts": [], "fused": False,
                         "notes": [f"The parts' interfaces could not be found ({type(exc).__name__})."]}
    return out


# #
# the CAD's names for its bodies
# #

def read_body_names(path: Path | str) -> list[tuple[str, np.ndarray, float]]:
    """(name, centroid, volume) of every solid the STEP file places, in the reader's own unit: the
    product it is an instance of names it (the instance's own name when the product has none).
    Empty for any other file, or one the reader cannot take."""
    p = Path(path)
    if p.suffix.lower() not in (".step", ".stp"):
        return []
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

    doc = TDocStd_Document(TCollection_ExtendedString("cad"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(p)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        return []
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return str(attr.Get().ToExtString()).strip()
        return ""

    out: list[tuple[str, np.ndarray, float]] = []

    def walk(label, loc, inherited: str, depth: int) -> None:
        if depth > 32:
            return
        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                ref = TDF_Label()
                if not XCAFDoc_ShapeTool.GetReferredShape_s(comp, ref):
                    continue
                own = readable_body_name(named(ref)) or readable_body_name(named(comp))
                walk(ref, loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(comp)), own or inherited, depth + 1)
            return
        shape = XCAFDoc_ShapeTool.GetShape_s(label)
        if shape.IsNull():
            return
        placed = shape.Moved(loc)
        name = readable_body_name(named(label)) or inherited
        for s in solids(placed):
            v, c = _props(s, True)
            out.append((name, c, v))

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), "", 0)
    return out


#: A named body matches a solid when its centroid lies within this share of the assembly's size and
#: its volume within this share of its own.
_CENTROID_TOL = 1e-4
_VOLUME_TOL = 1e-3


def match_body_names(named: list[tuple[str, np.ndarray, float]], parts: list) -> list[str]:
    """The name of each solid of `parts` (in its order), tied by where it is and how big it is,
    each side read in its own unit (both are taken relative to their own volume-weighted centre and
    total volume). A part no name fits is ""; one name used twice is told apart: "Bolt", "Bolt (2)"."""
    if not named or not parts:
        return [""] * len(parts)
    mine = [_props(s, True) for s in parts]

    def frame(rows):
        v = np.array([r[0] for r in rows], dtype=float)
        c = np.array([r[1] for r in rows], dtype=float)
        tot = float(v.sum())
        if tot <= 0:
            return None
        return (v[:, None] * c).sum(axis=0) / tot, tot ** (1.0 / 3.0), tot

    fn = frame([(v, c) for _n, c, v in named])
    fm = frame(mine)
    if fn is None or fm is None:
        return [""] * len(parts)
    out = []
    used: dict[str, int] = {}
    for v, c in mine:
        rel_c, rel_v = (c - fm[0]) / fm[1], v / fm[2]
        best, best_d = "", math.inf
        for n, cn, vn in named:
            d = float(np.linalg.norm((cn - fn[0]) / fn[1] - rel_c))
            if d <= _CENTROID_TOL * 10 and abs(vn / fn[2] - rel_v) <= _VOLUME_TOL * max(rel_v, 1e-300) and d < best_d:
                best, best_d = n, d
        if best:
            used[best] = used.get(best, 0) + 1
            best = best if used[best] == 1 else f"{best} ({used[best]})"
        out.append(best)
    return out


__all__ = ["BONDED", "CONNECTIONS", "CONTACT", "FACT_KEY", "SIDECAR", "STAGED", "interfaces_of", "stage", "FUZZY_SHARE", "Fused", "PAIR_BONDED", "PAIR_CONTACT",
           "PAIR_TIE", "VERSION", "compound", "face_name", "fuse", "global_face_names", "match_body_names", "measure_assembly", "pairwise_interfaces",
           "part_id", "part_of_face", "read_body_names", "readable_body_name", "record", "rename_record",
           "solids"]
