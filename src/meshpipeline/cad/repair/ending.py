# Responsibility: Name HOW A REPAIR RUN ENDED in one word that is true, and derive the older,
#                 coarser word from it so nothing that already reads the old vocabulary breaks.
# Owns: the ending vocabulary, the derivation from (kept, attempted, stop reason, gate), the
#       legacy mapping, and the backfill that reads an ending out of a bundle sealed before this
#       field existed.
# Boundaries: pure strings and arithmetic. It reads no geometry, opens no file and raises nothing.
# Collaborates with: cad/repair/loop.py (the stop reasons), cad/repair/conservative.py (which
#                    attaches the ending to its measurements and to `RepairRefused`),
#                    devtools/validation/repair_corpus.py (which tallies it),
#                    pipeline/repair_words.py (which says it to a customer).
from __future__ import annotations

import re

from meshpipeline.cad.repair.loop import (
    NO_CANDIDATE,
    RESOLVED,
)
from meshpipeline.cad.repair.loop import (
    SCAN_INCOMPLETE as STOP_SCAN_INCOMPLETE,
)
from meshpipeline.cad.repair.loop import (
    UNMESHABLE as STOP_UNMESHABLE,
)
from meshpipeline.cad.repair.session import ESCALATED as STOP_ESCALATED

# WHY THIS MODULE EXISTS. A 90-part corpus run reported `refused: 66`, and all but one of those
# were not refusals. By their own messages: 32 parts needed nothing done, 26 were tried and
# nothing held, 3 could not be fully seen, 1 had nothing in the catalogue that claims what it
# carries. ONE part had actually been refused - by a cap, for leaving a tolerance of 0.1159 mm
# over a 0.1 mm effective cap. Five different answers were being said with the worst-sounding of
# the five words, which is a reporting defect: `refused` is a SAFETY SIGNAL and it is worth
# nothing if almost every occurrence of it is something else.
#
# THE CUT THAT MATTERS, and the only one this vocabulary makes: did a GATE say no? A cap exceeded,
# a tolerance over the effective cap, the reviewer rejecting, two rejections escalating - those
# are decisions we made against a result we had. Everything else is a description of how far we
# got. The first is `refused`; the rest have their own words.
#
# WHAT THIS IS NOT. None of these words says a part is DELIVERABLE. `REPAIRED` is the only ending
# a usable file comes out of, and even it is re-credited downstream by re-inspection rather than
# believed (devtools/validation/repair_corpus.py derives `repaired`/`no_change`/
# `changed_without_fixing` from a second scan, never from this string).

#: The part already met the goal: the scan found nothing an operation should aim at, and nothing
#: was attempted. NOT A REFUSAL AND NOT A FAILURE - there was no work.
NOTHING_TO_REPAIR = "nothing_to_repair"

#: The catalogue has no operation that claims what is left (`no_candidate`). An honest "we
#: CANNOT", which is not "we will not": no gate was consulted, we simply have nothing to offer.
NO_OPERATION_CLAIMS = "no_operation_claims_it"

#: Operations were attempted and not one survived its own checks - stalled, out of operations, out
#: of clock, or every region tried. WE TRIED AND NOTHING HELD, and the part is still broken.
ATTEMPTED_NONE_HELD = "attempted_none_held"

#: A GATE SAID NO. The only true refusal, and the whole reason the word is kept scarce: a cap
#: exceeded, a tolerance over the effective cap, a sanity or cumulative check discarding the
#: result whole, the reviewer rejecting. The customer's file is untouched and a person decides.
REFUSED = "refused"

#: The defect scan hit its own budget, so the list is partial. WE DO NOT KNOW - no absence can be
#: claimed from a walk that stopped, so this is neither a pass nor a failure but its own answer.
SCAN_INCOMPLETE = STOP_SCAN_INCOMPLETE

#: THE PART STILL WILL NOT MESH, on a real mesher answer. Either every error left is a mesher one
#: (the loop's own `unmeshable` stop: the geometry checks pass and gmsh still refuses), or
#: operations were KEPT and the final whole-part probe answered and still refused - geometry
#: errors may remain beside it, and `stopped_because` / `operations_kept` still say so. A
#: first-class honest ending per the spec's "What stays sacred". A probe that was skipped, timed
#: out or died is NOT a mesher answer and never earns this word.
UNMESHABLE = STOP_UNMESHABLE

#: At least one region is queued for a person, whatever else was committed. The spec's second
#: honest ending; it must never be folded into a success.
ESCALATED = STOP_ESCALATED

#: Something was kept and written. The ONLY ending a file comes out of.
REPAIRED = "repaired"

#: Every ending, declaration order, worst-named last-resort first so a reader of a tally sees the
#: vocabulary and not a guess at it.
ENDINGS = (REPAIRED, NOTHING_TO_REPAIR, NO_OPERATION_CLAIMS, ATTEMPTED_NONE_HELD, UNMESHABLE,
           ESCALATED, SCAN_INCOMPLETE, REFUSED)

#: THE ENDINGS THAT MEAN THE PART IS NOT REPAIRED. Nothing reading this set can mistake a part
#: that still does not mesh for a delivery, whatever the ending happens to be called. `REPAIRED`
#: is deliberately the only member absent - and it is still re-credited by re-inspection.
NOT_REPAIRED = frozenset(ENDINGS) - {REPAIRED}

#: NEW WORD -> THE OLD WORD A READER BUILT BEFORE THIS FIELD EXISTED STILL EXPECTS. Every ending
#: reachable from a `RepairRefused` was called `refused`, so they all map back to it: the old
#: field is not redefined, it is DERIVED, and the benchmark track, the sealed bundles and
#: `corpus_summary`'s `outcomes` tally keep the exact numbers they had.
LEGACY_OUTCOME = {
    REPAIRED: "repaired",
    NOTHING_TO_REPAIR: "refused",
    NO_OPERATION_CLAIMS: "refused",
    ATTEMPTED_NONE_HELD: "refused",
    UNMESHABLE: "refused",
    ESCALATED: "refused",
    SCAN_INCOMPLETE: "refused",
    REFUSED: "refused",
}


def ending_for(*, kept: int, attempted: int, stopped_because: str,
               gate_refusal: bool = False, mesh_failed: bool = False) -> str:
    """The one word for how a run ended. ONE place, so no caller can disagree with another.

    `mesh_failed` is True only when the run's FINAL whole-part mesh probe ran, answered, and
    still refused the part (`loop.definitive_mesh_failure`). A skipped or timed-out probe, or the
    probe being off, is False - and then nothing here changes.

    THE PRECEDENCE IS NOT ARBITRARY and mirrors `region_loop._terminal`:

    1. `gate_refusal` WINS OVER EVERYTHING. A cap or a reviewer said no to a result we were
       holding, and that is the answer however the loop had got there. Burying it under a nicer
       word would be the worst thing this module could do.
    2. An incomplete scan beats every description of progress: we could not see the part, so we
       cannot say what state it is in.
    3. `escalated` and `unmeshable` beat `repaired`, deliberately. A run that committed three
       regions and queued a fourth is a PARTIAL repair, and a part whose geometry is clean and
       which gmsh still refuses is not a delivery.
    4. Something kept over a part the final probe still refuses is `unmeshable` too, NOT
       `repaired` (eval wave 4, abc/0051_00513292: a committed package, `regions_exhausted`,
       `repaired`, and the exported STEP still failed gmsh with edge_through_face). The word is
       reused rather than a new `repaired_*` one coined, because the honest answer is the same
       one - the part does not mesh - and every reader already treats it as not a delivery
       (`NOT_REPAIRED`, the legacy map); a word starting `repaired` would invite exactly the
       prefix-match misreading this ruling exists to stop.
    5. Only then does "something was kept" mean `repaired`.
    """
    if gate_refusal:
        return REFUSED
    if stopped_because == SCAN_INCOMPLETE:
        return SCAN_INCOMPLETE
    if stopped_because == ESCALATED:
        return ESCALATED
    if stopped_because == UNMESHABLE:
        return UNMESHABLE
    if int(kept) > 0 and mesh_failed:
        return UNMESHABLE
    if int(kept) > 0:
        return REPAIRED
    if stopped_because == NO_CANDIDATE:
        return NO_OPERATION_CLAIMS
    if stopped_because == RESOLVED and int(attempted) == 0:
        # NOTHING WAS WRONG. `resolved` with nothing attempted means the scan found no error for
        # an operation to aim at, so the loop finished before it started. This is the 32-part
        # majority of the measured "refusals".
        return NOTHING_TO_REPAIR
    # INCLUDING `resolved` WITH ATTEMPTS: the loop cleared its error list and the session kept
    # none of the attempts that did it, so the file is unchanged. "We tried and nothing held" is
    # the true statement; "nothing to repair" would be a lie about a part that had defects.
    return ATTEMPTED_NONE_HELD


def legacy_outcome(ending: str) -> str:
    """The pre-ending word for `ending`. Unknown endings read as `refused`, FAIL-LOUD-SOUNDING on
    purpose: a word this build does not know must not be quietly promoted to something benign."""
    return LEGACY_OUTCOME.get(str(ending), "refused")


_ATTEMPTED_RE = re.compile(r"\((\d+) operation")
_STOPPED_RE = re.compile(r"stopped because ([a-z_]+)")


def backfill_ending(row) -> str:
    """The ending of a row written BEFORE this field existed, read out of what it does carry.

    OLD BUNDLES MUST STILL BE READABLE, and an old `refused` row carries no `ending` and no
    `stopped_because` - the refusal path recorded neither. What it does carry is
    `result_discarded` (set only by the cap after-checks, which is exactly a gate saying no) and
    the refusal MESSAGE, whose two numbers this parses. A row that already has an `ending` is
    returned unchanged, so re-deriving a new bundle's summary is not a reinterpretation.

    WHEN THE MESSAGE CANNOT BE PARSED the answer is `refused` - the same word the row already had.
    Guessing something softer from evidence we do not have is the one failure mode this whole
    change exists to avoid, so the backfill is only ever allowed to be as kind as the facts are.
    """
    existing = str((row or {}).get("ending") or "")
    if existing:
        return existing
    outcome = str((row or {}).get("outcome") or "")
    if outcome != "refused":
        # Not a refusal path at all: `repaired`, `no_change`, `nothing_to_repair`,
        # `not_attempted:*`, `unreadable`, `unavailable`, `repair_failed`. Those words were
        # already precise, so they are their own ending and this module does not rename them.
        return outcome
    if (row or {}).get("result_discarded"):
        return REFUSED
    message = str((row or {}).get("error") or "")
    stopped = _STOPPED_RE.search(message)
    if stopped is None:
        return REFUSED
    attempted = _ATTEMPTED_RE.search(message)
    return ending_for(kept=0, attempted=int(attempted.group(1)) if attempted else 1,
                      stopped_because=stopped.group(1))


__all__ = ["ATTEMPTED_NONE_HELD", "ENDINGS", "ESCALATED", "LEGACY_OUTCOME", "NOTHING_TO_REPAIR",
           "NOT_REPAIRED", "NO_OPERATION_CLAIMS", "REFUSED", "REPAIRED", "SCAN_INCOMPLETE",
           "UNMESHABLE", "backfill_ending", "ending_for", "legacy_outcome"]
