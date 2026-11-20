# Responsibility: Verify the corpus aggregate reports how each run ENDED in the precise vocabulary
#                 without changing the coarse `outcomes` tally sealed bundles store.
# Boundaries: pure over recorded rows - no CAD is read and no repair runs.
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_endings_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load()

# The run the owner reported, as rows: the 63 "refusals" its own messages broke down into, plus
# the rest of the outcome tally. Four of the five refusal messages were never a refusal by
# anything - only the cap row at the end was.
_MEASURED_RUN = (
    [{"outcome": "refused", "ending": "nothing_to_repair"}] * 32
    + [{"outcome": "refused", "ending": "attempted_none_held"}] * 26
    + [{"outcome": "refused", "ending": "scan_incomplete"}] * 3
    + [{"outcome": "refused", "ending": "no_operation_claims_it"}]
    + [{"outcome": "refused", "ending": "refused", "result_discarded": True}]
    + [{"outcome": "no_change", "ending": "no_change"}] * 13
    + [{"outcome": "repaired", "ending": "repaired"}] * 7
    + [{"outcome": "nothing_to_repair", "ending": "nothing_to_repair"}] * 3
    + [{"outcome": "not_attempted:manual_cleanup", "ending": "not_attempted:manual_cleanup"}]
)


def test_the_run_that_reported_63_refusals_now_reports_one():
    summary = corpus.corpus_summary(list(_MEASURED_RUN))
    assert summary["endings"] == {
        "nothing_to_repair": 35, "attempted_none_held": 26, "scan_incomplete": 3,
        "no_operation_claims_it": 1, "refused": 1, "no_change": 13, "repaired": 7,
        "not_attempted": 1,
    }
    # THE GENUINE CAP REFUSAL IS STILL THERE, and it is now the only thing wearing that word.
    assert summary["endings"]["refused"] == 1


def test_the_coarse_outcomes_tally_is_untouched_so_sealed_bundles_still_verify():
    # benchmark_bundle.load_bundle rechecks every field a stored summary carries. Changing
    # `outcomes` would make every bundle sealed before today read as edited evidence.
    summary = corpus.corpus_summary(list(_MEASURED_RUN))
    assert summary["outcomes"] == {"refused": 63, "no_change": 13, "repaired": 7,
                                   "nothing_to_repair": 3, "not_attempted": 1}


def test_an_old_bundles_rows_are_backfilled_rather_than_read_as_refusals():
    # Rows from before `ending` existed: no ending, no stopped_because, only the message.
    old = [
        {"outcome": "refused",
         "error": "nothing was kept (0 operation(s) attempted, stopped because resolved), so "
                  "there is nothing to promote"},
        {"outcome": "refused",
         "error": "nothing was kept (9 operation(s) attempted, stopped because stalled)"},
        {"outcome": "refused", "result_discarded": True,
         "error": "the run left a tolerance of 0.1159 mm, over the 0.1 mm effective cap"},
    ]
    summary = corpus.corpus_summary(old)
    assert summary["endings"] == {"nothing_to_repair": 1, "attempted_none_held": 1, "refused": 1}
    assert summary["outcomes"] == {"refused": 3}


def test_every_ending_but_repaired_still_reads_as_not_repaired():
    from meshpipeline.cad.repair.ending import NOT_REPAIRED

    summary = corpus.corpus_summary(list(_MEASURED_RUN))
    bad = sum(count for word, count in summary["endings"].items() if word in NOT_REPAIRED)
    # `repaired`, `no_change` and `not_attempted` are the corpus's own pre-existing words and are
    # credited by re-inspection; 66 of the 87 rows end in a word THIS vocabulary names, and every
    # one of those 66 says "not repaired". Nothing was renamed into looking deliverable.
    assert bad == 66
    assert "repaired" not in NOT_REPAIRED
