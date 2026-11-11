# Responsibility: Author ONE target package per region from the existing operation catalogue,
#                 deterministically, so the region loop can be proved end to end with no model in
#                 it - a regression is then a bug in the loop rather than in a prompt.
# Owns: the translation from "the rules planner would try this operation" into a package: its
#       intent, its one-operation plan, the claim it states before execution, and its budget.
# Boundaries: it decides WHAT TO PROPOSE and nothing else. It applies nothing, measures nothing
#             and judges nothing - the session executes the package and a separate measurement
#             judges the claim, because an author may never grade its own work.
# Collaborates with: cad/repair/plan.py (RulesPlanner, whose choice this wraps),
#                    cad/repair/package.py (the package it builds), cad/repair/region_loop.py
#                    (which calls it through the `author` seam), settings/cad_repair.py (the caps
#                    its claim is bounded by).
from __future__ import annotations

from collections.abc import Callable

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.regions import Region

#: What this author reports itself as, beside `rules-v1`: the same choosing rules, one package at a
#: time instead of one operation at a time. A comparison that cannot name which portion produced a
#: run cannot attribute an improvement to model direction, which is the whole point of keeping the
#: deterministic driver (spec decision 6).
LABEL = "regions-rules-v1"


def rules_author(shape, *, original: Measure, reserved: frozenset = frozenset(),
                 planner: RulesPlanner | None = None, budget: PackageBudget | None = None,
                 max_area_delta_mm2: float | None = None,
                 max_volume_delta_mm3: float | None = None,
                 ) -> Callable[[Region, tuple], TargetPackage | None]:
    """Build the deterministic `author` the region loop calls: `(region, defects) -> package`.

    A FACTORY, so that the loop's seam stays `(region, defects)` and a language model can take
    this callable's place without the loop learning which kind of author it has.

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

    def author(region: Region, defects: tuple) -> TargetPackage | None:
        if not region.located:
            # THE UNLOCATED REGION IS NOT A LOCUS. Its defects have no measurable extent, so a
            # scan scoped to it is the whole-shape scan and nothing about a package proved there
            # would be scoped at all (session._package_refusal says the same, one layer down).
            # Those defects are a person's question; proposing for them is not this author's job.
            return None

        candidate = chosen.propose(tuple(defects), RunHistory(), shape=shape)
        if candidate is None:
            # NOTHING THE RULES CLAIM. Not a failure: the region is left as it arrived and the
            # run's final scan reports what is still in it.
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

    return author
