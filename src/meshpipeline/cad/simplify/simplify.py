# Responsibility: Defeature a CAD B-rep - fill its holes, remove its blends - keep only what a local simulation check accepts within the error budget, and write the result as a NEW STEP file - or refuse.
# Owns: which candidates an attempt may remove, the removal itself, the T0 checks (a valid closed solid, a bounded volume change), the hand-off to the T2 check, and the refusal.
# Boundaries: it writes a NEW file and never touches the input. It does not decide whether a job should be simplified, promotes nothing, uploads nothing, and does not record an attempt. The twin of cad/repair/conservative.py.
# Collaborates with: settings/cad_simplify.py (the switch and the budget), cad/simplify/recognise.py (the features), cad/simplify/verify.py (the simulation check), cad/repair/conservative.py (the unit-declaring STEP writer, reused).
"""REMOVE, MEASURE, VERIFY, KEEP WHAT PASSED.

The chosen candidates are removed in ONE BRepAlgoAPI_Defeaturing call; when that call fails as a
whole, one feature at a time on the running shape, keeping each that comes out valid. A feature is
never chosen when it is a slot end (OCCT hands a 180-degree concave round back unchanged), or when
it sits within the load case's trimmed end bands plus its own zone: there its faces carry the
boundary condition, and removing it changes the problem rather than the discretisation.

The T0 checks run on the defeatured part before anything is solved; the T2 check
(verify.verify_package) then judges each removed feature from one driver solve. When it accepts
fewer features than were removed, the part is defeatured again from the ORIGINAL with only the
accepted ones, and that is what is written.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import meshpipeline.settings.cad_simplify as scfg
from meshpipeline.cad.simplify.contracts import FeatureCandidate, FeatureKind, SimplifyReport

logger = logging.getLogger(__name__)

#: Seconds of CAD_SIMPLIFY_TIMEOUT_S kept back from the verification for the rebuild, the T0
#: checks and the STEP write - the margin CAD repair keeps (conservative.DEADLINE_MARGIN_S).
DEADLINE_MARGIN_S = 5.0


class SimplifyRefused(RuntimeError):
    """The simplification ran but its result may not be used.

    SEPARATE FROM A FAILURE. A refusal means something was produced and we are declining it: the
    kernel removed nothing, the result is not a valid closed solid, it moved too much material, or
    the simulation check accepted no feature. The customer's file is unharmed.
    """

    def __init__(self, message: str, *, measurements: dict | None = None) -> None:
        super().__init__(message)
        self.measurements = dict(measurements or {})


class SimplifyUnavailable(RuntimeError):
    """Simplification cannot run here at all - the switch is off, or the kernel or the solver is
    absent. OUR limitation, never a statement about the file."""


def _occ() -> None:
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised in the native tier
        raise SimplifyUnavailable(
            "the CAD kernel (OCP/OpenCASCADE) is not installed in this process") from exc


def eligible(candidates: Sequence[FeatureCandidate], lc, diag: float, *,
             max_features: int) -> tuple[list[FeatureCandidate], list[dict]]:
    """The candidates an attempt may remove, the costliest first, and why each other one is not."""
    from meshpipeline.cad.simplify.verify import near_ends, zone_pad

    chosen: list[FeatureCandidate] = []
    skipped: list[dict] = []
    for c in candidates:
        if c.kind is FeatureKind.slot_end or not c.removable:
            skipped.append({"feature": c.id, "reason": "not_removable"})
        elif near_ends(c.bbox, lc, zone_pad(c.radius, diag)):
            skipped.append({"feature": c.id, "reason": "near_boundary_condition"})
        else:
            chosen.append(c)
    chosen.sort(key=lambda c: (-c.est_surface_cells, c.id))
    for c in chosen[max(0, int(max_features)):]:
        skipped.append({"feature": c.id, "reason": "over_feature_cap"})
    return chosen[:max(0, int(max_features))], skipped


def remove_features(shape, features: Sequence[tuple[str, Sequence]], tol: float) -> tuple[object, list[str], dict[str, str]]:
    """Remove each (id, face signatures) from `shape`. Returns (shape, removed ids, {id: why not}).

    One call first. The kernel can report success for a set in which some features did not move,
    so afterwards each feature counts as removed only when none of its faces is still there."""
    from meshpipeline.cad.simplify.verify import defeature, face_sigs, find_faces

    index = face_sigs(shape)
    located = []
    failed: dict[str, str] = {}
    for fid, sigs in features:
        faces = find_faces(shape, sigs, tol, index=index)
        if faces is None:
            failed[fid] = "its faces were not found in the part"
        else:
            located.append((fid, sigs, faces))
    if not located:
        return shape, [], failed

    out, why = defeature(shape, [f for _, _, faces in located for f in faces])
    if out is not None:
        after = face_sigs(out)
        removed = []
        for fid, sigs, _ in located:
            if any(find_faces(out, [s], tol, index=after) is not None for s in sigs):
                failed[fid] = "the kernel left it in place"
            else:
                removed.append(fid)
        return out, removed, failed

    logger.info("simplify: one defeaturing call over %d features failed (%s); one at a time",
                len(located), why)
    cur, removed = shape, []
    for fid, sigs, _ in located:
        faces = find_faces(cur, sigs, tol)
        if faces is None:
            failed[fid] = "its faces were not found after an earlier removal"
            continue
        nxt, why_one = defeature(cur, faces)
        if nxt is None:
            failed[fid] = why_one
        else:
            cur = nxt
            removed.append(fid)
    return cur, removed, failed


def _solids_closed(shape) -> tuple[int, bool]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    solids = 0
    e = TopExp_Explorer(shape, TopAbs_SOLID)
    while e.More():
        solids += 1
        e.Next()
    closed = True
    e = TopExp_Explorer(shape, TopAbs_SHELL)
    while e.More():
        closed = closed and bool(BRep_Tool.IsClosed_s(TopoDS.Shell_s(e.Current())))
        e.Next()
    return solids, closed


def t0_checks(before, after) -> dict:
    """T0 on a defeatured part: measured, then refused if it is not a valid closed solid with the
    same number of solids, or if it moved more material than CAD_SIMPLIFY_MAX_VOLUME_CHANGE."""
    from meshpipeline.cad.simplify.verify import is_valid, volume

    v0, v1 = volume(before), volume(after)
    solids0, _ = _solids_closed(before)
    solids1, closed1 = _solids_closed(after)
    m = {"volume_before": v0, "volume_after": v1,
         "volume_change": (v1 - v0) / v0 if v0 else 0.0,
         "solids_before": solids0, "solids_after": solids1, "closed": closed1,
         "valid": is_valid(after)}
    if not m["valid"]:
        raise SimplifyRefused("the simplified shape is not valid", measurements=m)
    if solids1 != solids0 or solids1 == 0 or not closed1:
        raise SimplifyRefused(f"the simplified shape is not a closed solid ({solids1} solids where "
                              f"the part had {solids0}, closed={closed1})", measurements=m)
    if abs(m["volume_change"]) > scfg.CAD_SIMPLIFY_MAX_VOLUME_CHANGE:
        raise SimplifyRefused(f"the simplification changed the volume by {m['volume_change']:+.2%}, "
                              f"past the {scfg.CAD_SIMPLIFY_MAX_VOLUME_CHANGE:.0%} cap", measurements=m)
    return m


def _rows(chosen, skipped, failed, verdict) -> list[dict]:
    """One operation row per candidate the attempt considered: what happened to it, and why."""
    by_id = {v.id: v for v in (verdict.verdicts if verdict is not None else ())}
    est = {e.id: e for e in (verdict.estimates if verdict is not None else ())}
    rows = []
    for c in chosen:
        row = {"name": "remove_feature", "feature": c.id, "kind": c.kind.value, "radius": c.radius,
               "est_surface_cells": c.est_surface_cells}
        if c.id in failed:
            row.update(status="not_removed", mutated=False, reason=failed[c.id])
        elif c.id in by_id:
            v = by_id[c.id]
            e = est.get(c.id)
            row.update(status="kept" if v.accepted else "rejected", mutated=v.accepted,
                       reason=v.reason, estimate=v.estimate,
                       cut_mismatch=e.cut_mismatch if e else None, detail=v.detail or (e.why if e else ""))
        else:
            row.update(status="not_verified", mutated=False, reason="not_verified")
        rows.append(row)
    rows += [{"name": "remove_feature", "feature": s["feature"], "status": "skipped",
              "mutated": False, "reason": s["reason"]} for s in skipped]
    return rows


def simplify_step_file(source: Path, destination: Path, *, feature_ids: Sequence[str] | None = None,
                       verifier: Callable | None = None) -> SimplifyReport:
    """Simplify `source` into `destination`, or refuse. The input is never modified.

    Raises SimplifyUnavailable when simplification is switched off or the kernel or the solver is
    missing, and SimplifyRefused when nothing could be kept - the two are not the same answer and
    must not be reported as one. `verifier` replaces verify.verify_package (tests)."""
    if not scfg.CAD_SIMPLIFY_ENABLED:
        # REFUSED BEFORE ANY KERNEL CALL. A deployment that has not opted in cannot defeature a
        # customer's CAD by any path through this module.
        raise SimplifyUnavailable(
            "CAD simplification is switched off in this deployment (CAD_SIMPLIFY_ENABLED)")
    # THE ONE ABSOLUTE DEADLINE, taken at entry: the attempt node's wait_for started its clock
    # before this call, and every solve below spends a slice of this.
    deadline = time.monotonic() + float(scfg.CAD_SIMPLIFY_TIMEOUT_S) - DEADLINE_MARGIN_S
    _occ()
    src, dest = Path(source), Path(destination)
    if src.resolve() == dest.resolve():
        raise SimplifyUnavailable("simplification writes a new file; it never overwrites the input")

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.simplify import verify as V
    from meshpipeline.cad.simplify.recognise import recognise

    shape = _read_shape(src)
    candidates, diag = recognise(shape)
    if feature_ids is not None:
        wanted = set(feature_ids)
        candidates = [c for c in candidates if c.id in wanted]
    lc = V.load_case(V.bbox_of(shape))
    chosen, skipped = eligible(candidates, lc, diag, max_features=scfg.CAD_SIMPLIFY_MAX_FEATURES)
    measurements: dict = {"diagonal": diag, "load_case": lc.to_dict(), "budget": scfg.CAD_SIMPLIFY_BUDGET,
                          "screen_margin": scfg.CAD_SIMPLIFY_SCREEN_MARGIN}
    if not chosen:
        measurements["operations"] = _rows([], skipped, {}, None)
        raise SimplifyRefused("no candidate feature may be removed from this part", measurements=measurements)

    # each feature's faces by what they ARE (FeatureCandidate.faces are 1-based in TopExp order,
    # the order face_sigs walks), so they can be found again in a region or a rebuilt part
    all_sigs = [s for s, _ in V.face_sigs(shape)]
    sigs = {c.id: tuple(all_sigs[i - 1] for i in c.faces) for c in chosen}
    tol = 1e-5 * diag

    simplified, removed, failed = remove_features(shape, [(c.id, sigs[c.id]) for c in chosen], tol)
    if not removed:
        measurements["operations"] = _rows(chosen, skipped, failed, None)
        raise SimplifyRefused("the kernel removed none of the chosen features", measurements=measurements)
    try:
        measurements["t0"] = t0_checks(shape, simplified)
    except SimplifyRefused as exc:
        measurements.update(t0=exc.measurements, operations=_rows(chosen, skipped, failed, None))
        raise SimplifyRefused(str(exc), measurements=measurements) from None

    by_id = {c.id: c for c in chosen}
    to_verify = [V.FeatureToVerify(fid, by_id[fid].kind.value, by_id[fid].radius, by_id[fid].bbox, sigs[fid])
                 for fid in removed]
    verify = verifier or V.verify_package
    try:
        verdict = verify(shape, simplified, to_verify, budget=float(scfg.CAD_SIMPLIFY_BUDGET),
                         margin=float(scfg.CAD_SIMPLIFY_SCREEN_MARGIN), deadline=deadline,
                         max_tets=int(scfg.CAD_SIMPLIFY_MAX_SOLVE_TETS))
    except V.VerifyUnavailable as exc:
        raise SimplifyUnavailable(f"the simulation check cannot run here: {exc}") from exc
    measurements["verification"] = verdict.to_dict()
    measurements["operations"] = _rows(chosen, skipped, failed, verdict)
    accepted = list(verdict.accepted)
    if not accepted:
        why = verdict.abstained or "the simulation check accepted no removed feature within the budget"
        raise SimplifyRefused(why, measurements=measurements)

    if set(accepted) != set(removed):
        # KEEP ONLY WHAT PASSED: rebuilt from the ORIGINAL, so a rejected feature is not left half
        # removed by a shape that was defeatured with it
        simplified, rebuilt, refailed = remove_features(shape, [(fid, sigs[fid]) for fid in accepted], tol)
        if set(rebuilt) != set(accepted):
            measurements["rebuild_failed"] = dict(refailed)
            raise SimplifyRefused("the accepted features could not be removed again on their own",
                                  measurements=measurements)
        try:
            measurements["t0"] = t0_checks(shape, simplified)
        except SimplifyRefused as exc:
            measurements["t0"] = exc.measurements
            raise SimplifyRefused(str(exc), measurements=measurements) from None

    from meshpipeline.cad.repair.conservative import RepairRefused, _write_step
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    try:
        _write_step(simplified, dest, unit=parser_applied_unit(src))
    except RepairRefused as exc:
        raise SimplifyRefused(str(exc), measurements=measurements) from None

    kept = tuple(by_id[fid] for fid in removed if fid in set(accepted))
    measurements["features_kept"] = [c.id for c in kept]
    return SimplifyReport(
        candidates=kept,
        summary=_summary(kept, verdict, measurements["t0"]),
        operations=tuple(measurements["operations"]),
        diagnostics=measurements)


def _summary(kept, verdict, t0: dict) -> str:
    holes = sum(c.kind is FeatureKind.hole for c in kept)
    blends = sum(c.kind is FeatureKind.blend for c in kept)
    share = verdict.spent / verdict.budget if verdict.budget else 0.0
    return (f"Removed {len(kept)} features ({holes} holes, {blends} blends); the local simulation "
            f"check estimates the stiffness answer moves by {abs(verdict.estimated_change):.3%} "
            f"({share:.0%} of the {verdict.budget:.2%} budget). Volume changed by "
            f"{t0.get('volume_change', 0.0):+.3%}.")
