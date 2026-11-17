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
    check already ran, else from a fresh crossing search (`check_overlaps`). Each check is time-boxed
    (find_bounded); what it did not check is logged here (find_bounded says it)."""
    found, unchecked = find_bounded(shape, record, validity=validity, check_overlaps=check_overlaps)
    if unchecked:
        logger.info("suspect defects: not checked in the time set - %s", "; ".join(u["why"] for u in unchecked))
    return found


#: MESH-1's TIME BOX (Areen, 17 Nov: a turbocharger spent minutes of "Measuring your part" in these checks, unbounded):
#: every check runs in a child process (cad/bounded_steps) under its own limit - the crossing search CROSSING_S, each
#: solid's Boolean check BOOLEAN_FLOOR_S and BOOLEAN_S_PER_FACE a face of that solid, at most BOOLEAN_CAP_S. A check
#: past its limit is stopped and said as not checked; the run goes on.
CROSSING_S = 300.0
BOOLEAN_FLOOR_S = 60.0
BOOLEAN_S_PER_FACE = 0.2
BOOLEAN_CAP_S = 300.0
MODULE = "meshpipeline.cad.suspect_defects"


def boolean_limit_s(faces: int) -> float:
    """MESH-1: how long one solid's Boolean check may take."""
    return float(min(BOOLEAN_CAP_S, BOOLEAN_FLOOR_S + BOOLEAN_S_PER_FACE * max(int(faces), 0)))


def _solid_faces(shape) -> list[int]:
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    out = []
    for k in range(1, solids.Extent() + 1):
        own = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(solids.FindKey(k), TopAbs_FACE, own)
        out.append(own.Extent())
    return out


class _Checks:
    """The time-boxed checks of one shape: written once to a B-rep file the children read (the same faces, in the
    same order), each check a step (cad/bounded_steps). `unchecked` collects what ran out or stopped."""

    def __init__(self, shape, tmp: str):
        from pathlib import Path

        from OCP.BRepTools import BRepTools

        self.dir = Path(tmp)
        self.brep = self.dir / "shape.brep"
        self.ok = bool(BRepTools.Write_s(shape, str(self.brep)))
        self.faces = _solid_faces(shape)
        self.unchecked: list[dict] = []
        self.runs = 0

    def run(self, steps: list[dict], limits: list[float], *, start_by_s: float | None = None):
        from meshpipeline.cad import bounded_steps as BS

        self.runs += 1
        return BS.run(MODULE, {"brep": str(self.brep)}, steps, limits, start_by_s=start_by_s,
                      workdir=self.dir / f"run{self.runs}")

    def crossings(self) -> list | None:
        (got,) = self.run([{"op": "cross"}], [CROSSING_S])
        if got.ok:
            return list(got.value or [])
        self.unchecked.append({"check": "crossings",
                               "why": f"the search for faces crossing each other ({_why(got, CROSSING_S)})"})
        return None

    def booleans(self, bodies: list[int], *, total_s: float | None = None) -> dict[int, set[int] | None]:
        bodies = [b for b in bodies if 0 < b <= max(len(self.faces), 1)]
        limits = [boolean_limit_s(self.faces[b - 1] if b - 1 < len(self.faces) else 0) for b in bodies]
        got = self.run([{"op": "boolean", "body": b} for b in bodies], limits, start_by_s=total_s)
        out: dict[int, set[int] | None] = {}
        for b, s, lim in zip(bodies, got, limits, strict=True):
            if s.ok:
                out[b] = None if s.value is None else {int(x) for x in s.value}
            else:
                out[b] = None
                self.unchecked.append({"check": "boolean", "body": b,
                                       "why": f"body {b}'s Boolean self-interference check ({_why(s, lim)})"})
        return out


def _why(step, limit: float) -> str:
    from meshpipeline.cad import bounded_steps as BS

    return {BS.TIME: f"past its limit of {limit:.3g} s", BS.NOT_RUN: "not started: the time for it ran out",
            BS.STOPPED: "it stopped", BS.ERROR: "it failed"}.get(step.why, step.why or "not run")


def find_bounded(shape, record: dict, *, validity: dict | None = None,
                 check_overlaps: bool = True) -> tuple[list[dict], list[dict]]:
    """find(), and what it did not check in the time set for it: [{"check", "body"?, "why"}] (MESH-1's time box)."""
    import tempfile

    try:
        names = _face_names(shape, record)
    except Exception:  # noqa: BLE001
        return [], []
    with tempfile.TemporaryDirectory(prefix="suspects_") as tmp:
        try:
            checks: _Checks | None = _Checks(shape, tmp)
        except Exception as exc:  # noqa: BLE001 - checked in this process, as before
            logger.debug("suspect defects: no time-boxed checks (%s)", type(exc).__name__)
            checks = None
        if checks is not None and not checks.ok:
            checks = None
        found = _find(shape, record, names, validity=validity, check_overlaps=check_overlaps, checks=checks)
        return found, (checks.unchecked if checks is not None else [])


def _find(shape, record: dict, names: list[str], *, validity: dict | None, check_overlaps: bool,
          checks: _Checks | None) -> list[dict]:
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
        if checks is not None:
            reasons = checks.crossings() or []
        else:
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
    if checks is not None:
        # every suspect's solid checked once, each under its own limit (MESH-1's time box)
        wanted = sorted({int(s["body"] or 1) for s in out if any(n in index for n in s["faces"])})
        checked.update(checks.booleans(wanted))

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
        covered = {int(s["body"] or 0) for s in out}
        if checks is not None:
            rest = [k for k in range(1, len(checks.faces) + 1) if k not in covered and k not in checked]
            # no check STARTS past BOOLEAN_BUDGET_S (as before); one running keeps its own limit
            checked.update(checks.booleans(rest, total_s=BOOLEAN_BUDGET_S))
            out.extend(_boolean_only(shape, names, covered, known=checked, only_known=True))
        else:
            out.extend(_boolean_only(shape, names, covered, known=checked))
    return out


#: How long the solid-by-solid Boolean check may take in all (seconds); past it, the rest is not checked.
BOOLEAN_BUDGET_S = 60.0


def _boolean_only(shape, names: list[str], covered: set[int],
                  known: dict[int, set[int] | None] | None = None, *, only_known: bool = False) -> list[dict]:
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
        if only_known and (known is None or k not in known):
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


def unchecked_words(unchecked: list[dict]) -> str:
    """What the defect checks did not check in their time, in plain words ("" when they checked everything)."""
    if not unchecked:
        return ""
    bodies = sorted({int(u["body"]) for u in unchecked if u.get("body")})
    parts = []
    if any(u.get("check") == "crossings" for u in unchecked):
        parts.append("the search for faces crossing each other")
    if bodies:
        listed = ", ".join(str(b) for b in bodies[:8]) + (" and more" if len(bodies) > 8 else "")
        parts.append(f"the self-interference check of {'body' if len(bodies) == 1 else 'bodies'} {listed}")
    return ("Not checked for broken CAD in the time set for it: " + " and ".join(parts)
            + ". The mesh is tried as usual; only a failure there would point at the CAD.")


def _load(payload: dict):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(payload["brep"]), BRep_Builder()):
        raise RuntimeError("the shape could not be read back")
    return shape


def _handle(shape, step: dict, _work, _k: int):
    if step["op"] == "cross":
        from meshpipeline.cad.validity_brep import check_shape
        return [r.as_dict() if hasattr(r, "as_dict") else r for r in check_shape(shape).reasons]
    if step["op"] == "boolean":
        got = _boolean_faces(shape, int(step["body"]))
        return None if got is None else sorted(got)
    raise ValueError(f"unknown step {step.get('op')}")


__all__ = ["find", "find_bounded", "unchecked_words", "words"]


if __name__ == "__main__":
    import sys as _sys

    from meshpipeline.cad import bounded_steps as _BS

    if _BS.is_child(_sys.argv):
        _sys.exit(_BS.child_main(_sys.argv[2:], _load, _handle))
