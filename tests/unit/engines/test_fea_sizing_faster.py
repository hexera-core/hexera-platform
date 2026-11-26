"""TIME-12: the FEA stage sizing counts less and builds nothing it will not offer - with the same answer.

- a recount with more cubes goes on from where the last count of the same field stopped (same histogram to the bit);
- a pass that only asks whether the plan fits stops counting once it cannot (a plan that fits: same histogram);
- the quicker mesh is not planned when it would not be offered (the size choice: the same words);
- the joins' face pairs are measured only where the two faces can come within the gap limit (same joints).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.engines import fea_driver as FD
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines import fea_plan_options as PO
from meshpipeline.engines import fea_size_field as SF


def _cube_surface(cubes):
    """A labelled triangle surface (mm) of axis-aligned cubes, outward facing, faces Part_k_Face_j."""
    quads = ((0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5))
    xyz, tris, label, names = [], [], [], []
    for k, (x, y, z, d) in enumerate(cubes, start=1):
        base = len(xyz)
        xyz += [(x + d * i, y + d * j, z + d * m) for i in (0, 1) for j in (0, 1) for m in (0, 1)]
        for j, (q0, q1, q2, q3) in enumerate(quads, start=1):
            names.append(f"Part_{k}_Face_{j}")
            tris += [(base + q0, base + q2, base + q1), (base + q0, base + q3, base + q2)]
            label += [len(names) - 1] * 2
    return SimpleNamespace(xyz=np.asarray(xyz, float), tris=np.asarray(tris), label=np.asarray(label), names=names)


def _field():
    surf = _cube_surface([(0, 0, 0, 100)])
    zones = [{"id": "W1", "kind": "wall", "faces": ["Part_1_Face_1"], "size_m": 0.5e-3, "reach_m": 1e-3,
              "thickness_m": 5e-3},
             {"id": "F1", "kind": "fillet", "faces": ["Part_1_Face_3"], "size_m": 1.0e-3, "reach_m": 2e-3}]
    return surf, zones


def _same(a: SF.Histogram, b: SF.Histogram) -> None:
    assert a.rows == b.rows and a.surface == b.surface
    assert (a.complete, a.cubes, a.stopped) == (b.complete, b.cubes, b.stopped)


def test_a_recount_with_more_cubes_goes_on_from_where_it_stopped():
    surf, zones = _field()
    kw = {"coarsest": 20e-3, "per_h3": P.PER_H3, "stop_elements": float("inf"), "workers": 1}
    field = SF.SizeField(surf, zones, growth=1.3)
    first = field.histogram(max_cubes=20_000, **kw)
    assert first.stopped == "cubes" and field._held
    calls = []
    signed = field.signed
    field.signed = lambda p: calls.append(len(p)) or signed(p)      # type: ignore[method-assign]
    again = field.histogram(max_cubes=400_000, **kw)
    fresh = SF.SizeField(surf, zones, growth=1.3).histogram(max_cubes=400_000, **kw)
    _same(again, fresh)
    # only the levels past the first count's stop were measured again
    assert sum(calls) == fresh.cubes - first.cubes > 0


def test_a_count_made_is_read_again_without_measuring():
    """The same count, a finished count asked with a higher stop, a stopped one asked with its own budget: the same
    histogram, nothing measured."""
    surf, zones = _field()
    zones = [{**z, "size_m": 4 * z["size_m"]} for z in zones]
    kw = {"coarsest": 20e-3, "per_h3": P.PER_H3, "workers": 1}
    field = SF.SizeField(surf, zones, growth=1.3)
    whole =field.histogram(stop_elements=float("inf"), max_cubes=400_000, **kw)
    short = field.histogram(stop_elements=float("inf"), max_cubes=20_000, **kw)
    assert whole.complete and short.stopped == "cubes"
    field.signed = lambda p: (_ for _ in ()).throw(AssertionError("measured again"))  # type: ignore[method-assign]
    _same(field.histogram(stop_elements=float("inf"), max_cubes=400_000, **kw), whole)
    _same(field.histogram(stop_elements=1e12, max_cubes=800_000, **kw), whole)
    _same(field.histogram(stop_elements=1e12, max_cubes=20_000, **kw), short)


def test_a_count_asked_with_other_numbers_starts_again():
    surf, zones = _field()
    kw = {"per_h3": P.PER_H3, "stop_elements": float("inf"), "workers": 1}
    field = SF.SizeField(surf, zones, growth=1.3)
    field.histogram(coarsest=20e-3, max_cubes=20_000, **kw)
    other = field.histogram(coarsest=10e-3, max_cubes=400_000, **kw)
    _same(other, SF.SizeField(surf, zones, growth=1.3).histogram(coarsest=10e-3, max_cubes=400_000, **kw))


@pytest.mark.parametrize("limit", [5_000_000, 2_000])
def test_a_fits_only_count_gives_the_same_answer(limit):
    """Under the limit the histogram is the same to the bit; over it the plan does not fit either way."""
    surf, zones = _field()
    diag = float(np.linalg.norm([0.1, 0.1, 0.1]))
    kw = {"limit": limit, "recount": False, "base_m": 20e-3}
    full = FD._histogram(surf, zones, [], diag, **kw)
    quick = FD._histogram(surf, zones, [], diag, fits_only=True, **kw)

    def fits(h):
        # engines/fea_plan.finish's own test
        est = SF.elements(h, 20e-3, P.PER_H3)
        return (h.complete or h.stopped == "cubes") and est <= P.LIMIT_AIM * float(limit)

    assert fits(full) == fits(quick)
    if fits(full):
        _same(full, quick)
    else:
        assert quick.cubes <= full.cubes


def _full_plan(elements: int) -> dict:
    return {"estimate": {"elements": elements}, "zones": [], "coarsened": {}}


def test_the_quicker_mesh_is_not_planned_when_it_would_not_be_offered(monkeypatch):
    """Over the standard machine, with the quicker mesh unable to fit it even at the minimums: one choice, the same
    words as when the quicker mesh was planned to its minimums - and its passes never run."""
    record: dict = {"faces": {}, "hot_spots": []}
    full = _full_plan(40_000_000)
    planned: list = []

    def choose(record, surface, diag_m, *, ai=None, cap=None, **_k):
        planned.append(cap)
        return full if cap is None else {**_full_plan(9_000_000), "coarsened": {"at_minimums": True}}

    monkeypatch.setattr(FD, "choose_plan", choose)
    monkeypatch.setattr(FD, "could_fit", lambda *a, **k: False)
    out, plans = PO.options_and_plans(record, None, 1.0)
    assert planned == [None]                                   # the quicker mesh was never planned
    assert plans[PO.QUICKER] is None
    # today's answer, the quicker mesh planned to its minimums
    today, _q = PO.decide(record, full, {**_full_plan(9_000_000), "coarsened": {"at_minimums": True}})
    assert out == today and out["choice"] is False


def test_the_quicker_mesh_is_planned_when_it_may_be_offered(monkeypatch):
    record: dict = {"faces": {}, "hot_spots": []}
    planned: list = []

    def choose(record, surface, diag_m, *, ai=None, cap=None, **_k):
        planned.append(cap)
        return _full_plan(40_000_000) if cap is None else _full_plan(1_000_000)

    monkeypatch.setattr(FD, "choose_plan", choose)
    monkeypatch.setattr(FD, "could_fit", lambda *a, **k: True)
    out, plans = PO.options_and_plans(record, None, 1.0)
    assert planned == [None, PO.standard_elements()] and out["choice"] is True


def test_the_quicker_mesh_is_not_planned_when_the_full_one_fits_the_standard_machine(monkeypatch):
    record: dict = {"faces": {}, "hot_spots": []}
    planned: list = []
    checked: list = []

    def choose(record, surface, diag_m, *, ai=None, cap=None, **_k):
        planned.append(cap)
        return _full_plan(100_000)

    monkeypatch.setattr(FD, "choose_plan", choose)
    monkeypatch.setattr(FD, "could_fit", lambda *a, **k: checked.append(1) or True)
    out, _plans = PO.options_and_plans(record, None, 1.0)
    assert planned == [None] and not checked and out["choice"] is False


def _cells_before(tris: np.ndarray, size: float) -> list[np.ndarray]:
    """cad/fea_joins._cells as it was (np.unique over the rows)."""
    key = np.floor(tris.mean(axis=1) / max(size, 1e-300)).astype(np.int64)
    _u, inv = np.unique(key, axis=0, return_inverse=True)
    inv = np.asarray(inv).ravel()
    order = np.argsort(inv, kind="stable")
    return np.split(order, np.flatnonzero(np.diff(inv[order])) + 1)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_joins_cells_are_the_same_cells_in_the_same_order(seed):
    from meshpipeline.cad import fea_joins as J

    rng = np.random.default_rng(seed)
    tris = rng.normal(scale=20.0, size=(5000, 3, 3)) - 7.0          # negative keys too
    for size in (0.5, 3.0, 40.0):
        got, want = J._cells(tris, size), _cells_before(tris, size)
        assert len(got) == len(want) and all(np.array_equal(a, b) and a.dtype == b.dtype for a, b in zip(got, want))


def test_face_pairs_whose_boxes_are_apart_are_not_measured():
    from meshpipeline.cad import fea_joins as J

    box = (np.zeros(3), np.ones(3))
    shifted = [(np.zeros(3) + [1.0 + d, 0, 0], np.ones(3) + [1.0 + d, 0, 0]) for d in (0.0, 0.4, 0.6)]
    approx = {(0, 0): 0.0, (0, 1): 0.4, (0, 2): 0.6}
    # a limit of 0.5: the box 0.4 away may hold a gap within it, the one 0.6 away cannot
    assert J._far_apart(approx, [box], shifted, 0.5, 1e-6, 1.0) == {(0, 2)}
    assert J._far_apart(approx, [box], shifted, 0.6, 1e-6, 1.0) == set()
