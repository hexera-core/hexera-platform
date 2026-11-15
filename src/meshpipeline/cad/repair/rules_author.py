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

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import mesh_probe
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.operations import OPERATIONS
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.plan import (
    Candidate,
    RulesPlanner,
    RunHistory,
    held_fingerprints,
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
def mesh_ops(*, split_closed_faces: bool | None = None,
             fragment_bodies: bool | None = None) -> dict[DefectCode, str]:
    """MESH_OPS for the given consent (the deployment's flags when not told). Flags off is today's
    mapping, byte for byte."""
    if split_closed_faces is None:
        split_closed_faces = bool(rcfg.CAD_REPAIR_SPLIT_CLOSED_FACES)
    if fragment_bodies is None:
        fragment_bodies = bool(rcfg.CAD_REPAIR_FRAGMENT_BODIES)
    mapping = {
        DefectCode.mesh_open_loop: "fix_face_boundary",
        DefectCode.mesh_overlapping_facets: "fix_shape_global",
    }
    if split_closed_faces:
        mapping[DefectCode.mesh_periodic_surface] = "split_closed_faces"
    if fragment_bodies:
        mapping[DefectCode.mesh_edge_through_face] = "fragment_bodies"
    return mapping


MESH_OPS: dict[DefectCode, str] = mesh_ops()

#: Mesh codes whose mapping holds only when the probe RECORDED the 2D stage. Stricter than the
#: volume-stage refusal every mapping gets: an unrecorded stage is refused too.
SURFACE_ONLY: frozenset[DefectCode] = frozenset({DefectCode.mesh_periodic_surface})

#: Mesh codes whose mapping holds only when the probe RECORDED the 3D stage - and which are
#: therefore EXEMPT from the volume-stage refusal: their package is proved by whole-shape scans.
VOLUME_ONLY: frozenset[DefectCode] = frozenset({DefectCode.mesh_edge_through_face})

_OPS_BY_NAME = {op.name: op for op in OPERATIONS}


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


def _mesh_candidate(defects, history: RunHistory, *, reserved: frozenset) -> Candidate | None:
    """The MESH_OPS proposal for a region's mesh failures, or None - with the RulesPlanner's own
    bookkeeping, so the two halves of the baseline cannot disagree about what a refusal is.

    ORDERED BY ENTITY, then code, so identical runs propose identically. A failure with no
    fingerprint is `untargetable`; one MESH_OPS does not map (or a volume-stage one) is
    `no_operation_claims_it`; a pair the region already tried, an aim already declined, and a
    target an engineer holds are skipped exactly as the planner skips them.
    """
    held = held_fingerprints(defects, reserved)
    ordered = sorted((d for d in defects if _is_mesh(d)),
                     key=lambda d: (d.entity_index, d.code.value, d.fingerprint))
    for defect in ordered:
        if defect.code in reserved:
            continue
        if not defect.fingerprint:
            history.refuse(f"{defect.entity_type}:{defect.entity_index}", "untargetable",
                           f"a {defect.code.value} on a {defect.entity_type} with no "
                           "measurable extent")
            continue
        name = MESH_OPS.get(defect.code)
        # A NAME THE CATALOGUE DOES NOT HOLD IS NO OPERATION: refused, never a KeyError.
        if name is None or name not in _OPS_BY_NAME or _stage_refused(defect):
            history.refuse(defect.fingerprint, "no_operation_claims_it",
                           f"{defect.code.value} needs a judgement no bounded operation makes")
            continue
        if history.was_ineffective(defect.fingerprint, name):
            continue
        if history.was_declined(name, (defect.fingerprint,)):
            continue
        if defect.fingerprint in held:
            continue
        target = EntityRef(entity_type=str(defect.entity_type), fingerprint=defect.fingerprint,
                           entity_index=int(defect.entity_index),
                           ambiguous=bool(getattr(defect, "ambiguous", False)))
        return Candidate(op=_OPS_BY_NAME[name], targets=(target,), defect_code=defect.code,
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
        # KERNEL DEFECTS FIRST, MESH FAILURES AFTER. The rules are handed exactly the defects
        # they always were (with CAD_REPAIR_MESH_PROBE off there is no other kind, so this is
        # byte-for-byte the flag-off author); a mesh failure is aimed at only once they have
        # nothing, because a kernel defect is often what the mesher tripped on.
        candidate = chosen.propose(tuple(d for d in defects if not _is_mesh(d)), history,
                                   shape=shape)
        if candidate is None:
            candidate = _mesh_candidate(tuple(defects), history, reserved=frozenset(reserved))
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
            rationale=(f"{LABEL}: {candidate.op.name} is the operation MESH_OPS aims at a "
                       f"{candidate.defect_code.value}" if candidate.defect_code in MESH_OPS else
                       f"{LABEL}: {candidate.op.name} is the narrowest catalogue operation that "
                       f"claims the worst defect this region reports"))

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
