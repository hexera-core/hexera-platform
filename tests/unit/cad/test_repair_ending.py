# Responsibility: Verify the ending vocabulary says what actually happened - that each of the five
#                 ways a run really ends gets its OWN word, that a gate refusal is still
#                 unmistakably `refused`, and that the old coarse word is still derivable.
# Boundaries: pure strings. No kernel, no file, no repair.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair import ending as E

# THE FIVE ENDINGS MEASURED ON THE REAL-FAILURE CORPUS, by their own stop reason, with both sides
# written as LITERALS. Parametrising this table on `E.NOTHING_TO_REPAIR` and friends would make it
# pass for any value those names happen to hold, including the single word this change exists to
# stop them all from sharing.
_MEASURED = [
    # 32 of the 66: `nothing was kept (0 operations attempted, stopped because resolved)`
    (0, "resolved", "nothing_to_repair"),
    # 26 of the 66: `nothing was kept (N operations attempted, stopped because stalled)`
    (4, "stalled", "attempted_none_held"),
    # 3 of the 66
    (2, "scan_incomplete", "scan_incomplete"),
    # 1 of the 66
    (1, "no_candidate", "no_operation_claims_it"),
]


@pytest.mark.parametrize(("attempted", "stopped", "expected"), _MEASURED)
def test_each_real_stop_reason_gets_its_own_word(attempted, stopped, expected):
    assert E.ending_for(kept=0, attempted=attempted, stopped_because=stopped) == expected


def test_the_five_measured_endings_are_five_different_words():
    # The defect itself: four stop reasons and a cap refusal all said "refused". If any two of
    # these collapse again this fails, whatever the words have been renamed to.
    words = {E.ending_for(kept=0, attempted=a, stopped_because=s) for a, s, _ in _MEASURED}
    words.add(E.ending_for(kept=1, attempted=1, stopped_because="resolved", gate_refusal=True))
    assert len(words) == 5


def test_a_genuine_cap_refusal_is_still_reported_as_a_refusal():
    # The 1 of the 66 that was real: "the run left a tolerance of 0.1159 mm, over the 0.1 mm
    # effective cap". A gate said no to a result we were holding, and no other fact may soften it.
    assert E.ending_for(kept=3, attempted=3, stopped_because="resolved",
                        gate_refusal=True) == "refused"
    assert E.ending_for(kept=0, attempted=0, stopped_because="scan_incomplete",
                        gate_refusal=True) == "refused"
    assert E.ending_for(kept=2, attempted=2, stopped_because="escalated",
                        gate_refusal=True) == "refused"


def test_the_spec_honest_endings_survive_and_beat_a_kept_operation():
    # "unmeshable", "escalated" and "not-proved" are first-class per the spec. A run that kept
    # something and still queued a region is a PARTIAL repair, not a repair.
    assert E.ending_for(kept=5, attempted=6, stopped_because="escalated") == "escalated"
    assert E.ending_for(kept=5, attempted=6, stopped_because="unmeshable") == "unmeshable"
    # `not_proved` is the pipeline's promotion-proof word and is not a loop ending; this module
    # must not invent a loop ending that shadows it.
    assert "not_proved" not in E.ENDINGS


def test_only_a_kept_run_is_repaired_and_every_other_ending_says_not_repaired():
    assert E.ending_for(kept=1, attempted=3, stopped_because="resolved") == "repaired"
    assert E.NOT_REPAIRED == {"nothing_to_repair", "no_operation_claims_it", "attempted_none_held",
                              "unmeshable", "escalated", "scan_incomplete", "refused"}
    assert "repaired" not in E.NOT_REPAIRED


def test_resolved_with_attempts_but_nothing_kept_is_not_nothing_to_repair():
    # The dangerous confusion in the other direction: this part HAD defects, we tried, and the
    # file is unchanged. Calling it "nothing to repair" would make a failure look like success.
    assert E.ending_for(kept=0, attempted=3, stopped_because="resolved") == "attempted_none_held"


def test_the_old_word_is_derived_not_redefined():
    # Every ending reachable from a `RepairRefused` was called `refused`, so `outcomes` tallies in
    # sealed bundles keep the exact numbers they had.
    for word in ("nothing_to_repair", "no_operation_claims_it", "attempted_none_held",
                 "unmeshable", "escalated", "scan_incomplete", "refused"):
        assert E.legacy_outcome(word) == "refused"
    assert E.legacy_outcome("repaired") == "repaired"
    # A word this build does not know is not quietly promoted to something benign.
    assert E.legacy_outcome("some_future_ending") == "refused"


@pytest.mark.parametrize(("message", "expected"), [
    ("nothing was kept (0 operation(s) attempted, stopped because resolved), so there is nothing "
     "to promote - re-exporting the upload would change its topology without repairing it",
     "nothing_to_repair"),
    ("nothing was kept (7 operation(s) attempted, stopped because stalled), so there is nothing "
     "to promote", "attempted_none_held"),
    ("nothing was kept (2 operation(s) attempted, stopped because scan_incomplete)",
     "scan_incomplete"),
    ("nothing was kept (1 operation(s) attempted, stopped because no_candidate)",
     "no_operation_claims_it"),
])
def test_a_bundle_sealed_before_this_field_is_still_readable(message, expected):
    assert E.backfill_ending({"outcome": "refused", "error": message}) == expected


def test_an_unparseable_old_refusal_stays_refused():
    # A cap refusal's message names the figure, not a stop reason - and that row is also the one
    # that must never soften. So is a row whose message we cannot read at all.
    cap = {"outcome": "refused", "result_discarded": True,
           "error": "the run left a tolerance of 0.1159 mm, over the 0.1 mm effective cap"}
    assert E.backfill_ending(cap) == "refused"
    assert E.backfill_ending({"outcome": "refused", "error": "something else entirely"}) \
        == "refused"
    assert E.backfill_ending({"outcome": "refused"}) == "refused"


def test_the_backfill_does_not_reinterpret_a_row_that_states_its_ending():
    assert E.backfill_ending({"outcome": "refused", "ending": "escalated",
                              "error": "stopped because stalled"}) == "escalated"
    # Words that were already precise are their own ending and are not renamed.
    for outcome in ("repaired", "no_change", "nothing_to_repair", "not_attempted:manual_cleanup",
                    "unreadable", "unavailable", "repair_failed", "changed_without_fixing"):
        assert E.backfill_ending({"outcome": outcome}) == outcome


# A PART THAT STILL WILL NOT MESH IS NOT "REPAIRED" (eval wave 4, abc/0051_00513292): a package
# was committed and accepted, the run stopped `regions_exhausted` with geometry errors left beside
# a mesh one, and `kept > 0` alone made the ending `repaired` while gmsh still refused the part.

def test_kept_operations_over_a_definitive_mesh_failure_end_unmeshable():
    assert E.ending_for(kept=1, attempted=1, stopped_because="regions_exhausted",
                        mesh_failed=True) == "unmeshable"
    assert E.ending_for(kept=3, attempted=4, stopped_because="stalled",
                        mesh_failed=True) == "unmeshable"
    # Still not a delivery, whatever it is called.
    assert "unmeshable" in E.NOT_REPAIRED


def test_kept_operations_with_no_mesh_failure_are_still_repaired():
    # Mesh probe off, or a probe that passed: nothing changes.
    assert E.ending_for(kept=1, attempted=1, stopped_because="regions_exhausted") == "repaired"
    assert E.ending_for(kept=1, attempted=1, stopped_because="resolved",
                        mesh_failed=False) == "repaired"


def test_a_mesh_failure_does_not_outrank_the_harder_endings():
    assert E.ending_for(kept=1, attempted=1, stopped_because="resolved", mesh_failed=True,
                        gate_refusal=True) == "refused"
    assert E.ending_for(kept=1, attempted=1, stopped_because="escalated",
                        mesh_failed=True) == "escalated"
    assert E.ending_for(kept=1, attempted=1, stopped_because="scan_incomplete",
                        mesh_failed=True) == "scan_incomplete"
    # Nothing kept: the existing honest "we tried and nothing held" is not renamed.
    assert E.ending_for(kept=0, attempted=2, stopped_because="stalled",
                        mesh_failed=True) == "attempted_none_held"


# THE OTHER DIRECTION (eval wave 6): kept operations over a part whose DELIVERED file newly
# meshes is `repaired`, even when the loop stopped `unmeshable` (a stale in-session scan) or
# `escalated` (another region went to a person). The stop reason stays in `stopped_because`.

def test_kept_operations_over_a_newly_meshing_part_end_repaired():
    for stop in ("unmeshable", "escalated", "regions_exhausted", "resolved", "stalled"):
        assert E.ending_for(kept=1, attempted=1, stopped_because=stop,
                            newly_meshes=True) == "repaired"


def test_newly_meshing_does_not_outrank_a_gate_an_unseen_part_or_nothing_kept():
    assert E.ending_for(kept=1, attempted=1, stopped_because="resolved", newly_meshes=True,
                        gate_refusal=True) == "refused"
    assert E.ending_for(kept=1, attempted=1, stopped_because="scan_incomplete",
                        newly_meshes=True) == "scan_incomplete"
    assert E.ending_for(kept=0, attempted=2, stopped_because="stalled",
                        newly_meshes=True) == "attempted_none_held"


def test_the_two_mesh_rules_hold_together():
    # #505 and #520 side by side. The in-session final probe refusing (`mesh_failed`) keeps a
    # kept run from reading `repaired`; the DELIVERED file newly meshing (`newly_meshes`) is the
    # stronger evidence, because it is the artefact the customer meshes, so it wins when both hold.
    assert E.ending_for(kept=1, attempted=1, stopped_because="regions_exhausted",
                        mesh_failed=True) == "unmeshable"
    assert E.ending_for(kept=1, attempted=1, stopped_because="regions_exhausted",
                        mesh_failed=True, newly_meshes=True) == "repaired"
    assert E.ending_for(kept=0, attempted=1, stopped_because="regions_exhausted",
                        mesh_failed=True, newly_meshes=True) == "attempted_none_held"
