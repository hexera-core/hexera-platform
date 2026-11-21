# Responsibility: Author the next target package for a region from the existing operation
#                 catalogue, deterministically and in the light of what the region already tried,
#                 so the region loop can be proved end to end with no model in it - a regression
#                 is then a bug in the loop rather than in a prompt.
# Owns: the translation from "the rules planner would try this operation" into a package: its
#       intent, its one-operation plan, the claim it states before execution, and its budget.
# Boundaries: it decides WHAT TO PROPOSE and nothing else. It applies nothing, measures nothing
#             and judges nothing - the session executes the package and a separate measurement
#             judges the claim, because an author may never grade its own work.
# Collaborates with: cad/repair/plan.py (RulesPlanner, whose choice this wraps),
#                    cad/repair/package.py (the package it builds), cad/repair/region_loop.py
#                    (which calls it through the `author` seam), cad/repair/session.py (whose
#                    PackageVerdict is what an attempt looks like), settings/cad_repair.py (the
#                    caps its claim is bounded by).
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import mesh_probe
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.mesh_clash import GAP
from meshpipeline.cad.repair.operations import OPERATIONS
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.plan import (
    _SEVERITY_RANK,
    Candidate,
    RulesPlanner,
    RunHistory,
    held_fingerprints,
    mesh_finding_outranks_kernel,
    paired_rim_fingerprints,
    refuse_paired_rims,
)
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import PackageVerdict

#: What this author reports itself as, beside `rules-v1`: the same choosing rules, one package at a
#: time instead of one operation at a time. A comparison that cannot name which portion produced a
#: run cannot attribute an improvement to model direction, which is the whole point of keeping the
#: deterministic driver (spec decision 6).
LABEL = "regions-rules-v1"

#: WHICH EXISTING OPERATION THE BASELINE AIMS AT A MESH FAILURE (controller ruling R11b), and
#: nothing else. An open loop is a face whose boundary gmsh could not close, which is the face
#: fixer's own job; a surface-stage overlap is two surfaces whose discretisations cross, which only
#: the whole-shape pass can re-sew. Every other mesh code gets NO package - an edge through a face
#: and a periodic surface (flags off; see below), no volume, a timeout and anything unclassified need
#: judgement no bounded operation in the catalogue makes - and so does a VOLUME-STAGE overlap,
#: because the scoped (2D-only) probe that judges a package cannot see it go
#: (`mesh_probe.volume_stage`).
#: "This face will mesh" is then an ordinary `cleared` claim on the mesh defect's fingerprint.
#:
#: A PERIODIC SURFACE joins only with CAD_REPAIR_SPLIT_CLOSED_FACES (`mesh_ops`), mapped to
#: `split_closed_faces`, and only for a failure the probe recorded in the 2D stage (SURFACE_ONLY):
#: gmsh raises a periodic failure while meshing surfaces, so any other stage - including one
#: nobody recorded - is a failure this mapping does not understand, and gets no package.
#:
#: AN EDGE THROUGH A FACE joins only with CAD_REPAIR_FRAGMENT_BODIES, mapped to `fragment_bodies`,
#: and only for a failure the probe recorded in the 3D stage (VOLUME_ONLY). This revises R11b's
#: "an edge through a face gets no package", which predated an operation that answers it: the
#: volume-stage refusal existed because a scoped (2D-only) probe could not judge the package, and
#: a package carrying an operation that answers a volume code is now judged by WHOLE-SHAPE scans
#: (`session.try_package`), which can.
#:
#: AN OVERLAP prefers `rebuild_face` only with CAD_REPAIR_REBUILD_FACE: the narrowest operation
#: that claims a defect goes first (the planner's standing principle), and re-trimming the one face
#: gmsh named is narrower than the whole-shape pass. `fix_shape_global` stays as its FALLBACK
#: (`mesh_fallbacks`), so a file the whole-shape pass used to rescue is not lost to the narrower
#: idea failing first.
#:
#: AN OPEN LOOP prefers `close_wire_gaps` only with CAD_REPAIR_CLOSE_WIRE_GAPS, and only for a
#: 2D-stage failure (the operation's own `targets_for` refuses any other, so the chain passes it
#: over and an unrecorded-stage loop gets today's choice). The face fixer the evidence showed
#: cannot reach a valid face's open loop (devtools/validation/evidence/2026-11-15-open-loop-reach)
#: stays as its FALLBACK, the pattern rebuild_face introduced.
def mesh_ops(*, split_closed_faces: bool | None = None,
             fragment_bodies: bool | None = None,
             rebuild_face: bool | None = None,
             close_wire_gaps: bool | None = None) -> dict[DefectCode, str]:
    """MESH_OPS for the given consent (the deployment's flags when not told). Flags off is today's
    mapping, byte for byte."""
    if split_closed_faces is None:
        split_closed_faces = bool(rcfg.CAD_REPAIR_SPLIT_CLOSED_FACES)
    if fragment_bodies is None:
        fragment_bodies = bool(rcfg.CAD_REPAIR_FRAGMENT_BODIES)
    if rebuild_face is None:
        rebuild_face = bool(rcfg.CAD_REPAIR_REBUILD_FACE)
    if close_wire_gaps is None:
        close_wire_gaps = bool(rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS)
    mapping = {
        DefectCode.mesh_open_loop: "fix_face_boundary",
        DefectCode.mesh_overlapping_facets: "fix_shape_global",
    }
    if split_closed_faces:
        mapping[DefectCode.mesh_periodic_surface] = "split_closed_faces"
    if fragment_bodies:
        mapping[DefectCode.mesh_edge_through_face] = "fragment_bodies"
    if rebuild_face:
        mapping[DefectCode.mesh_overlapping_facets] = "rebuild_face"
    if close_wire_gaps:
        mapping[DefectCode.mesh_open_loop] = "close_wire_gaps"
    return mapping


MESH_OPS: dict[DefectCode, str] = mesh_ops()


def mesh_fallbacks(*, rebuild_face: bool | None = None,
                   close_wire_gaps: bool | None = None) -> dict[DefectCode, tuple[str, ...]]:
    """The operations a mesh code falls back to, in order, once its MESH_OPS choice is spent.

    HOW A FALLBACK IS REACHED: through the region's own attempts, exactly as the rules planner
    reaches its second idea. `_history` turns a tried-and-not-kept `rebuild_face` into an
    ineffective `(fingerprint, rebuild_face)` pair (a decline into a declined aim), so on the
    region's NEXT attempt `_mesh_candidate` passes the spent choice over and proposes the next name
    here - `fix_shape_global`, today's mapping. It needs a second attempt for the region
    (CAD_REPAIR_REGION_ATTEMPTS > 1). Empty with the flags off: no chain, today's single choice.
    `close_wire_gaps` falls back to `fix_face_boundary` the same way.
    """
    if rebuild_face is None:
        rebuild_face = bool(rcfg.CAD_REPAIR_REBUILD_FACE)
    if close_wire_gaps is None:
        close_wire_gaps = bool(rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS)
    chains: dict[DefectCode, tuple[str, ...]] = {}
    if rebuild_face:
        chains[DefectCode.mesh_overlapping_facets] = ("fix_shape_global",)
    if close_wire_gaps:
        chains[DefectCode.mesh_open_loop] = ("fix_face_boundary",)
    return chains


MESH_FALLBACKS: dict[DefectCode, tuple[str, ...]] = mesh_fallbacks()

#: Mesh codes whose mapping holds only when the probe RECORDED the 2D stage. Stricter than the
#: volume-stage refusal every mapping gets: an unrecorded stage is refused too.
SURFACE_ONLY: frozenset[DefectCode] = frozenset({DefectCode.mesh_periodic_surface})

#: Mesh codes whose mapping holds only when the probe RECORDED the 3D stage - and which are
#: therefore EXEMPT from the volume-stage refusal: their package is proved by whole-shape scans.
VOLUME_ONLY: frozenset[DefectCode] = frozenset({DefectCode.mesh_edge_through_face})

_OPS_BY_NAME = {op.name: op for op in OPERATIONS}

#: A CLASH THE PROBE MEASURED AS A GAP goes to the operation built for it first
#: (`gap_split.ResolveSubMeshGap`, CAD_REPAIR_RESOLVE_SUB_MESH_GAP): two faces that do not touch
#: but sit closer than the mesh's chord. The code's own MESH_OPS choice stays as its fallback, so a
#: gap the split cannot resolve still gets today's idea next. Only when the operation's own
#: `targets_for` takes the defect - a gap measured above 0, or none measured at all.
GAP_OP = "resolve_sub_mesh_gap"


def _gap_first(defect) -> tuple[str, ...]:
    op = _OPS_BY_NAME.get(GAP_OP)
    if op is None or defect.code not in op.answers or not op.targets_for(defect):
        return ()
    return (GAP_OP,)


@dataclass(frozen=True)
class GapTries:
    """What the region already did with the gap operation, read off its attempts.

    `failed` counts, per target fingerprint, the tries that were neither declined nor kept in a
    committed package - each one moves that face one rung down `gap_split.SAG_LADDER`. `declined`
    holds the fingerprints the operation refused outright (no gap, flat, too fine, kernel
    timeout): a finer rung asks the same question of the same geometry. `last_failed` is whether
    the region's most recent gap attempt left nothing behind - the signal to stop putting the gap
    first and let the kernel's cure have its turn.
    """

    failed: dict
    declined: frozenset
    last_failed: bool

    def rung(self, fingerprint: str) -> int:
        return int(self.failed.get(fingerprint, 0))

    def spent(self, fingerprint: str) -> bool:
        from meshpipeline.cad.repair.gap_split import SAG_LADDER

        return fingerprint in self.declined or self.rung(fingerprint) >= len(SAG_LADDER)


def gap_tries(attempts: tuple[PackageVerdict, ...]) -> GapTries:
    failed: dict[str, int] = {}
    declined: set[str] = set()
    last_failed = False
    for verdict in attempts:
        for op in verdict.verdicts:
            if op.op != GAP_OP:
                continue
            if op.kept and verdict.committed:
                last_failed = False
                continue
            last_failed = True
            if op.evidence.get("declined"):
                declined.update(op.targets)
                continue
            for fingerprint in op.targets:
                failed[fingerprint] = failed.get(fingerprint, 0) + 1
    return GapTries(failed=failed, declined=frozenset(declined), last_failed=last_failed)


def _gap_op(tries: GapTries, fingerprint: str):
    """The gap operation at this face's rung of the ladder."""
    from meshpipeline.cad.repair.gap_split import SAG_LADDER, ResolveSubMeshGap

    return ResolveSubMeshGap(sag_share=SAG_LADDER[min(tries.rung(fingerprint),
                                                      len(SAG_LADDER) - 1)])


def gap_defects(defects) -> tuple:
    """The defects the GAP PRE-PASS aims at (`region_loop._meshability_pass`): a 3D-stage clash the
    probe measured APART (`clash_kind` GAP) that the gap operation claims, in a stable order. Empty
    when the operation is not in the catalogue."""
    op = _OPS_BY_NAME.get(GAP_OP)
    if op is None:
        return ()
    from meshpipeline.cad.repair.gap_split import claims_gap

    return tuple(sorted((d for d in defects if claims_gap(d) and op.targets_for(d)),
                        key=lambda d: (d.entity_index, d.code.value, d.fingerprint)))


def gap_package(region: Region, defect, tries: GapTries, *, original: Measure,
                budget_s: float | None = None) -> TargetPackage:
    """The gap operation's package for one defect, at that face's rung of the ladder - built
    exactly as `rules_author` builds every package (one operation, the deployment's residual caps
    as the predicate's allowance), so the pre-pass's packages pass the same gates.

    `budget_s`, given, caps both the package's own timeout and the operation's kernel children:
    the pre-pass's share of the clock is a bound on the whole attempt, not only on starting it."""
    op = _gap_op(tries, defect.fingerprint)
    budget = None
    if budget_s is not None:
        op.budget_s = min(op.budget_s, max(float(budget_s), 0.0))
        budget = PackageBudget(max_ops=1, timeout_s=min(float(rcfg.CAD_REPAIR_TIMEOUT_S),
                                                        float(budget_s)),
                               model_calls=0, deviation_mm=0.0)
    candidate = Candidate(op=op,
                          targets=(EntityRef(entity_type=str(defect.entity_type),
                                             fingerprint=defect.fingerprint,
                                             entity_index=int(defect.entity_index),
                                             ambiguous=bool(getattr(defect, "ambiguous", False))),),
                          defect_code=defect.code, defect_fingerprint=defect.fingerprint)
    return _package(candidate, region, original=original, budget=budget,
                    rationale=(f"{LABEL}: gap pre-pass - {GAP_OP} at sag share "
                               f"{getattr(candidate.op, 'sag_share', 0.0):g} on a {defect.code.value} the probe "
                               "measured apart"))


def pass_package(region: Region, defect, op, *, original: Measure, rationale: str,
                 budget_s: float | None = None) -> TargetPackage:
    """One meshability-pass application as a package (`region_loop._meshability_pass`): `op` - a
    registered entry built at its rung - aimed at `defect`, built exactly as every rules package is,
    so it passes the same gates. `budget_s`, given, caps the package's own timeout (the pass's
    share of the clock bounds the whole attempt, as `gap_package`'s does)."""
    budget = None
    if budget_s is not None:
        budget = PackageBudget(max_ops=1, timeout_s=min(float(rcfg.CAD_REPAIR_TIMEOUT_S),
                                                        max(float(budget_s), 0.0)),
                               model_calls=0, deviation_mm=0.0)
    candidate = Candidate(op=op,
                          targets=(EntityRef(entity_type=str(defect.entity_type),
                                             fingerprint=defect.fingerprint,
                                             entity_index=int(defect.entity_index),
                                             ambiguous=bool(getattr(defect, "ambiguous", False))),),
                          defect_code=defect.code, defect_fingerprint=defect.fingerprint)
    return _package(candidate, region, original=original, rationale=rationale, budget=budget)


def _package(candidate: Candidate, region: Region, *, original: Measure, rationale: str,
             max_area_delta_mm2: float | None = None, max_volume_delta_mm3: float | None = None,
             budget: PackageBudget | None = None) -> TargetPackage:
    area = (max_area_delta_mm2 if max_area_delta_mm2 is not None
            else float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) * original.surface_area_mm2)
    volume = (max_volume_delta_mm3 if max_volume_delta_mm3 is not None
              else float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO) * original.volume_mm3)
    allowance = budget if budget is not None else PackageBudget(
        max_ops=1, timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S), model_calls=0, deviation_mm=0.0)
    predicate = Predicate(cleared=frozenset({candidate.defect_fingerprint}),
                          reclassified=frozenset(), unchanged=frozenset(),
                          max_area_delta_mm2=float(area), max_volume_delta_mm3=float(volume))
    plan = ({"op": candidate.op, "targets": candidate.targets,
             "defect_code": candidate.defect_code},)
    return TargetPackage(
        region=region, plan=plan, predicate=predicate, budget=allowance,
        intent=(f"clear the {candidate.defect_code.value} on "
                f"{candidate.defect_fingerprint} in region {region.index}"),
        rationale=rationale)


def _gap_lead(defects, tries: GapTries, region: Region, *,
              reserved: frozenset) -> Candidate | None:
    """THE GAP OPERATION FIRST (lead, wave 6: it ran on only 5 of 14 gap files because the kernel's
    cures used the region's attempts first, against a 40% yield where it did run). For a 3D-stage
    clash the probe measured APART (`clash_kind` GAP) the split is proposed before any kernel cure
    - but only while the region's last gap attempt did not fail, so a part whose gap the split
    cannot resolve still reaches the cure that fixed it before (0060_00608290's fix_small_faces).
    """
    op = _OPS_BY_NAME.get(GAP_OP)
    if op is None or tries.last_failed:
        return None
    held = held_fingerprints(defects, reserved)
    ordered = sorted((d for d in defects if _sub_element_gap(d) and _volume_stage_recorded(d)
                      and d.code in op.answers and d.code not in reserved),
                     key=lambda d: (tries.rung(d.fingerprint), d.entity_index, d.code.value,
                                    d.fingerprint))
    for defect in ordered:
        if (not defect.fingerprint or defect.fingerprint in held
                or defect.fingerprint not in region.fingerprints
                or tries.spent(defect.fingerprint) or not op.targets_for(defect)):
            continue
        return Candidate(op=_gap_op(tries, defect.fingerprint),
                         targets=(EntityRef(entity_type=str(defect.entity_type),
                                            fingerprint=defect.fingerprint,
                                            entity_index=int(defect.entity_index),
                                            ambiguous=bool(getattr(defect, "ambiguous", False))),),
                         defect_code=defect.code, defect_fingerprint=defect.fingerprint)
    return None


def _surface_stage(defect) -> bool:
    return (getattr(defect, "measurements", None) or {}).get("stage") == mesh_probe.STAGE_2D


def _volume_stage_recorded(defect) -> bool:
    return (getattr(defect, "measurements", None) or {}).get("stage") == mesh_probe.STAGE_3D


def _stage_refused(defect) -> bool:
    """Does the recorded stage put this mesh failure outside what its mapping understands?"""
    if defect.code in VOLUME_ONLY:
        return not _volume_stage_recorded(defect)
    if mesh_probe.volume_stage(defect):
        return True
    return defect.code in SURFACE_ONLY and not _surface_stage(defect)


def _is_mesh(defect) -> bool:
    return getattr(defect.code, "value", str(defect.code)).startswith("mesh_")


def _sub_element_gap(defect) -> bool:
    """A clash-located mesh failure whose two faces the kernel measured apart (mesh_clash.GAP).

    NARROWER THAN `plan.mesh_finding_outranks_kernel`, and for a different question. This one asks
    "is this particular finding the mesher's own chord", and `_mesh_candidate` uses it to decide
    what to AIM at. Whether a mesh finding may be proposed BEFORE the kernel's cure is the other
    question, and both layers now ask the shared helper for that - a finding with no clash
    evidence must still be aimable, while it must not jump the queue.
    """
    measurements = getattr(defect, "measurements", None) or {}
    return (measurements.get("located_by") == mesh_probe.LOCATED_BY_CLASH
            and measurements.get("clash_kind") == GAP)


def _spent(defect, name: str, ordered, history: RunHistory,
           tries: GapTries | None = None) -> bool:
    """Has the region already used up `name` on this defect - tried it, tried it on a sibling of
    the same contact, or had the aim declined? The gap operation is spent only once its LADDER is
    (`GapTries.spent`): a failed split is retried finer, not abandoned."""
    if name == GAP_OP and tries is not None:
        return tries.spent(defect.fingerprint) or history.was_declined(name,
                                                                       (defect.fingerprint,))
    if history.was_ineffective(defect.fingerprint, name):
        return True
    # SIBLINGS OF ONE CONTACT: faces located from the same solid pair are one physical
    # problem, so an op that already failed on one of them is not retried on the others.
    pair = (getattr(defect, "measurements", None) or {}).get("pair")
    if pair and any(history.was_ineffective(other.fingerprint, name) for other in ordered
                    if other is not defect
                    and (getattr(other, "measurements", None) or {}).get("pair") == pair):
        return True
    return history.was_declined(name, (defect.fingerprint,))


def _mesh_candidate(defects, history: RunHistory, *, reserved: frozenset,
                    skip_gaps: bool = False, tries: GapTries | None = None) -> Candidate | None:
    """The MESH_OPS proposal for a region's mesh failures, or None - with the RulesPlanner's own
    bookkeeping, so the two halves of the baseline cannot disagree about what a refusal is.

    ORDERED BY ENTITY, then code, so identical runs propose identically. A failure with no
    fingerprint is `untargetable`; one MESH_OPS does not map (or a volume-stage one) is
    `no_operation_claims_it`; a pair the region already tried, an aim already declined, and a
    target an engineer holds are skipped exactly as the planner skips them. `skip_gaps` passes
    over a sub-element gap (`_sub_element_gap`) - for the severity promotion, which must not put
    the mesher's chord ahead of a kernel cure.
    """
    held = held_fingerprints(defects, reserved)
    ordered = sorted((d for d in defects if _is_mesh(d)
                      and not (skip_gaps and _sub_element_gap(d))),
                     key=lambda d: (tries.rung(d.fingerprint) if tries is not None else 0,
                                    d.entity_index, d.code.value, d.fingerprint))
    for defect in ordered:
        if defect.code in reserved:
            continue
        if not defect.fingerprint:
            history.refuse(f"{defect.entity_type}:{defect.entity_index}", "untargetable",
                           f"a {defect.code.value} on a {defect.entity_type} with no "
                           "measurable extent")
            continue
        # THE CHOICE, THEN ITS FALLBACKS (`mesh_fallbacks`; none with every flag off, so the chain
        # is the single MESH_OPS name it always was). A name the catalogue does not hold is no
        # operation; an operation that CLAIMS the code but refuses this aim through its own
        # `targets_for` (rebuild_face on a non-2D overlap) is passed over, so the aim gets the
        # next name - for an unrecorded-stage overlap, exactly the flag-off choice.
        chain = [n for n in (*_gap_first(defect), MESH_OPS.get(defect.code),
                             *MESH_FALLBACKS.get(defect.code, ()))
                 if n is not None and n in _OPS_BY_NAME
                 and not (defect.code in _OPS_BY_NAME[n].answers
                          and not _OPS_BY_NAME[n].targets_for(defect))]
        # A NAME THE CATALOGUE DOES NOT HOLD IS NO OPERATION: refused, never a KeyError. A
        # sub-mesh gap is exempt from the stage refusal: its operation answers a volume code, so
        # its package is judged by whole-shape scans (`session`), whatever stage gmsh recorded.
        gap_led = bool(chain) and chain[0] == GAP_OP
        if not chain or (_stage_refused(defect) and not gap_led):
            history.refuse(defect.fingerprint, "no_operation_claims_it",
                           f"{defect.code.value} needs a judgement no bounded operation makes")
            continue
        if defect.fingerprint in held:
            continue
        name = next((n for n in chain if not _spent(defect, n, ordered, history, tries)), None)
        if name is None:
            continue
        chosen_op = (_gap_op(tries, defect.fingerprint) if name == GAP_OP and tries is not None
                     else _OPS_BY_NAME[name])
        target = EntityRef(entity_type=str(defect.entity_type), fingerprint=defect.fingerprint,
                           entity_index=int(defect.entity_index),
                           ambiguous=bool(getattr(defect, "ambiguous", False)))
        return Candidate(op=chosen_op, targets=(target,), defect_code=defect.code,
                         defect_fingerprint=defect.fingerprint)
    return None


def _history(attempts: tuple[PackageVerdict, ...]) -> RunHistory:
    """The region's attempts as the ledger the choosing rules already read.

    THIS IS THE WHOLE OF THE TASK IT WAS WRITTEN FOR. The operation-at-a-time loop's only
    advantage over the region driver was a `RunHistory` that PERSISTS across its iterations: it
    tries `sew_faces` on an open shell, marks the pair ineffective, and then proposes
    `fill_planar_hole` - the operation that actually closes the hole. An author handed a fresh
    `RunHistory` on every call re-proposes `sew_faces` for ever, so the region never gets to the
    second idea however many packages it is offered. Rebuilt from `attempts` rather than kept in a
    closure because the author must stay STATELESS: the loop owns what a region has tried, and a
    model author will be handed the same list.

    AN OPERATION THAT DID NOT SURVIVE IS INEFFECTIVE, and "survive" means kept AND inside a
    package that committed - the same rule `region_loop._abandoned` applies, for the same reason:
    a kept operation inside a reverted package left nothing behind either, so treating it as
    effective would have the author propose it again on geometry that no longer carries its work.

    A DECLINE IS NOT A FAILED ATTEMPT, and is recorded as `loop.py` records it: the operation
    judged the aim and ran nothing, so it says nothing about whether the pair can work, and the
    ledger keys it by the AIM instead (`RunHistory.declined`). Marking a decline ineffective would
    ban a pair the kernel never tried.

    KEYED PER `(fingerprint, operation)` over the operation's TARGETS, which is the key
    `RunHistory` documents wanting: an operation that could not fix one entity may be exactly
    right for another, and banning it outright would waste the rest of the catalogue. For every
    package this author writes the targets are the one defect it aimed at, so the pair is exactly
    `(defect_fingerprint, op_name)`; an operation that widened its aim (the self-intersection
    heal takes the analyzer's partner faces) bans the pair on those too, which is conservative in
    the right direction - every member of one tangle proposes the identical heal.
    """
    history = RunHistory()
    for verdict in attempts:
        for op in verdict.verdicts:
            if op.kept and verdict.committed:
                continue
            if op.evidence.get("declined"):
                history.mark_declined(op.op, op.targets)
                continue
            for fingerprint in op.targets:
                history.mark_ineffective(fingerprint, op.op)
    return history


def _keep_refusals(kept: list[dict], fresh: list[dict]) -> None:
    """Add this call's refusals to the run's, ONCE EACH.

    The dedup key is `(defect, reason)`, which is `RunHistory.refuse`'s own key, for its own
    reason: the author is asked once per attempt per region, the paired-rim check is recomputed
    from the current defects every time, and a list that grew on every ask would bury the report
    it exists to produce. The records are passed through unchanged - this is the shape the
    baseline's `RepairRun.refusals` carries and the corpus counts by `reason`, and a second shape
    for the same fact would split that counting in two.
    """
    seen = {(record["defect"], record["reason"]) for record in kept}
    for record in fresh:
        key = (record["defect"], record["reason"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(dict(record))


def _rationale(candidate: Candidate) -> str:
    """Why this operation, in words the package reviewer reads - so a FALLBACK says it is one
    rather than claiming to be the operation MESH_OPS aims at."""
    code = candidate.defect_code
    preferred = MESH_OPS.get(code)
    if candidate.op.name == GAP_OP:
        return (f"{LABEL}: {GAP_OP} is preferred for a {code.value} the probe located on two "
                "faces that do not touch: a gap narrower than the mesh size")
    if preferred is None:
        return (f"{LABEL}: {candidate.op.name} is the narrowest catalogue operation that "
                f"claims the worst defect this region reports")
    if candidate.op.name != preferred:
        return (f"{LABEL}: {candidate.op.name} is the fallback for a {code.value} after "
                f"{preferred} was spent on it")
    return f"{LABEL}: {candidate.op.name} is the operation MESH_OPS aims at a {code.value}"


def rules_author(shape, *, original: Measure, reserved: frozenset = frozenset(),
                 planner: RulesPlanner | None = None, budget: PackageBudget | None = None,
                 max_area_delta_mm2: float | None = None,
                 max_volume_delta_mm3: float | None = None,
                 ) -> Callable[[Region, tuple, tuple[PackageVerdict, ...]],
                               TargetPackage | None]:
    """Build the deterministic `author` the region loop calls:
    `(region, defects, attempts) -> package`.

    A FACTORY, so that the loop's seam stays `(region, defects, attempts)` and a language model
    can take this callable's place without the loop learning which kind of author it has.

    `attempts` IS WHAT THE REGION ALREADY TRIED THIS RUN, and it is the only reason this author
    can reach a second idea: it is turned back into the `RunHistory` the choosing rules already
    read (`_history`), so the rules propose the operation they would have proposed on the
    baseline's next iteration. An empty `attempts` - every region's first call - gives a fresh
    ledger, which is byte-for-byte what this author produced before it had a third argument.

    `shape` IS THE UPLOAD, and it is used for one thing: the through-hole (paired rim) check the
    rules planner runs, which refuses filling for the whole run when two loops are the two ends of
    one missing passage wall. Read from the upload rather than from the region's current geometry
    because the check is conservative either way - a pairing present on the upload is exactly what
    must not be patched - and the author has no access to the session's shape by design. THE
    DEFECTS THAT CHECK RUNS OVER ARE THE WHOLE PART'S, not a region's, and that is the one place
    this author deliberately looks past the region it was asked about: see `observe_part`.

    WHAT THE CLAIM IS BOUNDED BY. The predicate's area and volume allowances default to the
    deployment's own residual caps against the upload's measurement, so a package may never claim
    more movement than the after-checks would permit the whole run. A consequence worth stating:
    an operation that legitimately moves more than that - a hole fill, which DECLARES its added
    area to the caps - is reverted by its own predicate here, because a predicate is stated before
    execution and these rules cannot predict a patch's area. That is a limit of the deterministic
    author, not of the package: an author that can predict its own delta states it, which is
    exactly what a model author will do.
    """
    chosen = planner if planner is not None else RulesPlanner(reserved=frozenset(reserved))
    area = (max_area_delta_mm2 if max_area_delta_mm2 is not None
            else float(rcfg.CAD_REPAIR_MAX_AREA_CHANGE_RATIO) * original.surface_area_mm2)
    volume = (max_volume_delta_mm3 if max_volume_delta_mm3 is not None
              else float(rcfg.CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO) * original.volume_mm3)
    # ONE OPERATION, so the allowance is one. A budget is a bound on what the author asked for,
    # not room to grow into; the run's own clock is the outer bound and lives in the loop.
    allowance = budget if budget is not None else PackageBudget(
        max_ops=1, timeout_s=float(rcfg.CAD_REPAIR_TIMEOUT_S), model_calls=0, deviation_mm=0.0)
    #: EVERY REFUSAL THE RULES MADE ANYWHERE IN THIS RUN, read off the closure by the loop
    #: (`region_loop.run_regions`) the same way `loop.py` reads a planner's `unplayable`. It lives
    #: on the FACTORY, which is per run, and not in `author`, which is per region: a refusal is
    #: run-level policy (filling is off for the whole run once a missing tube wall is seen), so a
    #: per-region ledger would report it once per region or not at all.
    refusals: list[dict] = []
    #: THE PAIRED RIMS OF THE WHOLE PART, computed once per run by `observe_part` below and empty
    #: until the loop calls it. NOT recomputed per region, and that is the point of it - see
    #: `observe_part`.
    part_rims: frozenset = frozenset()

    def observe_part(part_defects: tuple) -> None:
        """Take the WHOLE part's defects, once per run, and work out its paired rims from them.

        A PAIRING IS A PROPERTY OF THE PART, NOT OF A REGION, and this is the only reason this
        function exists. `author` is handed ONE REGION's defects, and `RulesPlanner.propose`
        recomputes the paired-rim set from exactly the list it is handed - so the two ends of one
        missing tube wall that happen to decompose into two different regions never appear in the
        same list, never pair, and are BOTH patched: the through-hole the guard exists to protect
        is sealed, and no refusal is reported either. A region-scoped view of the pairing is not a
        conservative view of it, it is a blind one, and the direction of that blindness is the one
        that ruins the customer's part.

        So the whole part's rims are computed ONCE, here, and `author` seeds every per-region
        `RunHistory` with them (`plan.refuse_paired_rims`) before the rules are asked. A rim is
        then refused because of a partner its own region cannot see, and the run-level fill ban
        covers every region rather than the one that happened to hold both ends.

        READ FROM THE UPLOAD'S SCAN, like `shape` itself and for the same reason the factory's
        docstring gives: the check is conservative either way, and a pairing present on the upload
        is exactly what must not be patched. Once per run and not per attempt, so it also cannot
        be lost as kept operations change the geometry out from under it.

        WHAT STAYS REGION-SCOPED: everything else. This widens only what the author REFUSES, never
        what it proposes for - `author` still works one region, still claims only a fingerprint
        that region holds, and still orders by that region's own defects.

        OPTIONAL AT THE SEAM. The loop calls this through a `getattr` the same way it reads
        `refusals` off the callable, so the `(region, defects, attempts) -> package` signature is
        untouched and an author with no part-level view of its own simply has no attribute.
        """
        nonlocal part_rims
        part_rims = (paired_rim_fingerprints(shape, tuple(part_defects))
                     if shape is not None else frozenset())

    def author(region: Region, defects: tuple,
               attempts: tuple[PackageVerdict, ...] = ()) -> TargetPackage | None:
        if not region.located:
            # THE UNLOCATED REGION IS NOT A LOCUS. Its defects have no measurable extent, so a
            # scan scoped to it is the whole-shape scan and nothing about a package proved there
            # would be scoped at all (session._package_refusal says the same, one layer down).
            # Those defects are a person's question; proposing for them is not this author's job.
            return None

        history = _history(attempts)
        # THE WHOLE PART'S PAIRED RIMS, STATED BEFORE THE RULES ARE ASKED. The planner will also
        # compute the pairing over the region's own defects and refuse on that, which is a SUBSET
        # of this and so changes nothing it has already been told: `RunHistory.refuse` dedups on
        # `(defect, reason)` and `refuse_filling` keeps the first reason. The seeding is what makes
        # the guard whole-part again - without it a passage whose two rims decompose into two
        # regions is sealed (observe_part).
        refuse_paired_rims(history, part_rims)
        tries = gap_tries(attempts)
        leading = _gap_lead(tuple(defects), tries, region, reserved=frozenset(reserved))
        # KERNEL DEFECTS FIRST, MESH FAILURES AFTER. The rules are handed exactly the defects
        # they always were (with CAD_REPAIR_MESH_PROBE off there is no other kind, so this is
        # byte-for-byte the flag-off author); a mesh failure is aimed at only once they have
        # nothing, because a kernel defect is often what the mesher tripped on.
        kernel = tuple(d for d in defects if not _is_mesh(d))
        candidate = leading or chosen.propose(kernel, history, shape=shape)
        # P2-R8: SEVERITY FIRST, as the region walk is (region_loop._SEVERITY_RANK). A mesh
        # failure is an ERROR; when the kernel's pick is only a warning or info sweep, the
        # failure is reached first. Kernel errors keep their place ahead of it. With no mesh
        # defect present this branch never runs, so the flag-off proposal is unchanged.
        # A SUB-ELEMENT GAP DOES NOT JUMP THE QUEUE (wave 4, abc 0060_00608290): a clash whose
        # two faces the kernel measured APART (`clash_kind` GAP) is the mesher's chord, which no
        # heal or fragment closes, and promoting it over the kernel's warning replaced the
        # fix_small_faces every earlier wave kept with a fragment_bodies that could not help. It
        # is still aimed at once the kernel has nothing (the `elif` below), as before.
        jumping = tuple(d for d in defects if mesh_finding_outranks_kernel(d))
        if leading is None and candidate is not None and jumping:
            rank = min((_SEVERITY_RANK.get(d.severity, 9) for d in kernel
                        if d.fingerprint == candidate.defect_fingerprint), default=9)
            if rank > _SEVERITY_RANK[DefectSeverity.error]:
                candidate = (_mesh_candidate(tuple(defects), history,
                                             reserved=frozenset(reserved), skip_gaps=True,
                                             tries=tries)
                             or candidate)
        elif candidate is None:
            candidate = _mesh_candidate(tuple(defects), history, reserved=frozenset(reserved),
                                        tries=tries)
        # WHAT THE RULES REFUSED TO AIM AT, KEPT INSTEAD OF DISCARDED WITH THE LEDGER. The
        # ledger above is rebuilt per call because the author is stateless, but a refusal is not
        # feedback for the next package - it is a thing the customer is owed in the report (the
        # paired rims of a missing bore wall above all, which used to be a silent skip inside the
        # fill). The baseline collects exactly these off its one long-lived `RunHistory`; with
        # this author's ledger thrown away every call, a region run reported NO refusals at all,
        # and a published comparison with refusals on one arm and not the other is not a
        # comparison (docs/research/public-benchmarks.md requires every row to record them).
        _keep_refusals(refusals, history.refusals)
        if candidate is None:
            # NOTHING THE RULES CLAIM ANY MORE. Not a failure: the region is left as it arrived
            # and the run's final scan reports what is still in it. This is also how a region's
            # attempts end early - once every operation that answers its defects has been tried
            # and rolled back, the rules run dry and the loop stops asking.
            return None
        if candidate.defect_fingerprint not in region.fingerprints:
            # A CLAIM THE REGION'S OWN SCAN COULD NOT VERIFY. The scoped re-inspection that judges
            # this package only sees this region, so a claim about anything else is unprovable
            # here - refused by the session too, and not worth a scan to find out.
            return None

        predicate = Predicate(
            # EXACTLY WHAT THE OPERATION AIMS AT, and nothing else. `unchanged` is deliberately
            # empty: these rules cannot predict which neighbouring defects a kernel operation
            # legitimately clears, and `unchanged` fails in both directions - so claiming it would
            # revert correct repairs rather than catch collateral damage.
            cleared=frozenset({candidate.defect_fingerprint}),
            reclassified=frozenset(), unchanged=frozenset(),
            max_area_delta_mm2=float(area), max_volume_delta_mm3=float(volume))
        # NO `tolerance_mm` IN THE PLAN ITEM: the run's tolerance is the loop's to fill in
        # (region_loop._with_tolerance), because it is derived from the part's size and the
        # deployment's deviation ceiling rather than from anything about this region.
        plan = ({"op": candidate.op, "targets": candidate.targets,
                 "defect_code": candidate.defect_code},)
        return TargetPackage(
            region=region, plan=plan, predicate=predicate, budget=allowance,
            intent=(f"clear the {candidate.defect_code.value} on "
                    f"{candidate.defect_fingerprint} in region {region.index}"),
            rationale=_rationale(candidate))

    # THE LOOP READS THIS OFF THE CALLABLE, because the `author` seam is
    # `(region, defects, attempts) -> package | None` and must stay that way: a model author has
    # refusals of its own to report and gains a `refusals` attribute, not a different signature.
    author.refusals = refusals  # type: ignore[attr-defined]
    # AND SO IS THIS, for the same reason: the paired-rim guard needs a view of the whole part
    # (observe_part), and the loop is the only thing that has one. An attribute rather than a
    # fourth argument, because the seam is a commitment to the model author that will take this
    # callable's place and a model author with no such need should not have to accept one.
    author.observe_part = observe_part  # type: ignore[attr-defined]
    return author
