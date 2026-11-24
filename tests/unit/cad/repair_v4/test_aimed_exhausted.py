# Responsibility: the ONE read-only fact a generator may gate a last-resort op on - has this lineage's AIMED work
#                 all been tried without cleaning? - and the case it must refuse: aimed rows never offered at all.
# Boundaries: hermetic. The fact's shape and its arithmetic; the wiring into the expansion loop is exercised by
#             the search tests.
from __future__ import annotations

from types import SimpleNamespace as NS

from meshpipeline.cad.repair_v4.search import AIMED_MEASURED, AimedProgress, _aimed_candidates, _lineage_aimed
from meshpipeline.cad.repair_v4.types import Site


def _site(**graph):
    return Site(id="S1", kind="proximity", entities=graph.pop("entities", ["F1", "F2"]), xyz=[],
                meshers_failing=["gmsh_default"], dist_over_h=None, graph=dict(graph), view_png=None)


def _cand(op="defeature", targets=("F1",), **params):
    return NS(op=op, targets=list(targets), params=dict(params))


# --- the fact nobody may misread -------------------------------------------------------------------------------


def test_nothing_offered_is_NOT_exhausted() -> None:
    """THE CASE THE OWNER'S GATING EXCLUDES. A file whose aimed rows were never proposed has exhausted nothing, so
    a part-wide last resort must not be reached for."""
    assert AimedProgress().exhausted is False
    assert AimedProgress(offered=0, tried=0, cleaned=0).exhausted is False


def test_offered_and_all_tried_with_none_clean_IS_exhausted() -> None:
    assert AimedProgress(offered=3, tried=3, cleaned=0).exhausted is True


def test_an_untried_aimed_row_is_not_exhausted() -> None:
    """A row pruned, deduplicated or cut by k was never put to the test, so the aimed work is not spent."""
    assert AimedProgress(offered=3, tried=2, cleaned=0).exhausted is False


def test_an_aimed_row_that_CLEANED_is_never_exhausted() -> None:
    """If the aimed work succeeded there is nothing to fall back from."""
    assert AimedProgress(offered=3, tried=3, cleaned=1).exhausted is False


def test_plus_accumulates_and_leaves_the_original_alone() -> None:
    a = AimedProgress(offered=1, tried=1)
    b = a.plus(offered=2, tried=1, cleaned=1)
    assert (a.offered, a.tried, a.cleaned) == (1, 1, 0)
    assert (b.offered, b.tried, b.cleaned) == (3, 2, 1)


# --- what counts as aimed --------------------------------------------------------------------------------------


def test_a_located_face_is_aimed_and_a_stranger_is_not() -> None:
    site = _site(entities=["F1", "F2"], mesher_faces=["F1"])
    assert _aimed_candidates(NS(sites=[site]), [_cand(targets=("F1",))]) == 1
    assert _aimed_candidates(NS(sites=[site]), [_cand(targets=("F2",))]) == 0      # the site's, not measured
    assert _aimed_candidates(NS(sites=[site]), [_cand(targets=("F9",))]) == 0      # neither


def test_a_part_wide_row_is_never_aimed() -> None:
    """The last resort itself must not count as the aimed work it is waiting for."""
    site = _site(entities=["F1"], mesher_faces=["F1"])
    assert _aimed_candidates(NS(sites=[site]), [_cand(op="defeature", targets=())]) == 0


def test_a_state_with_no_sites_counts_nothing() -> None:
    assert _aimed_candidates(NS(sites=[]), [_cand()]) == 0


def test_the_best_classification_over_the_sites_wins() -> None:
    """A candidate built for one site is not made less aimed by another site that does not name its targets."""
    measured = _site(entities=["F1"], mesher_faces=["F1"])
    other = _site(entities=["F7"], mesher_faces=["F7"])
    assert _aimed_candidates(NS(sites=[other, measured]), [_cand(targets=("F1",))]) == 1


def test_an_unclassifiable_candidate_is_not_counted_as_aimed() -> None:
    """The safe direction: a miss makes `exhausted` FALSE, so the last resort stays shut rather than opening."""
    site = _site(entities=["F1"], mesher_faces=["F1"])
    assert _aimed_candidates(NS(sites=[site]), [NS(op="x", targets=None, params=None)]) == 0


# --- the lineage sum -------------------------------------------------------------------------------------------


def test_the_fact_sums_over_the_LINEAGE_not_one_state() -> None:
    by_state = {"S0": AimedProgress(offered=2, tried=2), "S1": AimedProgress(offered=1, tried=1),
                "S9": AimedProgress(offered=5, tried=0)}        # another branch: must not count
    got = _lineage_aimed(by_state, NS(lineage=("S0", "S1"), id="S1"))
    assert (got.offered, got.tried, got.cleaned) == (3, 3, 0) and got.exhausted is True


def test_a_clean_anywhere_on_the_lineage_unexhausts_it() -> None:
    by_state = {"S0": AimedProgress(offered=1, tried=1, cleaned=1), "S1": AimedProgress(offered=1, tried=1)}
    assert _lineage_aimed(by_state, NS(lineage=("S0", "S1"), id="S1")).exhausted is False


def test_a_lineage_with_no_record_is_not_exhausted() -> None:
    assert _lineage_aimed({}, NS(lineage=("S0",), id="S0")).exhausted is False


def test_the_constant_the_definition_rests_on_is_the_measured_one() -> None:
    assert AIMED_MEASURED == 0
