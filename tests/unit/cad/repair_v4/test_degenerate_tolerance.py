# Responsibility: lane C23 - the degenerate-Boolean guard (`body_kernel._degenerate`, C15) compares a union's bounds
#                 within the Boolean's own volume rounding, so an exact fragment is not refused as "not a union".
# Boundaries: hermetic; `_volume` is replaced by reading the numbers the test hands in.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4 import body_kernel


@pytest.fixture(autouse=True)
def numeric_volumes(monkeypatch):
    monkeypatch.setattr(body_kernel, "_volume", lambda v: float(v))


def test_an_exact_fragment_a_rounding_over_the_sum_is_a_union() -> None:
    """f9501251fb30: 7 bodies summing 927,163.93 mm3 fragmented to 927,164.04 mm3 (+1.2e-7); the 0.084 mm3 floor of a
    437 mm part refused it."""
    targets = [666834.342620988, 260329.583274919]
    assert body_kernel._degenerate(targets, [927164.0366919412], body_kernel.zero_volume_mm3(437.1)) is None


def test_the_broken_fuse_of_00510567_is_still_refused() -> None:
    targets = [6000.0] * 7 + [5870.0]  # 47,870 mm3 in all
    bad = body_kernel._degenerate(targets, [0.14, 19.6, 0.02, 13.5], body_kernel.zero_volume_mm3(40.0))
    assert bad is not None and "not a union" in bad["why"]
    assert body_kernel._degenerate(targets, [0.14, 19.6, 0.02, -13.5], 1e-6)["why"].startswith("a result solid")


def test_a_real_loss_beyond_rounding_is_refused() -> None:
    targets = [1000.0, 500.0]
    assert body_kernel._degenerate(targets, [999.9], 1e-9) is not None  # under the largest target by 1e-4


# ---- a FUZZY Boolean may move each surface by up to fuzzy_mm: its bound widens by fuzzy x the targets' area ----------

@pytest.fixture
def numeric_areas(monkeypatch):
    # each target here is a 10 mm cube: 600 mm2 of surface
    monkeypatch.setattr(body_kernel, "_area", lambda v: 600.0)


def test_a_fuzzy_fragment_that_closes_a_rounding_gap_is_a_union(numeric_areas) -> None:
    """Two 10 mm cubes 1e-4 mm apart, fragmented at fuzzy 1e-3: the gap's 0.01 mm3 joins the result. Exactly, that is
    5x over the 2e-3 mm3 rounding bound; the fuzzy allowance (1e-3 x 1200 mm2 = 1.2 mm3) covers it."""
    targets, result = [1000.0, 1000.0], [1000.0, 1000.01]
    eps = body_kernel.zero_volume_mm3(24.5)
    assert body_kernel._degenerate(targets, result, eps) is not None, "the exact bound must still refuse it"
    assert body_kernel._degenerate(targets, result, eps, fuzzy_mm=1e-3) is None


def test_a_fuzzy_result_outside_the_widened_bound_is_still_refused(numeric_areas) -> None:
    # 1.2 mm3 of allowance; 2 mm3 over the sum is not what a 1e-3 mm glue can do
    assert body_kernel._degenerate([1000.0, 1000.0], [2002.0], 1e-9, fuzzy_mm=1e-3) is not None
    assert body_kernel._degenerate([1000.0, 1000.0], [997.0], 1e-9, fuzzy_mm=1e-3) is not None


def test_the_broken_fuse_of_00510567_is_still_refused_at_fuzzy(numeric_areas) -> None:
    targets = [6000.0] * 7 + [5870.0]
    bad = body_kernel._degenerate(targets, [0.14, 19.6, 0.02, 13.5], body_kernel.zero_volume_mm3(40.0), fuzzy_mm=0.01)
    assert bad is not None and "not a union" in bad["why"]


def test_zero_fuzzy_adds_nothing_and_never_measures_area(monkeypatch) -> None:
    monkeypatch.setattr(body_kernel, "_area", lambda v: pytest.fail("an exact Boolean measured area"))
    assert body_kernel._degenerate([1000.0, 500.0], [999.9], 1e-9, fuzzy_mm=0.0) is not None
