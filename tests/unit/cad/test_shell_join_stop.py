# Responsibility: Verify SHELL-63, THE GENERAL JOIN STOPS ON ITS OWN SIGNS, NEVER ON THE CLOCK: a fuse that already
# widened a tolerance past SHELL-12's line stops before its re-sew, said as SHELL-12 says it; once one way of the
# general join was turned down for that, or could not be made, no other way is tried, said.
# Boundaries: parts built in the test (tests/shell_fixtures.py); the real extraction in process; the forked join
# replaced where a test needs a join to fail.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402


def test_a_fuse_past_the_tolerance_line_stops_before_its_resew(monkeypatch):
    """With a tolerance line no fuse can stay under, the join raises JoinTolerance right after its fuse; the
    extraction says it as SHELL-12 does and keeps the straight join."""
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    monkeypatch.setattr(ms, "JOIN_EARLY_TOL", True)
    monkeypatch.setattr(ms, "JOIN_TOL_WALLS", 1e-12)
    sewn: list[int] = []
    real_sew = ms.sew

    def counted(*a, **k):
        sewn.append(1)
        return real_sew(*a, **k)

    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == "straight"
    assert any("a general join left a tolerance of" in n and "(SHELL-12)" in n for n in rec["notes"])
    # in process (no fork), the fuse's pieces are never sewn again once past the line
    monkeypatch.setattr(ms, "JOIN_DEADLINE_S", 0.0)
    mine, body_faces, solid, kw = _join_inputs()
    monkeypatch.setattr(ms, "sew", counted)
    with pytest.raises(ms.JoinTolerance):
        ms._join_general(mine, body_faces, solid, t_max=kw["t_max"], fuzzy=kw["fuzzy"], remake=kw["remake"],
                         patch_alone=kw["patch_alone"], tol_cap=1e-12)
    assert sewn == []


def _join_inputs():
    """A tube on a plate's mid-faces, body faces and solid, as _extract hands them to the general join."""
    captured: dict = {}
    real = ms._join_general_bounded

    def grab(mine, body_faces, solid, **kw):
        captured.update(mine=mine, body_faces=body_faces, solid=solid, kw=kw)
        return real(mine, body_faces, solid, **kw)

    ms._join_general_bounded = grab
    try:
        ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    finally:
        ms._join_general_bounded = real
    return captured["mine"], captured["body_faces"], captured["solid"], captured["kw"]


def test_after_a_way_is_turned_down_no_other_way_is_tried(monkeypatch):
    calls: list[int] = []

    def wide(*_a, **_k):
        calls.append(1)
        raise ValueError("JoinTolerance: 5.0")

    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)   # the general join is the subject here
    monkeypatch.setattr(ms, "JOIN_STOP_AFTER", True)
    monkeypatch.setattr(ms, "_join_general_bounded", wide)
    # every wall read as free-form: two ways of the general join would be tried (each patch alone, and with the
    # mid-face beside it)
    monkeypatch.setattr(ms, "FREE_FORM", (object,))
    rec, _ = ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert len(calls) == 1
    assert any("left a tolerance of 5 mm" in n for n in rec["notes"])
    assert any("was not tried again with the same walls" in n and "(SHELL-63)" in n for n in rec["notes"])
    calls.clear()
    monkeypatch.setattr(ms, "JOIN_STOP_AFTER", False)
    ms.extract(sf.tube_on_plate(), mm_per_unit=1.0)
    assert len(calls) >= 2                               # as before: every way tried


def test_the_tolerance_is_read_back_across_the_pipe():
    assert ms._join_tolerance(ValueError("JoinTolerance: 0.25")) == 0.25
    assert ms._join_tolerance(ms.JoinTolerance("0.5")) == 0.5
    assert ms._join_tolerance(ValueError("the grown mid-faces could not be fused")) is None
