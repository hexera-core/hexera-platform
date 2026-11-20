# Responsibility: Prove fragment_bodies' kernel calls cannot hang the worker, and that when one
#                 does not finish the OPERATION DECLINES rather than acting on a partial answer.
# Boundaries: No kernel. The bounded batch and the dispatcher are stubbed; what is tested is the
#             contract on expiry, which is the whole difference between rows 4-6 and rows 1-3.
"""Rows 4-6 of the OCC audit, and the reason they were not part of rows 1-3.

A measurement that runs out of budget reports "unmeasured" and the caller carries on. An
OPERATION that runs out of budget must DECLINE: it is about to rebuild two bodies on the strength
of the answer, and the best of a partial measurement is not an answer. `BRepExtrema` picking the
partner, `BRepAlgoAPI_Common` proving the bodies do not interpenetrate - neither may be guessed.
"""
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.operations as operations
from meshpipeline.cad.repair import occ_distance


class _Solid:
    def __init__(self, name: str) -> None:
        self.name = name

    def IsSame(self, other) -> bool:                               # noqa: N802 - OCC's name
        return getattr(other, "name", None) == self.name


class _Map:
    def __init__(self, solids: list) -> None:
        self._solids = solids

    def Extent(self) -> int:                                       # noqa: N802 - OCC's name
        return len(self._solids)

    def FindKey(self, index: int):                                 # noqa: N802 - OCC's name
        return self._solids[index - 1]


class _Box:
    """A bounding box double. `apart` names the solids this box does not meet."""

    def __init__(self, apart: set[str] | None = None) -> None:
        self.apart = apart or set()

    def IsOut(self, other) -> bool:                                # noqa: N802 - OCC's name
        return getattr(other, "owner", None) in self.apart


@pytest.fixture
def geometry(monkeypatch):
    """One owner and three candidate solids, all of whose boxes meet the owner's."""
    from meshpipeline.cad.repair import localize

    owner = _Solid("owner")
    others = [_Solid("a"), _Solid("b"), _Solid("c")]
    monkeypatch.setattr(localize, "_indexed", lambda shape, kind: _Map([owner, *others]))

    def _bnd(shape):
        box = _Box()
        box.owner = getattr(shape, "name", "face")
        return box

    monkeypatch.setattr(operations, "_bnd", _bnd)
    monkeypatch.setattr(operations, "_centre", lambda box: None)
    return owner, others


def _answers(monkeypatch, answers):
    seen: dict = {}

    def _distances(shapes, pairs, *, budget_s, bounds=None, tol=0.0):
        seen.update(shapes=shapes, pairs=pairs, budget_s=budget_s)
        return answers

    monkeypatch.setattr(occ_distance, "distances", _distances)
    return seen


def _ok(value):
    return {"min_distance_mm": value, "point_a": None, "point_b": None,
            "distance_status": "measured"}


def _spent():
    return {**occ_distance.UNMEASURED, "distance_status": "unmeasured: budget_spent"}


def test_the_candidates_are_measured_in_one_bounded_child(monkeypatch, geometry):
    """Every box-meeting candidate against the failing face, in one child: the loop used to make
    one uninterruptible call per candidate in the parent, with nothing able to stop it."""
    owner, others = geometry
    seen = _answers(monkeypatch, [_ok(5.0), _ok(0.0), _ok(2.0)])

    partner = operations._meeting_partner("shape", owner, "face", budget_s=9.0)

    assert seen["pairs"] == [(0, 1), (0, 2), (0, 3)], "the face against each candidate"
    assert seen["shapes"][0] == "face"
    assert seen["budget_s"] == 9.0
    assert partner is others[1], "the nearest candidate to the failing face wins, as before"


def test_an_unfinished_measurement_is_not_a_partner(monkeypatch, geometry):
    """THE ROW 4-6 CONTRACT. Two candidates measured, one killed: the nearest of the two is NOT
    the partner, because the unmeasured body may be nearer and the operation is about to rebuild
    whichever it picks. It says so, and `apply` declines."""
    owner, _others = geometry
    _answers(monkeypatch, [_ok(5.0), _spent(), _ok(2.0)])

    assert operations._meeting_partner("shape", owner, "face",
                                       budget_s=9.0) is operations.PARTNER_UNMEASURED


def test_a_candidate_the_kernel_refuses_is_merely_not_nearest(monkeypatch, geometry):
    """Unchanged from the loop: a pair the kernel cannot measure was scored `inf` and lost. That
    is a judgement about geometry, not an unfinished search."""
    owner, others = geometry
    _answers(monkeypatch, [{**occ_distance.UNMEASURED,
                            "distance_status": "unmeasured: the kernel gave no distance"},
                           _ok(4.0), _ok(7.0)])

    assert operations._meeting_partner("shape", owner, "face", budget_s=9.0) is others[1]


def test_no_candidate_at_all_is_still_no_partner(monkeypatch, geometry):
    """An empty candidate list must not read as unmeasured: there is nothing to measure, and
    `no_partner` is the honest decline."""
    owner, _others = geometry
    monkeypatch.setattr(operations, "_bnd", lambda shape: _Box({"a", "b", "c"}))
    _answers(monkeypatch, [])

    assert operations._meeting_partner("shape", owner, "face", budget_s=9.0) is None


def test_the_common_volume_crosses_to_the_dispatcher_not_the_parent(monkeypatch):
    """ROW 5. `_common_volume` is registered with the bounded dispatcher and the investigator
    already takes that route; `apply` called it straight. The call site is what decides whether a
    kernel is bounded, so this is the call site."""
    asked: dict = {}

    def _run(fn, args, budget_s):
        asked.update(fn=fn, args=args, budget_s=budget_s)
        return 12.5

    monkeypatch.setattr(operations.bounded_child, "run", _run)

    assert operations._bounded_common_volume("a", "b") == 12.5
    assert asked["fn"] is operations._common_volume
    assert asked["args"] == ("a", "b")
    assert asked["budget_s"] == operations.FRAGMENT_KERNEL_BUDGET_S


def test_a_dispatcher_that_gives_no_answer_is_unmeasured(monkeypatch):
    """`run` reports None for every failure and every expiry, and an unmeasured common volume is
    never read as "the bodies do not interpenetrate" - the census's rule, now also the clock's."""
    monkeypatch.setattr(operations.bounded_child, "run", lambda fn, args, budget_s: None)

    assert operations._bounded_common_volume("a", "b") is None
