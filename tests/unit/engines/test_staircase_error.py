"""Staircase evidence, and the line between evidence and a sizing rule.

The first version of this module was a second sizing policy: it had its own tolerance and its own
cells-across answer, which disagreed with PASSAGE_CELLS_ACROSS. Two rules for one decision is two
readers of one fact that can disagree. These tests hold it to being evidence only.
"""
import pytest

from meshpipeline.engines import staircase_error as S
from meshpipeline.engines.passage import PASSAGE_CELLS_ACROSS, PASSAGE_FLOOR_CELLS


def test_the_measured_points_reproduce_themselves():
    for n, err in S.MEASURED:
        assert S.volume_error_pct(n) == pytest.approx(err, rel=1e-6)


def test_the_error_falls_as_resolution_rises():
    errs = [S.volume_error_pct(n) for n in range(2, 13)]
    assert errs == sorted(errs, reverse=True)


def test_between_measured_points_it_interpolates():
    assert S.volume_error_pct(12) < S.volume_error_pct(8) < S.volume_error_pct(5)


def test_outside_the_range_it_clamps_and_says_so():
    # Clamping is conservative on the fine side: it reports the worst case still supported by
    # evidence rather than extrapolating a number nobody measured.
    assert S.volume_error_pct(50) == pytest.approx(S.MEASURED[-1][1])
    assert S.is_extrapolated(50) is True
    assert S.is_extrapolated(1) is True
    assert S.is_extrapolated(8) is False
    assert S.annotate(50)["clamped_to_measured_range"] is True
    assert "true error is smaller" in S.annotate(50)["note"]


def test_nonsense_is_refused_not_silently_handled():
    for bad in (0, -3, float("nan"), float("inf")):
        with pytest.raises(S.StaircaseEvidenceError):
            S.volume_error_pct(bad)


def test_it_corroborates_the_floor_the_gate_already_enforces():
    # The useful result: the gate's floor of 12 was chosen from practice, and 12 across measured
    # 0.12% on our own meshes. The number was right; now it carries a measurement too.
    c = S.corroborates_floor(PASSAGE_FLOOR_CELLS)
    assert c["inside_measured_range"] is True
    assert c["expected_volume_error_pct"] < 0.5
    # and the product target sits at or above that floor, so it inherits the support
    assert PASSAGE_CELLS_ACROSS >= PASSAGE_FLOOR_CELLS


def test_this_module_does_not_size_anything():
    # The guard against what the first version of this file did. If a sizing entry point ever
    # appears here, two rules exist for one decision and they will disagree.
    import inspect
    src = inspect.getsource(S)
    for banned in ("def wall_cell", "def cells_across_for", "DEFAULT_TOLERANCE"):
        assert banned not in src, (
            f"{banned!r} makes this a sizing rule; sizing belongs to engines/gap_sizing alone")
    assert not any(n.startswith("wall_cell") or n.startswith("cells_across_for")
                   for n in S.__all__)


def test_annotate_is_phrased_as_evidence_not_a_verdict():
    a = S.annotate(PASSAGE_FLOOR_CELLS)
    assert "basis" in a and "measured" in a["basis"]
    # No pass/fail language: the verdict belongs to the resolution_floor gate.
    assert not any(k in a for k in ("passed", "ok", "verdict", "acceptable"))
