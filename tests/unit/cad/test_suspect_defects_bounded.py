# Responsibility: Verify MESH-1's time box on the suspected-defect checks (cad/suspect_defects): each check runs in a child process under its own limit, names the same faces as the check run here, and a check past its limit is said as not checked - it never holds the run.
# Boundaries: shapes are built here with the CAD kernel; the checks run in real child processes (cad/bounded_steps).
from __future__ import annotations

import time

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import suspect_defects as SD  # noqa: E402


def _self_crossing_solid():
    """ONE solid whose two outer shells pass through each other (a CAD defect: the body overlaps itself)."""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Solid

    b = BRep_Builder()
    solid = TopoDS_Solid()
    b.MakeSolid(solid)
    for box in (fx._box(0, 0, 0, 10, 10, 10), fx._box(5, 5, 5, 10, 10, 10)):
        ex = TopExp_Explorer(box, TopAbs_SHELL)
        b.Add(solid, TopoDS.Shell_s(ex.Current()))
    return solid


def _perforated(n: int = 20):
    """A 10 mm cube with an n x n grid of round holes along z: hundreds of faces for the Boolean check to read."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.TopTools import TopTools_ListOfShape

    tools = TopTools_ListOfShape()
    for i in range(n):
        for j in range(n):
            tools.Append(fx._cyl((i + 0.5) * 10 / n, (j + 0.5) * 10 / n, -1, 10 / n / 2.5, 12))
    args = TopTools_ListOfShape()
    args.Append(fx._box(0, 0, 0, 10, 10, 10))
    cut = BRepAlgoAPI_Cut()
    cut.SetArguments(args)
    cut.SetTools(tools)
    cut.Build()
    return cut.Shape()


def test_the_checks_in_their_child_name_the_same_faces_as_here():
    shape = fx._compound(_self_crossing_solid(), fx._box(30, 0, 0, 10, 10, 10))
    names = SD._face_names(shape, {})
    here = SD._find(shape, {}, names, validity=None, check_overlaps=True, checks=None)
    found, unchecked = SD.find_bounded(shape, {})
    assert unchecked == []
    assert found == here
    assert any(s["kind"] == "self_overlap" and s["body"] == 1 and s["confirmed"] for s in found)


def test_a_boolean_check_past_its_limit_is_said_and_never_holds_the_run(monkeypatch):
    shape = fx._compound(_perforated(), fx._box(30, 0, 0, 10, 10, 10))
    # the perforated body's check gets a limit far under its time; the plain cube a usual one
    monkeypatch.setattr(SD, "boolean_limit_s", lambda faces: 0.01 if faces > 100 else 60.0)
    monkeypatch.setattr(SD, "CROSSING_S", 0.01)               # and the crossing search too
    t0 = time.time()
    found, unchecked = SD.find_bounded(shape, {})
    assert time.time() - t0 < 60
    assert [u["check"] for u in unchecked] == ["crossings", "boolean"]
    assert [u.get("body") for u in unchecked if u["check"] == "boolean"] == [1]
    assert all("past its limit" in u["why"] for u in unchecked)
    said = SD.unchecked_words(unchecked)
    assert said == ("Not checked for broken CAD in the time set for it: the search for faces crossing each other "
                    "and the self-interference check of body 1. The mesh is tried as usual; only a failure there "
                    "would point at the CAD.")
    assert not any(s.get("body") == 1 for s in found)


def test_no_boolean_check_starts_past_the_time_for_them(monkeypatch):
    shape = fx._compound(*[fx._box(20 * k, 0, 0, 10, 10, 10) for k in range(4)])
    monkeypatch.setattr(SD, "BOOLEAN_BUDGET_S", 0.0)
    _found, unchecked = SD.find_bounded(shape, {})
    assert sorted(u["body"] for u in unchecked if u["check"] == "boolean") == [1, 2, 3, 4]
    assert all("not started" in u["why"] for u in unchecked if u["check"] == "boolean")
