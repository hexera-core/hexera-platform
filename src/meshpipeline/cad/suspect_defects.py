# Responsibility: Name the places a solid MAY be broken (a face crossing another of its body, a surface folding back on itself) with how sure we are - "confirmed" only when two independent measures agree - so the stage can warn, the mesher can still try, and a mesh that then fails THERE can be told as the CAD's, faces named.
# Boundaries: detect only, on the exact B-rep; never heals and never refuses (Areen, 24 Oct: a good file is never blocked by our own broken-CAD checks - only a mesh that fails at the flagged spot says "needs CAD repair"). Never raises.
# Collaborates with: cad/fold_back.py and cad/validity_brep.py (the first measures), application/fea_check.py (the stage's warning), engines/fea_driver.py (the delivered note, or the failure tied to the spot).
"""SUSPECTED DEFECTS.

Each suspect: {"kind": "fold" | "self_overlap", "body": k (1-based solid, 0 unknown), "faces": [names],
"confirmed": bool, "evidence": [plain words]}.

  * a FOLD is first measured by cad/fold_back (two faces lying on each other beside an edge: the gap
    measured a step into each face);
  * a SELF-OVERLAP by the geometry check's crossing search (cad/validity_brep: faces of one body whose
    exact surfaces meet away from their shared edges).

CONFIRMED needs a SECOND, independent measure to agree: OpenCASCADE's Boolean self-interference check
(BRepAlgoAPI_Check) run on that solid ALONE names one of the suspect's faces. Anything less is a note.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The gate every change to these checks passes: zero stops on the known-good files
#: (devtools/quality/broken_cad_gate.py).
GATE_RULE = "MESH-3"
#: A solid with more faces than this is not given the second (Boolean) check: the suspect stays a note.
MAX_CONFIRM_FACES = 4000
#: THE BOOLEAN CHECK RUNS ON EVERY CORE (1 Nov): it was the whole of the geometry check's slowest step - an
#: 11 x 27 x 15 in IGES landing gear spent 169 s of its 186 s FEA measurement in one single-threaded
#: BRepAlgoAPI_Check (424 s on a shared-dev worker). What it answers is a SET of faulty faces, which does
#: not depend on the order OpenCASCADE's threads find them in, so the parallel run is repeatable: the
#: same faces, 2.5 times sooner on 16 threads (tests/unit/cad/test_suspect_defects_speed.py).
BOOLEAN_PARALLEL = True


def _face_names(shape, record: dict) -> list[str]:
    """The k-th face's name (OpenCASCADE's face order): Part_p_Face_j for an assembly, else Face_k."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    if str(record.get("face_names") or "") == "Part_k_Face_j":
        try:
            from meshpipeline.cad.fea_assembly import global_face_names
            return list(global_face_names(shape))
        except Exception:  # noqa: BLE001 - plain names, then
            pass
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    return [f"Face_{k}" for k in range(1, fmap.Extent() + 1)]


def _boolean_faces(shape, body: int) -> set[int] | None:
    """The faces (1-based, the whole shape's order) OpenCASCADE's Boolean self-interference check names
    on solid `body` alone; None when it cannot say."""
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Check
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedMapOfShape

        solids = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
        target = solids.FindKey(body) if 0 < body <= solids.Extent() else (shape if solids.Extent() <= 1 else None)
        if target is None:
            return None
        own = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(target, TopAbs_FACE, own)
        if own.Extent() > MAX_CONFIRM_FACES:
            return None
        fmap = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
        chk = BRepAlgoAPI_Check()
        chk.SetData(target, True, True)
        chk.SetRunParallel(BOOLEAN_PARALLEL)
        chk.Perform()
        out: set[int] = set()
        for r in chk.Result():
            for s in r.GetFaultyShapes1():
                if s.ShapeType() == TopAbs_FACE:
                    k = fmap.FindIndex(s)
                    if k:
                        out.add(int(k))
        return out
    except Exception as exc:  # noqa: BLE001 - no second measure: the suspect stays a note
        logger.debug("suspect defects: the Boolean check did not run (%s)", type(exc).__name__)
        return None


def find(shape, record: dict, *, validity: dict | None = None, check_overlaps: bool = True) -> list[dict]:
    """Every suspect on `shape` (the shape `record` measured). The folds come from the record
    (cad/fea_measure keeps cad/fold_back's); the self-overlaps from `validity`'s reasons when the geometry
    check already ran, else from a fresh crossing search (`check_overlaps`)."""
    try:
        names = _face_names(shape, record)
    except Exception:  # noqa: BLE001
        return []
    index = {n: k + 1 for k, n in enumerate(names)}
    out: list[dict] = []
    for f in record.get("fold_back") or []:
        faces = [str(x) for x in f.get("faces") or []]
        gap = f.get("gap_mm")
        out.append({"kind": "fold", "body": int(f.get("body") or 0), "faces": faces,
                    "length_mm": f.get("length_mm"),
                    "evidence": [f"the two faces stay {gap} mm apart beside an edge {f.get('length_mm')} mm long"
                                 if gap is not None else "the two faces' normals are opposite along an edge"]})
    reasons = (validity or {}).get("reasons") if validity else None
    if reasons is None and check_overlaps:
        try:
            from meshpipeline.cad.validity_brep import check_shape
            reasons = [r.as_dict() if hasattr(r, "as_dict") else r for r in check_shape(shape).reasons]
        except Exception as exc:  # noqa: BLE001 - no crossing search, no self-overlap suspect
            logger.debug("suspect defects: the crossing search did not run (%s)", type(exc).__name__)
            reasons = []
    for r in reasons or []:
        if not isinstance(r, dict) or str(r.get("code")) != "self_intersection":
            continue
        det = r.get("details") or {}
        pairs = [[int(a), int(b)] for a, b in det.get("faces") or []]
        bodies = [int(b) for b in det.get("bodies") or []]
        faces = sorted({names[k - 1] for p in pairs for k in p if 0 < k <= len(names)},
                       key=lambda s: index.get(s, 0))
        out.append({"kind": "self_overlap", "body": bodies[0] if len(bodies) == 1 else 0, "faces": faces[:12],
                    "evidence": [f"{len(pairs)} pair(s) of its faces meet away from their shared edges"]})
    # THE SECOND MEASURE: the Boolean check on that solid alone names one of the faces - run ONCE per
    # solid however many suspects lie on it (the answer is the solid's, not the suspect's)
    checked: dict[int, set[int] | None] = {}

    def boolean(body: int) -> set[int] | None:
        if body not in checked:
            checked[body] = _boolean_faces(shape, body)
        return checked[body]

    for s in out:
        want = {index[n] for n in s["faces"] if n in index}
        hit = boolean(int(s["body"] or 1)) if want else None
        s["confirmed"] = bool(hit and (hit & want))
        if s["confirmed"]:
            s["evidence"].append("OpenCASCADE's Boolean self-interference check names the same faces")
    # THE BOOLEAN CHECK'S OWN FINDINGS, solid by solid, where no other measure found anything: a note
    # (one measure), so a mesh that fails there can still be told as the CAD's (fc_folding_chair, 24 Oct:
    # its frame crosses itself where the tessellated crossing search saw nothing). In the run only (no
    # stored check given): the stage stays as quick as it was
    if check_overlaps and validity is None:
        out.extend(_boolean_only(shape, names, {int(s["body"] or 0) for s in out}, known=checked))
    return out


#: How long the solid-by-solid Boolean check may take in all (seconds); past it, the rest is not checked.
BOOLEAN_BUDGET_S = 60.0


def _boolean_only(shape, names: list[str], covered: set[int],
                  known: dict[int, set[int] | None] | None = None) -> list[dict]:
    import time

    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    t0 = time.monotonic()
    out = []
    for k in range(1, solids.Extent() + 1):
        if k in covered or time.monotonic() - t0 > BOOLEAN_BUDGET_S:
            continue
        hit = known[k] if known is not None and k in known else _boolean_faces(shape, k)
        if not hit:
            continue
        faces = [names[i - 1] for i in sorted(hit) if 0 < i <= len(names)][:12]
        if faces:
            out.append({"kind": "self_overlap", "body": k, "faces": faces, "confirmed": False,
                        "evidence": ["OpenCASCADE's Boolean self-interference check names these faces; the "
                                     "crossing search saw nothing there"]})
    return out


def words(s: dict, who: str = "") -> str:
    """One suspect in plain words, for the stage and the delivered note."""
    faces = " and ".join(list(s.get("faces") or [])[:3]) or "some faces"
    what = ("a paper-thin fold (two faces lying on each other)" if s.get("kind") == "fold"
            else "faces crossing each other")
    sure = "confirmed by two independent checks" if s.get("confirmed") else "one check's finding, not confirmed"
    return f"{who or 'The part'} may have {what} at {faces} ({sure})."


__all__ = ["find", "words"]
