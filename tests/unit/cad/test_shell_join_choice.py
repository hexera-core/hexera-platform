# Responsibility: Verify the join choice JOIN_CLOSE_FIRST (SHELL-61's option 2): the straight join's closing passes
# first, kept without a general join when they leave it clean (nothing unjoined, no edge under SLIVER_EDGE_WALLS of the
# thinnest wall, a shell body's metal within VOLUME_BAND); otherwise the general join as before, the fallback.
# Boundaries: parts built in the test (tests/shell_fixtures.py); the real extraction in process. The flag is set in
# each test, so the tests hold whatever its default.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402


def _short_edges(surface, t: float) -> int:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    em = ms._map(surface, TopAbs_EDGE)
    return sum(1 for i in range(1, em.Extent() + 1)
               if not BRep_Tool.Degenerated_s(TopoDS.Edge_s(em.FindKey(i)))
               and ms._length(TopoDS.Edge_s(em.FindKey(i))) < ms.SLIVER_EDGE_WALLS * t)


def _bytes(shape, path) -> bytes:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path.read_bytes()


def test_a_straight_join_its_passes_close_cleanly_is_kept_without_a_general_join(monkeypatch):
    """A gusseted angle (4 / 3 / 3 mm): its straight join's closing passes leave nothing open and no short edge, so no
    general join is made at all; before, the general join was kept (two more faces, 1.3% more metal than the part)."""
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", True)
    monkeypatch.setattr(ms, "CLEAN_ON_OWN", False)   # its closing strip lies in the gusset's plane (next tests)
    calls: list[int] = []
    real = ms._join_general_bounded

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(ms, "_join_general_bounded", counted)
    rec, surface = ms.extract(sf.gusseted_angle(), mm_per_unit=1.0)
    (row,) = rec["sewn"]
    assert row["join"] == "straight" and row["open_edges"] == 0 and calls == []
    assert _short_edges(surface, 3.0) == 0
    assert rec["bodies"][0]["mid_volume_ratio"] == pytest.approx(1.0, abs=0.01)
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)
    before, _ = ms.extract(sf.gusseted_angle(), mm_per_unit=1.0)
    assert before["sewn"][0]["join"] == "general"


def test_a_straight_join_its_passes_close_badly_falls_back_to_the_general_join(monkeypatch, tmp_path):
    """A spool (two flanges on a tube): its straight join's passes close it with edges a hundredth of a millimetre long
    and 19% more metal than the part, so the general join is made and kept, as before - the same mid-surface."""
    shape = sf.spool()
    before, s_before = ms.extract(shape, mm_per_unit=1.0)
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", True)
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    assert before["sewn"][0]["join"] == rec["sewn"][0]["join"] == "general"
    assert {k: v for k, v in rec.items() if k != "seconds"} == {k: v for k, v in before.items() if k != "seconds"}
    # the very same mid-surface, byte for byte: the trial passes ran in a child and touched nothing here
    assert _bytes(surface, tmp_path / "a.brep") == _bytes(s_before, tmp_path / "b.brep")


def test_a_part_whose_straight_join_meets_everywhere_is_made_as_before(monkeypatch):
    shape = sf.t_joint()
    before, _ = ms.extract(shape, mm_per_unit=1.0)
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", True)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    assert {k: v for k, v in rec.items() if k != "seconds"} == {k: v for k, v in before.items() if k != "seconds"}


def _channel():
    """The structured mesher's channel (tests/unit/engines/test_shell_structured.py, SHMESH-47c): a 10 mm web 363 mm
    wide, two 100 mm flanges flush with its underside, 900 mm long, with bolt holes."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    L, W, T_, F = 900.0, 363.0, 10.0, 100.0

    def box(x, y, z, dx, dy, dz):
        return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()

    shape = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(box(0, 0, 0, L, W, T_), box(0, -T_, 0, L, T_, F)).Shape(),
                             box(0, W, 0, L, T_, F)).Shape()
    for x, y in [(120.0 + 150 * i, 61.5) for i in range(5)] + [(208.0 + 150 * i, W - 61.5) for i in range(4)]:
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, -1), gp_Dir(0, 0, 1)), 8.7,
                                                                T_ + 2).Shape()).Shape()
    for x, y0, d, z in [(200.0 + 320 * i, -T_ - 1, 17.4, F - 41.7) for i in range(3)] +                        [(300.0 + 450 * i, W - 1, 12.2, F - 42.1) for i in range(2)]:
        shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y0, z), gp_Dir(0, 1, 0)), d / 2,
                                                                T_ + 2).Shape()).Shape()
    return shape


def test_a_closing_strip_in_another_walls_plane_is_not_clean(monkeypatch):
    """A channel's straight join closes with a strip the web owns lying in a flange's plane (SHELL-13's bridge): the
    grid rule could not cut across that joint (3 faces lost their grids). CLEAN_ON_OWN: not clean, so the general
    join is made, as today (each wall its own strip to the joint line); without it, the closed straight join."""
    shape = _channel()
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", True)
    monkeypatch.setattr(ms, "CLEAN_ON_OWN", True)
    rec, _ = ms.extract(shape, mm_per_unit=1.0)
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", False)
    today, _ = ms.extract(shape, mm_per_unit=1.0)
    assert rec["sewn"][0]["join"] == today["sewn"][0]["join"] == "general"
    assert rec["surface_faces"] == today["surface_faces"]
    monkeypatch.setattr(ms, "JOIN_CLOSE_FIRST", True)
    monkeypatch.setattr(ms, "CLEAN_ON_OWN", False)
    loose, _ = ms.extract(shape, mm_per_unit=1.0)
    assert loose["sewn"][0]["join"] == "straight"
