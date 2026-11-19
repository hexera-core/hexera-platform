# Responsibility: Replay an AGENT-DRIVEN repair by re-applying the packages it recorded, in order,
#                 through a fresh session - and say honestly which of them no longer has a region.
# Owns: `replay_packages`, the record -> `TargetPackage` rehydration, the fingerprint-based region
#       match, and the `replay_miss` vocabulary.
# Boundaries: it authors nothing, reviews nothing and calls no model. A recorded verdict stands in
#             for the review, so no reviewer is consulted and no author is asked for anything; it
#             runs no mesher of its own beyond the session's own scans. IT IS NOT A SECOND REPAIR
#             PATH: it states no caps, waives none, and makes no claim a customer is handed - it
#             answers "do these recorded decisions still produce that result?", which is what
#             byte-identical replay answered for a rules-only run and cannot answer for an agent.
# Collaborates with: cad/repair/region_loop.py (`replay_record`, which writes what this reads),
#                    cad/repair/session.py (`try_package`, the one executor), cad/repair/regions.py
#                    (the fresh decomposition this matches records against),
#                    cad/repair/package_review.py (whose posture is held off for the replay).
from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path

from meshpipeline.cad.repair import package_review
from meshpipeline.cad.repair.contracts import RepairMeasurement, RepairReport
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region, decompose
from meshpipeline.cad.repair.session import RepairSession

logger = logging.getLogger(__name__)

#: A RECORDED PACKAGE WHOSE REGION IS NOT IN THIS DECOMPOSITION. Recorded, never guessed: a
#: package names its region by the fingerprints it claimed, and if a re-run's decomposition holds
#: no region with those fingerprints then the geometry the package was written for is not there.
#: Applying it to the nearest-looking region would be a repair nobody authored.
REPLAY_MISS = "replay_miss"

#: A RECORDED PACKAGE THE ORIGINAL RUN'S REVIEWER DID NOT ACCEPT. `region_loop.replay_record`
#: only writes committed packages, so this is a record that was edited or came from somewhere
#: else; it is skipped rather than trusted, because the recorded verdict is the only review a
#: replay has. `reviewed: null` is NOT this: it means no reviewer was in force at all (the review
#: posture was off and the package committed on its predicate alone), which is a run that is
#: perfectly replayable - reading a missing reviewer as a rejection would make every unreviewed
#: agent run unreplayable.
REPLAY_NOT_ACCEPTED = "replay_not_accepted"

#: THE PACKAGE WAS APPLIED AND THE SESSION DID NOT COMMIT IT. Not an error of the replay: it is
#: the interesting answer, because it says the recorded decision no longer holds on this part.
REPLAY_NOT_COMMITTED = "replay_not_committed"

REPLAY_COMMITTED = "committed"


@contextlib.contextmanager
def _review_off() -> Iterator[None]:
    """Hold the package reviewer off for the duration of a replay.

    WHY A SWAP AND NOT A FLAG. `session.try_package` reads `package_review.posture()` at call time
    and there is no argument to override it, so this is the narrowest honest way to say "the
    verdict is already recorded, do not ask a model". It is swapped on the review module's own
    public reader, restored unconditionally, and the repair thread is synchronous, so nothing else
    observes it.

    SEAM, STATED: the right shape is a `review=` override on `try_package` (session.py), and when
    that exists this helper is deleted rather than kept beside it.
    """
    original = package_review.posture
    package_review.posture = lambda: "off"
    try:
        yield
    finally:
        package_review.posture = original


def _package_of(record: dict, region: Region) -> TargetPackage:
    """One recorded package, rehydrated against the region a fresh decomposition found.

    THE PLAN AND THE CLAIM ARE TAKEN EXACTLY AS WRITTEN, including each item's `tolerance_mm`:
    `region_loop` records the package as it was OFFERED, so the run's tolerance is already in it
    and nothing here invents one. The budget is the record's own plan length and no clock - a
    replay is bounded by the caller, not by a per-package deadline it would have to make up.
    """
    predicate = record.get("predicate") or {}
    plan = tuple(dict(item) for item in record.get("plan") or ())
    return TargetPackage(
        region=region,
        intent="replay",
        plan=plan,
        predicate=Predicate(
            cleared=frozenset(str(x) for x in predicate.get("cleared") or ()),
            reclassified=frozenset(str(x) for x in predicate.get("reclassified") or ()),
            unchanged=frozenset(str(x) for x in predicate.get("unchanged") or ()),
            max_area_delta_mm2=float(predicate.get("max_area_delta_mm2", 0.0) or 0.0),
            max_volume_delta_mm3=float(predicate.get("max_volume_delta_mm3", 0.0) or 0.0)),
        budget=PackageBudget(max_ops=max(1, len(plan)), timeout_s=float("inf"),
                             model_calls=0, deviation_mm=0.0),
        rationale=f"replay of a recorded {record.get('review_tier') or 'reviewer'} acceptance")


def _match(regions: tuple[Region, ...], wanted: list[str]) -> Region | None:
    """The region whose fingerprints are EXACTLY the recorded set, or None.

    EXACT AND NOT OVERLAPPING. A region is a conservative, over-merged cluster, so a decomposition
    that merged two of the original's regions into one would overlap both records and applying
    either would aim a claim at geometry it never claimed. A miss is the honest answer.
    """
    target = frozenset(str(f) for f in wanted)
    for region in regions:
        if frozenset(region.fingerprints) == target:
            return region
    return None


def replay_packages(source: Path, destination: Path, records: list[dict]) -> RepairReport:
    """Re-apply `records` to `source` in order and write the result to `destination`.

    THE REPLAY CONTRACT FOR AGENT-DRIVEN RUNS (spec pillar 4). An agent's proposal is not a
    function of the part, so re-running the agent proves nothing about the run being reproduced;
    what is reproducible is what it DECIDED, which is what `region_loop.replay_record` writes.
    Byte-identical replay remains the contract for rules-only runs, and nothing here touches it.

    REVIEW IS THE RECORDED VERDICT, so there is no model call: a record whose `reviewed` is not an
    acceptance is skipped as `replay_not_accepted`, and an accepted one is applied with the
    reviewer held off (`_review_off`). THE PREDICATE IS STILL JUDGED - the session's own scoped
    proof runs exactly as it did - because a recorded acceptance says a person or a model was
    willing to ship that claim, not that the claim is true of whatever this part is now.

    `diagnostics["replay"]` is the per-record outcome in order, and the report's measurements carry
    the committed count, the final `valid` and the solid count, which is what a caller compares
    against the run it is replaying.
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.caps import measure
    from meshpipeline.cad.repair.conservative import _write_step

    src, dest = Path(source), Path(destination)
    shape = _read_shape(src)
    original = measure(shape)
    work = RepairSession(shape)
    before = work.defects()
    # THE DECOMPOSITION OF THIS PART, not the recorded one. The records name their regions by
    # fingerprint precisely so that the replay may decompose freshly and still match.
    regions = decompose(before, diagonal_mm=original.diagonal_mm)

    outcomes: list[dict] = []
    committed = 0
    with _review_off():
        for position, record in enumerate(records or ()):
            wanted = list(record.get("region_fingerprints") or ())
            reviewed = record.get("reviewed")
            if reviewed is not None and str(reviewed) != package_review.ACCEPT:
                outcomes.append({"position": position, "outcome": REPLAY_NOT_ACCEPTED,
                                 "region_fingerprints": sorted(str(f) for f in wanted),
                                 "reviewed": record.get("reviewed")})
                continue
            region = _match(regions, wanted)
            if region is None:
                # RECORDED, NEVER GUESSED.
                outcomes.append({"position": position, "outcome": REPLAY_MISS,
                                 "region_fingerprints": sorted(str(f) for f in wanted)})
                continue
            verdict = work.try_package(_package_of(record, region))
            outcomes.append({
                "position": position,
                "outcome": REPLAY_COMMITTED if verdict.committed else REPLAY_NOT_COMMITTED,
                "region_fingerprints": sorted(str(f) for f in wanted),
                "status": verdict.status, "reason": verdict.reason,
                "review_tier": record.get("review_tier")})
            if verdict.committed:
                committed += 1

    final = measure(work.shape)
    _write_step(work.shape, dest)
    logger.info("replay: records=%d committed=%d misses=%d valid=%s",
                len(records or ()), committed,
                sum(1 for o in outcomes if o["outcome"] == REPLAY_MISS), final.valid)
    return RepairReport(
        defects=(),
        measurements=(
            RepairMeasurement(name="replay_records", value=float(len(records or ())), unit="count"),
            RepairMeasurement(name="replay_committed", value=float(committed), unit="count"),
            RepairMeasurement(
                name="replay_misses", unit="count",
                value=float(sum(1 for o in outcomes if o["outcome"] == REPLAY_MISS))),
            RepairMeasurement(name="solids", value=float(final.solids), unit="count"),
        ),
        operations=tuple(dict(o) for o in outcomes),
        summary=(f"replayed {committed} of {len(records or ())} recorded package(s); "
                 f"result is {'valid' if final.valid else 'invalid'}"),
        diagnostics={"replay": [dict(o) for o in outcomes],
                     "before": original.to_dict(), "after": final.to_dict(),
                     "valid": bool(final.valid)})
