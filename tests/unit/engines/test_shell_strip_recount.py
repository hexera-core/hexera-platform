"""SHMESH-101 (rounds 8-9): the finer strip counts are kept on a part only where no quality row is worse, on every
mesh the part can keep - SHMESH-36's #435 mesh too (round 9: it never passed the keep check)."""
from types import SimpleNamespace

import pytest

from meshpipeline.engines.gmsh import shell_adapter as SA


@pytest.fixture(autouse=True)
def _shmesh_101_on(monkeypatch):
    # (SHMESH-101 is off by default since round 13; these tests cover what it does when on)
    monkeypatch.setattr(SA, "STRIP_AIM", True)
    monkeypatch.setattr(SA, "STRIP_OPEN", True)


def _rec(beyond: int, strips: int = 0) -> dict:
    q: dict = {"structure": {"strip_aim": {"strips": strips, "added": 10}} if strips else {}}
    return {"quality": q, "requirements": {"rows": [{"id": "r1", "name": "worst aspect ratio", "met": False,
                                                     "beyond": beyond}]}}


def test_rows_worse() -> None:
    a = {"requirements": {"rows": [{"id": "r1", "name": "worst aspect ratio", "met": False, "beyond": 12},
                                   {"id": "r2", "name": "min angle", "met": False, "beyond": 3}]}}
    b = {"requirements": {"rows": [{"id": "r1", "name": "worst aspect ratio", "met": False, "beyond": 10},
                                   {"id": "r2", "name": "min angle", "met": True}]}}
    got = SA._rows_worse(a, b, 2)
    assert len(got) == 3 and "worst aspect ratio" in got[0] and "not met" in got[1] and "solver" in got[2]
    assert SA._rows_worse(b, b) == []


def test_recount_is_gone() -> None:
    # round 9: the re-count acted on one part of 173 and doubled its aspect misses; it is taken out, not left off
    assert not hasattr(SA, "STRIP_RECOUNT") and not hasattr(SA, "_recount_together")


def test_classic_mesh_with_worse_rows_keeps_the_plan(monkeypatch) -> None:
    monkeypatch.setitem(SA._RUN, "end", None)
    monkeypatch.setitem(SA._RUN, "cut", False)
    seen: list = []

    def plain() -> tuple:
        seen.append(SA.STRIP_AIM)
        return SimpleNamespace(notes=[]), _rec(1400), 0

    old = SimpleNamespace(notes=[])
    kept: dict = {}
    m, a, n = SA._aim_check_classic(plain, old, _rec(2868, strips=14), 0, kept)
    assert seen == [False] and SA.STRIP_AIM is True          # made again with the finer counts off, then put back
    assert m is not old and a["quality"]["structure"]["strip_aim_left"]
    assert kept["strip_aim_check"]["kept"] is True and "worst aspect ratio" in kept["strip_aim_check"]["worse"][0]
    assert any("left as planned" in t for t in m.notes)
    assert a["quality"]["structure"]["strip_aim_check"]["kept"] is True   # (in the mesh's own record too)


def test_classic_mesh_no_worse_keeps_its_counts(monkeypatch) -> None:
    monkeypatch.setitem(SA._RUN, "end", None)
    monkeypatch.setitem(SA._RUN, "cut", False)
    old = SimpleNamespace(notes=[])
    applied = _rec(10, strips=4)
    kept: dict = {}
    m, a, _n = SA._aim_check_classic(lambda: (SimpleNamespace(notes=[]), _rec(12), 0), old, applied, 0, kept)
    assert m is old and a is applied and kept["strip_aim_check"]["kept"] is False


def test_classic_mesh_untouched_when_counts_did_not_act() -> None:
    kept: dict = {}
    old = SimpleNamespace(notes=[])
    applied = _rec(10)

    def never() -> tuple:
        raise AssertionError("no mesh is made when the finer counts did not act")

    assert SA._aim_check_classic(never, old, applied, 0, kept) == (old, applied, 0) and kept == {}
