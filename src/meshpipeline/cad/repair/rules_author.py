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
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import PackageVerdict

#: What this author reports itself as, beside `rules-v1`: the same choosing rules, one package at a
#: time instead of one operation at a time. A comparison that cannot name which portion produced a
#: run cannot attribute an improvement to model direction, which is the whole point of keeping the
#: deterministic driver (spec decision 6).
LABEL = "regions-rules-v1"


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
    must not be patched - and the author has no access to the session's shape by design.

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

    def author(region: Region, defects: tuple,
               attempts: tuple[PackageVerdict, ...] = ()) -> TargetPackage | None:
        if not region.located:
            # THE UNLOCATED REGION IS NOT A LOCUS. Its defects have no measurable extent, so a
            # scan scoped to it is the whole-shape scan and nothing about a package proved there
            # would be scoped at all (session._package_refusal says the same, one layer down).
            # Those defects are a person's question; proposing for them is not this author's job.
            return None

        history = _history(attempts)
        candidate = chosen.propose(tuple(defects), history, shape=shape)
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
            rationale=(f"{LABEL}: {candidate.op.name} is the narrowest catalogue operation that "
                       f"claims the worst defect this region reports"))

    # THE LOOP READS THIS OFF THE CALLABLE, because the `author` seam is
    # `(region, defects, attempts) -> package | None` and must stay that way: a model author has
    # refusals of its own to report and gains a `refusals` attribute, not a different signature.
    author.refusals = refusals  # type: ignore[attr-defined]
    return author
