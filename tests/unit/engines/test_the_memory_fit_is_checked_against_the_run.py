"""The memory model that chooses the machine is checked against the run it chose it for.

WHY THIS TEST EXISTS. budget.PEAK_BYTES_PER_CELL is fitted by hand on three builds and it decides
which machine a mesh lands on - driver.drive prices the run with budget.peak_gib(estimate) and
hands that to contracts/mesh_machine.choose. The mesher measures its own peak on every single run
and writes it into the report as peak_rss_bytes. Until 2026-10-28 nothing compared the two.

The field's only appearance in the whole test suite was as a zero inside a fixture
(test_conjugate_row.py: '"seconds": {}, "peak_rss_bytes": 0,'), so the one test that carried it
carried it on the "cannot be attributed" branch, and no test asserted any value derived from it.

THE DIRECTION MATTERS. budget.py records an earlier fit that used 588 bytes a cell, taken from a
spot reading rather than the high-water mark, which under-priced by 27%. Under-pricing is the
direction that gets a run killed hours in rather than refused up front.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import criteria as C

#: The three builds budget.PEAK_BYTES_PER_CELL was fitted on, straight out of its own comment.
FITTED = [
    (1_852_814, 1_802_297_344),
    (17_447_384, 13_377_228_800),
    (99_172_391, 74_610_000_000),
]


def _ratio(cells, peak_bytes):
    return (peak_bytes / 1024.0 ** 3) / B.peak_gib(cells)


@pytest.mark.parametrize("cells, peak", FITTED)
def test_the_fit_reads_about_one_on_the_builds_it_was_fitted_on(cells, peak):
    """A model checked against its own training data must read 1.0, or the arithmetic is wrong."""
    assert _ratio(cells, peak) == pytest.approx(1.0, abs=0.02)


def test_a_peak_twice_the_prediction_reads_about_two():
    """The ratio has to be sensitive in the direction that matters, not merely present."""
    cells, peak = FITTED[-1]
    assert _ratio(cells, 2 * peak) == pytest.approx(2.0, rel=0.02)


def test_the_old_underpricing_fit_would_have_been_caught():
    """budget.py records a fit of 588 bytes a cell that under-priced by 27%. Had this check
    existed, the first run on it would have reported the miss instead of being killed."""
    cells = 99_172_391
    would_have_predicted = (B.PEAK_FIXED_BYTES + cells * 588.0) / 1024.0 ** 3
    real_peak_gib = 74_610_000_000 / 1024.0 ** 3
    assert real_peak_gib / would_have_predicted > 1.25, (
        "the declared advisory bar of 1.25 has to be low enough to catch the one real miss this "
        "project has recorded")


def test_the_criterion_is_declared_and_advisory():
    """Non-gating on purpose: peak_rss_bytes is legitimately 0 when the mark cannot be attributed
    to this build, and a GATING key absent from the quality block fails closed - which would turn
    an unmeasurable peak into a refused mesh."""
    rows = [c for c in C.CRITERIA_ROWS if c.key == "peak_over_predicted"]
    assert len(rows) == 1, "the memory fit has no declared criterion, so nothing reports it"
    row = rows[0]
    assert row.gating is False
    assert row.op == "<="
    assert row.threshold == pytest.approx(1.25)


def test_an_unattributable_peak_leaves_the_key_absent():
    """ABSENT, not 0.0. A zero ratio reads as a perfect fit to every reader that compares it
    against a bar - the same trap the engine already records for max_skew."""
    from meshpipeline.engines.cfmesh_conjugate import finalize as F

    src = (F.__file__).replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert 'peak = int(report.get("peak_rss_bytes") or 0)' in body
    assert "if peak > 0 and q.get(\"cells\")" in body, (
        "the ratio must be written only when both numbers were measured")
