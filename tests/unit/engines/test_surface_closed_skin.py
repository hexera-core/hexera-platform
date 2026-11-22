# Responsibility: Verify SURF-14 in the run: a file of surfaces that close round a space with no wall in it, which the
# user asked to mesh as a shell, is meshed as its skin (a surface part, at the user's thickness or none, told in one
# line); a closed skin whose solid has a real shell wall, open surfaces, and a run that did not ask for a shell go on
# exactly as before.
# Boundaries: the real FEA driver, extraction and surface run on small shapes, the solid mesher's next step faked; no
# solver run, no store.
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402
from tests.unit.engines.test_surface_run import _as_before, _AsBefore, _drive, _Run, _stage  # noqa: E402

from meshpipeline.cad import surface_model as sm  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import shell_run, surface_run  # noqa: E402


def test_a_record_with_every_body_solid_and_no_plate_is_a_closed_skin_with_no_wall():
    solid = {"bodies": [{"body": "Part_1", "verdict": "solid",
                         "why": "no wall of it is a plate: its most plate-like wall is 55.1 mm thick but only 210 mm wide"}]}
    assert surface_run.closed_skin_no_wall(solid)
    assert not surface_run.closed_skin_no_wall({"bodies": [{"verdict": "shell", "why": "every wall is a plate"}]})
    assert not surface_run.closed_skin_no_wall({"bodies": [solid["bodies"][0], {"verdict": "shell", "why": ""}]})
    assert not surface_run.closed_skin_no_wall({"bodies": []}) and not surface_run.closed_skin_no_wall(None)


def test_closed_sheets_are_a_surface_only_when_read_as_a_skin():
    sheets = [{"area_mm2": 100.0, "open_share": 0.0, "closed": True}]
    assert sm.classify(sheets)[0] == "closed"
    kind, why, _share = sm.classify(sheets, closed_skin=True)
    assert kind == "surface" and why == sm.CLOSED_SKIN_WHY


def _skin_drive(tmp_path, monkeypatch, shape, state):
    ws = _stage(shape, tmp_path / "ws")
    monkeypatch.setattr(FD, "open_single_skins", _as_before)
    return ws, *_drive(ws, monkeypatch, state)


def test_a_closed_hull_with_no_wall_is_meshed_as_its_skin_at_the_users_thickness(tmp_path, monkeypatch):
    # a 40 mm cube's six faces, no solid: the block they bound has no wall a shell could stand for
    state = {"purpose": "structural", "fea_mesh_as": "shell", "request_txt": "shell mesh of the hull",
             "user_notes": [{"id": "n1", "text": "aluminium, 2 mm thick"}]}
    ws, ok, _marker, calls, cited = _skin_drive(tmp_path, monkeypatch, sx.closed_skin(40.0, 40.0, 40.0), state)
    assert ok is True and calls == []
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and rec["walls_mm"] == [2.0]
    assert rec["surface"]["why"] == sm.CLOSED_SKIN_WHY
    told = [c for c in cited if c.get("rule") == "SURF-14"]
    assert len(told) == 1 and told[0]["words"] == (
        "The file's surfaces close round a space with no wall in it, so they were meshed as the part's skin, 2 mm thick.")


def test_a_closed_hull_with_no_thickness_is_still_meshed_as_its_skin(tmp_path, monkeypatch):
    state = {"purpose": "structural", "fea_mesh_as": "shell", "request_txt": "shell mesh of the hull"}
    ws, ok, _marker, calls, cited = _skin_drive(tmp_path, monkeypatch, sx.closed_skin(40.0, 40.0, 40.0), state)
    assert ok is True and calls == []
    rec = shell_run.read(ws)
    assert surface_run.is_surface(rec) and rec["surface"]["thickness_mm"] is None
    (told,) = [c for c in cited if c.get("rule") == "SURF-14"]
    assert told["words"].endswith("with no thickness given (sized as a thin sheet).")


def test_the_skin_of_a_solid_with_a_real_wall_goes_on_as_before(tmp_path, monkeypatch):
    # a 3 mm plate written as its six faces: the solid they bound IS a plate, its extracted shell stands
    state = {"purpose": "structural", "fea_mesh_as": "shell", "user_notes": [{"id": "n1", "text": "3 mm thick"}]}
    with pytest.raises(_AsBefore):
        _skin_drive(tmp_path, monkeypatch, sx.closed_skin(200.0, 100.0, 3.0), state)
    assert surface_run.read(tmp_path / "ws") == {}


def test_a_closed_hull_not_asked_to_shell_goes_on_as_before(tmp_path, monkeypatch):
    def never(_ws):
        raise AssertionError("the closed skin was read for a run that did not ask for a shell")

    monkeypatch.setattr(surface_run, "closed_skin_check", never)
    with pytest.raises(_AsBefore):
        _skin_drive(tmp_path, monkeypatch, sx.closed_skin(40.0, 40.0, 40.0), {"purpose": "structural"})


def test_open_surfaces_are_not_read_as_a_closed_skin(tmp_path, monkeypatch):
    ws = _stage(sx.bracket(), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 0 and seen == {"open": True}

    def never(_ws):
        raise AssertionError("open surfaces were read as a closed skin")

    monkeypatch.setattr(surface_run, "closed_skin_check", never)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    state = {"purpose": "structural", "fea_mesh_as": "shell", "user_notes": [{"id": "n1", "text": "1.2 mm thick"}]}
    ok, _m, _o = asyncio.run(FD.drive(ws, state, job_id="t", publish=None, run=_Run()))
    assert ok is True and surface_run.is_surface(shell_run.read(ws))
