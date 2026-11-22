# Responsibility: Verify INS-27 - the sealed-space reading is bounded by its work, never by a clock: the same reading
# on a machine many times slower; a surface over the sample budget read at a coarser voxel instead of not at all; the
# spaces tested capped, largest first.
# Boundaries: the measurement on synthetic shells (tests/unit/engines/test_sealed_cavities.py's builders).
from __future__ import annotations

from tests.unit.engines.test_sealed_cavities import _hollow_shell

from meshpipeline.engines import sealed_cavities as sc


class _SlowClock:
    """time.monotonic on a machine where every step takes 100 s."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 100.0
        return self.t


def test_the_reading_does_not_depend_on_the_machines_speed(monkeypatch):
    """The 3962 case: a reading that is 1 s of work on a quiet machine ran past its 120 s clock on a loaded one and
    the attempt was thrown away. Old code: None (and a guard trip noted); now the same reading as on a fast one."""
    tris = _hollow_shell(gap=0.006)
    fast = sc.read_cavities(tris, cell_m=0.004)
    trips: list = []
    monkeypatch.setattr("meshpipeline.engines.determinism.note_guard_trip", lambda *a, **k: trips.append((a, k)))
    monkeypatch.setattr(sc.time, "monotonic", _SlowClock())
    monkeypatch.setattr("meshpipeline.engines.passage_flow.time.monotonic", _SlowClock(), raising=False)
    slow = sc.read_cavities(tris, cell_m=0.004)
    assert fast is not None and slow is not None and not trips
    assert [c.point for c in slow.cavities] == [c.point for c in fast.cavities]
    assert (slow.wet_sides == fast.wet_sides).all()


def test_a_surface_over_the_sample_budget_is_read_at_a_coarser_voxel(monkeypatch):
    """Old code returned None ("too large to read") and the case was authored without the reading."""
    tris = _hollow_shell(gap=0.006)
    full = sc.read_cavities(tris, cell_m=0.004)
    monkeypatch.setattr(sc, "MAX_SAMPLES", 200_000)
    coarse = sc.read_cavities(tris, cell_m=0.004)
    assert full is not None and coarse is not None
    assert coarse.voxel_m > full.voxel_m
    assert len(coarse.cavities) == len(full.cavities) == 1


def test_the_spaces_tested_are_capped_largest_first(monkeypatch):
    tris = _hollow_shell(gap=0.006)
    monkeypatch.setattr(sc, "MAX_REGION_TESTS", 0)
    capped = sc.read_cavities(tris, cell_m=0.004)
    assert capped is not None and capped.cavities == ()
