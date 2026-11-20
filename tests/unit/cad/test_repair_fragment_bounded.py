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
    """Stand in for the dispatcher. `answers` is what the child returns, or None for a child that
    was killed, failed, or never started."""
    seen: dict = {}

    def _run(fn, args, budget_s):
        seen.update(fn=fn, args=args, budget_s=budget_s)
        return answers

    monkeypatch.setattr(operations.bounded_child, "run", _run)
    return seen


def test_the_candidates_are_measured_in_one_bounded_child(monkeypatch, geometry):
    """Every box-meeting candidate against the failing face, in ONE child: the loop used to make
    one uninterruptible call per candidate in the parent, with nothing able to stop it.

    THE SOLIDS THEMSELVES CROSS, NOT THEIR INDICES. The dispatcher writes the operands as a
    compound and reads them back in order, so the child measures the bodies the parent meant
    positionally - no agreement between two `_indexed` walks of a round-tripped shape to assume.
    """
    owner, others = geometry
    seen = _answers(monkeypatch, [5.0, 0.0, 2.0])

    partner = operations._meeting_partner("shape", owner, "face", budget_s=9.0)

    assert seen["fn"] is operations._distances_to
    assert seen["args"] == ("face", *others), "the face, then each candidate, in that order"
    assert seen["budget_s"] == 9.0
    assert partner is others[1], "the nearest candidate to the failing face wins, as before"


def test_an_unfinished_measurement_is_not_a_partner(monkeypatch, geometry):
    """THE ROW 4-6 CONTRACT. The dispatcher reports None for a child it killed, and the operation
    is about to rebuild whichever body it picks: the best of a partial measurement is not an
    answer, so the search says it did not finish and `apply` declines."""
    owner, _others = geometry
    _answers(monkeypatch, None)

    assert operations._meeting_partner("shape", owner, "face",
                                       budget_s=9.0) is operations.PARTNER_UNMEASURED


def test_a_candidate_the_kernel_refuses_is_merely_not_nearest(monkeypatch, geometry):
    """Unchanged from the loop: a pair the kernel could not measure scored `inf` and lost. That is
    a judgement about geometry, not an unfinished search, so the child reports it per candidate
    and the parent still picks a partner."""
    owner, others = geometry
    _answers(monkeypatch, [None, 4.0, 7.0])

    assert operations._meeting_partner("shape", owner, "face", budget_s=9.0) is others[1]


def test_an_answer_that_does_not_fit_the_candidates_is_unmeasured(monkeypatch, geometry):
    """A short or long answer list means the child and the parent disagree about what was asked,
    which is not something to pick a body from."""
    owner, _others = geometry
    _answers(monkeypatch, [1.0, 2.0])

    assert operations._meeting_partner("shape", owner, "face",
                                       budget_s=9.0) is operations.PARTNER_UNMEASURED


def test_no_candidate_at_all_is_still_no_partner(monkeypatch, geometry):
    """An empty candidate list must not read as unmeasured: there is nothing to measure, and
    `no_partner` is the honest decline. No child is started for it either."""
    owner, _others = geometry
    def _apart(shape):
        box = _Box({"a", "b", "c"})
        box.owner = getattr(shape, "name", "face")
        return box

    monkeypatch.setattr(operations, "_bnd", _apart)

    def _never(*_a, **_k):
        raise AssertionError("no candidate means no child")

    monkeypatch.setattr(operations.bounded_child, "run", _never)

    assert operations._meeting_partner("shape", owner, "face", budget_s=9.0) is None


def test_every_distance_is_measured_in_the_child_never_in_the_parent(monkeypatch, geometry):
    """The point of the row, as an assertion: the parent constructs no DistShapeShape at all."""
    import OCP.BRepExtrema
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    made = []

    class Counting(BRepExtrema_DistShapeShape):
        def __init__(self, *args):
            made.append(1)
            super().__init__(*args)

    monkeypatch.setattr(OCP.BRepExtrema, "BRepExtrema_DistShapeShape", Counting)
    owner, others = geometry
    _answers(monkeypatch, [1.0, 2.0, 3.0])

    operations._meeting_partner("shape", owner, "face", budget_s=9.0)

    assert made == [], "the kernel call belongs to the child"


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


# --- row 6: the fuse itself runs in the child, and its history is re-expressed ------------------


def _faces():
    """Two REAL faces. The history is a real `ShapeBuild_ReShape`, which records TopoDS_Shapes
    and nothing else, so a double cannot stand in for one here."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    box = _indexed(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), TopAbs_FACE)
    return box.FindKey(1), box.FindKey(2)


def _fate(monkeypatch, answer):
    seen: dict = {}

    def _run(fn, args, budget_s):
        seen.update(fn=fn, args=args, budget_s=budget_s)
        return answer

    monkeypatch.setattr(operations.bounded_child, "run", _run)
    return seen


def test_the_fuse_runs_in_the_child_and_its_history_is_rebuilt_here(monkeypatch):
    """ROW 6. `BOPAlgo_Builder` is uninterruptible and ran in the parent. The blocker was never
    the Boolean: it was the ANSWER. `_general_fuse` returns a `ShapeBuild_ReShape`, which no wire
    can carry - so the child answers with the facts that history holds (recorded, gone, or the
    image shape) and the parent builds an equivalent history from them. `mapping_from_context`
    then reads it exactly as it reads any operation's own context, unchanged."""
    face, image = _faces()
    seen = _fate(monkeypatch, {"a": ["owner_piece"], "b": ["partner_piece"],
                               "fate": [{"state": "image", "image": image}]})

    pieces, history = operations._fuse_in_child("owner", "partner", (face,), budget_s=9.0)

    assert seen["fn"] is operations._fuse_kernel
    assert seen["args"] == ("owner", "partner", face)
    assert pieces == (("owner_piece",), ("partner_piece",))
    assert history.IsRecorded(face)
    assert history.Value(face).IsSame(image), "the child's image is what the history now names"


def test_a_face_the_fuse_deleted_reads_as_gone(monkeypatch):
    """`mapping_from_context` reads a recorded-but-null value as the kernel saying the entity is
    gone, so that is what a deleted face has to come back as."""
    face, _other = _faces()
    _fate(monkeypatch, {"a": [], "b": [], "fate": [{"state": "gone"}]})

    _pieces, history = operations._fuse_in_child("owner", "partner", (face,), budget_s=9.0)

    assert history.IsRecorded(face)
    assert history.Value(face).IsNull(), "gone, not merely unrecorded"


def test_a_face_the_fuse_never_touched_is_not_recorded(monkeypatch):
    """Untouched is a third answer, and it is not "gone": `fate_of` calls it unknown and the
    geometric comparison decides instead."""
    face, _other = _faces()
    _fate(monkeypatch, {"a": [], "b": [], "fate": [{"state": "absent"}]})

    _pieces, history = operations._fuse_in_child("owner", "partner", (face,), budget_s=9.0)

    assert not history.IsRecorded(face)


def test_a_fuse_the_child_could_not_finish_is_no_answer(monkeypatch):
    """The row 4-6 contract again: the operation DECLINES rather than splice a half-built fuse."""
    _fate(monkeypatch, None)

    assert operations._fuse_in_child("owner", "partner", (_faces()[0],), budget_s=9.0) is None


def test_an_answer_that_does_not_cover_the_faces_is_no_answer(monkeypatch):
    """A short or long fate list means the child and the parent disagree about what was asked."""
    _fate(monkeypatch, {"a": [], "b": [], "fate": []})

    assert operations._fuse_in_child("owner", "partner", (_faces()[0],), budget_s=9.0) is None
