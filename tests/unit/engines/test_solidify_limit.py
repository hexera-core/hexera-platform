# Responsibility: Verify TIME-10: the second reading of a file's loose faces (fea_driver._solidified) runs in a process
# of its own and is stopped at its limit, the plain stitch's reading then standing and the run told; the closed-skin
# check (SURF-14) stops its extraction at its own limit and the file goes on as the solid its surfaces close round.
# Boundaries: small synthetic shapes; the bounded child is real in one test and faked as stopped in the others; no
# mesher, no store.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402
from tests.unit.engines.test_surface_run import _stage  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.contracts.decision_record import RULES_VERSION  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import surface_run  # noqa: E402


def _faces(path):
    from meshpipeline.cad.scout import read_cad

    return FD._faces_in(read_cad(path))


def test_the_second_reading_runs_beside_the_run_and_gives_the_same_solid(tmp_path):
    # a 40 mm box written as its six faces: the bounded child reads it as the in-process reading does
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1
    assert seen == {"closed": 1} and _faces(ws / "geometry.brep") == 6


def test_a_second_reading_past_its_limit_leaves_the_plain_stitch_and_is_told(tmp_path, monkeypatch):
    calls = []

    def stopped(entry, kwargs, deadline_s):
        calls.append((entry, deadline_s))
        return "timeout", None

    monkeypatch.setattr(ms, "run_bounded", stopped)
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1                    # the plain stitch's solid stands
    assert calls == [(FD.SOLIDIFY_ENTRY, FD.SOLIDIFY_LIMIT_S)] and FD.SOLIDIFY_LIMIT_S == 300.0
    assert seen == {"closed": 1, "solidify_unfinished": FD.SOLIDIFY_LIMIT_S}
    words = FD.solidify_unfinished_words()
    assert "did not finish in 5 minutes" in words and "plain stitching" in words and "goes ahead" in words


def test_a_child_that_cannot_start_is_read_here(tmp_path, monkeypatch):
    def cannot(entry, kwargs, deadline_s):
        raise OSError("no process")

    monkeypatch.setattr(ms, "run_bounded", cannot)
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    seen: dict = {}
    assert FD.solids_from_shells(ws, seen) == 1 and seen == {"closed": 1}


def test_the_closed_skin_check_stops_at_its_limit_and_is_not_a_skin(tmp_path, monkeypatch):
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    assert FD.solids_from_shells(ws, {}) == 1
    asked = []

    def stopped(path, out_dir=None, *, mm_per_unit, deadline_s=None, **_k):
        asked.append(deadline_s)
        why = f"the mid-surface extraction ran past {deadline_s:.0f} s and was stopped"
        return {"bodies": [{"body": "Part_1", "verdict": "solid", "why": why}]}

    monkeypatch.setattr(ms, "extract_file", stopped)
    told: dict = {}
    assert surface_run.closed_skin_check(ws, told) is False
    assert asked == [surface_run.CLOSED_SKIN_LIMIT_S] and surface_run.CLOSED_SKIN_LIMIT_S == 300.0
    assert told == {"unfinished": surface_run.CLOSED_SKIN_LIMIT_S}
    assert "did not finish in 5 minutes" in surface_run.CLOSED_SKIN_UNFINISHED


def test_a_finished_closed_skin_check_is_not_told(tmp_path, monkeypatch):
    ws = _stage(sx.closed_skin(40.0, 40.0, 40.0), tmp_path / "ws")
    assert FD.solids_from_shells(ws, {}) == 1
    no_plate = {"bodies": [{"body": "Part_1", "verdict": "solid", "why": surface_run.CLOSED_SKIN_NO_PLATE}]}
    monkeypatch.setattr(ms, "extract_file", lambda *a, **k: no_plate)
    told: dict = {}
    assert surface_run.closed_skin_check(ws, told) is True and told == {}


def test_a_nearly_closed_file_meshed_as_a_shell_is_told_it_has_gaps():
    from meshpipeline.cad import surface_model as sm

    kind, why, _share = sm.classify([{"area_mm2": 100.0, "open_share": 0.003, "closed": False}], said_shell=True)
    assert kind == "surface" and "gaps between them" in why and "no solid" in why and "a shell was asked for" in why


def test_the_rulebook_says_time_10():
    assert RULES_VERSION == "77"
